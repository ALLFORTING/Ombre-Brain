from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path

import pytest
from PIL import Image

from asset_store import AssetStore
from asset_cutover_state import CutoverStateStore
from remember_me_adapter import RememberMeAdapter
from remember_me_cutover_operations import (
    CutoverOperationsError,
    acceptance_check_spec,
    classify_topology,
    create_backup,
    evaluate_readiness,
    preflight,
    restore_backup,
    run_frozen_acceptance_checks,
    verify_backup,
)


def _image_bytes() -> bytes:
    stream = io.BytesIO()
    image = Image.new("RGB", (4, 3), "green")
    image.save(stream, format="PNG")
    image.close()
    return stream.getvalue()


def _legacy_fixture(tmp_path: Path) -> tuple[Path, Path, Path]:
    legacy = tmp_path / "legacy"
    rm = legacy / "remember-me"
    state = legacy / "state"
    store = AssetStore(legacy)
    source = _image_bytes()
    temporary = store.create_temp_path(".png")
    temporary.write_bytes(source)
    store.persist_upload(
        temporary,
        hashlib.sha256(source).hexdigest(),
        len(source),
        "fixture.png",
        "image/png",
        require_image=True,
    )
    CutoverStateStore(state / "migration.sqlite3")
    rm.mkdir(parents=True)
    return legacy, rm, state


def test_backup_manifest_and_sqlite_snapshot_are_verified(tmp_path):
    legacy, rm, state = _legacy_fixture(tmp_path)
    destination = tmp_path / "backup"
    result = create_backup(
        profile="legacy-authoritative",
        legacy_root=legacy,
        rm_root=rm,
        state_db=state / "migration.sqlite3",
        destination=destination,
    )
    assert result["status"] == "PASS"
    manifest = json.loads((destination / "manifest.json").read_text())
    assert {"legacy", "remember-me", "state", "reports"}.issubset(
        {item.name for item in destination.iterdir()}
    )
    assert any(
        entry["relative_path"] == "legacy/assets.sqlite3"
        and entry["entry_type"] == "sqlite_snapshot"
        for entry in manifest["entries"]
    )
    assert manifest["blob_manifest"]
    assert verify_backup(destination)["status"] == "PASS"


def test_backup_fails_closed_on_missing_or_unexpected_blob(tmp_path):
    legacy, rm, state = _legacy_fixture(tmp_path)
    blob = next((legacy / "assets").rglob("*.png"))
    blob.unlink()
    with pytest.raises(CutoverOperationsError) as missing:
        create_backup(
            profile="legacy-authoritative", legacy_root=legacy, rm_root=rm,
            state_db=state / "migration.sqlite3", destination=tmp_path / "missing",
        )
    assert missing.value.code == "legacy_blob_audit_failed"

    legacy, rm, state = _legacy_fixture(tmp_path / "unexpected")
    (legacy / "assets" / "aa").mkdir(parents=True)
    (legacy / "assets" / "aa" / ("f" * 64 + ".png")).write_bytes(b"unexpected")
    with pytest.raises(CutoverOperationsError) as unexpected:
        create_backup(
            profile="legacy-authoritative", legacy_root=legacy, rm_root=rm,
            state_db=state / "migration.sqlite3", destination=tmp_path / "unexpected-backup",
        )
    assert unexpected.value.code == "legacy_blob_audit_failed"


def test_backup_rejects_existing_destination(tmp_path):
    legacy, rm, state = _legacy_fixture(tmp_path)
    destination = tmp_path / "backup"
    destination.mkdir()
    (destination / "unrelated.txt").write_text("keep")
    with pytest.raises(CutoverOperationsError) as exc:
        create_backup(
            profile="legacy-authoritative", legacy_root=legacy, rm_root=rm,
            state_db=state / "migration.sqlite3", destination=destination,
        )
    assert exc.value.code == "backup_destination_exists"


def test_restore_requires_isolated_empty_roots_and_reopens_readers(tmp_path):
    legacy, rm, state = _legacy_fixture(tmp_path)
    backup = tmp_path / "backup"
    create_backup(
        profile="legacy-authoritative", legacy_root=legacy, rm_root=rm,
        state_db=state / "migration.sqlite3", destination=backup,
    )
    restored = restore_backup(
        backup_root=backup,
        legacy_root=tmp_path / "restored-legacy",
        rm_root=tmp_path / "restored-rm",
        state_root=tmp_path / "restored-state",
    )
    assert restored["status"] == "PASS"
    assert restored["verification"]["legacy_reader"] == "PASS"
    assert restored["verification"]["state_reader"] == "PASS"
    assert restored["verification"]["rm_authoritative_capable"] is True


def test_frozen_ready_backup_rehearses_the_pinned_rm_reader(tmp_path):
    legacy, rm, state = _legacy_fixture(tmp_path)
    RememberMeAdapter().create_runtime(rm)
    backup = tmp_path / "frozen-backup"
    result = create_backup(
        profile="frozen-ready", legacy_root=legacy, rm_root=rm,
        state_db=state / "migration.sqlite3", destination=backup,
    )
    assert result["status"] == "PASS"
    restored = restore_backup(
        backup_root=backup,
        legacy_root=tmp_path / "rehearsal-legacy",
        rm_root=tmp_path / "rehearsal-rm",
        state_root=tmp_path / "rehearsal-state",
    )
    assert restored["status"] == "PASS"
    assert restored["verification"]["remember_me_reader"] == "PASS"


def test_preflight_is_read_only_and_reports_conservative_unknowns(tmp_path):
    legacy, rm, state = _legacy_fixture(tmp_path)
    result = preflight(
        legacy_root=legacy,
        rm_root=rm,
        state_db=state / "migration.sqlite3",
        embedding_enabled="false",
        topology={"worker_count": 1, "multiprocess": False, "shared_state": None},
    )
    assert result["read_only"] is True
    assert result["external_calls"] == 0
    assert result["vectors"]["status"] == "KEYWORD_ONLY"
    assert result["topology"]["classification"] == "SINGLE_PROCESS_CONFIRMED"
    assert result["state"]["authority"] == "legacy"
    assert result["gates"]["freeze_held"] is False


def test_topology_classification_never_guesses_multiprocess_safety():
    assert classify_topology()["classification"] == "UNKNOWN"
    assert classify_topology(multiprocess=True, shared_state=False)["classification"] == "MULTI_PROCESS_UNSAFE"
    assert classify_topology(multiprocess=True, shared_state=True)["classification"] == "MULTI_PROCESS_SUPPORTED"
    assert classify_topology(worker_count=1, multiprocess=False)["classification"] == "SINGLE_PROCESS_CONFIRMED"


def test_acceptance_primitives_and_readiness_gate_are_pure():
    spec = acceptance_check_spec()
    assert spec["status"] == "READY"
    assert spec["authority_switch_implemented"] is False
    evidence = {name: True for name in (
        "dependency_exact", "storage_layout", "state_healthy", "freeze_held",
        "legacy_authority_active", "migration_complete", "reconciliation_exact",
        "verification_passed", "vector_profile", "backup_verified", "disk_acceptable",
        "topology_safe", "stale_authority_clear",
    )}
    gate = evaluate_readiness(evidence)
    assert gate["READY_FOR_AUTHORITY_SWITCH"] == "YES"
    assert gate["authority_switch_implemented"] is False
    acceptance = run_frozen_acceptance_checks(
        state={"authority": "rm", "frozen": True},
        checks={name: True for name in spec["checks"]},
    )
    assert acceptance["status"] == "PASS"
    assert acceptance["production_access_occurred"] is False


def test_acceptance_requires_frozen_rm_state():
    result = run_frozen_acceptance_checks(
        state={"authority": "legacy", "frozen": False},
        checks={name: True for name in acceptance_check_spec()["checks"]},
    )
    assert result["status"] == "FAIL"

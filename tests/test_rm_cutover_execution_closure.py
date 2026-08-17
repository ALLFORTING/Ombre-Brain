from __future__ import annotations

import hashlib
import os
from pathlib import Path
import sqlite3
import stat
from types import SimpleNamespace

import pytest

from asset_authority import AssetAuthority
from asset_cutover_state import CutoverState, CutoverStateStore, MigrationIdentity
from cutover_lease_capability import (
    LeaseCapability,
    capability_path,
    read_capability,
    replace_capability,
    write_capability,
)
from remember_me.core import ImportAssetDisposition
from remember_me_cutover_migration import (
    MigrationInputs,
    abort,
    acquire_freeze,
    initialize_cutover,
    migrate,
)
from remember_me_cutover_operations import acceptance_check_spec, create_backup, verify_backup, restore_backup
from remember_me_cutover_transition import CutoverTransitionController, RM_SOURCE_COMMIT, build_parser
from tests._rm_acceptance_artifact import valid_rm_acceptance_artifact


def _legacy(root: Path, *, with_asset: bool = False) -> Path:
    (root / "assets").mkdir(parents=True)
    columns = """
        asset_id TEXT PRIMARY KEY, source_sha256 TEXT NOT NULL,
        stored_sha256 TEXT NOT NULL, stored_relpath TEXT NOT NULL,
        original_filename TEXT NOT NULL, mime_type TEXT NOT NULL,
        kind TEXT NOT NULL, decoded_bytes INTEGER NOT NULL,
        stored_bytes INTEGER NOT NULL, width INTEGER NOT NULL,
        height INTEGER NOT NULL, created_at TEXT NOT NULL,
        title TEXT NOT NULL, description TEXT NOT NULL,
        updated_at TEXT NOT NULL
    """
    with sqlite3.connect(root / "assets.sqlite3") as connection:
        connection.execute(f"CREATE TABLE assets ({columns})")
        connection.execute("CREATE TABLE asset_tags (asset_id TEXT, tag_normalized TEXT, tag_display TEXT, created_at TEXT)")
        if with_asset:
            asset_id = "0" * 31 + "1"
            content = b"png-payload"
            digest = hashlib.sha256(content).hexdigest()
            relative = f"assets/{digest}.png"
            (root / relative).write_bytes(content)
            connection.execute(
                "INSERT INTO assets VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (asset_id, digest, digest, relative, "image.png", "image/png", "image", len(content), len(content), 1, 1, "2026-08-15T00:00:00+00:00", "title", "description", "2026-08-15T00:00:00+00:00"),
            )
        connection.commit()
    return root


def _inputs(legacy: Path, rm: Path, state: Path) -> MigrationInputs:
    return MigrationInputs.create(
        legacy_root=legacy,
        rm_root=rm,
        state_db_path=state / "migration.sqlite3",
        report_path=state / "report.json",
    )


class _Service:
    def import_asset(self, request):
        return SimpleNamespace(disposition=ImportAssetDisposition.IMPORTED)


def test_bootstrap_backup_records_and_restores_absent_rm_and_state(tmp_path):
    legacy = _legacy(tmp_path / "legacy")
    rm = legacy / "remember-me"
    state = legacy / "state"
    backup = tmp_path / "backup"
    result = create_backup(
        profile="legacy-authoritative",
        legacy_root=legacy,
        rm_root=rm,
        state_db=state / "migration.sqlite3",
        destination=backup,
    )
    assert result["status"] == "PASS"
    manifest = (backup / "manifest.json").read_text(encoding="utf-8")
    assert '"state_db_relative_path":null' in manifest
    assert verify_backup(backup)["status"] == "PASS"

    restored_legacy = tmp_path / "restored" / "legacy"
    restored_rm = restored_legacy / "remember-me"
    restored_state = restored_legacy / "state"
    restored = restore_backup(
        backup_root=backup,
        legacy_root=restored_legacy,
        rm_root=restored_rm,
        state_root=restored_state,
    )
    assert restored["status"] == "PASS"
    assert not restored_rm.exists()
    assert not restored_state.exists()


def test_backup_excludes_capability_material_with_explicit_reason(tmp_path):
    legacy = _legacy(tmp_path / "legacy")
    state_root = legacy / "state"
    state_db = state_root / "migration.sqlite3"
    store = CutoverStateStore(state_db)
    store.set_rm_available(True)
    store.transition(CutoverState.LEGACY_AUTHORITY_RM_READY)
    cap = capability_path(state_root)
    write_capability(cap, LeaseCapability("0" * 32, "local-test-capability"), state_root=state_root)
    backup = tmp_path / "backup"
    result = create_backup(
        profile="legacy-authoritative",
        legacy_root=legacy,
        rm_root=legacy / "remember-me",
        state_db=state_db,
        destination=backup,
    )
    assert result["status"] == "PASS"
    assert verify_backup(backup)["capability_exclusion"] == "PASS"
    assert "local-test-capability" not in (backup / "manifest.json").read_text(encoding="utf-8")
    assert not any("lease-token.json" in str(path) for path in backup.rglob("*"))


def test_capability_replacement_is_atomic_and_keeps_private_mode(tmp_path):
    state_root = tmp_path / "state"
    cap = capability_path(state_root)
    write_capability(cap, LeaseCapability("0" * 32, "old-token"), state_root=state_root)
    replace_capability(cap, LeaseCapability("1" * 32, "new-token"), state_root=state_root)
    current = read_capability(cap, state_root=state_root)
    assert current == LeaseCapability("1" * 32, "new-token")
    if os.name != "nt":
        assert stat.S_IMODE(cap.stat().st_mode) == 0o600


def test_initialize_acquire_shared_lease_and_active_abort(tmp_path):
    legacy = _legacy(tmp_path / "legacy", with_asset=True)
    rm = legacy / "remember-me"
    state = legacy / "state"
    initialized = initialize_cutover(legacy_root=legacy, rm_root=rm, state_db=state / "migration.sqlite3")
    assert initialized["status"] == "success"
    assert CutoverStateStore(state / "migration.sqlite3").get_snapshot().state is CutoverState.LEGACY_AUTHORITY_RM_READY
    repeated = initialize_cutover(legacy_root=legacy, rm_root=rm, state_db=state / "migration.sqlite3")
    assert repeated["initialized"] is False

    inputs = _inputs(legacy, rm, state)
    cap_file = capability_path(state)
    acquired = acquire_freeze(inputs, lease_capability_file=cap_file)
    assert acquired["status"] == "success"
    capability = read_capability(cap_file, state_root=state)
    state_bytes = (state / "migration.sqlite3").read_bytes()
    assert capability.token.encode() not in state_bytes

    with pytest.raises(Exception):
        CutoverStateStore(state / "migration.sqlite3").load_lease(capability.lease_id, "wrong-token")
    migrated = migrate(inputs, runtime=SimpleNamespace(service=_Service()), lease_capability_file=cap_file)
    assert migrated["exit_code"] == 0
    assert migrated["cutover_state"]["state"] == CutoverState.FROZEN_LEGACY_MIGRATION.value

    rolled_back = abort(inputs, reason="pre-d2 rehearsal rollback", lease_capability_file=cap_file)
    assert rolled_back["exit_code"] == 0
    assert rolled_back["cutover_state"]["state"] == CutoverState.LEGACY_AUTHORITY_RM_READY.value
    assert not cap_file.exists()


def test_d2_parser_accepts_capability_file_without_plaintext_token():
    args = build_parser().parse_args([
        "release-freeze-to-legacy",
        "--state-db", "D:/cutover/state/migration.sqlite3",
        "--configured-authority", "legacy",
        "--lease-capability-file", "D:/cutover/state/operator/lease-token.json",
    ])
    assert args.lease_capability_file.name == "lease-token.json"
    assert args.lease_token is None


def test_d2_handoff_releases_and_cleans_shared_capability(tmp_path):
    state_db = tmp_path / "state" / "migration.sqlite3"
    store = CutoverStateStore(state_db)
    store.set_rm_available(True)
    store.transition(CutoverState.LEGACY_AUTHORITY_RM_READY)
    identity = MigrationIdentity("ombre-rm-production-cutover", 1, "path-sha256:legacy-source", 7, "path-sha256:rm-target")
    lease = store.acquire_freeze(
        expected_state=CutoverState.LEGACY_AUTHORITY_RM_READY,
        frozen_state=CutoverState.FROZEN_LEGACY_MIGRATION,
        ttl_seconds=300,
        migration_identity=identity,
    )
    store.transition(CutoverState.FROZEN_READY_FOR_RM_SWITCH, lease=lease, migration_identity=identity)
    cap = capability_path(state_db.parent)
    write_capability(cap, LeaseCapability(lease.lease_id, lease.token), state_root=state_db.parent)
    evidence = {
        "transition_identity": "d2-test",
        "readiness_evidence_id": "readiness-test",
        "dependency": {"version": "0.1.0.dev7", "source_commit": RM_SOURCE_COMMIT},
        "rm_runtime_healthy": True,
        "rm_data_root_healthy": True,
        "state_healthy": True,
        "migration_complete": True,
        "reconciliation_pass": True,
        "verification_pass": True,
        "vector_readiness_pass": True,
        "backup_evidence_present": True,
        "backup_evidence_id": "backup-test",
        "storage_root_validation_pass": True,
        "disk_readiness_pass": True,
        "topology_readiness_pass": True,
        "no_stale_authority": True,
    }
    controller = CutoverTransitionController(state_db, capability_file=cap)
    controller.prepare_rm_switch(lease, evidence=evidence)
    controller.switch_to_rm(lease, configured_authority=AssetAuthority.RM, restart_validated=True)
    checks, evidence = valid_rm_acceptance_artifact(store, controller)
    controller.accept_rm(lease, checks=checks, evidence=evidence)
    opened = controller.release_to_rm(lease)
    assert opened["phase"] == "RM_OPEN"
    assert not cap.exists()

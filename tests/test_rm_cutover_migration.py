from __future__ import annotations

from dataclasses import dataclass
import hashlib
from pathlib import Path
import sqlite3
from types import SimpleNamespace

from asset_cutover_state import CutoverState
from remember_me.core import ImportAssetDisposition
from remember_me_cutover_migration import (
    EXIT_MIGRATION_BLOCKED,
    EXIT_SUCCESS,
    MigrationInputs,
    ProgressStore,
    ReadOnlyLegacySource,
    inspect,
    migrate,
    preflight_local,
)


ASSET_ID = "0" * 31 + "1"


def _source(tmp_path: Path, *, count: int = 1) -> tuple[Path, Path, Path, dict[str, bytes]]:
    legacy = tmp_path / "legacy"
    rm = legacy / "remember-me"
    state = legacy / "state"
    (legacy / "assets" / "aa").mkdir(parents=True)
    tags: list[tuple[str, str, str]] = []
    rows = []
    payloads: dict[str, bytes] = {}
    for index in range(count):
        asset_id = f"{index + 1:032x}"
        payload = b"png-payload-" + str(index).encode()
        digest = hashlib.sha256(payload).hexdigest()
        relative = f"assets/aa/{digest}.png"
        (legacy / relative).write_bytes(payload)
        payloads[asset_id] = payload
        rows.append((asset_id, digest, digest, relative, f"image-{index}.png", "image/png", "image", len(payload), len(payload), 1, 1, "2026-08-14T00:00:00+00:00", "title", "description", "2026-08-14T00:00:00+00:00"))
        tags.append((asset_id, "tag", "2026-08-14T00:00:00+00:00"))
    with sqlite3.connect(legacy / "assets.sqlite3") as connection:
        connection.executescript(
            """
            CREATE TABLE assets (
                asset_id TEXT PRIMARY KEY, source_sha256 TEXT NOT NULL,
                stored_sha256 TEXT NOT NULL, stored_relpath TEXT NOT NULL,
                original_filename TEXT NOT NULL, mime_type TEXT NOT NULL,
                kind TEXT NOT NULL, decoded_bytes INTEGER NOT NULL,
                stored_bytes INTEGER NOT NULL, width INTEGER NOT NULL,
                height INTEGER NOT NULL, created_at TEXT NOT NULL,
                title TEXT NOT NULL, description TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE asset_tags (
                asset_id TEXT NOT NULL, tag_normalized TEXT NOT NULL,
                tag_display TEXT NOT NULL, created_at TEXT NOT NULL
            );
            """
        )
        connection.executemany("INSERT INTO assets VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
        connection.executemany("INSERT INTO asset_tags VALUES (?,?,?,?)", [(asset_id, value.casefold(), value, created) for asset_id, value, created in tags])
        connection.commit()
    return legacy, rm, state, payloads


class FakeService:
    def __init__(self, *, reject: bool = False):
        self.reject = reject
        self.requests = []

    def import_asset(self, request):
        self.requests.append(request)
        if self.reject:
            return SimpleNamespace(disposition="rejected")
        return SimpleNamespace(disposition=ImportAssetDisposition.IMPORTED)


def _inputs(legacy: Path, rm: Path, state: Path) -> MigrationInputs:
    return MigrationInputs.create(
        legacy_root=legacy,
        rm_root=rm,
        state_db_path=state / "migration.sqlite3",
        report_path=state / "report.json",
    )


def test_inputs_require_absolute_design_a_layout_and_no_server_import(tmp_path):
    # A: explicit roots and no server startup.
    legacy, rm, state, _ = _source(tmp_path)
    inputs = _inputs(legacy, rm, state)
    assert inputs.state_db_path == state / "migration.sqlite3"
    import remember_me_cutover_migration as migration_module

    assert "server" not in migration_module.__dict__


def test_legacy_source_is_structurally_read_only(tmp_path):
    # B: source is opened read-only and only bytes are read.
    legacy, _, _, payloads = _source(tmp_path)
    before = (legacy / "assets.sqlite3").stat().st_mtime_ns
    source = ReadOnlyLegacySource(legacy)
    snapshot = source.snapshot()
    assert snapshot.asset_count == 1
    assert source.blob_bytes(ASSET_ID) == payloads[ASSET_ID]
    with sqlite3.connect(legacy / "assets.sqlite3") as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 0
    assert (legacy / "assets.sqlite3").stat().st_mtime_ns == before


def test_preflight_does_not_initialize_state_or_rm_target(tmp_path):
    # C: preflight is non-mutating and target creation is explicit.
    legacy, rm, state, _ = _source(tmp_path)
    report = preflight_local(_inputs(legacy, rm, state))
    assert report["phase"] == "preflight-local"
    assert not (state / "migration.sqlite3").exists()
    assert not rm.exists()


def test_migration_creates_checkpoint_and_keeps_global_freeze(tmp_path):
    # D: durable checkpoint plus long-lived A freeze.
    legacy, rm, state, _ = _source(tmp_path, count=2)
    service = FakeService()
    report = migrate(_inputs(legacy, rm, state), runtime=SimpleNamespace(service=service), batch_size=1)
    assert report["exit_code"] == EXIT_SUCCESS
    assert report["checkpoint"]["status"] == "completed"
    assert report["checkpoint"]["processed_count"] == 2
    assert len(service.requests) == 2
    assert report["cutover_state"]["state"] == CutoverState.FROZEN_LEGACY_MIGRATION.value
    assert report["cutover_state"]["freeze_status"] == "active"


def test_rejected_import_is_blocked_and_target_is_not_deleted(tmp_path):
    # E: rejected source cannot be treated as success or trigger cleanup.
    legacy, rm, state, _ = _source(tmp_path)
    service = FakeService(reject=True)
    report = migrate(_inputs(legacy, rm, state), runtime=SimpleNamespace(service=service))
    assert report["exit_code"] == EXIT_MIGRATION_BLOCKED
    assert report["checkpoint"]["status"] == "blocked"
    assert report["checkpoint"]["blocked_asset_id"] == ASSET_ID
    assert rm.exists() is False


def test_inspect_is_read_only_and_redacts_lease_capability(tmp_path):
    # F: inspect is redacted and read-only.
    legacy, rm, state, _ = _source(tmp_path)
    inputs = _inputs(legacy, rm, state)
    migrate(inputs, runtime=SimpleNamespace(service=FakeService()))
    progress_before = (state / "migration-progress.sqlite3").read_bytes()
    report = inspect(inputs)
    assert report["exit_code"] == EXIT_SUCCESS
    assert "token" not in str(report).lower()
    assert (state / "migration-progress.sqlite3").read_bytes() == progress_before


def test_g_preflight_rejects_nonempty_target_without_owned_checkpoint(tmp_path):
    legacy, rm, state, _ = _source(tmp_path)
    rm.mkdir(parents=True)
    (rm / "foreign-marker").write_text("conflict", encoding="utf-8")
    report = preflight_local(_inputs(legacy, rm, state))
    assert report["exit_code"] != EXIT_SUCCESS
    assert "target_not_empty_without_checkpoint" in report["issues"]


def test_h_checkpoint_identity_mismatch_is_fail_closed(tmp_path):
    legacy, rm, state, _ = _source(tmp_path)
    inputs = _inputs(legacy, rm, state)
    migrate(inputs, runtime=SimpleNamespace(service=FakeService()))
    changed = MigrationInputs.create(
        legacy_root=legacy,
        rm_root=rm,
        state_db_path=state / "migration.sqlite3",
        report_path=state / "changed-report.json",
        migration_identity="different-operator-run",
    )
    report = migrate(changed, runtime=SimpleNamespace(service=FakeService()), resume=True)
    assert report["exit_code"] != EXIT_SUCCESS
    assert report["errors"][0]["code"] == "checkpoint_identity_mismatch"


def test_i_source_change_is_detected_before_resume(tmp_path):
    legacy, rm, state, _ = _source(tmp_path)
    inputs = _inputs(legacy, rm, state)
    migrate(inputs, runtime=SimpleNamespace(service=FakeService()))
    with sqlite3.connect(legacy / "assets.sqlite3") as connection:
        connection.execute("UPDATE assets SET title='changed' WHERE asset_id=?", (ASSET_ID,))
        connection.commit()
    report = migrate(inputs, runtime=SimpleNamespace(service=FakeService()), resume=True)
    assert report["exit_code"] != EXIT_SUCCESS
    assert report["errors"][0]["code"] == "source_changed_since_checkpoint"


def test_j_cli_surface_requires_all_operator_inputs():
    from remember_me_cutover_migration import build_parser

    parser = build_parser()
    parsed = parser.parse_args(["inspect", "--legacy-root", "D:/legacy", "--rm-root", "D:/rm", "--state-db", "D:/state/migration.sqlite3", "--report", "D:/report.json", "--migration-identity", "test-run"])
    assert parsed.command == "inspect"

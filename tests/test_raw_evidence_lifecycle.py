"""Focused O5D lifecycle tests using only isolated synthetic evidence."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from raw_evidence_lifecycle import LifecycleConfig, RawEvidenceLifecycle
from raw_evidence_store import RawEvidenceError, RawEvidenceStore


def _store(tmp_path):
    return RawEvidenceStore(tmp_path / "raw-evidence")


def _at(days: int) -> str:
    return (datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(days=days)).isoformat(
        timespec="seconds"
    )


def test_config_defaults_and_bounds(monkeypatch):
    for name in (
        "OMBRE_RAW_EVIDENCE_RETENTION_DAYS",
        "OMBRE_RAW_EVIDENCE_AUDIT_RETENTION_DAYS",
        "OMBRE_RAW_EVIDENCE_PURGE_BATCH_SIZE",
    ):
        monkeypatch.delenv(name, raising=False)
    config = LifecycleConfig.from_env()
    assert config.retention_days == 30
    assert config.audit_retention_days == 365
    assert config.purge_batch_size == 100

    monkeypatch.setenv("OMBRE_RAW_EVIDENCE_RETENTION_DAYS", "366")
    with pytest.raises(RawEvidenceError, match="retention_config_invalid"):
        LifecycleConfig.from_env()
    monkeypatch.setenv("OMBRE_RAW_EVIDENCE_RETENTION_DAYS", "30")
    monkeypatch.setenv("OMBRE_RAW_EVIDENCE_AUDIT_RETENTION_DAYS", "29")
    with pytest.raises(RawEvidenceError, match="audit_retention_config_invalid"):
        LifecycleConfig.from_env()
    monkeypatch.setenv("OMBRE_RAW_EVIDENCE_AUDIT_RETENTION_DAYS", "365")
    monkeypatch.setenv("OMBRE_RAW_EVIDENCE_PURGE_BATCH_SIZE", "1001")
    with pytest.raises(RawEvidenceError, match="purge_batch_config_invalid"):
        LifecycleConfig.from_env()


def test_schema_v3_to_v4_migration_preserves_identity_without_purge(tmp_path):
    store = _store(tmp_path)
    created = store.create(
        b"migration-bytes",
        source_system="fixture",
        source_kind="item",
        captured_at=_at(0),
    )
    with sqlite3.connect(store.registry_path) as conn:
        conn.execute("DROP TABLE lifecycle_audit")
        conn.execute("DROP TABLE evidence_lifecycle")
        conn.execute("DROP TABLE cas_objects")
        conn.execute("UPDATE store_schema SET schema_version = 3 WHERE singleton = 1")

    migrated = RawEvidenceStore(store.root)
    loaded = migrated.get_evidence(created["evidence_id"])
    assert loaded["revision_id"] == created["revision_id"]
    assert loaded["lifecycle_state"] == "available"
    assert loaded["retention_deadline"] == _at(30)
    assert migrated.get_content(created["revision_id"]) == b"migration-bytes"
    with sqlite3.connect(migrated.registry_path) as conn:
        assert conn.execute(
            "SELECT schema_version FROM store_schema WHERE singleton = 1"
        ).fetchone()[0] == 4


def test_schema_v3_captured_state_migrates_as_readable_available(tmp_path):
    store = _store(tmp_path)
    created = store.create(
        b"captured-state",
        source_system="fixture",
        source_kind="item",
        captured_at=_at(0),
    )
    with sqlite3.connect(store.registry_path) as conn:
        conn.execute("DROP TABLE lifecycle_audit")
        conn.execute("DROP TABLE evidence_lifecycle")
        conn.execute("DROP TABLE cas_objects")
        conn.execute(
            "UPDATE evidence_objects SET lifecycle_state = 'captured' WHERE evidence_id = ?",
            (created["evidence_id"],),
        )
        conn.execute("UPDATE store_schema SET schema_version = 3 WHERE singleton = 1")

    migrated = RawEvidenceStore(store.root)
    assert migrated.get_content(created["revision_id"]) == b"captured-state"
    assert migrated.get_revision(created["revision_id"])["lifecycle_state"] == "available"


def test_deadline_is_frozen_when_configuration_changes(tmp_path, monkeypatch):
    monkeypatch.setenv("OMBRE_RAW_EVIDENCE_RETENTION_DAYS", "5")
    store = _store(tmp_path)
    created = store.create(
        b"frozen-deadline",
        source_system="fixture",
        source_kind="item",
        captured_at=_at(0),
    )
    monkeypatch.setenv("OMBRE_RAW_EVIDENCE_RETENTION_DAYS", "30")
    lifecycle = RawEvidenceLifecycle(store)
    assert store.get_revision(created["revision_id"])["retention_deadline"] == _at(5)
    assert lifecycle._expire_one(_at(4)) is False
    assert lifecycle._expire_one(_at(5)) is True
    assert store.get_revision(created["revision_id"])["lifecycle_state"] == "expired"


def test_redaction_tombstones_before_purge_and_keeps_identity(tmp_path):
    store = _store(tmp_path)
    created = store.create(
        b"redact-me",
        source_system="fixture",
        source_kind="item",
        captured_at=_at(0),
    )
    lifecycle = RawEvidenceLifecycle(store)
    blob = store.root / created["blob_relpath"]

    result = lifecycle.redact(created["evidence_id"], reason="user_redaction")
    assert result["state"] == "tombstoned"
    assert blob.exists()
    with pytest.raises(RawEvidenceError, match="evidence_unavailable"):
        store.get_content(created["revision_id"])
    assert store.get_revision(created["revision_id"])["lifecycle_state"] == "tombstoned"

    run = lifecycle.run(now=_at(30))
    assert run["purged"] == 1
    assert not blob.exists()
    retained = store.get_revision(created["revision_id"])
    assert retained["lifecycle_state"] == "purged"
    assert retained["payload_deleted"] == 1
    with pytest.raises(RawEvidenceError, match="evidence_unavailable"):
        store.get_content(created["revision_id"])


def test_shared_cas_is_kept_until_final_logical_reference_is_eligible(tmp_path):
    store = _store(tmp_path)
    first = store.create(
        b"shared-cas",
        source_system="fixture",
        source_kind="first",
        captured_at=_at(0),
    )
    second = store.create(
        b"shared-cas",
        source_system="fixture",
        source_kind="second",
        captured_at=_at(10),
    )
    assert first["blob_relpath"] == second["blob_relpath"]
    blob = store.root / first["blob_relpath"]
    lifecycle = RawEvidenceLifecycle(store)
    lifecycle.redact(first["evidence_id"], reason="user_redaction")
    first_pass = lifecycle.run(now=_at(30))
    assert first_pass["purged"] == 0
    assert blob.exists()

    lifecycle.redact(second["evidence_id"], reason="user_redaction")
    second_pass = lifecycle.run(now=_at(40))
    assert second_pass["purged"] == 2
    assert not blob.exists()


def test_dry_run_has_zero_mutations(tmp_path):
    store = _store(tmp_path)
    created = store.create(
        b"dry-run",
        source_system="fixture",
        source_kind="item",
        captured_at=_at(0),
    )
    lifecycle = RawEvidenceLifecycle(store)
    before = store.get_revision(created["revision_id"])
    result = lifecycle.run(now=_at(30), dry_run=True)
    after = store.get_revision(created["revision_id"])
    assert result["mutations"] == 0
    assert result["eligible_count"] == 1
    assert before["lifecycle_state"] == after["lifecycle_state"] == "available"


def test_repeated_purge_is_idempotent_and_audit_is_metadata_only(tmp_path):
    store = _store(tmp_path)
    created = store.create(
        b"repeat-purge",
        source_system="fixture",
        source_kind="item",
        captured_at=_at(0),
    )
    lifecycle = RawEvidenceLifecycle(store)
    lifecycle.redact(created["evidence_id"], reason="user_redaction")
    first = lifecycle.run(now=_at(30))
    second = lifecycle.run(now=_at(31))
    assert first["purged"] == 1
    assert second["purged"] == 0
    with sqlite3.connect(store.registry_path) as conn:
        rows = conn.execute(
            "SELECT reason, actor_class, payload_deleted FROM lifecycle_audit"
        ).fetchall()
    assert rows
    assert all(row[0] and row[1] and row[2] in (0, 1) for row in rows)


def test_future_schema_refuses_raw_evidence(tmp_path):
    store = _store(tmp_path)
    with sqlite3.connect(store.registry_path) as conn:
        conn.execute("UPDATE store_schema SET schema_version = 99 WHERE singleton = 1")
    with pytest.raises(RawEvidenceError, match="schema_unsupported"):
        RawEvidenceStore(store.root)


def test_missing_payload_is_not_reported_as_policy_purge(tmp_path):
    store = _store(tmp_path)
    created = store.create(
        b"missing-payload",
        source_system="fixture",
        source_kind="item",
        captured_at=_at(0),
    )
    (store.root / created["blob_relpath"]).unlink()
    with pytest.raises(RawEvidenceError, match="evidence_missing"):
        store.get_content(created["revision_id"])
    assert store.get_revision(created["revision_id"])["lifecycle_state"] == "missing"


def test_gc_pending_blocks_capture_and_is_recoverable(tmp_path):
    store = _store(tmp_path)
    first = store.create(
        b"gc-pending",
        source_system="fixture",
        source_kind="item",
        captured_at=_at(0),
    )
    with sqlite3.connect(store.registry_path) as conn:
        conn.execute(
            "UPDATE cas_objects SET state = 'gc_pending', operation_id = '" + "a" * 32 + "'"
        )
    with pytest.raises(RawEvidenceError, match="cas_gc_pending"):
        store.create(b"gc-pending", source_system="fixture", source_kind="retry")
    assert store.get_content(first["revision_id"]) == b"gc-pending"


def test_metadata_cannot_bypass_durable_purge_protocol(tmp_path):
    store = _store(tmp_path)
    created = store.create(
        b"protocol-boundary",
        source_system="fixture",
        source_kind="item",
        captured_at=_at(0),
    )
    with pytest.raises(RawEvidenceError, match="lifecycle_state_invalid"):
        store.update_state(created["evidence_id"], "purged")


def test_publish_pending_without_reference_is_cleaned_by_recovery(tmp_path):
    store = _store(tmp_path)
    content_hash = "b" * 64
    with sqlite3.connect(store.registry_path) as conn:
        conn.execute(
            """
            INSERT INTO cas_objects (
                hash_algorithm, content_hash, content_size_bytes, blob_relpath,
                state, operation_id, created_at, updated_at
            ) VALUES ('sha256-v1', ?, 1, ?, 'publish_pending', ?, ?, ?)
            """,
            (
                content_hash,
                f"blobs/sha256/{content_hash[:2]}/{content_hash}",
                "c" * 32,
                _at(0),
                _at(0),
            ),
        )
    result = RawEvidenceLifecycle(store).run(now=_at(1))
    assert result["publish_recovered"] == 1
    with sqlite3.connect(store.registry_path) as conn:
        assert conn.execute(
            "SELECT state FROM cas_objects WHERE content_hash = ?", (content_hash,)
        ).fetchone()[0] == "purged"

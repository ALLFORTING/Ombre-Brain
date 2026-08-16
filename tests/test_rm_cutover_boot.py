from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import sqlite3

import pytest

from asset_authority import AssetAuthority
from asset_cutover_state import (
    CutoverSnapshot,
    CutoverState,
    CutoverStateError,
    CutoverStateStore,
    ExpiredRmAcceptanceRecoveryBootProof,
    MigrationIdentity,
    validate_cutover_boot,
)
from cutover_boot_coordination import read_expired_rm_acceptance_recovery_boot_proof


IDENTITY = MigrationIdentity(
    migration_key="ombre-rm-production-cutover",
    migration_version=1,
    source_identity="path-sha256:legacy-source",
    source_generation=7,
    target_identity="path-sha256:rm-target",
)


def _identity_payload() -> dict[str, object]:
    return {
        "migration_key": IDENTITY.migration_key,
        "migration_version": IDENTITY.migration_version,
        "source_identity": IDENTITY.source_identity,
        "source_generation": IDENTITY.source_generation,
        "target_identity": IDENTITY.target_identity,
    }


def _expired_snapshot(
    *,
    state: CutoverState = CutoverState.FROZEN_RM_ACCEPTANCE,
    authority: AssetAuthority = AssetAuthority.RM,
    rm_available: bool = True,
    lease_id: str | None = "a" * 32,
    migration_identity: MigrationIdentity | None = IDENTITY,
) -> CutoverSnapshot:
    return CutoverSnapshot(
        schema_version=1,
        revision=4,
        state=state,
        authority=authority,
        rm_available=rm_available,
        freeze_status="expired",
        lease_id=lease_id,
        lease_expires_at="2000-01-01T00:00:00+00:00",
        transition_id="state-transition-1",
        transitioned_at="2026-08-16T00:00:00+00:00",
        migration_identity=migration_identity,
    )


def _proof(snapshot: CutoverSnapshot) -> ExpiredRmAcceptanceRecoveryBootProof:
    return ExpiredRmAcceptanceRecoveryBootProof(
        phase="RM_FROZEN_ACCEPTANCE",
        expected_authority=AssetAuthority.RM,
        state=CutoverState.FROZEN_RM_ACCEPTANCE,
        lease_id=snapshot.lease_id or "a" * 32,
        lease_generation=3,
        transition_identity="d2-transition-1",
        migration_identity=IDENTITY,
    )


def _record(lease_id: str, generation: int, *, phase: str = "RM_FROZEN_ACCEPTANCE") -> dict[str, object]:
    return {
        "phase": phase,
        "transition_identity": "d2-transition-1",
        "expected_authority": "rm",
        "authority_before": "legacy",
        "authority_after": "rm",
        "state_before": "frozen_ready_for_rm_switch",
        "state_after": "frozen_rm_acceptance",
        "lease_id": lease_id,
        "lease_generation": generation,
        "migration_identity": _identity_payload(),
        "readiness": {
            "status": "PASS",
            "READY_FOR_AUTHORITY_SWITCH": "YES",
            "evidence_identity": "readiness-1",
            "hard_gates": {"migration_complete": "PASS", "state_healthy": "PASS"},
        },
        "acceptance": {},
        "legacy_acceptance": {},
        "failures": [],
        "warnings": [],
        "prepared_at": "2026-08-16T00:00:00+00:00",
        "restart_validated_at": "2026-08-16T00:00:00+00:00",
        "updated_at": "2026-08-16T00:00:00+00:00",
        "freeze_released_at": None,
    }


def _expired_d2_db(tmp_path: Path) -> tuple[CutoverStateStore, dict[str, object]]:
    store = CutoverStateStore(tmp_path / "state" / "migration.sqlite3")
    store.set_rm_available(True)
    store.transition(CutoverState.LEGACY_AUTHORITY_RM_READY)
    lease = store.acquire_freeze(
        expected_state=CutoverState.LEGACY_AUTHORITY_RM_READY,
        frozen_state=CutoverState.FROZEN_LEGACY_MIGRATION,
        ttl_seconds=300,
        migration_identity=IDENTITY,
    )
    store.transition(CutoverState.FROZEN_READY_FOR_RM_SWITCH, lease=lease, migration_identity=IDENTITY)
    store.transition(CutoverState.FROZEN_RM_ACCEPTANCE, lease=lease, migration_identity=IDENTITY)
    with sqlite3.connect(store.db_path) as connection:
        connection.execute(
            """
            CREATE TABLE d2_transition_record (
                singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                schema_version INTEGER NOT NULL,
                payload_json TEXT NOT NULL
            )
            """
        )
        connection.execute(
            "UPDATE cutover_freeze SET expires_at = '2000-01-01T00:00:00+00:00' WHERE singleton = 1"
        )
        record = _record(lease.lease_id, lease.generation)
        connection.execute(
            "INSERT INTO d2_transition_record (singleton, schema_version, payload_json) VALUES (1, 1, ?)",
            (json.dumps(record, sort_keys=True),),
        )
    return store, record


def test_valid_expired_rm_boot_proof_is_recovery_required_and_fail_closed():
    snapshot = _expired_snapshot()
    result = validate_cutover_boot(
        AssetAuthority.RM,
        snapshot,
        rm_available=True,
        expired_rm_recovery=_proof(snapshot),
    )
    assert result.boot_mode == "EXPIRED_RM_RECOVERY"
    assert result.requires_recovery is True
    assert result.authority is AssetAuthority.RM
    assert result.writes_allowed is False
    assert result.frozen is True
    assert result.legacy_fallback_allowed is False


@pytest.mark.parametrize(
    "changes",
    [
        {"phase": "RM_PREPARED"},
        {"lease_id": "b" * 32},
        {"migration_identity": replace(IDENTITY, source_generation=99)},
        {"transition_identity": "not valid!"},
    ],
)
def test_expired_rm_boot_proof_rejects_mismatched_identity_or_phase(changes):
    snapshot = _expired_snapshot()
    proof = replace(_proof(snapshot), **changes)
    with pytest.raises(CutoverStateError, match="^state_freeze_ambiguous$"):
        validate_cutover_boot(
            AssetAuthority.RM,
            snapshot,
            rm_available=True,
            expired_rm_recovery=proof,
        )


@pytest.mark.parametrize(
    "snapshot",
    [
        _expired_snapshot(state=CutoverState.FROZEN_LEGACY_MIGRATION, authority=AssetAuthority.LEGACY),
        _expired_snapshot(state=CutoverState.FROZEN_READY_FOR_RM_SWITCH, authority=AssetAuthority.LEGACY),
        _expired_snapshot(state=CutoverState.FROZEN_RM_ROLLBACK),
        _expired_snapshot(state=CutoverState.FROZEN_LEGACY_ACCEPTANCE, authority=AssetAuthority.LEGACY),
        _expired_snapshot(),
    ],
)
def test_other_expired_states_remain_fail_closed(snapshot):
    with pytest.raises(CutoverStateError, match="^state_freeze_ambiguous$"):
        validate_cutover_boot(snapshot.authority, snapshot, rm_available=True)


def test_read_only_bridge_accepts_only_complete_current_d2_record(tmp_path):
    store, record = _expired_d2_db(tmp_path)
    snapshot = store.get_snapshot()
    proof = read_expired_rm_acceptance_recovery_boot_proof(store.db_path, snapshot)
    assert proof is not None
    assert proof.lease_id == snapshot.lease_id
    assert proof.lease_generation == record["lease_generation"]
    assert proof.migration_identity == snapshot.migration_identity

    with sqlite3.connect(store.db_path) as connection:
        connection.execute("DELETE FROM d2_transition_record WHERE singleton = 1")
    assert read_expired_rm_acceptance_recovery_boot_proof(store.db_path, snapshot) is None


@pytest.mark.parametrize("mutation", ["schema", "malformed", "stale_phase", "wrong_lease", "wrong_migration", "bad_readiness"])
def test_read_only_bridge_rejects_corrupt_or_stale_d2_record(tmp_path, mutation):
    store, record = _expired_d2_db(tmp_path)
    with sqlite3.connect(store.db_path) as connection:
        if mutation == "schema":
            connection.execute("UPDATE d2_transition_record SET schema_version = 2 WHERE singleton = 1")
        elif mutation == "malformed":
            connection.execute(
                "UPDATE d2_transition_record SET payload_json = ? WHERE singleton = 1",
                ("[]",),
            )
        else:
            changed = dict(record)
            if mutation == "stale_phase":
                changed["phase"] = "RM_OPEN"
            elif mutation == "wrong_lease":
                changed["lease_id"] = "b" * 32
            elif mutation == "wrong_migration":
                changed["migration_identity"] = _identity_payload()
                changed["migration_identity"]["source_generation"] = 99
            else:
                changed["readiness"] = {"status": "FAIL"}
            connection.execute(
                "UPDATE d2_transition_record SET payload_json = ? WHERE singleton = 1",
                (json.dumps(changed, sort_keys=True),),
            )
    assert read_expired_rm_acceptance_recovery_boot_proof(store.db_path, store.get_snapshot()) is None


def test_expired_rm_boot_proof_requires_durable_and_actual_rm_availability(tmp_path):
    snapshot = _expired_snapshot()
    with pytest.raises(CutoverStateError, match="^state_freeze_ambiguous$"):
        validate_cutover_boot(
            AssetAuthority.RM,
            snapshot,
            rm_available=False,
            expired_rm_recovery=_proof(snapshot),
        )
    unavailable = replace(snapshot, rm_available=False)
    with pytest.raises(CutoverStateError, match="^state_freeze_ambiguous$"):
        validate_cutover_boot(
            AssetAuthority.RM,
            unavailable,
            rm_available=True,
            expired_rm_recovery=_proof(unavailable),
        )

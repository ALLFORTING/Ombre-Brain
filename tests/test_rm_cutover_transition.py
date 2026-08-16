from __future__ import annotations

from pathlib import Path
import sqlite3

import pytest

from asset_authority import AssetAuthority
from asset_cutover_state import (
    CutoverState,
    CutoverStateError,
    CutoverStateStore,
    MigrationIdentity,
)
from asset_mutation_gate import AssetMutationGate
from remember_me_cutover_operations import acceptance_check_spec
from remember_me_cutover_transition import (
    CutoverTransitionController,
    CutoverTransitionError,
    LEGACY_ACCEPTANCE_NAMES,
    RM_SOURCE_COMMIT,
)


def _identity() -> MigrationIdentity:
    return MigrationIdentity(
        migration_key="ombre-rm-production-cutover",
        migration_version=1,
        source_identity="path-sha256:legacy-source",
        source_generation=7,
        target_identity="path-sha256:rm-target",
    )


def _prepared_store(tmp_path: Path) -> tuple[CutoverStateStore, object]:
    store = CutoverStateStore(tmp_path / "state" / "migration.sqlite3")
    store.set_rm_available(True)
    store.transition(CutoverState.LEGACY_AUTHORITY_RM_READY)
    lease = store.acquire_freeze(
        expected_state=CutoverState.LEGACY_AUTHORITY_RM_READY,
        frozen_state=CutoverState.FROZEN_LEGACY_MIGRATION,
        ttl_seconds=300,
        migration_identity=_identity(),
    )
    store.transition(
        CutoverState.FROZEN_READY_FOR_RM_SWITCH,
        lease=lease,
        migration_identity=_identity(),
    )
    return store, lease


def _evidence(identity: str = "d2-transition-1") -> dict[str, object]:
    return {
        "transition_identity": identity,
        "readiness_evidence_id": "readiness-1",
        "migration_identity": {
            "migration_key": "ombre-rm-production-cutover",
            "migration_version": 1,
            "source_identity": "path-sha256:legacy-source",
            "source_generation": 7,
            "target_identity": "path-sha256:rm-target",
        },
        "dependency": {
            "version": "0.1.0.dev7",
            "source_commit": RM_SOURCE_COMMIT,
        },
        "rm_runtime_healthy": True,
        "rm_data_root_healthy": True,
        "state_healthy": True,
        "migration_complete": True,
        "reconciliation_pass": True,
        "verification_pass": True,
        "vector_readiness_pass": True,
        "backup_evidence_present": True,
        "backup_evidence_id": "backup-1",
        "storage_root_validation_pass": True,
        "disk_readiness_pass": True,
        "topology_readiness_pass": True,
        "no_stale_authority": True,
    }


def _all_rm_checks() -> dict[str, bool]:
    return {name: True for name in acceptance_check_spec()["checks"]}


def _all_legacy_checks() -> dict[str, bool]:
    return {name: True for name in LEGACY_ACCEPTANCE_NAMES}


def test_successful_cutover_is_restart_safe_and_opens_rm_only_after_acceptance(tmp_path):
    store, lease = _prepared_store(tmp_path)
    controller = CutoverTransitionController(store.db_path, state_store=store)

    prepared = controller.prepare_rm_switch(lease, evidence=_evidence())
    assert prepared["phase"] == "RM_PREPARED"
    assert prepared["cutover_state"] == CutoverState.FROZEN_READY_FOR_RM_SWITCH.value

    pending = CutoverTransitionController(store.db_path).validate_restart(
        configured_authority=AssetAuthority.RM,
        rm_available=True,
    )
    assert pending["boot_mode"] == "COORDINATION_PENDING"
    assert pending["writes_allowed"] is False
    assert pending["legacy_fallback_allowed"] is False

    switched = CutoverTransitionController(store.db_path).switch_to_rm(
        lease,
        configured_authority=AssetAuthority.RM,
        restart_validated=True,
    )
    assert switched["phase"] == "RM_FROZEN_ACCEPTANCE"
    reopened = CutoverStateStore(store.db_path)
    assert reopened.get_snapshot().state is CutoverState.FROZEN_RM_ACCEPTANCE
    assert not AssetMutationGate(reopened).public_mutations_allowed()

    accepted = CutoverTransitionController(store.db_path).accept_rm(
        lease,
        checks=_all_rm_checks(),
    )
    assert accepted["acceptance_status"] == "PASS"
    assert accepted["LOSSLESS_ROLLBACK_WINDOW_OPEN"] == "YES"

    opened = CutoverTransitionController(store.db_path).release_to_rm(lease)
    assert opened["phase"] == "RM_OPEN"
    assert opened["cutover_state"] == CutoverState.RM_AUTHORITY_OPEN.value
    assert opened["LOSSLESS_ROLLBACK_WINDOW_OPEN"] == "NO"
    assert AssetMutationGate(CutoverStateStore(store.db_path)).public_mutations_allowed()

    with pytest.raises(CutoverStateError, match="^state_transition_invalid$"):
        CutoverStateStore(store.db_path).transition(CutoverState.LEGACY_AUTHORITY_RM_READY)
    with pytest.raises(CutoverTransitionError, match="^class_a_window_closed$"):
        CutoverTransitionController(store.db_path).begin_class_a_rollback(
            lease,
            reason="too-late",
        )


def test_failed_frozen_acceptance_rehearses_lossless_class_a_rollback(tmp_path):
    store, lease = _prepared_store(tmp_path)
    controller = CutoverTransitionController(store.db_path, state_store=store)
    controller.prepare_rm_switch(lease, evidence=_evidence("d2-rollback-1"))
    controller.switch_to_rm(lease, configured_authority=AssetAuthority.RM, restart_validated=True)

    checks = _all_rm_checks()
    checks["dashboard_detail"] = False
    failed = controller.accept_rm(lease, checks=checks)
    assert failed["acceptance_status"] == "FAIL"
    with pytest.raises(CutoverTransitionError, match="^rm_acceptance_not_passed$"):
        controller.release_to_rm(lease)

    prepared = controller.begin_class_a_rollback(
        lease,
        reason="frozen acceptance failed",
    )
    assert prepared["phase"] == "ROLLBACK_PREPARED"
    pending = controller.validate_restart(
        configured_authority=AssetAuthority.LEGACY,
        rm_available=True,
    )
    assert pending["coordination_pending"] is True
    assert pending["legacy_fallback_allowed"] is False

    controller.finalize_class_a_rollback(
        lease,
        configured_authority=AssetAuthority.LEGACY,
        restart_validated=True,
    )
    assert CutoverStateStore(store.db_path).get_snapshot().state is CutoverState.FROZEN_LEGACY_ACCEPTANCE
    controller.accept_legacy(lease, checks=_all_legacy_checks())
    restored = controller.release_to_legacy(lease)
    assert restored["phase"] == "LEGACY_OPEN"
    assert restored["authority"] == AssetAuthority.LEGACY.value
    assert restored["cutover_state"] == CutoverState.LEGACY_AUTHORITY_RM_READY.value
    assert restored["production_access_occurred"] is False


def test_prepare_refuses_any_failed_hard_gate_and_preserves_frozen_state(tmp_path):
    store, lease = _prepared_store(tmp_path)
    controller = CutoverTransitionController(store.db_path, state_store=store)
    evidence = _evidence()
    evidence["vector_readiness_pass"] = False
    with pytest.raises(CutoverTransitionError, match="^readiness_gate_failed$"):
        controller.prepare_rm_switch(lease, evidence=evidence)
    snapshot = CutoverStateStore(store.db_path).get_snapshot()
    assert snapshot.state is CutoverState.FROZEN_READY_FOR_RM_SWITCH
    assert snapshot.authority is AssetAuthority.LEGACY
    assert snapshot.freeze_status == "active"


def test_config_coordination_and_restart_validation_fail_closed(tmp_path):
    store, lease = _prepared_store(tmp_path)
    controller = CutoverTransitionController(store.db_path, state_store=store)
    controller.prepare_rm_switch(lease, evidence=_evidence("d2-coordination-1"))
    with pytest.raises(CutoverTransitionError, match="^authority_coordination_required$"):
        controller.switch_to_rm(
            lease,
            configured_authority=AssetAuthority.LEGACY,
            restart_validated=True,
        )
    with pytest.raises(CutoverTransitionError, match="^restart_validation_required$"):
        controller.switch_to_rm(
            lease,
            configured_authority=AssetAuthority.RM,
            restart_validated=False,
        )
    assert CutoverStateStore(store.db_path).get_snapshot().state is CutoverState.FROZEN_READY_FOR_RM_SWITCH


def test_rm_unavailable_and_lease_loss_never_advance_authority(tmp_path):
    store, lease = _prepared_store(tmp_path)
    controller = CutoverTransitionController(store.db_path, state_store=store)
    controller.prepare_rm_switch(lease, evidence=_evidence("d2-runtime-failure-1"))
    with pytest.raises(CutoverTransitionError, match="^rm_authority_unavailable$"):
        controller.switch_to_rm(
            lease,
            configured_authority=AssetAuthority.RM,
            rm_available=False,
            restart_validated=True,
        )
    controller.switch_to_rm(lease, configured_authority=AssetAuthority.RM, restart_validated=True)
    with store.db_path.open("rb") as stream:
        assert lease.token.encode("utf-8") not in stream.read()
    with store.db_path.open("r+b") as stream:
        contents = stream.read()
    assert lease.token.encode("utf-8") not in contents

    with sqlite3.connect(store.db_path) as connection:
        connection.execute(
            "UPDATE cutover_freeze SET expires_at = '2000-01-01T00:00:00+00:00' WHERE singleton = 1"
        )
    with pytest.raises(CutoverTransitionError, match="^freeze_lease_expired$"):
        controller.accept_rm(lease, checks=_all_rm_checks())
    assert CutoverStateStore(store.db_path).get_snapshot().state is CutoverState.FROZEN_RM_ACCEPTANCE


def test_idempotency_and_invalid_boundaries_are_explicit(tmp_path):
    store, lease = _prepared_store(tmp_path)
    controller = CutoverTransitionController(store.db_path, state_store=store)
    first = controller.prepare_rm_switch(lease, evidence=_evidence("d2-idempotent-1"))
    second = controller.prepare_rm_switch(lease, evidence=_evidence("d2-idempotent-1"))
    assert second["phase"] == first["phase"] == "RM_PREPARED"
    with pytest.raises(CutoverTransitionError, match="^transition_identity_mismatch$"):
        controller.prepare_rm_switch(lease, evidence=_evidence("d2-other-transition"))

    controller.switch_to_rm(lease, configured_authority=AssetAuthority.RM, restart_validated=True)
    repeated = controller.switch_to_rm(lease, configured_authority=AssetAuthority.RM, restart_validated=True)
    assert repeated["phase"] == "RM_FROZEN_ACCEPTANCE"
    with pytest.raises(CutoverTransitionError, match="^legacy_acceptance_state_invalid$"):
        controller.accept_legacy(lease, checks=_all_legacy_checks())


def test_pre_d2_abort_does_not_leave_rm_prepared_as_current_phase(tmp_path):
    store, lease = _prepared_store(tmp_path)
    controller = CutoverTransitionController(store.db_path, state_store=store)
    controller.prepare_rm_switch(lease, evidence=_evidence("d2-stale"))

    store.transition(
        CutoverState.LEGACY_AUTHORITY_RM_READY,
        lease=lease,
        migration_identity=_identity(),
    )
    stale_status = CutoverTransitionController(store.db_path).status(
        configured_authority=AssetAuthority.LEGACY,
    )
    assert stale_status["cutover_state"] == CutoverState.LEGACY_AUTHORITY_RM_READY.value
    assert stale_status["phase"] == "NONE"
    assert stale_status["transition_identity"] is None
    assert stale_status["next_legal_operator_actions"] == [
        "prepare RM switch only after D1 readiness is PASS"
    ]

    new_lease = store.acquire_freeze(
        expected_state=CutoverState.LEGACY_AUTHORITY_RM_READY,
        frozen_state=CutoverState.FROZEN_LEGACY_MIGRATION,
        ttl_seconds=300,
        migration_identity=_identity(),
    )
    store.transition(
        CutoverState.FROZEN_READY_FOR_RM_SWITCH,
        lease=new_lease,
        migration_identity=_identity(),
    )
    assert new_lease.lease_id != lease.lease_id
    refreshed = controller.prepare_rm_switch(new_lease, evidence=_evidence("d2-fresh"))
    assert refreshed["phase"] == "RM_PREPARED"
    assert refreshed["transition_identity"] == "d2-fresh"
    assert refreshed["freeze_lease_id"] == new_lease.lease_id


@pytest.mark.parametrize(
    "configured,restart_validated,expected",
    [
        (AssetAuthority.RM, False, "restart_validation_required"),
        (AssetAuthority.LEGACY, True, "authority_coordination_required"),
    ],
)
def test_boundary_refusals_do_not_open_mutations(tmp_path, configured, restart_validated, expected):
    store, lease = _prepared_store(tmp_path)
    controller = CutoverTransitionController(store.db_path, state_store=store)
    controller.prepare_rm_switch(lease, evidence=_evidence("d2-boundary-1"))
    kwargs = {"configured_authority": configured, "restart_validated": restart_validated}
    with pytest.raises(CutoverTransitionError, match=f"^{expected}$"):
        controller.switch_to_rm(lease, **kwargs)
    assert not AssetMutationGate(CutoverStateStore(store.db_path)).public_mutations_allowed()

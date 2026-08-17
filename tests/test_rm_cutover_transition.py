from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sqlite3
import sys

import pytest

from asset_authority import AssetAuthority
from asset_cutover_state import (
    CutoverState,
    CutoverStateError,
    CutoverStateStore,
    MigrationIdentity,
)
from asset_mutation_gate import AssetMutationGate
from cutover_lease_capability import LeaseCapability, capability_path, write_capability
from remember_me_cutover_operations import acceptance_check_spec
from remember_me_cutover_transition import (
    CutoverTransitionController,
    CutoverTransitionError,
    LEGACY_ACCEPTANCE_NAMES,
    RM_SOURCE_COMMIT,
    _digest,
    main,
)
from tests._rm_acceptance_artifact import valid_rm_acceptance_artifact


PROBE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "rm_frozen_acceptance_probe.py"


def _probe_module():
    spec = importlib.util.spec_from_file_location(
        "rm_frozen_acceptance_probe_contract_test",
        PROBE_PATH,
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


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


def _accept_rm(store, controller, lease, *, passed=True):
    checks, evidence = valid_rm_acceptance_artifact(store, controller, passed=passed)
    return controller.accept_rm(lease, checks=checks, evidence=evidence)


def _all_legacy_checks() -> dict[str, bool]:
    return {name: True for name in LEGACY_ACCEPTANCE_NAMES}


def _rm_acceptance_context(tmp_path: Path):
    store, lease = _prepared_store(tmp_path)
    cap = capability_path(store.db_path.parent)
    write_capability(cap, LeaseCapability(lease.lease_id, lease.token), state_root=store.db_path.parent)
    controller = CutoverTransitionController(
        store.db_path,
        state_store=store,
        capability_file=cap,
    )
    controller.prepare_rm_switch(lease, evidence=_evidence("d2-expired-recovery"))
    controller.switch_to_rm(lease, configured_authority=AssetAuthority.RM, restart_validated=True)
    return store, lease, cap, controller


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

    accepted = _accept_rm(store, CutoverTransitionController(store.db_path), lease)
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


def test_accept_rm_rejects_legacy_checks_only_artifact(tmp_path):
    store, lease, _, controller = _rm_acceptance_context(tmp_path)
    with pytest.raises(CutoverTransitionError, match="^acceptance_evidence_required$"):
        controller.accept_rm(lease, checks=_all_rm_checks())


def test_accept_rm_accepts_probe_produced_canonical_phase(monkeypatch, tmp_path):
    store, lease, _, controller = _rm_acceptance_context(tmp_path)
    monkeypatch.setenv("RENDER_INSTANCE_ID", "test-instance")
    monkeypatch.setenv("RENDER_GIT_COMMIT", "test-commit")
    monkeypatch.setenv("RENDER_SERVICE_ID", "test-service")
    monkeypatch.setenv("RM_PROBE_STATE_DB", str(store.db_path))

    probe_identity = _probe_module().cutover_identity_observation()
    assert probe_identity is not None
    assert probe_identity["phase"] == "RM_FROZEN_ACCEPTANCE"

    checks, evidence = valid_rm_acceptance_artifact(store, controller)
    checks["cutover_identity"] = probe_identity
    evidence["cutover_identity"] = probe_identity
    checks["evidence_sha256"] = _digest(evidence)

    accepted = controller.accept_rm(lease, checks=checks, evidence=evidence)

    assert accepted["acceptance_status"] == "PASS"


def test_accept_rm_rejects_lowercase_probe_phase(monkeypatch, tmp_path):
    store, lease, _, controller = _rm_acceptance_context(tmp_path)
    monkeypatch.setenv("RENDER_INSTANCE_ID", "test-instance")
    monkeypatch.setenv("RENDER_GIT_COMMIT", "test-commit")
    monkeypatch.setenv("RENDER_SERVICE_ID", "test-service")
    monkeypatch.setenv("RM_PROBE_STATE_DB", str(store.db_path))

    probe_identity = _probe_module().cutover_identity_observation()
    assert probe_identity is not None
    bad_identity = {**probe_identity, "phase": "frozen_rm_acceptance"}
    checks, evidence = valid_rm_acceptance_artifact(store, controller)
    checks["cutover_identity"] = bad_identity
    evidence["cutover_identity"] = bad_identity
    checks["evidence_sha256"] = _digest(evidence)

    with pytest.raises(
        CutoverTransitionError,
        match="^acceptance_state_binding_invalid$",
    ):
        controller.accept_rm(lease, checks=checks, evidence=evidence)


def test_accept_rm_cli_rejects_legacy_checks_only_file(tmp_path, capsys):
    store, lease, cap, _ = _rm_acceptance_context(tmp_path)
    checks_path = tmp_path / "rm-acceptance-checks.json"
    checks_path.write_text(json.dumps(_all_rm_checks()), encoding="utf-8")
    result = main(
        [
            "accept-rm",
            "--state-db",
            str(store.db_path),
            "--configured-authority",
            "rm",
            "--lease-capability-file",
            str(cap),
            "--checks",
            str(checks_path),
        ]
    )
    assert result == 2
    assert "evidence_file_invalid" in capsys.readouterr().out


@pytest.mark.parametrize("mutation", ["run", "digest", "generation", "instance", "commit", "service", "timestamp"])
def test_accept_rm_rejects_unbound_or_stale_artifact(tmp_path, mutation):
    store, lease, _, controller = _rm_acceptance_context(tmp_path)
    checks, evidence = valid_rm_acceptance_artifact(store, controller, run_id="bound-run")
    if mutation == "run":
        checks["acceptance_run_id"] = "other-run"
    elif mutation == "digest":
        evidence["summary"]["pass"] = 0
    elif mutation == "generation":
        checks["cutover_identity"]["lease_generation"] += 1
        evidence["cutover_identity"]["lease_generation"] += 1
    elif mutation in {"instance", "commit", "service"}:
        field = {"instance": "instance_id", "commit": "git_commit", "service": "service_id"}[mutation]
        checks["runtime_identity"][field] = "other-runtime"
        evidence["runtime_identity"][field] = "other-runtime"
    else:
        checks["created_at"] = "2000-01-01T00:00:00.000000+00:00"
        checks["completed_at"] = "2000-01-01T00:00:00.000000+00:00"
        evidence["created_at"] = checks["created_at"]
        evidence["completed_at"] = checks["completed_at"]
    if mutation != "digest":
        checks["evidence_sha256"] = _digest(evidence)
    with pytest.raises(CutoverTransitionError):
        controller.accept_rm(lease, checks=checks, evidence=evidence)


def test_expired_rm_recovery_invalidates_prior_acceptance(tmp_path):
    store, lease, cap, controller = _rm_acceptance_context(tmp_path)
    _accept_rm(store, controller, lease)
    with sqlite3.connect(store.db_path) as connection:
        connection.execute(
            "UPDATE cutover_freeze SET expires_at = '2000-01-01T00:00:00+00:00' WHERE singleton = 1"
        )
    recovered = controller.recover_expired_rm_acceptance(
        transition_identity="d2-expired-recovery",
        lease_capability_file=cap,
        lease_ttl_seconds=300,
    )
    assert recovered["acceptance_status"] is None


def test_failed_frozen_acceptance_rehearses_lossless_class_a_rollback(tmp_path):
    store, lease = _prepared_store(tmp_path)
    controller = CutoverTransitionController(store.db_path, state_store=store)
    controller.prepare_rm_switch(lease, evidence=_evidence("d2-rollback-1"))
    controller.switch_to_rm(lease, configured_authority=AssetAuthority.RM, restart_validated=True)

    failed = _accept_rm(store, controller, lease, passed=False)
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
        _accept_rm(store, controller, lease)
    assert CutoverStateStore(store.db_path).get_snapshot().state is CutoverState.FROZEN_RM_ACCEPTANCE


def test_expired_rm_recovery_rejects_active_lease_and_naive_legacy_open(tmp_path):
    store, lease, cap, controller = _rm_acceptance_context(tmp_path)
    with pytest.raises(CutoverTransitionError, match="^freeze_lease_still_active$"):
        controller.recover_expired_rm_acceptance(
            transition_identity="d2-expired-recovery",
            lease_capability_file=cap,
        )
    with sqlite3.connect(store.db_path) as connection:
        connection.execute(
            "UPDATE cutover_freeze SET expires_at = '2000-01-01T00:00:00+00:00' WHERE singleton = 1"
        )
    with pytest.raises(CutoverStateError, match="^recovery_state_invalid$"):
        store.recover_expired_freeze(
            expected_lease_id=lease.lease_id,
            target_state=CutoverState.LEGACY_AUTHORITY_RM_READY,
        )
    assert store.get_snapshot().state is CutoverState.FROZEN_RM_ACCEPTANCE
    assert store.get_snapshot().authority is AssetAuthority.RM
    assert not AssetMutationGate(store).public_mutations_allowed()


@pytest.mark.parametrize("wrong_phase", ["rm_open", "rm_prepared", "legacy", "rollback"])
def test_expired_rm_recovery_rejects_wrong_phase(tmp_path, wrong_phase):
    if wrong_phase == "rm_open":
        store, lease, cap, controller = _rm_acceptance_context(tmp_path)
        _accept_rm(store, controller, lease)
        controller.release_to_rm(lease)
    elif wrong_phase == "rm_prepared":
        store, lease = _prepared_store(tmp_path)
        cap = capability_path(store.db_path.parent)
        write_capability(cap, LeaseCapability(lease.lease_id, lease.token), state_root=store.db_path.parent)
        controller = CutoverTransitionController(store.db_path, state_store=store, capability_file=cap)
        controller.prepare_rm_switch(lease, evidence=_evidence("d2-prepared-only"))
    elif wrong_phase == "legacy":
        store = CutoverStateStore(tmp_path / "state" / "migration.sqlite3")
        store.set_rm_available(True)
        store.transition(CutoverState.LEGACY_AUTHORITY_RM_READY)
        lease = None
        cap = capability_path(store.db_path.parent)
        controller = CutoverTransitionController(store.db_path, state_store=store, capability_file=cap)
    else:
        store, lease, cap, controller = _rm_acceptance_context(tmp_path)
        controller.begin_class_a_rollback(lease, reason="rollback phase")

    with pytest.raises(CutoverTransitionError, match="^recovery_state_invalid$"):
        controller.recover_expired_rm_acceptance(
            transition_identity="d2-expired-recovery",
            lease_capability_file=cap,
        )


def test_expired_rm_recovery_rejects_identity_mismatch_and_rm_unavailable(tmp_path):
    store, lease, cap, controller = _rm_acceptance_context(tmp_path)
    with sqlite3.connect(store.db_path) as connection:
        connection.execute(
            "UPDATE cutover_freeze SET expires_at = '2000-01-01T00:00:00+00:00' WHERE singleton = 1"
        )
        payload = connection.execute(
            "SELECT payload_json FROM d2_transition_record WHERE singleton = 1"
        ).fetchone()[0]
        record = json.loads(payload)
        record["migration_identity"]["source_generation"] = 999
        connection.execute(
            "UPDATE d2_transition_record SET payload_json = ? WHERE singleton = 1",
            (json.dumps(record, sort_keys=True),),
        )
    with pytest.raises(CutoverTransitionError, match="^migration_identity_mismatch$"):
        controller.recover_expired_rm_acceptance(
            transition_identity="d2-expired-recovery",
            lease_capability_file=cap,
        )

    with sqlite3.connect(store.db_path) as connection:
        record["migration_identity"] = {
            "migration_key": _identity().migration_key,
            "migration_version": _identity().migration_version,
            "source_identity": _identity().source_identity,
            "source_generation": _identity().source_generation,
            "target_identity": _identity().target_identity,
        }
        connection.execute(
            "UPDATE d2_transition_record SET payload_json = ? WHERE singleton = 1",
            (json.dumps(record, sort_keys=True),),
        )
    with pytest.raises(CutoverTransitionError, match="^rm_authority_unavailable$"):
        controller.recover_expired_rm_acceptance(
            transition_identity="d2-expired-recovery",
            rm_available=False,
            lease_capability_file=cap,
        )


def test_expired_rm_recovery_requires_matching_transition_identity(tmp_path):
    store, lease, cap, controller = _rm_acceptance_context(tmp_path)
    with sqlite3.connect(store.db_path) as connection:
        connection.execute(
            "UPDATE cutover_freeze SET expires_at = '2000-01-01T00:00:00+00:00' WHERE singleton = 1"
        )
    with pytest.raises(CutoverTransitionError, match="^transition_identity_mismatch$"):
        controller.recover_expired_rm_acceptance(
            transition_identity="stale-transition",
            lease_capability_file=cap,
        )


def test_expired_rm_recovery_failure_after_capability_publish_stays_frozen(tmp_path, monkeypatch):
    store, lease, cap, controller = _rm_acceptance_context(tmp_path)
    with sqlite3.connect(store.db_path) as connection:
        connection.execute(
            "UPDATE cutover_freeze SET expires_at = '2000-01-01T00:00:00+00:00' WHERE singleton = 1"
        )

    def fail_rotation(**kwargs):
        raise CutoverStateError("state_db_unavailable")

    monkeypatch.setattr(store, "rotate_expired_rm_acceptance", fail_rotation)
    with pytest.raises(CutoverTransitionError, match="^state_db_unavailable$"):
        controller.recover_expired_rm_acceptance(
            transition_identity="d2-expired-recovery",
            lease_capability_file=cap,
        )
    snapshot = store.get_snapshot()
    assert snapshot.state is CutoverState.FROZEN_RM_ACCEPTANCE
    assert snapshot.authority is AssetAuthority.RM
    assert snapshot.freeze_status == "expired"
    assert not AssetMutationGate(store).public_mutations_allowed()
    assert store.get_expired_rm_recovery_pending() is not None
    monkeypatch.undo()
    resumed = controller.recover_expired_rm_acceptance(
        transition_identity="d2-expired-recovery",
        lease_capability_file=cap,
    )
    assert resumed["freeze_status"] == "active"
    assert resumed["expired_recovery_pending"] is False


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

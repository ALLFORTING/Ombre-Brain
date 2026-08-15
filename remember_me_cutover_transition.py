"""Implementation D2: explicit authority transition and frozen acceptance.

This module is an operator/control-plane tool.  It is intentionally not
imported by :mod:`server`, does not call Render or any provider, and does not
open the public MCP or Dashboard routes.  The only durable state it changes
is the operator-supplied local cutover database.

The external ``OMBRE_ASSET_AUTHORITY`` change remains an operator action.  A
prepared transition records the expected value before that change.  During a
restart with the new value but before finalization, the controller reports a
coordination-pending boot: all mutations are blocked and no authority is
silently selected.  Finalization then atomically moves the durable state into
the frozen acceptance state.  Freeze release is a separate, acceptance-gated
operation.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import sys
from typing import Any

from asset_authority import AssetAuthority, parse_asset_authority
from asset_cutover_state import (
    CutoverSnapshot,
    CutoverState,
    CutoverStateError,
    CutoverStateStore,
    FreezeLease,
    MigrationIdentity,
    validate_cutover_boot,
)
from remember_me_adapter import EXPECTED_PACKAGE_VERSION
from remember_me_cutover_operations import (
    ACCEPTANCE_CHECK_NAMES,
    evaluate_readiness,
    run_frozen_acceptance_checks,
)
from cutover_lease_capability import (
    LeaseCapabilityError,
    read_capability,
    remove_capability,
)


TOOL_NAME = "ombre-rm-cutover-transition"
TOOL_VERSION = "1.0.0-d2"
D2_SCHEMA_VERSION = 1
RM_SOURCE_COMMIT = "a00ea991442d7581a3856b178525a8e77da833fe"
SAFE_ID = re.compile(r"[A-Za-z0-9_.:@/-]{1,160}\Z")
SAFE_REASON = re.compile(r"[A-Za-z0-9_.:@/ -]{1,256}\Z")

PHASE_NONE = "NONE"
PHASE_RM_PREPARED = "RM_PREPARED"
PHASE_RM_FROZEN_ACCEPTANCE = "RM_FROZEN_ACCEPTANCE"
PHASE_RM_OPEN = "RM_OPEN"
PHASE_ROLLBACK_PREPARED = "ROLLBACK_PREPARED"
PHASE_LEGACY_FROZEN_ACCEPTANCE = "LEGACY_FROZEN_ACCEPTANCE"
PHASE_LEGACY_OPEN = "LEGACY_OPEN"

RM_COORDINATION_PHASES = frozenset({PHASE_RM_PREPARED})
ROLLBACK_COORDINATION_PHASES = frozenset({PHASE_ROLLBACK_PREPARED})

LEGACY_ACCEPTANCE_NAMES = (
    "legacy_reads",
    "legacy_authorization",
    "rm_target_preserved",
    "no_rm_only_ordinary_write",
    "data_identity_exact",
)

_STATUS_TRUE = frozenset({"PASS", "READY", "YES", "TRUE", "EXACT", "COMPLETE"})


class CutoverTransitionError(RuntimeError):
    """Stable operator-facing error without paths, content, or secrets."""

    def __init__(self, code: str, *, details: Mapping[str, Any] | None = None) -> None:
        self.code = code
        self.details = dict(details or {})
        super().__init__(code)


@dataclass(frozen=True)
class TransitionRecord:
    phase: str
    transition_identity: str
    expected_authority: AssetAuthority | None
    authority_before: AssetAuthority | None
    authority_after: AssetAuthority | None
    state_before: CutoverState | None
    state_after: CutoverState | None
    lease_id: str | None
    migration_identity: MigrationIdentity | None
    readiness: dict[str, Any]
    acceptance: dict[str, Any]
    legacy_acceptance: dict[str, Any]
    failures: list[str]
    warnings: list[str]
    prepared_at: str | None
    restart_validated_at: str | None
    updated_at: str | None
    freeze_released_at: str | None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _safe_id(value: Any, code: str) -> str:
    if not isinstance(value, str) or SAFE_ID.fullmatch(value) is None:
        raise CutoverTransitionError(code)
    return value


def _safe_reason(value: Any) -> str:
    if not isinstance(value, str) or SAFE_REASON.fullmatch(value) is None:
        raise CutoverTransitionError("rollback_reason_invalid")
    return value.strip()


def _authority(value: Any) -> AssetAuthority:
    try:
        return parse_asset_authority(value.value if isinstance(value, AssetAuthority) else value)
    except (TypeError, ValueError) as exc:
        raise CutoverTransitionError("authority_invalid") from exc


def _state(value: Any) -> CutoverState:
    if isinstance(value, CutoverState):
        return value
    try:
        return CutoverState(value)
    except (TypeError, ValueError) as exc:
        raise CutoverTransitionError("state_invalid") from exc


def _truth(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().upper() in _STATUS_TRUE
    if isinstance(value, Mapping):
        return _truth(value.get("status"))
    return False


def _value(evidence: Mapping[str, Any], *paths: tuple[str, ...]) -> Any:
    for path in paths:
        current: Any = evidence
        for key in path:
            if not isinstance(current, Mapping) or key not in current:
                current = None
                break
            current = current[key]
        if current is not None:
            return current
    return None


def _safe_gate_map(values: Mapping[str, bool]) -> dict[str, str]:
    return {key: "PASS" if value else "FAIL" for key, value in values.items()}


def _identity_payload(identity: MigrationIdentity | None) -> dict[str, Any] | None:
    if identity is None:
        return None
    return {
        "migration_key": identity.migration_key,
        "migration_version": identity.migration_version,
        "source_identity": identity.source_identity,
        "source_generation": identity.source_generation,
        "target_identity": identity.target_identity,
    }


def _identity_from_payload(value: Any) -> MigrationIdentity | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise CutoverTransitionError("migration_identity_invalid")
    try:
        return MigrationIdentity(
            migration_key=_safe_id(value.get("migration_key"), "migration_identity_invalid"),
            migration_version=value["migration_version"],
            source_identity=_safe_id(value.get("source_identity"), "migration_identity_invalid"),
            source_generation=value["source_generation"],
            target_identity=_safe_id(value.get("target_identity"), "migration_identity_invalid"),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise CutoverTransitionError("migration_identity_invalid") from exc


def _record_json(record: TransitionRecord) -> dict[str, Any]:
    return {
        "phase": record.phase,
        "transition_identity": record.transition_identity,
        "expected_authority": record.expected_authority.value if record.expected_authority else None,
        "authority_before": record.authority_before.value if record.authority_before else None,
        "authority_after": record.authority_after.value if record.authority_after else None,
        "state_before": record.state_before.value if record.state_before else None,
        "state_after": record.state_after.value if record.state_after else None,
        "lease_id": record.lease_id,
        "migration_identity": _identity_payload(record.migration_identity),
        "readiness": record.readiness,
        "acceptance": record.acceptance,
        "legacy_acceptance": record.legacy_acceptance,
        "failures": list(record.failures),
        "warnings": list(record.warnings),
        "prepared_at": record.prepared_at,
        "restart_validated_at": record.restart_validated_at,
        "updated_at": record.updated_at,
        "freeze_released_at": record.freeze_released_at,
    }


def _readiness_input(evidence: Mapping[str, Any], snapshot: CutoverSnapshot, lease: FreezeLease) -> dict[str, Any]:
    dependency = _value(evidence, ("dependency",), ("contract",))
    dependency = dependency if isinstance(dependency, Mapping) else {}
    dependency_exact = (
        dependency.get("version", dependency.get("package_version")) == EXPECTED_PACKAGE_VERSION
        and dependency.get("source_commit") == RM_SOURCE_COMMIT
    )
    gates = {
        "current_authority_legacy": snapshot.authority is AssetAuthority.LEGACY,
        "freeze_active": snapshot.freeze_status == "active",
        "matching_freeze_lease": snapshot.lease_id == lease.lease_id,
        "rm_runtime_healthy": _truth(_value(evidence, ("rm_runtime_healthy",), ("rm_healthy",), ("runtime", "status"))),
        "rm_data_root_healthy": _truth(_value(evidence, ("rm_data_root_healthy",), ("storage_layout",), ("storage", "status"))),
        "dependency_exact": dependency_exact,
        "migration_complete": _truth(_value(evidence, ("migration_complete",), ("migration", "status"))),
        "reconciliation_pass": _truth(_value(evidence, ("reconciliation_pass",), ("reconciliation", "status"))),
        "verification_pass": _truth(_value(evidence, ("verification_pass",), ("verification", "status"))),
        "vector_readiness_pass": _truth(_value(evidence, ("vector_readiness_pass",), ("vectors", "status"))),
        "backup_evidence_present": _truth(_value(evidence, ("backup_evidence_present",), ("backup", "status"))) and bool(
            _value(evidence, ("backup_evidence_id",), ("backup", "identity"))
        ),
        "storage_root_validation_pass": _truth(_value(evidence, ("storage_root_validation_pass",), ("storage_layout",), ("storage", "status"))),
        "disk_readiness_pass": _truth(_value(evidence, ("disk_readiness_pass",), ("disk", "status"))),
        "topology_readiness_pass": _truth(_value(evidence, ("topology_readiness_pass",), ("topology", "status"))),
        "no_stale_authority": _truth(_value(evidence, ("no_stale_authority",), ("stale_authority_clear",), ("authority", "status"))),
        "state_healthy": _truth(_value(evidence, ("state_healthy",), ("state", "status"))),
    }
    d1_evidence = {
        "dependency_exact": gates["dependency_exact"],
        "storage_layout": gates["storage_root_validation_pass"],
        "state_healthy": gates["state_healthy"],
        "freeze_held": gates["freeze_active"],
        "legacy_authority_active": gates["current_authority_legacy"],
        "migration_complete": gates["migration_complete"],
        "reconciliation_exact": gates["reconciliation_pass"],
        "verification_passed": gates["verification_pass"],
        "vector_profile": gates["vector_readiness_pass"],
        "backup_verified": gates["backup_evidence_present"],
        "disk_acceptable": gates["disk_readiness_pass"],
        "topology_safe": gates["topology_readiness_pass"],
        "stale_authority_clear": gates["no_stale_authority"],
    }
    evaluator = evaluate_readiness(d1_evidence)
    gates["readiness_evaluator_yes"] = evaluator["READY_FOR_AUTHORITY_SWITCH"] == "YES"
    return {
        "status": "PASS" if all(gates.values()) else "FAIL",
        "READY_FOR_AUTHORITY_SWITCH": "YES" if all(gates.values()) else "NO",
        "hard_gates": _safe_gate_map(gates),
        "d1_readiness": evaluator["READY_FOR_AUTHORITY_SWITCH"],
        "evidence_identity": _safe_id(
            _value(evidence, ("readiness_evidence_id",), ("transition_identity",)),
            "readiness_evidence_identity_invalid",
        ),
        "backup_evidence_id": _safe_id(
            _value(evidence, ("backup_evidence_id",), ("backup", "identity")),
            "backup_evidence_identity_invalid",
        ) if gates["backup_evidence_present"] else None,
    }


def _acceptance_payload(result: Mapping[str, Any], identity: str) -> dict[str, Any]:
    return {
        "status": result.get("status"),
        "evidence_identity": identity,
        "checks": {
            str(name): str(value.get("status"))
            for name, value in result.get("checks", {}).items()
            if isinstance(name, str) and isinstance(value, Mapping)
        },
        "state_prerequisite": result.get("state_prerequisite"),
        "production_access_occurred": False,
        "recorded_at": _now(),
    }


def _simple_acceptance(checks: Mapping[str, Any], names: tuple[str, ...]) -> dict[str, Any]:
    results = {
        name: "PASS" if _truth(checks.get(name)) else "FAIL" if checks.get(name) is False else "INCOMPLETE"
        for name in names
    }
    statuses = set(results.values())
    overall = "FAIL" if "FAIL" in statuses else "PASS" if statuses == {"PASS"} else "INCOMPLETE"
    return {
        "status": overall,
        "checks": results,
        "production_access_occurred": False,
        "recorded_at": _now(),
    }


class CutoverTransitionController:
    """Durable D2 transition coordinator for one local cutover state DB."""

    def __init__(self, state_db: str | Path, *, state_store: CutoverStateStore | None = None, capability_file: str | Path | None = None) -> None:
        if isinstance(state_db, bool):
            raise CutoverTransitionError("state_db_invalid")
        try:
            path = Path(state_db).expanduser()
        except (TypeError, ValueError, OSError) as exc:
            raise CutoverTransitionError("state_db_invalid") from exc
        if not path.is_absolute():
            raise CutoverTransitionError("state_db_not_absolute")
        self.state_db = path.resolve(strict=False)
        if state_store is None and not self.state_db.is_file():
            raise CutoverTransitionError("state_db_missing")
        self.capability_file = (
            Path(capability_file).expanduser().resolve(strict=False)
            if capability_file is not None
            else self.state_db.parent / "operator" / "lease-token.json"
        )
        try:
            self.state = state_store or CutoverStateStore(self.state_db)
        except CutoverStateError as exc:
            raise CutoverTransitionError(exc.code) from exc
        self._ensure_schema()

    def _connect(self) -> sqlite3.Connection:
        try:
            connection = sqlite3.connect(self.state_db, timeout=5)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA busy_timeout = 5000")
            return connection
        except (OSError, sqlite3.Error) as exc:
            raise CutoverTransitionError("transition_db_unavailable") from exc

    def _ensure_schema(self) -> None:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS d2_transition_record (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                    schema_version INTEGER NOT NULL,
                    payload_json TEXT NOT NULL
                )
                """
            )
            row = connection.execute(
                "SELECT schema_version FROM d2_transition_record WHERE singleton = 1"
            ).fetchone()
            if row is not None and int(row["schema_version"]) != D2_SCHEMA_VERSION:
                raise CutoverTransitionError("transition_schema_incompatible")
            connection.commit()
        except CutoverTransitionError:
            connection.rollback()
            raise
        except (OSError, sqlite3.Error) as exc:
            connection.rollback()
            raise CutoverTransitionError("transition_db_unavailable") from exc
        finally:
            connection.close()

    def _read_record(self) -> TransitionRecord | None:
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT schema_version, payload_json FROM d2_transition_record WHERE singleton = 1"
            ).fetchone()
            if row is None:
                return None
            if int(row["schema_version"]) != D2_SCHEMA_VERSION:
                raise CutoverTransitionError("transition_schema_incompatible")
            payload = json.loads(row["payload_json"])
            if not isinstance(payload, Mapping):
                raise CutoverTransitionError("transition_record_corrupt")
            return TransitionRecord(
                phase=str(payload.get("phase")),
                transition_identity=str(payload.get("transition_identity")),
                expected_authority=_authority(payload["expected_authority"]) if payload.get("expected_authority") else None,
                authority_before=_authority(payload["authority_before"]) if payload.get("authority_before") else None,
                authority_after=_authority(payload["authority_after"]) if payload.get("authority_after") else None,
                state_before=_state(payload["state_before"]) if payload.get("state_before") else None,
                state_after=_state(payload["state_after"]) if payload.get("state_after") else None,
                lease_id=payload.get("lease_id"),
                migration_identity=_identity_from_payload(payload.get("migration_identity")),
                readiness=dict(payload.get("readiness") or {}),
                acceptance=dict(payload.get("acceptance") or {}),
                legacy_acceptance=dict(payload.get("legacy_acceptance") or {}),
                failures=[str(item) for item in payload.get("failures", [])],
                warnings=[str(item) for item in payload.get("warnings", [])],
                prepared_at=payload.get("prepared_at"),
                restart_validated_at=payload.get("restart_validated_at"),
                updated_at=payload.get("updated_at"),
                freeze_released_at=payload.get("freeze_released_at"),
            )
        except CutoverTransitionError:
            raise
        except (OSError, sqlite3.Error, ValueError, TypeError, json.JSONDecodeError) as exc:
            raise CutoverTransitionError("transition_record_corrupt") from exc
        finally:
            connection.close()

    def _write_record(self, record: TransitionRecord) -> None:
        payload = _record_json(record)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO d2_transition_record (singleton, schema_version, payload_json)
                VALUES (1, ?, ?)
                ON CONFLICT(singleton) DO UPDATE SET
                    schema_version = excluded.schema_version,
                    payload_json = excluded.payload_json
                """,
                (D2_SCHEMA_VERSION, _canonical(payload)),
            )
            connection.commit()
        except (OSError, sqlite3.Error) as exc:
            connection.rollback()
            raise CutoverTransitionError("transition_db_unavailable") from exc
        finally:
            connection.close()

    @staticmethod
    def _current_phase(record: TransitionRecord | None, snapshot: CutoverSnapshot) -> str:
        if snapshot.state is CutoverState.FROZEN_RM_ACCEPTANCE:
            return PHASE_RM_FROZEN_ACCEPTANCE
        if snapshot.state is CutoverState.RM_AUTHORITY_OPEN:
            return PHASE_RM_OPEN
        if snapshot.state is CutoverState.FROZEN_RM_ROLLBACK:
            return PHASE_ROLLBACK_PREPARED
        if snapshot.state is CutoverState.FROZEN_LEGACY_ACCEPTANCE:
            return PHASE_LEGACY_FROZEN_ACCEPTANCE
        if record is not None:
            return record.phase
        return PHASE_NONE

    @staticmethod
    def _record_with(record: TransitionRecord, **changes: Any) -> TransitionRecord:
        values = _record_json(record)
        values.update(changes)
        return TransitionRecord(
            phase=values["phase"],
            transition_identity=values["transition_identity"],
            expected_authority=_authority(values["expected_authority"]) if values.get("expected_authority") else None,
            authority_before=_authority(values["authority_before"]) if values.get("authority_before") else None,
            authority_after=_authority(values["authority_after"]) if values.get("authority_after") else None,
            state_before=_state(values["state_before"]) if values.get("state_before") else None,
            state_after=_state(values["state_after"]) if values.get("state_after") else None,
            lease_id=values.get("lease_id"),
            migration_identity=_identity_from_payload(values.get("migration_identity")),
            readiness=dict(values.get("readiness") or {}),
            acceptance=dict(values.get("acceptance") or {}),
            legacy_acceptance=dict(values.get("legacy_acceptance") or {}),
            failures=[str(item) for item in values.get("failures", [])],
            warnings=[str(item) for item in values.get("warnings", [])],
            prepared_at=values.get("prepared_at"),
            restart_validated_at=values.get("restart_validated_at"),
            updated_at=values.get("updated_at"),
            freeze_released_at=values.get("freeze_released_at"),
        )

    def load_lease(self, lease_id: str, token: str) -> FreezeLease:
        """Rehydrate a lease handle without persisting or printing its token."""
        _safe_id(lease_id, "freeze_lease_invalid")
        if not isinstance(token, str) or not token:
            raise CutoverTransitionError("freeze_lease_invalid")
        try:
            return self.state.load_lease(lease_id, token)
        except CutoverTransitionError:
            raise
        except CutoverStateError as exc:
            raise CutoverTransitionError(exc.code) from exc

    def load_capability(self, path: str | Path | None = None) -> FreezeLease:
        capability_path = path or self.capability_file
        if capability_path is None:
            raise CutoverTransitionError("capability_file_required")
        try:
            capability = read_capability(capability_path, state_root=self.state_db.parent)
        except LeaseCapabilityError as exc:
            raise CutoverTransitionError(exc.code) from exc
        return self.load_lease(capability.lease_id, capability.token)

    def _cleanup_capability(self) -> None:
        if self.capability_file is None:
            return
        try:
            remove_capability(self.capability_file, state_root=self.state_db.parent)
        except LeaseCapabilityError as exc:
            raise CutoverTransitionError(exc.code) from exc

    def _assert_lease(self, lease: FreezeLease, snapshot: CutoverSnapshot) -> None:
        if not isinstance(lease, FreezeLease) or snapshot.lease_id != lease.lease_id:
            raise CutoverTransitionError("freeze_lease_invalid")
        try:
            self.state.issue_privileged_capability(lease, purpose="d2_transition")
        except CutoverStateError as exc:
            raise CutoverTransitionError(exc.code) from exc

    def _assert_identity(self, snapshot: CutoverSnapshot, record: TransitionRecord | None = None) -> MigrationIdentity:
        identity = snapshot.migration_identity
        if identity is None:
            raise CutoverTransitionError("migration_identity_missing")
        if record is not None and record.migration_identity not in {None, identity}:
            raise CutoverTransitionError("migration_identity_mismatch")
        return identity

    def _require_config(self, configured_authority: Any, expected: AssetAuthority) -> AssetAuthority:
        actual = _authority(configured_authority)
        if actual is not expected:
            raise CutoverTransitionError("authority_coordination_required")
        return actual

    def prepare_rm_switch(
        self,
        lease: FreezeLease,
        *,
        evidence: Mapping[str, Any],
        configured_authority: AssetAuthority | str = AssetAuthority.LEGACY,
    ) -> dict[str, Any]:
        """Persist a gated RM candidate without changing external config."""
        if not isinstance(evidence, Mapping):
            raise CutoverTransitionError("readiness_evidence_invalid")
        self._require_config(configured_authority, AssetAuthority.LEGACY)
        snapshot = self.state.get_snapshot()
        record = self._read_record()
        phase = self._current_phase(record, snapshot)
        transition_identity = _safe_id(evidence.get("transition_identity"), "transition_identity_invalid")
        if phase in {PHASE_RM_PREPARED, PHASE_RM_FROZEN_ACCEPTANCE, PHASE_RM_OPEN}:
            if record is not None and record.transition_identity != transition_identity:
                raise CutoverTransitionError("transition_identity_mismatch")
            return self.status(configured_authority=configured_authority)
        if snapshot.state is not CutoverState.FROZEN_READY_FOR_RM_SWITCH:
            raise CutoverTransitionError("switch_state_invalid")
        if snapshot.authority is not AssetAuthority.LEGACY:
            raise CutoverTransitionError("legacy_authority_required")
        self._assert_lease(lease, snapshot)
        identity = self._assert_identity(snapshot)
        readiness = _readiness_input(evidence, snapshot, lease)
        if readiness["READY_FOR_AUTHORITY_SWITCH"] != "YES":
            raise CutoverTransitionError(
                "readiness_gate_failed",
                details={"blocking_gates": [key for key, value in readiness["hard_gates"].items() if value != "PASS"]},
            )
        supplied_identity = evidence.get("migration_identity")
        if supplied_identity is not None and _identity_from_payload(supplied_identity) != identity:
            raise CutoverTransitionError("migration_identity_mismatch")
        record = TransitionRecord(
            phase=PHASE_RM_PREPARED,
            transition_identity=transition_identity,
            expected_authority=AssetAuthority.RM,
            authority_before=AssetAuthority.LEGACY,
            authority_after=AssetAuthority.RM,
            state_before=snapshot.state,
            state_after=CutoverState.FROZEN_RM_ACCEPTANCE,
            lease_id=lease.lease_id,
            migration_identity=identity,
            readiness=readiness,
            acceptance={},
            legacy_acceptance={},
            failures=[],
            warnings=[],
            prepared_at=_now(),
            restart_validated_at=None,
            updated_at=_now(),
            freeze_released_at=None,
        )
        self._write_record(record)
        return self.status(configured_authority=configured_authority)

    def validate_restart(
        self,
        *,
        configured_authority: AssetAuthority | str,
        rm_available: bool,
    ) -> dict[str, Any]:
        """Validate a boot boundary without selecting a silent fallback."""
        if type(rm_available) is not bool:
            raise CutoverTransitionError("rm_availability_invalid")
        configured = _authority(configured_authority)
        snapshot = self.state.get_snapshot()
        record = self._read_record()
        phase = self._current_phase(record, snapshot)
        if phase in RM_COORDINATION_PHASES:
            self._require_config(configured, AssetAuthority.RM)
            if not rm_available:
                raise CutoverTransitionError("rm_authority_unavailable")
            if snapshot.state is not CutoverState.FROZEN_READY_FOR_RM_SWITCH:
                raise CutoverTransitionError("transition_state_corrupt")
            if snapshot.freeze_status != "active":
                raise CutoverTransitionError("freeze_lease_lost")
            return {
                "status": "PASS",
                "boot_mode": "COORDINATION_PENDING",
                "writes_allowed": False,
                "legacy_fallback_allowed": False,
                "coordination_pending": True,
                "state": snapshot.state.value,
                "authority": snapshot.authority.value,
                "production_access_occurred": False,
            }
        if phase in ROLLBACK_COORDINATION_PHASES:
            self._require_config(configured, AssetAuthority.LEGACY)
            if snapshot.state is not CutoverState.FROZEN_RM_ROLLBACK:
                raise CutoverTransitionError("transition_state_corrupt")
            if snapshot.freeze_status != "active":
                raise CutoverTransitionError("freeze_lease_lost")
            return {
                "status": "PASS",
                "boot_mode": "COORDINATION_PENDING",
                "writes_allowed": False,
                "legacy_fallback_allowed": False,
                "coordination_pending": True,
                "state": snapshot.state.value,
                "authority": snapshot.authority.value,
                "production_access_occurred": False,
            }
        try:
            result = validate_cutover_boot(configured, snapshot, rm_available=rm_available)
        except CutoverStateError as exc:
            raise CutoverTransitionError(exc.code) from exc
        return {
            "status": "PASS",
            "boot_mode": "NORMAL",
            "writes_allowed": result.writes_allowed,
            "legacy_fallback_allowed": result.authority is AssetAuthority.LEGACY,
            "coordination_pending": False,
            "state": snapshot.state.value,
            "authority": result.authority.value,
            "production_access_occurred": False,
        }

    def switch_to_rm(
        self,
        lease: FreezeLease,
        *,
        configured_authority: AssetAuthority | str,
        rm_available: bool = True,
        restart_validated: bool = False,
    ) -> dict[str, Any]:
        """Finalize a prepared RM switch into frozen RM acceptance."""
        snapshot = self.state.get_snapshot()
        record = self._read_record()
        phase = self._current_phase(record, snapshot)
        if phase in {PHASE_RM_FROZEN_ACCEPTANCE, PHASE_RM_OPEN}:
            self._require_config(configured_authority, AssetAuthority.RM)
            return self.status(configured_authority=configured_authority)
        if phase != PHASE_RM_PREPARED or record is None:
            raise CutoverTransitionError("switch_not_prepared")
        if type(restart_validated) is not bool or not restart_validated:
            raise CutoverTransitionError("restart_validation_required")
        self._require_config(configured_authority, AssetAuthority.RM)
        self.validate_restart(configured_authority=configured_authority, rm_available=rm_available)
        if record.readiness.get("READY_FOR_AUTHORITY_SWITCH") != "YES":
            raise CutoverTransitionError("readiness_gate_failed")
        self._assert_lease(lease, snapshot)
        identity = self._assert_identity(snapshot, record)
        try:
            self.state.transition(
                CutoverState.FROZEN_RM_ACCEPTANCE,
                lease=lease,
                migration_identity=identity,
            )
        except CutoverStateError as exc:
            raise CutoverTransitionError(exc.code) from exc
        updated = self._record_with(
            record,
            phase=PHASE_RM_FROZEN_ACCEPTANCE,
            restart_validated_at=_now(),
            updated_at=_now(),
        )
        self._write_record(updated)
        return self.status(configured_authority=configured_authority)

    def accept_rm(
        self,
        lease: FreezeLease,
        *,
        checks: Mapping[str, Callable[[], Any] | bool],
        configured_authority: AssetAuthority | str = AssetAuthority.RM,
        acceptance_identity: str | None = None,
    ) -> dict[str, Any]:
        """Record frozen RM acceptance; this never releases the freeze."""
        self._require_config(configured_authority, AssetAuthority.RM)
        snapshot = self.state.get_snapshot()
        record = self._read_record()
        if self._current_phase(record, snapshot) != PHASE_RM_FROZEN_ACCEPTANCE:
            raise CutoverTransitionError("rm_acceptance_state_invalid")
        self._assert_lease(lease, snapshot)
        identity = _safe_id(
            acceptance_identity or (record.transition_identity + ":acceptance" if record else None),
            "acceptance_identity_invalid",
        )
        result = run_frozen_acceptance_checks(
            state={"authority": AssetAuthority.RM.value, "frozen": True},
            checks=checks,
        )
        acceptance = _acceptance_payload(result, identity)
        if record is None:
            raise CutoverTransitionError("transition_record_missing")
        self._write_record(self._record_with(record, acceptance=acceptance, updated_at=_now()))
        return self.status(configured_authority=configured_authority)

    def release_to_rm(
        self,
        lease: FreezeLease,
        *,
        configured_authority: AssetAuthority | str = AssetAuthority.RM,
    ) -> dict[str, Any]:
        """Atomically establish RM open state and remove the freeze lease."""
        self._require_config(configured_authority, AssetAuthority.RM)
        snapshot = self.state.get_snapshot()
        record = self._read_record()
        if self._current_phase(record, snapshot) == PHASE_RM_OPEN:
            return self.status(configured_authority=configured_authority)
        if self._current_phase(record, snapshot) != PHASE_RM_FROZEN_ACCEPTANCE:
            raise CutoverTransitionError("rm_release_state_invalid")
        if record is None or record.acceptance.get("status") != "PASS":
            raise CutoverTransitionError("rm_acceptance_not_passed")
        if record.warnings:
            raise CutoverTransitionError("unresolved_hard_warning")
        self._assert_lease(lease, snapshot)
        if snapshot.freeze_status != "active":
            raise CutoverTransitionError("freeze_lease_lost")
        self._assert_identity(snapshot, record)
        self.validate_restart(
            configured_authority=configured_authority,
            rm_available=snapshot.rm_available,
        )
        try:
            self.state.release_freeze(lease, target_state=CutoverState.RM_AUTHORITY_OPEN)
        except CutoverStateError as exc:
            raise CutoverTransitionError(exc.code) from exc
        self._write_record(
            self._record_with(
                record,
                phase=PHASE_RM_OPEN,
                updated_at=_now(),
                freeze_released_at=_now(),
            )
        )
        self._cleanup_capability()
        return self.status(configured_authority=configured_authority)

    def begin_class_a_rollback(
        self,
        lease: FreezeLease,
        *,
        reason: str,
        configured_authority: AssetAuthority | str = AssetAuthority.RM,
    ) -> dict[str, Any]:
        """Enter the frozen rollback candidate; RM data and evidence remain."""
        self._require_config(configured_authority, AssetAuthority.RM)
        reason = _safe_reason(reason)
        snapshot = self.state.get_snapshot()
        record = self._read_record()
        phase = self._current_phase(record, snapshot)
        if phase == PHASE_ROLLBACK_PREPARED:
            return self.status(configured_authority=configured_authority)
        if phase != PHASE_RM_FROZEN_ACCEPTANCE or record is None:
            raise CutoverTransitionError("class_a_window_closed")
        self._assert_lease(lease, snapshot)
        identity = self._assert_identity(snapshot, record)
        try:
            self.state.transition(
                CutoverState.FROZEN_RM_ROLLBACK,
                lease=lease,
                migration_identity=identity,
            )
        except CutoverStateError as exc:
            raise CutoverTransitionError(exc.code) from exc
        updated = self._record_with(
            record,
            phase=PHASE_ROLLBACK_PREPARED,
            expected_authority=AssetAuthority.LEGACY.value,
            authority_before=AssetAuthority.RM.value,
            authority_after=AssetAuthority.LEGACY.value,
            state_before=CutoverState.FROZEN_RM_ACCEPTANCE.value,
            state_after=CutoverState.FROZEN_LEGACY_ACCEPTANCE.value,
            failures=list(record.failures) + ["class_a_rollback:" + reason],
            updated_at=_now(),
        )
        self._write_record(updated)
        return self.status(configured_authority=configured_authority)

    def finalize_class_a_rollback(
        self,
        lease: FreezeLease,
        *,
        configured_authority: AssetAuthority | str,
        restart_validated: bool = False,
        rm_available: bool = True,
    ) -> dict[str, Any]:
        self._require_config(configured_authority, AssetAuthority.LEGACY)
        if not restart_validated:
            raise CutoverTransitionError("restart_validation_required")
        snapshot = self.state.get_snapshot()
        record = self._read_record()
        if self._current_phase(record, snapshot) == PHASE_LEGACY_FROZEN_ACCEPTANCE:
            return self.status(configured_authority=configured_authority)
        if self._current_phase(record, snapshot) != PHASE_ROLLBACK_PREPARED or record is None:
            raise CutoverTransitionError("rollback_not_prepared")
        self.validate_restart(configured_authority=configured_authority, rm_available=rm_available)
        self._assert_lease(lease, snapshot)
        identity = self._assert_identity(snapshot, record)
        try:
            self.state.transition(
                CutoverState.FROZEN_LEGACY_ACCEPTANCE,
                lease=lease,
                migration_identity=identity,
            )
        except CutoverStateError as exc:
            raise CutoverTransitionError(exc.code) from exc
        self._write_record(
            self._record_with(
                record,
                phase=PHASE_LEGACY_FROZEN_ACCEPTANCE,
                restart_validated_at=_now(),
                updated_at=_now(),
            )
        )
        return self.status(configured_authority=configured_authority)

    def accept_legacy(
        self,
        lease: FreezeLease,
        *,
        checks: Mapping[str, Any],
        configured_authority: AssetAuthority | str = AssetAuthority.LEGACY,
    ) -> dict[str, Any]:
        self._require_config(configured_authority, AssetAuthority.LEGACY)
        snapshot = self.state.get_snapshot()
        record = self._read_record()
        if self._current_phase(record, snapshot) != PHASE_LEGACY_FROZEN_ACCEPTANCE:
            raise CutoverTransitionError("legacy_acceptance_state_invalid")
        self._assert_lease(lease, snapshot)
        if record is None:
            raise CutoverTransitionError("transition_record_missing")
        result = _simple_acceptance(checks, LEGACY_ACCEPTANCE_NAMES)
        self._write_record(self._record_with(record, legacy_acceptance=result, updated_at=_now()))
        return self.status(configured_authority=configured_authority)

    def release_to_legacy(
        self,
        lease: FreezeLease,
        *,
        configured_authority: AssetAuthority | str = AssetAuthority.LEGACY,
    ) -> dict[str, Any]:
        self._require_config(configured_authority, AssetAuthority.LEGACY)
        snapshot = self.state.get_snapshot()
        record = self._read_record()
        if self._current_phase(record, snapshot) == PHASE_LEGACY_OPEN:
            return self.status(configured_authority=configured_authority)
        if self._current_phase(record, snapshot) != PHASE_LEGACY_FROZEN_ACCEPTANCE:
            raise CutoverTransitionError("legacy_release_state_invalid")
        if record is None or record.legacy_acceptance.get("status") != "PASS":
            raise CutoverTransitionError("legacy_acceptance_not_passed")
        self._assert_lease(lease, snapshot)
        self.validate_restart(
            configured_authority=configured_authority,
            rm_available=snapshot.rm_available,
        )
        try:
            self.state.release_freeze(lease, target_state=CutoverState.LEGACY_AUTHORITY_RM_READY)
        except CutoverStateError as exc:
            raise CutoverTransitionError(exc.code) from exc
        self._write_record(
            self._record_with(
                record,
                phase=PHASE_LEGACY_OPEN,
                updated_at=_now(),
                freeze_released_at=_now(),
            )
        )
        self._cleanup_capability()
        return self.status(configured_authority=configured_authority)

    def status(self, *, configured_authority: AssetAuthority | str | None = None) -> dict[str, Any]:
        """Return operator-safe status and the next legal actions."""
        snapshot = self.state.get_snapshot()
        record = self._read_record()
        phase = self._current_phase(record, snapshot)
        configured = _authority(configured_authority) if configured_authority is not None else None
        acceptance_status = record.acceptance.get("status") if record else None
        legacy_acceptance_status = record.legacy_acceptance.get("status") if record else None
        actions: list[str] = []
        if phase == PHASE_RM_PREPARED:
            actions = ["set OMBRE_ASSET_AUTHORITY=rm externally", "controlled restart", "switch-to-rm"]
        elif phase == PHASE_RM_FROZEN_ACCEPTANCE:
            actions = ["run frozen RM acceptance", "release-freeze-to-rm", "class-a-rollback"]
        elif phase == PHASE_ROLLBACK_PREPARED:
            actions = ["set OMBRE_ASSET_AUTHORITY=legacy externally", "controlled restart", "finalize-class-a-rollback"]
        elif phase == PHASE_LEGACY_FROZEN_ACCEPTANCE:
            actions = ["run legacy acceptance", "release-freeze-to-legacy"]
        elif phase == PHASE_RM_OPEN:
            actions = ["no legacy rollback; use future Class B reverse reconciliation"]
        elif phase == PHASE_LEGACY_OPEN:
            actions = ["no D2 action; a new gated RM preparation may be started"]
        else:
            actions = ["prepare RM switch only after D1 readiness is PASS"]
        lease_healthy = snapshot.freeze_status == "active" and bool(snapshot.lease_id)
        return {
            "status": "PASS",
            "tool": TOOL_NAME,
            "tool_version": TOOL_VERSION,
            "authority": snapshot.authority.value,
            "configured_authority": configured.value if configured else None,
            "cutover_state": snapshot.state.value,
            "phase": phase,
            "freeze_active": snapshot.freeze_status == "active",
            "freeze_status": snapshot.freeze_status,
            "lease_healthy": lease_healthy,
            "migration_complete": record is not None and record.readiness.get("hard_gates", {}).get("migration_complete") == "PASS",
            "reconciliation": record.readiness.get("hard_gates", {}).get("reconciliation_pass") if record else None,
            "verification": record.readiness.get("hard_gates", {}).get("verification_pass") if record else None,
            "vector_readiness": record.readiness.get("hard_gates", {}).get("vector_readiness_pass") if record else None,
            "backup_readiness": record.readiness.get("hard_gates", {}).get("backup_evidence_present") if record else None,
            "acceptance_status": acceptance_status,
            "legacy_acceptance_status": legacy_acceptance_status,
            "rollback_class_currently_available": "CLASS_A" if phase == PHASE_RM_FROZEN_ACCEPTANCE else "NONE",
            "LOSSLESS_ROLLBACK_WINDOW_OPEN": "YES" if phase == PHASE_RM_FROZEN_ACCEPTANCE else "NO",
            "legacy_fallback_allowed": phase in {PHASE_NONE, PHASE_LEGACY_OPEN},
            "transition_identity": record.transition_identity if record else None,
            "migration_identity": _identity_payload(record.migration_identity) if record else None,
            "freeze_lease_id": snapshot.lease_id,
            "next_legal_operator_actions": actions,
            "production_access_occurred": False,
            "evidence": {
                "readiness_evidence_id": record.readiness.get("evidence_identity") if record else None,
                "backup_evidence_id": record.readiness.get("backup_evidence_id") if record else None,
                "acceptance_evidence_id": record.acceptance.get("evidence_identity") if record else None,
            },
        }


def _json_file(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise CutoverTransitionError("evidence_file_invalid") from exc
    if not isinstance(value, dict):
        raise CutoverTransitionError("evidence_file_invalid")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=TOOL_NAME)
    sub = parser.add_subparsers(dest="command", required=True)

    def common(command: argparse.ArgumentParser) -> None:
        command.add_argument("--state-db", required=True, type=Path)
        command.add_argument("--configured-authority", choices=("legacy", "rm"), required=True)

    status = sub.add_parser("status")
    common(status)
    status.add_argument("--rm-available", choices=("true", "false"), default="true")

    prepare = sub.add_parser("prepare-rm")
    common(prepare)
    prepare.add_argument("--lease-id")
    prepare.add_argument("--lease-token")
    prepare.add_argument("--lease-capability-file", type=Path)
    prepare.add_argument("--evidence", required=True, type=Path)

    switch = sub.add_parser("switch-to-rm")
    common(switch)
    switch.add_argument("--lease-id")
    switch.add_argument("--lease-token")
    switch.add_argument("--lease-capability-file", type=Path)
    switch.add_argument("--restart-validated", action="store_true")
    switch.add_argument("--rm-available", choices=("true", "false"), default="true")

    accept = sub.add_parser("accept-rm")
    common(accept)
    accept.add_argument("--lease-id")
    accept.add_argument("--lease-token")
    accept.add_argument("--lease-capability-file", type=Path)
    accept.add_argument("--checks", required=True, type=Path)

    release = sub.add_parser("release-freeze-to-rm")
    common(release)
    release.add_argument("--lease-id")
    release.add_argument("--lease-token")
    release.add_argument("--lease-capability-file", type=Path)

    rollback = sub.add_parser("class-a-rollback")
    common(rollback)
    rollback.add_argument("--lease-id")
    rollback.add_argument("--lease-token")
    rollback.add_argument("--lease-capability-file", type=Path)
    rollback.add_argument("--reason", required=True)
    rollback.add_argument("--mode", choices=("prepare", "finalize"), required=True)
    rollback.add_argument("--restart-validated", action="store_true")
    rollback.add_argument("--rm-available", choices=("true", "false"), default="true")

    accept_legacy = sub.add_parser("accept-legacy")
    common(accept_legacy)
    accept_legacy.add_argument("--lease-id")
    accept_legacy.add_argument("--lease-token")
    accept_legacy.add_argument("--lease-capability-file", type=Path)
    accept_legacy.add_argument("--checks", required=True, type=Path)

    release_legacy = sub.add_parser("release-freeze-to-legacy")
    common(release_legacy)
    release_legacy.add_argument("--lease-id")
    release_legacy.add_argument("--lease-token")
    release_legacy.add_argument("--lease-capability-file", type=Path)
    return parser


def _lease_from_args(controller: CutoverTransitionController, args: argparse.Namespace) -> FreezeLease:
    if args.lease_capability_file is not None:
        if args.lease_id is not None or args.lease_token is not None:
            raise CutoverTransitionError("lease_argument_conflict")
        return controller.load_capability(args.lease_capability_file)
    if args.lease_id is None or args.lease_token is None:
        raise CutoverTransitionError("capability_file_required")
    return controller.load_lease(args.lease_id, args.lease_token)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        controller = CutoverTransitionController(args.state_db, capability_file=args.lease_capability_file if args.command != "status" else None)
        configured = args.configured_authority
        if args.command == "status":
            result = controller.status(configured_authority=configured)
        else:
            lease = _lease_from_args(controller, args)
            if args.command == "prepare-rm":
                result = controller.prepare_rm_switch(lease, evidence=_json_file(args.evidence), configured_authority=configured)
            elif args.command == "switch-to-rm":
                result = controller.switch_to_rm(
                    lease,
                    configured_authority=configured,
                    rm_available=args.rm_available == "true",
                    restart_validated=args.restart_validated,
                )
            elif args.command == "accept-rm":
                result = controller.accept_rm(lease, checks=_json_file(args.checks), configured_authority=configured)
            elif args.command == "release-freeze-to-rm":
                result = controller.release_to_rm(lease, configured_authority=configured)
            elif args.command == "class-a-rollback":
                if args.mode == "prepare":
                    result = controller.begin_class_a_rollback(lease, reason=args.reason, configured_authority=configured)
                else:
                    result = controller.finalize_class_a_rollback(
                        lease,
                        configured_authority=configured,
                        restart_validated=args.restart_validated,
                        rm_available=args.rm_available == "true",
                    )
            elif args.command == "accept-legacy":
                result = controller.accept_legacy(lease, checks=_json_file(args.checks), configured_authority=configured)
            else:
                result = controller.release_to_legacy(lease, configured_authority=configured)
        print(json.dumps(result, ensure_ascii=True, sort_keys=True, indent=2))
        return 0 if result.get("status") == "PASS" else 2
    except CutoverTransitionError as exc:
        payload = {"status": "FAIL", "error": exc.code}
        if exc.details:
            payload["details"] = exc.details
        print(json.dumps(payload, ensure_ascii=True, sort_keys=True))
        return 2


if __name__ == "__main__":
    sys.exit(main())


__all__ = [
    "CutoverTransitionController",
    "CutoverTransitionError",
    "LEGACY_ACCEPTANCE_NAMES",
    "PHASE_LEGACY_FROZEN_ACCEPTANCE",
    "PHASE_LEGACY_OPEN",
    "PHASE_RM_FROZEN_ACCEPTANCE",
    "PHASE_RM_OPEN",
    "PHASE_RM_PREPARED",
    "PHASE_ROLLBACK_PREPARED",
    "RM_SOURCE_COMMIT",
    "build_parser",
    "main",
]

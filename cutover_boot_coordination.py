"""Read-only bridge between D2 coordination and the server boot path.

The public server must not import the operator transition controller.  This
module exposes only the narrow, redacted RM_PREPARED proof that the runtime
needs to recognize a controlled restart.  It performs no writes and never
reads or returns lease capability material.
"""

from __future__ import annotations

from collections.abc import Mapping
import json
from pathlib import Path
import sqlite3
from typing import Any

from asset_authority import AssetAuthority
from asset_cutover_state import (
    CutoverSnapshot,
    CutoverState,
    MigrationIdentity,
    RmPreparedBootCoordination,
)


D2_TRANSITION_SCHEMA_VERSION = 1
RM_PREPARED_PHASE = "RM_PREPARED"


def _text(value: Any) -> str | None:
    if isinstance(value, str):
        return value
    enum_value = getattr(value, "value", None)
    return enum_value if isinstance(enum_value, str) else None


def _identity_from_record(value: Any) -> MigrationIdentity | None:
    if not isinstance(value, Mapping):
        return None
    migration_key = value.get("migration_key")
    migration_version = value.get("migration_version")
    source_identity = value.get("source_identity")
    source_generation = value.get("source_generation")
    target_identity = value.get("target_identity")
    if (
        not isinstance(migration_key, str)
        or not migration_key
        or type(migration_version) is not int
        or migration_version < 1
        or not isinstance(source_identity, str)
        or not source_identity
        or type(source_generation) is not int
        or source_generation < 0
        or not isinstance(target_identity, str)
        or not target_identity
    ):
        return None
    return MigrationIdentity(
        migration_key=migration_key,
        migration_version=migration_version,
        source_identity=source_identity,
        source_generation=source_generation,
        target_identity=target_identity,
    )


def is_rm_prepared_coordination_record_active(
    record: Mapping[str, Any] | None,
    snapshot: CutoverSnapshot,
) -> bool:
    """Return whether one record is exactly current for RM_PREPARED boot."""

    if not isinstance(record, Mapping):
        return False
    if (
        snapshot.state is not CutoverState.FROZEN_READY_FOR_RM_SWITCH
        or snapshot.authority is not AssetAuthority.LEGACY
        or snapshot.freeze_status != "active"
        or not snapshot.lease_id
        or snapshot.migration_identity is None
    ):
        return False
    if (
        record.get("phase") != RM_PREPARED_PHASE
        or _text(record.get("expected_authority")) != AssetAuthority.RM.value
        or _text(record.get("authority_before")) != AssetAuthority.LEGACY.value
        or _text(record.get("authority_after")) != AssetAuthority.RM.value
        or _text(record.get("state_before")) != CutoverState.FROZEN_READY_FOR_RM_SWITCH.value
        or _text(record.get("state_after")) != CutoverState.FROZEN_RM_ACCEPTANCE.value
        or record.get("lease_id") != snapshot.lease_id
    ):
        return False
    return _identity_from_record(record.get("migration_identity")) == snapshot.migration_identity


def _proof_from_record(record: Mapping[str, Any], snapshot: CutoverSnapshot) -> RmPreparedBootCoordination | None:
    if not is_rm_prepared_coordination_record_active(record, snapshot):
        return None
    try:
        return RmPreparedBootCoordination(
            phase=RM_PREPARED_PHASE,
            expected_authority=AssetAuthority.RM,
            authority_before=AssetAuthority.LEGACY,
            authority_after=AssetAuthority.RM,
            state_before=CutoverState.FROZEN_READY_FOR_RM_SWITCH,
            state_after=CutoverState.FROZEN_RM_ACCEPTANCE,
            lease_id=str(record["lease_id"]),
            migration_identity=snapshot.migration_identity,
        )
    except (KeyError, TypeError, ValueError):
        return None


def read_rm_prepared_boot_coordination(
    state_db: str | Path,
    snapshot: CutoverSnapshot,
) -> RmPreparedBootCoordination | None:
    """Read a validated RM_PREPARED proof without mutating the state DB."""

    if not isinstance(snapshot, CutoverSnapshot):
        return None
    try:
        path = Path(state_db).expanduser().resolve(strict=False)
        if not path.is_file():
            return None
        connection = sqlite3.connect(
            f"file:{path.as_posix()}?mode=ro",
            uri=True,
            timeout=5,
        )
        connection.row_factory = sqlite3.Row
    except (OSError, sqlite3.Error, TypeError, ValueError):
        return None
    try:
        row = connection.execute(
            "SELECT schema_version, payload_json FROM d2_transition_record WHERE singleton = 1"
        ).fetchone()
        if row is None or int(row["schema_version"]) != D2_TRANSITION_SCHEMA_VERSION:
            return None
        payload = json.loads(row["payload_json"])
        return _proof_from_record(payload, snapshot) if isinstance(payload, Mapping) else None
    except (OSError, sqlite3.Error, TypeError, ValueError, json.JSONDecodeError):
        return None
    finally:
        connection.close()


__all__ = [
    "D2_TRANSITION_SCHEMA_VERSION",
    "RM_PREPARED_PHASE",
    "is_rm_prepared_coordination_record_active",
    "read_rm_prepared_boot_coordination",
]

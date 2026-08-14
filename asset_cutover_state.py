"""Persistent state machine and freeze lease for RM asset cutover.

This module is a control-plane foundation only.  It is deliberately not
imported by ``server.py`` in Implementation A, so creating this module or
setting ``OMBRE_ASSET_AUTHORITY`` cannot activate RM production routing.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
import hashlib
import re
import secrets
import sqlite3
from pathlib import Path
from typing import Callable
import uuid

from asset_authority import AssetAuthority
from maintenance_write_gate import (
    DEFAULT_WRITE_COORDINATOR,
    guarded_mutation,
)


CUTOVER_SCHEMA_VERSION = 1
DEFAULT_BUSY_TIMEOUT_MS = 5_000
_TOKEN_HASH = re.compile(r"[0-9a-f]{64}")
_LEASE_ID = re.compile(r"[0-9a-f]{32}")


class CutoverState(str, Enum):
    """Explicit authority/freeze states accepted by the design contract."""

    LEGACY_UNAVAILABLE_RM = "legacy_unavailable_rm"
    LEGACY_AUTHORITY_RM_READY = "legacy_authority_rm_ready"
    FROZEN_LEGACY_MIGRATION = "frozen_legacy_migration"
    FROZEN_READY_FOR_RM_SWITCH = "frozen_ready_for_rm_switch"
    FROZEN_RM_ACCEPTANCE = "frozen_rm_acceptance"
    RM_AUTHORITY_OPEN = "rm_authority_open"
    FROZEN_RM_ROLLBACK = "frozen_rm_rollback"
    FROZEN_LEGACY_ACCEPTANCE = "frozen_legacy_acceptance"

    @property
    def authority(self) -> AssetAuthority:
        if self in {
            CutoverState.FROZEN_RM_ACCEPTANCE,
            CutoverState.RM_AUTHORITY_OPEN,
            CutoverState.FROZEN_RM_ROLLBACK,
        }:
            return AssetAuthority.RM
        return AssetAuthority.LEGACY

    @property
    def frozen(self) -> bool:
        return self in FROZEN_STATES


FROZEN_STATES = frozenset(
    {
        CutoverState.FROZEN_LEGACY_MIGRATION,
        CutoverState.FROZEN_READY_FOR_RM_SWITCH,
        CutoverState.FROZEN_RM_ACCEPTANCE,
        CutoverState.FROZEN_RM_ROLLBACK,
        CutoverState.FROZEN_LEGACY_ACCEPTANCE,
    }
)

OPEN_STATES = frozenset(
    {
        CutoverState.LEGACY_UNAVAILABLE_RM,
        CutoverState.LEGACY_AUTHORITY_RM_READY,
        CutoverState.RM_AUTHORITY_OPEN,
    }
)

VALID_TRANSITIONS = {
    CutoverState.LEGACY_UNAVAILABLE_RM: {
        CutoverState.LEGACY_AUTHORITY_RM_READY,
    },
    CutoverState.LEGACY_AUTHORITY_RM_READY: {
        CutoverState.LEGACY_UNAVAILABLE_RM,
        CutoverState.FROZEN_LEGACY_MIGRATION,
    },
    CutoverState.FROZEN_LEGACY_MIGRATION: {
        CutoverState.FROZEN_LEGACY_MIGRATION,
        CutoverState.FROZEN_READY_FOR_RM_SWITCH,
        CutoverState.LEGACY_UNAVAILABLE_RM,
        CutoverState.LEGACY_AUTHORITY_RM_READY,
    },
    CutoverState.FROZEN_READY_FOR_RM_SWITCH: {
        CutoverState.FROZEN_READY_FOR_RM_SWITCH,
        CutoverState.FROZEN_RM_ACCEPTANCE,
        CutoverState.LEGACY_UNAVAILABLE_RM,
        CutoverState.LEGACY_AUTHORITY_RM_READY,
    },
    CutoverState.FROZEN_RM_ACCEPTANCE: {
        CutoverState.FROZEN_RM_ACCEPTANCE,
        CutoverState.RM_AUTHORITY_OPEN,
        CutoverState.LEGACY_AUTHORITY_RM_READY,
    },
    CutoverState.RM_AUTHORITY_OPEN: {
        CutoverState.FROZEN_RM_ROLLBACK,
    },
    CutoverState.FROZEN_RM_ROLLBACK: {
        CutoverState.FROZEN_RM_ROLLBACK,
        CutoverState.FROZEN_LEGACY_ACCEPTANCE,
    },
    CutoverState.FROZEN_LEGACY_ACCEPTANCE: {
        CutoverState.FROZEN_LEGACY_ACCEPTANCE,
        CutoverState.LEGACY_AUTHORITY_RM_READY,
    },
}


class CutoverStateError(RuntimeError):
    """Stable, non-secret control-plane failure."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class MigrationIdentity:
    """Identity binding for a migration or reverse-reconciliation run."""

    migration_key: str
    migration_version: int
    source_identity: str
    source_generation: int
    target_identity: str


@dataclass(frozen=True)
class FreezeLease:
    """In-memory lease handle; only the hash of ``token`` is persisted."""

    lease_id: str
    token: str
    generation: int
    acquired_at: str
    expires_at: str

    def __repr__(self) -> str:
        return "FreezeLease(lease_id={!r}, generation={!r}, active={!r})".format(
            self.lease_id,
            self.generation,
            True,
        )


@dataclass(frozen=True)
class CutoverSnapshot:
    """Redacted read-only state; never exposes lease capabilities."""

    schema_version: int
    revision: int
    state: CutoverState
    authority: AssetAuthority
    rm_available: bool
    freeze_status: str
    lease_id: str | None
    lease_expires_at: str | None
    transition_id: str
    transitioned_at: str
    migration_identity: MigrationIdentity | None


@dataclass(frozen=True)
class BootValidationResult:
    """Result for later server boot wiring."""

    state: CutoverState | None
    authority: AssetAuthority
    writes_allowed: bool
    frozen: bool
    requires_recovery: bool
    rm_ready_pending: bool = False


class CutoverStateStore:
    """Durable SQLite source of truth for authority and freeze state."""

    def __init__(
        self,
        db_path: str | Path,
        *,
        clock: Callable[[], datetime] | None = None,
        busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS,
        write_coordinator=None,
    ) -> None:
        if isinstance(db_path, bool):
            raise CutoverStateError("state_db_path_invalid")
        try:
            candidate = Path(db_path).expanduser()
        except (TypeError, ValueError, OSError) as exc:
            raise CutoverStateError("state_db_path_invalid") from exc
        if not candidate.is_absolute():
            raise CutoverStateError("state_db_path_not_absolute")
        if (
            isinstance(busy_timeout_ms, bool)
            or not isinstance(busy_timeout_ms, int)
            or not 1 <= busy_timeout_ms <= 60_000
        ):
            raise CutoverStateError("state_busy_timeout_invalid")
        self.db_path = candidate.resolve(strict=False)
        self.write_coordinator = write_coordinator or DEFAULT_WRITE_COORDINATOR
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._busy_timeout_ms = busy_timeout_ms
        try:
            self._preexisting_nonempty_db = (
                self.db_path.exists() and self.db_path.stat().st_size > 0
            )
        except OSError as exc:
            raise CutoverStateError("state_db_unavailable") from exc
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        try:
            connection = sqlite3.connect(
                self.db_path,
                timeout=self._busy_timeout_ms / 1000,
            )
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute(
                f"PRAGMA busy_timeout = {self._busy_timeout_ms}"
            )
            return connection
        except (OSError, sqlite3.Error) as exc:
            raise CutoverStateError("state_db_unavailable") from exc

    def _initialize(self) -> None:
        try:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS cutover_schema (
                        singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                        schema_version INTEGER NOT NULL
                    )
                    """
                )
                schema = connection.execute(
                    "SELECT schema_version FROM cutover_schema WHERE singleton = 1"
                ).fetchone()
                if schema is None:
                    if self._preexisting_nonempty_db:
                        raise CutoverStateError("state_db_corrupt")
                    connection.execute(
                        "INSERT INTO cutover_schema (singleton, schema_version) VALUES (1, ?)",
                        (CUTOVER_SCHEMA_VERSION,),
                    )
                elif int(schema["schema_version"]) != CUTOVER_SCHEMA_VERSION:
                    raise CutoverStateError("state_schema_incompatible")
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS cutover_state (
                        singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                        revision INTEGER NOT NULL CHECK (revision >= 0),
                        state TEXT NOT NULL,
                        authority TEXT NOT NULL,
                        rm_available INTEGER NOT NULL CHECK (rm_available IN (0, 1)),
                        freeze_status TEXT NOT NULL,
                        transition_id TEXT NOT NULL,
                        transitioned_at TEXT NOT NULL,
                        migration_key TEXT,
                        migration_version INTEGER,
                        source_identity TEXT,
                        source_generation INTEGER,
                        target_identity TEXT
                    )
                    """
                )
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS cutover_freeze (
                        singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                        lease_id TEXT NOT NULL,
                        token_hash TEXT NOT NULL,
                        generation INTEGER NOT NULL CHECK (generation >= 0),
                        acquired_at TEXT NOT NULL,
                        renewed_at TEXT NOT NULL,
                        expires_at TEXT NOT NULL
                    )
                    """
                )
                existing = connection.execute(
                    "SELECT singleton FROM cutover_state WHERE singleton = 1"
                ).fetchone()
                if existing is None:
                    if self._preexisting_nonempty_db:
                        raise CutoverStateError("state_db_corrupt")
                    now = self._timestamp()
                    connection.execute(
                        """
                        INSERT INTO cutover_state (
                            singleton, revision, state, authority, rm_available,
                            freeze_status, transition_id, transitioned_at
                        ) VALUES (1, 0, ?, ?, 0, 'open', ?, ?)
                        """,
                        (
                            CutoverState.LEGACY_UNAVAILABLE_RM.value,
                            AssetAuthority.LEGACY.value,
                            uuid.uuid4().hex,
                            now,
                        ),
                    )
                self._assert_schema(connection)
                connection.commit()
        except CutoverStateError:
            raise
        except (OSError, sqlite3.Error, ValueError, TypeError) as exc:
            raise CutoverStateError("state_db_unavailable") from exc

    def _now(self) -> datetime:
        try:
            value = self._clock()
        except Exception as exc:
            raise CutoverStateError("state_clock_unavailable") from exc
        if not isinstance(value, datetime):
            raise CutoverStateError("state_clock_unavailable")
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    def _timestamp(self, value: datetime | None = None) -> str:
        return (value or self._now()).isoformat(timespec="microseconds")

    def _assert_schema(self, connection: sqlite3.Connection) -> None:
        try:
            quick_check = connection.execute("PRAGMA quick_check(1)").fetchone()
            schema = connection.execute(
                "SELECT schema_version FROM cutover_schema WHERE singleton = 1"
            ).fetchone()
            row = connection.execute(
                "SELECT * FROM cutover_state WHERE singleton = 1"
            ).fetchone()
        except sqlite3.Error as exc:
            raise CutoverStateError("state_db_corrupt") from exc
        if (
            quick_check is None
            or quick_check[0] != "ok"
            or schema is None
            or int(schema["schema_version"]) != CUTOVER_SCHEMA_VERSION
            or row is None
        ):
            raise CutoverStateError("state_db_corrupt")
        try:
            state = CutoverState(row["state"])
            authority = AssetAuthority(row["authority"])
        except ValueError as exc:
            raise CutoverStateError("state_value_invalid") from exc
        if authority is not state.authority:
            raise CutoverStateError("state_authority_ambiguous")
        if int(row["rm_available"]) not in {0, 1}:
            raise CutoverStateError("state_value_invalid")
        freeze_status = row["freeze_status"]
        if freeze_status not in {"open", "frozen"}:
            raise CutoverStateError("state_value_invalid")
        if (state in FROZEN_STATES) != (freeze_status == "frozen"):
            raise CutoverStateError("state_freeze_ambiguous")
        if int(row["revision"]) < 0:
            raise CutoverStateError("state_value_invalid")
        self._read_identity(row)
        freeze = connection.execute(
            "SELECT * FROM cutover_freeze WHERE singleton = 1"
        ).fetchone()
        if freeze is not None:
            if state not in FROZEN_STATES:
                raise CutoverStateError("state_freeze_ambiguous")
            if _LEASE_ID.fullmatch(str(freeze["lease_id"])) is None:
                raise CutoverStateError("freeze_lease_invalid")
            if _TOKEN_HASH.fullmatch(str(freeze["token_hash"])) is None:
                raise CutoverStateError("freeze_lease_invalid")
            if int(freeze["generation"]) < 0:
                raise CutoverStateError("freeze_lease_invalid")

    def _read_identity(self, row: sqlite3.Row) -> MigrationIdentity | None:
        values = [
            row["migration_key"],
            row["migration_version"],
            row["source_identity"],
            row["source_generation"],
            row["target_identity"],
        ]
        if all(value is None for value in values):
            return None
        if any(value is None for value in values):
            raise CutoverStateError("migration_identity_incomplete")
        if (
            not isinstance(row["migration_key"], str)
            or not row["migration_key"]
            or type(row["migration_version"]) is not int
            or row["migration_version"] < 1
            or not isinstance(row["source_identity"], str)
            or not row["source_identity"]
            or type(row["source_generation"]) is not int
            or row["source_generation"] < 0
            or not isinstance(row["target_identity"], str)
            or not row["target_identity"]
        ):
            raise CutoverStateError("migration_identity_invalid")
        return MigrationIdentity(
            migration_key=row["migration_key"],
            migration_version=row["migration_version"],
            source_identity=row["source_identity"],
            source_generation=row["source_generation"],
            target_identity=row["target_identity"],
        )

    def get_snapshot(self) -> CutoverSnapshot:
        try:
            with self._connect() as connection:
                self._assert_schema(connection)
                return self._snapshot_from_connection(connection)
        except CutoverStateError:
            raise
        except (OSError, sqlite3.Error, ValueError, TypeError) as exc:
            raise CutoverStateError("state_db_unavailable") from exc

    inspect = get_snapshot

    def _snapshot_from_connection(
        self,
        connection: sqlite3.Connection,
    ) -> CutoverSnapshot:
        row = connection.execute(
            "SELECT * FROM cutover_state WHERE singleton = 1"
        ).fetchone()
        if row is None:
            raise CutoverStateError("state_db_corrupt")
        state = CutoverState(row["state"])
        freeze = connection.execute(
            "SELECT * FROM cutover_freeze WHERE singleton = 1"
        ).fetchone()
        lease_id = None
        expires_at = None
        if state in FROZEN_STATES:
            if freeze is None:
                freeze_status = "missing"
            else:
                lease_id = str(freeze["lease_id"])
                expires_at = str(freeze["expires_at"])
                freeze_status = (
                    "active"
                    if _parse_timestamp(expires_at) > self._now()
                    else "expired"
                )
        elif freeze is not None:
            lease_id = str(freeze["lease_id"])
            expires_at = str(freeze["expires_at"])
            freeze_status = "unexpected"
        else:
            freeze_status = "open"
        return CutoverSnapshot(
            schema_version=CUTOVER_SCHEMA_VERSION,
            revision=int(row["revision"]),
            state=state,
            authority=AssetAuthority(row["authority"]),
            rm_available=bool(row["rm_available"]),
            freeze_status=freeze_status,
            lease_id=lease_id,
            lease_expires_at=expires_at,
            transition_id=str(row["transition_id"]),
            transitioned_at=str(row["transitioned_at"]),
            migration_identity=self._read_identity(row),
        )

    @guarded_mutation("cutover_rm_availability")
    def set_rm_available(self, available: bool) -> CutoverSnapshot:
        if type(available) is not bool:
            raise CutoverStateError("rm_availability_invalid")
        connection = self._begin()
        try:
            self._assert_schema(connection)
            row = self._state_row(connection)
            self._update_state(
                connection,
                revision=int(row["revision"]) + 1,
                transition_id=uuid.uuid4().hex,
                transitioned_at=self._timestamp(),
                rm_available=1 if available else 0,
            )
            connection.commit()
            return self._snapshot_from_connection(connection)
        except CutoverStateError:
            connection.rollback()
            raise
        except (OSError, sqlite3.Error, ValueError, TypeError) as exc:
            connection.rollback()
            raise CutoverStateError("state_db_unavailable") from exc
        finally:
            connection.close()

    @guarded_mutation("cutover_freeze_acquire")
    def acquire_freeze(
        self,
        *,
        expected_state: CutoverState | str,
        frozen_state: CutoverState | str,
        ttl_seconds: int,
        migration_identity: MigrationIdentity | None = None,
    ) -> FreezeLease:
        expected = _coerce_state(expected_state)
        target = _coerce_state(frozen_state)
        if not isinstance(ttl_seconds, int) or isinstance(ttl_seconds, bool):
            raise CutoverStateError("freeze_ttl_invalid")
        if ttl_seconds <= 0:
            raise CutoverStateError("freeze_ttl_invalid")
        if target not in FROZEN_STATES:
            raise CutoverStateError("freeze_state_invalid")
        connection = self._begin()
        try:
            self._assert_schema(connection)
            row = self._state_row(connection)
            current = CutoverState(row["state"])
            if current is not expected:
                raise CutoverStateError("state_transition_invalid")
            self._validate_transition(current, target, row)
            existing = connection.execute(
                "SELECT * FROM cutover_freeze WHERE singleton = 1"
            ).fetchone()
            if existing is not None:
                if _parse_timestamp(existing["expires_at"]) > self._now():
                    raise CutoverStateError("freeze_lease_busy")
                raise CutoverStateError("freeze_lease_stale")
            self._validate_identity_for_transition(
                row,
                migration_identity,
                target,
                allow_replace=(current is CutoverState.RM_AUTHORITY_OPEN),
            )
            now = self._now()
            expires = now + timedelta(seconds=ttl_seconds)
            lease_id = uuid.uuid4().hex
            token = secrets.token_urlsafe(32)
            revision = int(row["revision"]) + 1
            connection.execute(
                """
                INSERT INTO cutover_freeze (
                    singleton, lease_id, token_hash, generation,
                    acquired_at, renewed_at, expires_at
                ) VALUES (1, ?, ?, ?, ?, ?, ?)
                """,
                (
                    lease_id,
                    _hash_token(token),
                    revision,
                    self._timestamp(now),
                    self._timestamp(now),
                    self._timestamp(expires),
                ),
            )
            self._update_state(
                connection,
                revision=revision,
                state=target.value,
                authority=target.authority.value,
                freeze_status="frozen",
                transition_id=uuid.uuid4().hex,
                transitioned_at=self._timestamp(now),
                migration_identity=migration_identity,
            )
            connection.commit()
            return FreezeLease(
                lease_id=lease_id,
                token=token,
                generation=revision,
                acquired_at=self._timestamp(now),
                expires_at=self._timestamp(expires),
            )
        except CutoverStateError:
            connection.rollback()
            raise
        except (OSError, sqlite3.Error, ValueError, TypeError) as exc:
            connection.rollback()
            raise CutoverStateError("state_db_unavailable") from exc
        finally:
            connection.close()

    @guarded_mutation("cutover_freeze_renew")
    def renew_freeze(self, lease: FreezeLease, *, ttl_seconds: int) -> FreezeLease:
        if not isinstance(lease, FreezeLease):
            raise CutoverStateError("freeze_lease_invalid")
        if not isinstance(ttl_seconds, int) or isinstance(ttl_seconds, bool) or ttl_seconds <= 0:
            raise CutoverStateError("freeze_ttl_invalid")
        connection = self._begin()
        try:
            self._assert_schema(connection)
            row = self._lease_row(connection)
            self._assert_lease(row, lease)
            now = self._now()
            if _parse_timestamp(row["expires_at"]) <= now:
                raise CutoverStateError("freeze_lease_expired")
            expires = now + timedelta(seconds=ttl_seconds)
            connection.execute(
                """
                UPDATE cutover_freeze
                SET renewed_at = ?, expires_at = ?
                WHERE singleton = 1 AND lease_id = ?
                """,
                (
                    self._timestamp(now),
                    self._timestamp(expires),
                    lease.lease_id,
                ),
            )
            connection.commit()
            return FreezeLease(
                lease_id=lease.lease_id,
                token=lease.token,
                generation=lease.generation,
                acquired_at=lease.acquired_at,
                expires_at=self._timestamp(expires),
            )
        except CutoverStateError:
            connection.rollback()
            raise
        except (OSError, sqlite3.Error, ValueError, TypeError) as exc:
            connection.rollback()
            raise CutoverStateError("state_db_unavailable") from exc
        finally:
            connection.close()

    @guarded_mutation("cutover_state_transition")
    def transition(
        self,
        target_state: CutoverState | str,
        *,
        lease: FreezeLease | None = None,
        migration_identity: MigrationIdentity | None = None,
    ) -> CutoverSnapshot:
        target = _coerce_state(target_state)
        connection = self._begin()
        try:
            self._assert_schema(connection)
            row = self._state_row(connection)
            current = CutoverState(row["state"])
            if current is target:
                if current not in FROZEN_STATES or lease is None:
                    raise CutoverStateError("state_transition_invalid")
                self._assert_active_lease(connection, lease)
                self._validate_identity_for_transition(row, migration_identity, target)
                if migration_identity is not None:
                    self._update_state(
                        connection,
                        revision=int(row["revision"]) + 1,
                        transition_id=uuid.uuid4().hex,
                        transitioned_at=self._timestamp(),
                        migration_identity=migration_identity,
                    )
                    connection.commit()
                else:
                    connection.rollback()
                return self._snapshot_from_connection(connection)
            self._validate_transition(current, target, row)
            if current in OPEN_STATES and target in OPEN_STATES:
                self._update_state(
                    connection,
                    revision=int(row["revision"]) + 1,
                    state=target.value,
                    authority=target.authority.value,
                    freeze_status="open",
                    transition_id=uuid.uuid4().hex,
                    transitioned_at=self._timestamp(),
                    clear_identity=True,
                    migration_identity=None,
                )
                connection.commit()
                return self._snapshot_from_connection(connection)
            if current not in FROZEN_STATES or lease is None:
                raise CutoverStateError("freeze_lease_required")
            self._assert_active_lease(connection, lease)
            self._validate_identity_for_transition(row, migration_identity, target)
            now = self._timestamp()
            if target not in FROZEN_STATES:
                connection.execute(
                    "DELETE FROM cutover_freeze WHERE singleton = 1"
                )
            clear_identity = target in {
                CutoverState.LEGACY_UNAVAILABLE_RM,
                CutoverState.LEGACY_AUTHORITY_RM_READY,
            }
            self._update_state(
                connection,
                revision=int(row["revision"]) + 1,
                state=target.value,
                authority=target.authority.value,
                freeze_status="frozen" if target in FROZEN_STATES else "open",
                transition_id=uuid.uuid4().hex,
                transitioned_at=now,
                migration_identity=(
                    None if clear_identity else migration_identity
                ),
                clear_identity=clear_identity,
            )
            connection.commit()
            return self._snapshot_from_connection(connection)
        except CutoverStateError:
            connection.rollback()
            raise
        except (OSError, sqlite3.Error, ValueError, TypeError) as exc:
            connection.rollback()
            raise CutoverStateError("state_db_unavailable") from exc
        finally:
            connection.close()

    @guarded_mutation("cutover_freeze_release")
    def release_freeze(
        self,
        lease: FreezeLease,
        *,
        target_state: CutoverState | str,
    ) -> CutoverSnapshot:
        """Release only through an explicit valid state transition."""

        target = _coerce_state(target_state)
        if target in FROZEN_STATES:
            raise CutoverStateError("freeze_release_requires_open_state")
        return self.transition(target, lease=lease)

    @guarded_mutation("cutover_freeze_recovery")
    def recover_expired_freeze(
        self,
        *,
        expected_lease_id: str,
        target_state: CutoverState | str,
    ) -> CutoverSnapshot:
        """Explicitly recover a stale pre-release freeze to legacy authority."""

        target = _coerce_state(target_state)
        if target not in {
            CutoverState.LEGACY_UNAVAILABLE_RM,
            CutoverState.LEGACY_AUTHORITY_RM_READY,
        }:
            raise CutoverStateError("recovery_target_invalid")
        if _LEASE_ID.fullmatch(expected_lease_id or "") is None:
            raise CutoverStateError("freeze_lease_invalid")
        connection = self._begin()
        try:
            self._assert_schema(connection)
            row = self._state_row(connection)
            current = CutoverState(row["state"])
            if current not in {
                CutoverState.FROZEN_LEGACY_MIGRATION,
                CutoverState.FROZEN_READY_FOR_RM_SWITCH,
                CutoverState.FROZEN_RM_ACCEPTANCE,
            }:
                raise CutoverStateError("recovery_state_invalid")
            lease_row = self._lease_row(connection)
            if lease_row is None or lease_row["lease_id"] != expected_lease_id:
                raise CutoverStateError("freeze_lease_invalid")
            if _parse_timestamp(lease_row["expires_at"]) > self._now():
                raise CutoverStateError("freeze_lease_still_active")
            if target is CutoverState.LEGACY_AUTHORITY_RM_READY and not bool(
                row["rm_available"]
            ):
                raise CutoverStateError("rm_readiness_mismatch")
            self._validate_transition(current, target, row)
            connection.execute(
                "DELETE FROM cutover_freeze WHERE singleton = 1"
            )
            self._update_state(
                connection,
                revision=int(row["revision"]) + 1,
                state=target.value,
                authority=target.authority.value,
                freeze_status="open",
                transition_id=uuid.uuid4().hex,
                transitioned_at=self._timestamp(),
                clear_identity=True,
                migration_identity=None,
            )
            connection.commit()
            return self._snapshot_from_connection(connection)
        except CutoverStateError:
            connection.rollback()
            raise
        except (OSError, sqlite3.Error, ValueError, TypeError) as exc:
            connection.rollback()
            raise CutoverStateError("state_db_unavailable") from exc
        finally:
            connection.close()

    def assert_public_mutation_allowed(self) -> None:
        snapshot = self.get_snapshot()
        if snapshot.freeze_status == "open":
            if snapshot.state is CutoverState.RM_AUTHORITY_OPEN and not snapshot.rm_available:
                raise CutoverStateError("rm_authority_unavailable")
            return
        if snapshot.freeze_status in {"active", "expired", "missing", "unexpected"}:
            raise CutoverStateError("asset_mutation_frozen")
        raise CutoverStateError("state_freeze_ambiguous")

    def issue_privileged_capability(
        self,
        lease: FreezeLease,
        *,
        purpose: str,
    ) -> "MutationCapability":
        if not isinstance(purpose, str) or not purpose or len(purpose) > 64:
            raise CutoverStateError("capability_purpose_invalid")
        self._assert_active_lease_handle(lease)
        return MutationCapability(
            lease_id=lease.lease_id,
            token=lease.token,
            generation=lease.generation,
            purpose=purpose,
        )

    def assert_privileged_capability(
        self,
        capability: "MutationCapability",
        *,
        purpose: str,
    ) -> None:
        if not isinstance(capability, MutationCapability):
            raise CutoverStateError("capability_invalid")
        if capability.purpose != purpose:
            raise CutoverStateError("capability_invalid")
        self._assert_active_lease_handle(
            FreezeLease(
                lease_id=capability.lease_id,
                token=capability.token,
                generation=capability.generation,
                acquired_at="",
                expires_at="",
            )
        )

    def _assert_active_lease_handle(self, lease: FreezeLease) -> None:
        if not isinstance(lease, FreezeLease):
            raise CutoverStateError("freeze_lease_invalid")
        try:
            with self._connect() as connection:
                self._assert_schema(connection)
                self._assert_active_lease(connection, lease)
        except CutoverStateError:
            raise
        except (OSError, sqlite3.Error, ValueError, TypeError) as exc:
            raise CutoverStateError("state_db_unavailable") from exc

    def _begin(self) -> sqlite3.Connection:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            return connection
        except (OSError, sqlite3.Error) as exc:
            connection.close()
            raise CutoverStateError("state_db_unavailable") from exc

    @staticmethod
    def _state_row(connection: sqlite3.Connection) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM cutover_state WHERE singleton = 1"
        ).fetchone()
        if row is None:
            raise CutoverStateError("state_db_corrupt")
        return row

    @staticmethod
    def _lease_row(connection: sqlite3.Connection) -> sqlite3.Row | None:
        return connection.execute(
            "SELECT * FROM cutover_freeze WHERE singleton = 1"
        ).fetchone()

    def _assert_active_lease(
        self,
        connection: sqlite3.Connection,
        lease: FreezeLease,
    ) -> None:
        row = self._lease_row(connection)
        self._assert_lease(row, lease)
        if _parse_timestamp(row["expires_at"]) <= self._now():
            raise CutoverStateError("freeze_lease_expired")

    @staticmethod
    def _assert_lease(
        row: sqlite3.Row | None,
        lease: FreezeLease,
    ) -> None:
        if (
            row is None
            or not isinstance(lease, FreezeLease)
            or row["lease_id"] != lease.lease_id
            or not secrets.compare_digest(
                str(row["token_hash"]),
                _hash_token(lease.token),
            )
            or int(row["generation"]) != lease.generation
        ):
            raise CutoverStateError("freeze_lease_invalid")

    def _validate_identity_for_transition(
        self,
        row: sqlite3.Row,
        identity: MigrationIdentity | None,
        target: CutoverState,
        *,
        allow_replace: bool = False,
    ) -> None:
        existing = self._read_identity(row)
        if target in {
            CutoverState.FROZEN_LEGACY_MIGRATION,
            CutoverState.FROZEN_RM_ROLLBACK,
        } and identity is None and existing is None:
            raise CutoverStateError("migration_identity_required")
        if identity is not None:
            _validate_identity(identity)
            if existing is not None and existing != identity and not allow_replace:
                raise CutoverStateError("migration_identity_mismatch")

    def _validate_transition(
        self,
        current: CutoverState,
        target: CutoverState,
        row: sqlite3.Row,
    ) -> None:
        if target not in VALID_TRANSITIONS.get(current, set()):
            raise CutoverStateError("state_transition_invalid")
        rm_available = bool(row["rm_available"])
        if target in {
            CutoverState.LEGACY_AUTHORITY_RM_READY,
            CutoverState.FROZEN_LEGACY_MIGRATION,
            CutoverState.FROZEN_READY_FOR_RM_SWITCH,
            CutoverState.FROZEN_RM_ACCEPTANCE,
            CutoverState.RM_AUTHORITY_OPEN,
            CutoverState.FROZEN_RM_ROLLBACK,
            CutoverState.FROZEN_LEGACY_ACCEPTANCE,
        } and not rm_available:
            raise CutoverStateError("rm_readiness_mismatch")
        if target is CutoverState.LEGACY_UNAVAILABLE_RM and rm_available:
            raise CutoverStateError("rm_readiness_mismatch")

    def _update_state(
        self,
        connection: sqlite3.Connection,
        *,
        revision: int | None = None,
        state: str | None = None,
        authority: str | None = None,
        rm_available: int | None = None,
        freeze_status: str | None = None,
        transition_id: str | None = None,
        transitioned_at: str | None = None,
        migration_identity: MigrationIdentity | None = None,
        clear_identity: bool = False,
    ) -> None:
        updates = []
        values: list[object] = []
        for column, value in (
            ("revision", revision),
            ("state", state),
            ("authority", authority),
            ("rm_available", rm_available),
            ("freeze_status", freeze_status),
            ("transition_id", transition_id),
            ("transitioned_at", transitioned_at),
        ):
            if value is not None:
                updates.append(f"{column} = ?")
                values.append(value)
        if clear_identity:
            updates.extend(
                [
                    "migration_key = NULL",
                    "migration_version = NULL",
                    "source_identity = NULL",
                    "source_generation = NULL",
                    "target_identity = NULL",
                ]
            )
        elif migration_identity is not None:
            _validate_identity(migration_identity)
            updates.extend(
                [
                    "migration_key = ?",
                    "migration_version = ?",
                    "source_identity = ?",
                    "source_generation = ?",
                    "target_identity = ?",
                ]
            )
            values.extend(
                [
                    migration_identity.migration_key,
                    migration_identity.migration_version,
                    migration_identity.source_identity,
                    migration_identity.source_generation,
                    migration_identity.target_identity,
                ]
            )
        if not updates:
            return
        values.append(1)
        connection.execute(
            f"UPDATE cutover_state SET {', '.join(updates)} WHERE singleton = ?",
            values,
        )


@dataclass(frozen=True)
class MutationCapability:
    """Ephemeral capability for future migration-owned writes."""

    lease_id: str
    token: str
    generation: int
    purpose: str

    def __repr__(self) -> str:
        return "MutationCapability(lease_id={!r}, purpose={!r})".format(
            self.lease_id,
            self.purpose,
        )


def validate_cutover_boot(
    authority: AssetAuthority | str,
    snapshot: CutoverSnapshot | None,
    *,
    rm_available: bool,
) -> BootValidationResult:
    """Validate a later server boot without mutating state or routing.

    With no initialized state, only the legacy selector is accepted.  This is
    the backward-compatible v1.4.0 behavior; Implementation A does not call
    this function from server startup.
    """

    configured = _coerce_authority(authority)
    if type(rm_available) is not bool:
        raise CutoverStateError("rm_availability_invalid")
    if snapshot is None:
        if configured is AssetAuthority.RM:
            raise CutoverStateError("rm_authority_without_state")
        return BootValidationResult(
            state=None,
            authority=AssetAuthority.LEGACY,
            writes_allowed=True,
            frozen=False,
            requires_recovery=False,
        )
    if snapshot.authority is not configured:
        raise CutoverStateError("state_authority_ambiguous")
    if snapshot.freeze_status in {"expired", "missing", "unexpected"}:
        raise CutoverStateError("state_freeze_ambiguous")
    if configured is AssetAuthority.RM and not rm_available:
        raise CutoverStateError("rm_authority_unavailable")
    if snapshot.state in {
        CutoverState.RM_AUTHORITY_OPEN,
        CutoverState.FROZEN_RM_ACCEPTANCE,
        CutoverState.FROZEN_RM_ROLLBACK,
    } and not rm_available:
        raise CutoverStateError("rm_authority_unavailable")
    if snapshot.state is CutoverState.LEGACY_AUTHORITY_RM_READY and (
        not snapshot.rm_available or not rm_available
    ):
        raise CutoverStateError("rm_readiness_mismatch")
    if snapshot.state is CutoverState.LEGACY_UNAVAILABLE_RM and snapshot.rm_available:
        return BootValidationResult(
            state=snapshot.state,
            authority=configured,
            writes_allowed=True,
            frozen=False,
            requires_recovery=False,
            rm_ready_pending=True,
        )
    if snapshot.state in FROZEN_STATES:
        return BootValidationResult(
            state=snapshot.state,
            authority=configured,
            writes_allowed=False,
            frozen=True,
            requires_recovery=not rm_available,
        )
    return BootValidationResult(
        state=snapshot.state,
        authority=configured,
        writes_allowed=True,
        frozen=False,
        requires_recovery=False,
    )


def _coerce_state(value: CutoverState | str) -> CutoverState:
    if isinstance(value, CutoverState):
        return value
    if not isinstance(value, str):
        raise CutoverStateError("state_invalid")
    try:
        return CutoverState(value)
    except ValueError as exc:
        raise CutoverStateError("state_invalid") from exc


def _coerce_authority(value: AssetAuthority | str) -> AssetAuthority:
    if isinstance(value, AssetAuthority):
        return value
    if not isinstance(value, str):
        raise CutoverStateError("authority_invalid")
    try:
        return AssetAuthority(value)
    except ValueError as exc:
        raise CutoverStateError("authority_invalid") from exc


def _validate_identity(identity: MigrationIdentity) -> None:
    if not isinstance(identity, MigrationIdentity):
        raise CutoverStateError("migration_identity_invalid")
    if (
        not isinstance(identity.migration_key, str)
        or not identity.migration_key
        or type(identity.migration_version) is not int
        or identity.migration_version < 1
        or not isinstance(identity.source_identity, str)
        or not identity.source_identity
        or type(identity.source_generation) is not int
        or identity.source_generation < 0
        or not isinstance(identity.target_identity, str)
        or not identity.target_identity
    ):
        raise CutoverStateError("migration_identity_invalid")


def _hash_token(token: str) -> str:
    if not isinstance(token, str) or not token:
        raise CutoverStateError("freeze_lease_invalid")
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _parse_timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError) as exc:
        raise CutoverStateError("state_timestamp_invalid") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


__all__ = [
    "BootValidationResult",
    "CUTOVER_SCHEMA_VERSION",
    "CutoverSnapshot",
    "CutoverState",
    "CutoverStateError",
    "CutoverStateStore",
    "FROZEN_STATES",
    "FreezeLease",
    "MigrationIdentity",
    "MutationCapability",
    "OPEN_STATES",
    "VALID_TRANSITIONS",
    "validate_cutover_boot",
]

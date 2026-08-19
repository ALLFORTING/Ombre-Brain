"""Isolated Raw Evidence registry and content-addressed storage foundation.

O5A deliberately has no application integration.  A caller must construct a
store with an explicit root before this module creates any filesystem state.
The store records source/evidence identity separately from content identity;
it never reads a caller-provided filesystem path.
"""

from __future__ import annotations

import hashlib
import os
import re
import secrets
import sqlite3
import stat
import tempfile
import threading
import uuid
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO, Iterable

from maintenance_write_gate import DEFAULT_WRITE_COORDINATOR, guarded_mutation


SCHEMA_VERSION = 1
REVISION_SCHEMA_VERSION = 1
RECORD_SCHEMA_VERSION = 1
HASH_ALGORITHM = "sha256-v1"

FIDELITY_LEVELS = frozenset(
    {
        "IMPORT_SNAPSHOT",
        "SOURCE_TEXT",
        "SOURCE_ITEM",
        "EXACT_SPAN",
        "ORIGINAL_BYTES",
    }
)
PRIVACY_CLASSES = frozenset({"ordinary", "sealed", "restricted_admin"})
LIFECYCLE_STATES = frozenset(
    {"captured", "available", "quarantined", "integrity_failed", "tombstoned"}
)
VERIFICATION_STATES = frozenset({"verified", "quarantined", "failed"})
IDENTITY_ORIGINS = frozenset({"upstream", "local", "unknown"})

_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")
_HASH_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_RELATIVE_BLOB_PATTERN = re.compile(
    r"^blobs/sha256/[0-9a-f]{2}/[0-9a-f]{64}$"
)
_MAX_READ_CHUNK = 1024 * 1024
_REPARSE_ATTRIBUTE = 0x400


class RawEvidenceError(RuntimeError):
    """Stable, content-free O5A failure."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class RawEvidenceLimits:
    """Internal bounded-write settings; not a public product guarantee."""

    max_evidence_bytes: int = 16 * 1024 * 1024
    max_temp_bytes: int = 16 * 1024 * 1024
    max_metadata_chars: int = 4096
    max_store_bytes: int = 512 * 1024 * 1024

    def validate(self) -> None:
        values = (
            self.max_evidence_bytes,
            self.max_temp_bytes,
            self.max_metadata_chars,
            self.max_store_bytes,
        )
        if any(isinstance(value, bool) or not isinstance(value, int) or value <= 0 for value in values):
            raise RawEvidenceError("limits_invalid")


class RawEvidenceStore:
    """A private registry plus CAS store rooted at an explicit directory."""

    def __init__(
        self,
        evidence_root: str | Path | None,
        *,
        enabled: bool = True,
        limits: RawEvidenceLimits | None = None,
        forbidden_roots: Iterable[str | Path] = (),
        write_coordinator=None,
    ) -> None:
        if not isinstance(enabled, bool):
            raise RawEvidenceError("invalid_input")
        self.enabled = enabled
        self.write_coordinator = write_coordinator or DEFAULT_WRITE_COORDINATOR
        self.limits = limits or RawEvidenceLimits()
        self.limits.validate()
        self._lock = threading.RLock()
        self.root: Path | None = None
        self.blobs_root: Path | None = None
        self.temp_root: Path | None = None
        self.quarantine_root: Path | None = None
        self.registry_path: Path | None = None

        # Disabled construction is intentionally inert, including for an
        # invalid or absent path.  O5A has no startup/configuration hook.
        if not enabled:
            return

        self.root = _validate_owned_root(evidence_root, forbidden_roots)
        self.blobs_root = self.root / "blobs" / "sha256"
        self.temp_root = self.root / ".tmp"
        self.quarantine_root = self.root / ".quarantine"
        self.registry_path = self.root / "registry.sqlite3"
        self._prepare_layout()
        self._init_schema()

    @classmethod
    def open(cls, evidence_root: str | Path | None, **kwargs: Any) -> "RawEvidenceStore":
        """Construct an explicitly requested store or disabled handle."""

        return cls(evidence_root, **kwargs)

    def __enter__(self) -> "RawEvidenceStore":
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        return None

    @property
    def is_disabled(self) -> bool:
        return not self.enabled

    @property
    def database_path(self) -> Path | None:
        return self.registry_path

    def close(self) -> None:
        """The store uses short-lived SQLite connections and needs no close."""

        return None

    @guarded_mutation("raw_evidence_create")
    def create_evidence(
        self,
        content: bytes | bytearray | memoryview | BinaryIO,
        *,
        source_system: str = "local",
        source_kind: str = "unknown",
        source_scope: str = "local",
        upstream_source_id: str | None = None,
        upstream_item_id: str | None = None,
        source_occurrence_key: str | None = None,
        identity_origin: str = "unknown",
        fidelity_level: str = "IMPORT_SNAPSHOT",
        media_type: str = "application/octet-stream",
        privacy_class: str = "ordinary",
        captured_at: str | None = None,
    ) -> dict[str, Any]:
        """Create one logical evidence object and one immutable revision."""

        self._require_enabled()
        metadata = self._validate_metadata(
            source_system=source_system,
            source_kind=source_kind,
            source_scope=source_scope,
            upstream_source_id=upstream_source_id,
            upstream_item_id=upstream_item_id,
            source_occurrence_key=source_occurrence_key,
            identity_origin=identity_origin,
            fidelity_level=fidelity_level,
            media_type=media_type,
            privacy_class=privacy_class,
            captured_at=captured_at,
        )
        evidence_id = uuid.uuid4().hex
        revision_id = uuid.uuid4().hex
        now = _now_iso()
        temp_path: Path | None = None

        with self._lock:
            try:
                temp_path, content_size, content_hash = self._stage_content(content)
                blob_path = self._cas_path(content_hash)
                if not blob_path.exists():
                    self._ensure_store_capacity(content_size)
                blob_relpath = self._publish_blob(
                    temp_path,
                    content_hash,
                    content_size,
                )
                temp_path = None

                with self._connect() as conn:
                    conn.execute("BEGIN IMMEDIATE")
                    conn.execute(
                        """
                        INSERT INTO evidence_objects (
                            evidence_id, source_system, source_kind, source_scope,
                            upstream_source_id, upstream_item_id,
                            source_occurrence_key, identity_origin, privacy_class,
                            lifecycle_state, captured_at, created_at, updated_at,
                            record_schema_version
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            evidence_id,
                            metadata["source_system"],
                            metadata["source_kind"],
                            metadata["source_scope"],
                            metadata["upstream_source_id"],
                            metadata["upstream_item_id"],
                            metadata["source_occurrence_key"],
                            metadata["identity_origin"],
                            metadata["privacy_class"],
                            "available",
                            metadata["captured_at"],
                            now,
                            now,
                            RECORD_SCHEMA_VERSION,
                        ),
                    )
                    conn.execute(
                        """
                        INSERT INTO evidence_revisions (
                            revision_id, evidence_id, fidelity_level, media_type,
                            hash_algorithm, content_hash, content_size_bytes,
                            blob_relpath, created_at, verification_state,
                            revision_schema_version
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            revision_id,
                            evidence_id,
                            metadata["fidelity_level"],
                            metadata["media_type"],
                            HASH_ALGORITHM,
                            content_hash,
                            content_size,
                            blob_relpath,
                            now,
                            "verified",
                            REVISION_SCHEMA_VERSION,
                        ),
                    )
                    conn.commit()
            except RawEvidenceError:
                raise
            except sqlite3.Error as exc:
                raise RawEvidenceError("storage_unavailable") from exc
            except OSError as exc:
                raise RawEvidenceError("storage_unavailable") from exc
            finally:
                if temp_path is not None:
                    self._remove_temp(temp_path)

        return self.get_evidence(evidence_id, allow_sealed=True)

    def create(self, content: bytes | bytearray | memoryview | BinaryIO, **kwargs: Any) -> dict[str, Any]:
        """Internal shorthand for :meth:`create_evidence`."""

        return self.create_evidence(content, **kwargs)

    def get_evidence(
        self,
        evidence_id: str,
        *,
        allow_sealed: bool = False,
    ) -> dict[str, Any]:
        self._require_enabled()
        evidence_id = _validate_id(evidence_id, "evidence_id")
        with self._lock:
            with self._connect() as conn:
                row = conn.execute(
                    """
                    SELECT
                        e.evidence_id, e.source_system, e.source_kind,
                        e.source_scope, e.upstream_source_id,
                        e.upstream_item_id, e.source_occurrence_key,
                        e.identity_origin, e.privacy_class, e.lifecycle_state,
                        e.captured_at, e.created_at, e.updated_at,
                        e.record_schema_version, r.revision_id,
                        r.fidelity_level, r.media_type, r.hash_algorithm,
                        r.content_hash, r.content_size_bytes, r.blob_relpath,
                        r.created_at AS revision_created_at,
                        r.verification_state, r.revision_schema_version
                    FROM evidence_objects AS e
                    JOIN evidence_revisions AS r
                      ON r.evidence_id = e.evidence_id
                    WHERE e.evidence_id = ?
                    ORDER BY r.created_at DESC, r.revision_id DESC
                    LIMIT 1
                    """,
                    (evidence_id,),
                ).fetchone()
        if row is None:
            raise RawEvidenceError("not_found")
        self._check_visibility(row["privacy_class"], allow_sealed)
        return dict(row)

    def get_revision(
        self,
        revision_id: str,
        *,
        allow_sealed: bool = False,
    ) -> dict[str, Any]:
        self._require_enabled()
        row = self._fetch_revision(revision_id)
        self._check_visibility(row["privacy_class"], allow_sealed)
        return dict(row)

    def get_content(self, revision_id: str, *, allow_sealed: bool = False) -> bytes:
        self._require_enabled()
        row = self._fetch_revision(revision_id)
        self._check_visibility(row["privacy_class"], allow_sealed)
        return self._read_verified(row, return_content=True)

    def verify_content(self, revision_id: str, *, allow_sealed: bool = False) -> bool:
        self._require_enabled()
        row = self._fetch_revision(revision_id)
        self._check_visibility(row["privacy_class"], allow_sealed)
        self._read_verified(row, return_content=False)
        return True

    def verify(self, revision_id: str, *, allow_sealed: bool = False) -> bool:
        """Internal shorthand for :meth:`verify_content`."""

        return self.verify_content(revision_id, allow_sealed=allow_sealed)

    @guarded_mutation("raw_evidence_metadata_update")
    def update_metadata(
        self,
        evidence_id: str,
        *,
        privacy_class: str | None = None,
        lifecycle_state: str | None = None,
    ) -> dict[str, Any]:
        """Update only O5A operational metadata; content never changes."""

        self._require_enabled()
        evidence_id = _validate_id(evidence_id, "evidence_id")
        if privacy_class is None and lifecycle_state is None:
            raise RawEvidenceError("invalid_input")
        if privacy_class is not None:
            privacy_class = _validate_choice(
                privacy_class, PRIVACY_CLASSES, "privacy_class"
            )
        if lifecycle_state is not None:
            lifecycle_state = _validate_choice(
                lifecycle_state, LIFECYCLE_STATES, "lifecycle_state"
            )
        assignments: list[str] = []
        values: list[Any] = []
        if privacy_class is not None:
            assignments.append("privacy_class = ?")
            values.append(privacy_class)
        if lifecycle_state is not None:
            assignments.append("lifecycle_state = ?")
            values.append(lifecycle_state)
        assignments.append("updated_at = ?")
        values.extend((_now_iso(), evidence_id))

        with self._lock:
            try:
                with self._connect() as conn:
                    conn.execute("BEGIN IMMEDIATE")
                    cursor = conn.execute(
                        f"UPDATE evidence_objects SET {', '.join(assignments)} "
                        "WHERE evidence_id = ?",
                        values,
                    )
                    if cursor.rowcount != 1:
                        conn.rollback()
                        raise RawEvidenceError("not_found")
                    conn.commit()
            except RawEvidenceError:
                raise
            except sqlite3.Error as exc:
                raise RawEvidenceError("storage_unavailable") from exc
        return self.get_evidence(evidence_id, allow_sealed=True)

    def update_state(self, evidence_id: str, lifecycle_state: str) -> dict[str, Any]:
        """Internal shorthand for the O5A metadata-only state update."""

        return self.update_metadata(evidence_id, lifecycle_state=lifecycle_state)

    def _require_enabled(self) -> None:
        if not self.enabled or self.root is None:
            raise RawEvidenceError("store_disabled")

    def _prepare_layout(self) -> None:
        assert self.root is not None
        assert self.blobs_root is not None
        assert self.temp_root is not None
        assert self.quarantine_root is not None
        try:
            for path in (
                self.root,
                self.blobs_root,
                self.temp_root,
                self.quarantine_root,
            ):
                path.mkdir(parents=True, exist_ok=True)
                _reject_reparse_components(path)
            _chmod_private(self.root, directory=True)
            _chmod_private(self.blobs_root, directory=True)
            _chmod_private(self.temp_root, directory=True)
            _chmod_private(self.quarantine_root, directory=True)
        except RawEvidenceError:
            raise
        except OSError as exc:
            raise RawEvidenceError("storage_unavailable") from exc

    def _connect(self) -> sqlite3.Connection:
        self._require_enabled()
        assert self.registry_path is not None
        try:
            _reject_reparse_components(self.registry_path)
            conn = sqlite3.connect(str(self.registry_path), timeout=30)
        except (OSError, sqlite3.Error) as exc:
            raise RawEvidenceError("storage_unavailable") from exc
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 30000")
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS store_schema (
                        singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                        schema_version INTEGER NOT NULL,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    )
                    """
                )
                schema_row = conn.execute(
                    "SELECT schema_version FROM store_schema WHERE singleton = 1"
                ).fetchone()
                if schema_row is None:
                    now = _now_iso()
                    conn.execute(
                        "INSERT INTO store_schema "
                        "(singleton, schema_version, created_at, updated_at) "
                        "VALUES (1, ?, ?, ?)",
                        (SCHEMA_VERSION, now, now),
                    )
                elif schema_row["schema_version"] != SCHEMA_VERSION:
                    raise RawEvidenceError("schema_unsupported")
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS evidence_objects (
                        evidence_id TEXT PRIMARY KEY,
                        source_system TEXT NOT NULL,
                        source_kind TEXT NOT NULL,
                        source_scope TEXT NOT NULL,
                        upstream_source_id TEXT,
                        upstream_item_id TEXT,
                        source_occurrence_key TEXT,
                        identity_origin TEXT NOT NULL
                            CHECK (identity_origin IN ('upstream', 'local', 'unknown')),
                        privacy_class TEXT NOT NULL
                            CHECK (privacy_class IN ('ordinary', 'sealed', 'restricted_admin')),
                        lifecycle_state TEXT NOT NULL
                            CHECK (lifecycle_state IN (
                                'captured', 'available', 'quarantined',
                                'integrity_failed', 'tombstoned'
                            )),
                        captured_at TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        record_schema_version INTEGER NOT NULL
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS evidence_revisions (
                        revision_id TEXT PRIMARY KEY,
                        evidence_id TEXT NOT NULL,
                        fidelity_level TEXT NOT NULL
                            CHECK (fidelity_level IN (
                                'IMPORT_SNAPSHOT', 'SOURCE_TEXT', 'SOURCE_ITEM',
                                'EXACT_SPAN', 'ORIGINAL_BYTES'
                            )),
                        media_type TEXT NOT NULL,
                        hash_algorithm TEXT NOT NULL,
                        content_hash TEXT NOT NULL,
                        content_size_bytes INTEGER NOT NULL
                            CHECK (content_size_bytes >= 0),
                        blob_relpath TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        verification_state TEXT NOT NULL
                            CHECK (verification_state IN ('verified', 'quarantined', 'failed')),
                        revision_schema_version INTEGER NOT NULL,
                        FOREIGN KEY (evidence_id) REFERENCES evidence_objects(evidence_id)
                            ON DELETE RESTRICT
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE TRIGGER IF NOT EXISTS evidence_objects_immutable_identity
                    BEFORE UPDATE OF evidence_id, source_system, source_kind,
                        source_scope, upstream_source_id, upstream_item_id,
                        source_occurrence_key, identity_origin, captured_at,
                        created_at, record_schema_version ON evidence_objects
                    BEGIN
                        SELECT RAISE(ABORT, 'immutable_evidence_identity');
                    END
                    """
                )
                conn.execute(
                    """
                    CREATE TRIGGER IF NOT EXISTS evidence_revisions_immutable_content
                    BEFORE UPDATE OF revision_id, evidence_id, fidelity_level,
                        media_type, hash_algorithm, content_hash,
                        content_size_bytes, blob_relpath, created_at,
                        revision_schema_version ON evidence_revisions
                    BEGIN
                        SELECT RAISE(ABORT, 'immutable_evidence_content');
                    END
                    """
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_evidence_revisions_evidence "
                    "ON evidence_revisions(evidence_id, created_at)"
                )
                conn.commit()
                assert self.registry_path is not None
                _chmod_private(self.registry_path)
            except RawEvidenceError:
                conn.rollback()
                raise
            except sqlite3.Error as exc:
                conn.rollback()
                raise RawEvidenceError("storage_unavailable") from exc

    def _stage_content(
        self,
        content: bytes | bytearray | memoryview | BinaryIO,
    ) -> tuple[Path, int, str]:
        assert self.temp_root is not None
        data: bytes | None = None
        if isinstance(content, bytes):
            data = content
        elif isinstance(content, (bytearray, memoryview)):
            if len(content) > min(self.limits.max_evidence_bytes, self.limits.max_temp_bytes):
                raise RawEvidenceError("limit_exceeded")
            data = bytes(content)
        elif not hasattr(content, "read"):
            raise RawEvidenceError("invalid_input")
        if data is not None and len(data) > min(
            self.limits.max_evidence_bytes,
            self.limits.max_temp_bytes,
        ):
            raise RawEvidenceError("limit_exceeded")

        temp_path: Path | None = None
        digest = hashlib.sha256()
        size = 0
        try:
            fd, raw_path = tempfile.mkstemp(
                prefix=".evidence-",
                suffix=".part",
                dir=str(self.temp_root),
            )
            temp_path = Path(raw_path)
            _assert_owned_path(temp_path, self.temp_root)
            with os.fdopen(fd, "wb") as handle:
                if data is not None:
                    chunks: Iterable[bytes] = (data,)
                else:
                    chunks = self._read_chunks(content)
                for chunk in chunks:
                    if not isinstance(chunk, (bytes, bytearray, memoryview)):
                        raise RawEvidenceError("content_invalid")
                    chunk_bytes = bytes(chunk)
                    size += len(chunk_bytes)
                    if size > min(
                        self.limits.max_evidence_bytes,
                        self.limits.max_temp_bytes,
                    ):
                        raise RawEvidenceError("limit_exceeded")
                    handle.write(chunk_bytes)
                    digest.update(chunk_bytes)
                handle.flush()
                os.fsync(handle.fileno())
            _chmod_private(temp_path)
            return temp_path, size, digest.hexdigest()
        except RawEvidenceError:
            if temp_path is not None:
                self._remove_temp(temp_path)
            raise
        except (OSError, ValueError) as exc:
            if temp_path is not None:
                self._remove_temp(temp_path)
            raise RawEvidenceError("content_write_failed") from exc

    def _read_chunks(self, content: Any) -> Iterable[bytes]:
        while True:
            try:
                chunk = content.read(_MAX_READ_CHUNK)
            except Exception as exc:
                raise RawEvidenceError("content_read_failed") from exc
            if chunk in (b"", ""):
                return
            yield chunk

    def _ensure_store_capacity(self, additional_size: int) -> None:
        assert self.blobs_root is not None
        total = 0
        try:
            for path in self.blobs_root.rglob("*"):
                if path.is_dir():
                    _reject_reparse_components(path)
                    continue
                _assert_owned_path(path, self.blobs_root)
                if not path.is_file():
                    raise RawEvidenceError("storage_unavailable")
                total += path.stat().st_size
                if total + additional_size > self.limits.max_store_bytes:
                    raise RawEvidenceError("limit_exceeded")
        except RawEvidenceError:
            raise
        except OSError as exc:
            raise RawEvidenceError("storage_unavailable") from exc

    def _publish_blob(self, temp_path: Path, content_hash: str, size: int) -> str:
        assert self.blobs_root is not None
        if _HASH_PATTERN.fullmatch(content_hash) is None:
            raise RawEvidenceError("integrity_failed")
        destination = self._cas_path(content_hash)
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            _assert_owned_path(destination.parent, self.blobs_root)
            if destination.exists():
                if not _verify_file(destination, content_hash, size):
                    raise RawEvidenceError("integrity_conflict")
                self._remove_temp(temp_path)
                return self._relative_blob_path(destination)
            try:
                os.link(temp_path, destination)
            except FileExistsError:
                if not _verify_file(destination, content_hash, size):
                    raise RawEvidenceError("integrity_conflict")
            except OSError as exc:
                raise RawEvidenceError("content_publish_failed") from exc
            self._remove_temp(temp_path)
            _chmod_private(destination)
            return self._relative_blob_path(destination)
        except RawEvidenceError:
            raise
        except OSError as exc:
            raise RawEvidenceError("content_publish_failed") from exc

    def _cas_path(self, content_hash: str) -> Path:
        assert self.blobs_root is not None
        if _HASH_PATTERN.fullmatch(content_hash) is None:
            raise RawEvidenceError("integrity_failed")
        destination = self.blobs_root / content_hash[:2] / content_hash
        _assert_owned_path(destination, self.blobs_root)
        return destination

    def _relative_blob_path(self, path: Path) -> str:
        assert self.root is not None
        try:
            relative = path.resolve(strict=False).relative_to(self.root.resolve(strict=False))
        except (ValueError, OSError) as exc:
            raise RawEvidenceError("invalid_stored_path") from exc
        result = PurePosixPath(*relative.parts).as_posix()
        if _RELATIVE_BLOB_PATTERN.fullmatch(result) is None:
            raise RawEvidenceError("invalid_stored_path")
        return result

    def _fetch_revision(self, revision_id: str) -> sqlite3.Row:
        revision_id = _validate_id(revision_id, "revision_id")
        with self._lock:
            with self._connect() as conn:
                row = conn.execute(
                    """
                    SELECT
                        r.revision_id, r.evidence_id, r.fidelity_level,
                        r.media_type, r.hash_algorithm, r.content_hash,
                        r.content_size_bytes, r.blob_relpath, r.created_at,
                        r.verification_state, r.revision_schema_version,
                        e.privacy_class, e.lifecycle_state
                    FROM evidence_revisions AS r
                    JOIN evidence_objects AS e ON e.evidence_id = r.evidence_id
                    WHERE r.revision_id = ?
                    """,
                    (revision_id,),
                ).fetchone()
        if row is None:
            raise RawEvidenceError("not_found")
        return row

    def _read_verified(self, row: sqlite3.Row, *, return_content: bool) -> bytes:
        assert self.root is not None
        if row["verification_state"] != "verified":
            raise RawEvidenceError("integrity_failed")
        if row["lifecycle_state"] != "available":
            raise RawEvidenceError("evidence_unavailable")
        if row["hash_algorithm"] != HASH_ALGORITHM:
            raise RawEvidenceError("integrity_failed")
        expected_hash = row["content_hash"]
        expected_size = row["content_size_bytes"]
        if (
            not isinstance(expected_hash, str)
            or _HASH_PATTERN.fullmatch(expected_hash) is None
            or isinstance(expected_size, bool)
            or not isinstance(expected_size, int)
            or expected_size < 0
            or expected_size > self.limits.max_evidence_bytes
        ):
            self._best_effort_mark_integrity_failed(row["evidence_id"], row["revision_id"])
            raise RawEvidenceError("integrity_failed")
        path = self._path_from_stored_reference(row["blob_relpath"], expected_hash)
        digest = hashlib.sha256()
        size = 0
        output = bytearray() if return_content else None
        try:
            if not path.is_file():
                raise OSError
            with path.open("rb") as handle:
                while True:
                    chunk = handle.read(_MAX_READ_CHUNK)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > self.limits.max_evidence_bytes:
                        raise RawEvidenceError("limit_exceeded")
                    digest.update(chunk)
                    if output is not None:
                        output.extend(chunk)
        except RawEvidenceError:
            self._best_effort_mark_integrity_failed(row["evidence_id"], row["revision_id"])
            raise
        except (OSError, ValueError):
            self._best_effort_mark_integrity_failed(row["evidence_id"], row["revision_id"])
            raise RawEvidenceError("integrity_failed")
        if size != expected_size or digest.hexdigest() != expected_hash:
            self._best_effort_mark_integrity_failed(row["evidence_id"], row["revision_id"])
            raise RawEvidenceError("integrity_failed")
        return bytes(output or b"")

    def _path_from_stored_reference(self, relative: Any, expected_hash: str) -> Path:
        assert self.root is not None
        if not isinstance(relative, str) or _RELATIVE_BLOB_PATTERN.fullmatch(relative) is None:
            self._best_effort_mark_integrity_failed(None, None)
            raise RawEvidenceError("invalid_stored_path")
        if not relative.endswith(expected_hash):
            self._best_effort_mark_integrity_failed(None, None)
            raise RawEvidenceError("integrity_failed")
        parts = PurePosixPath(relative).parts
        path = self.root.joinpath(*parts)
        try:
            _assert_owned_path(path, self.blobs_root)
        except RawEvidenceError:
            raise RawEvidenceError("invalid_stored_path")
        return path

    def _best_effort_mark_integrity_failed(
        self,
        evidence_id: str | None,
        revision_id: str | None,
    ) -> None:
        if evidence_id is None or revision_id is None:
            return
        try:
            self._mark_integrity_failed(evidence_id, revision_id)
        except Exception:
            return

    @guarded_mutation("raw_evidence_integrity_state")
    def _mark_integrity_failed(self, evidence_id: str, revision_id: str) -> None:
        with self._lock:
            try:
                with self._connect() as conn:
                    conn.execute("BEGIN IMMEDIATE")
                    conn.execute(
                        "UPDATE evidence_revisions SET verification_state = 'failed' "
                        "WHERE revision_id = ? AND evidence_id = ?",
                        (revision_id, evidence_id),
                    )
                    conn.execute(
                        "UPDATE evidence_objects SET lifecycle_state = 'integrity_failed', "
                        "updated_at = ? WHERE evidence_id = ?",
                        (_now_iso(), evidence_id),
                    )
                    conn.commit()
            except sqlite3.Error as exc:
                raise RawEvidenceError("storage_unavailable") from exc

    def _remove_temp(self, path: Path) -> None:
        try:
            _assert_owned_path(path, self.temp_root)
            path.unlink(missing_ok=True)
        except (OSError, RawEvidenceError):
            return

    @staticmethod
    def _check_visibility(privacy_class: str, allow_sealed: bool) -> None:
        if privacy_class != "ordinary" and not allow_sealed:
            raise RawEvidenceError("sealed_access_denied")

    def _validate_metadata(self, **values: Any) -> dict[str, Any]:
        for name in (
            "source_system",
            "source_kind",
            "source_scope",
            "upstream_source_id",
            "upstream_item_id",
            "source_occurrence_key",
            "captured_at",
        ):
            value = values[name]
            if value is not None:
                _validate_text(value, name, self.limits.max_metadata_chars)
        for name in ("source_system", "source_kind", "source_scope"):
            if not values[name]:
                raise RawEvidenceError("invalid_input")
        values["identity_origin"] = _validate_choice(
            values["identity_origin"], IDENTITY_ORIGINS, "identity_origin"
        )
        values["fidelity_level"] = _validate_choice(
            values["fidelity_level"], FIDELITY_LEVELS, "fidelity_level"
        )
        values["privacy_class"] = _validate_choice(
            values["privacy_class"], PRIVACY_CLASSES, "privacy_class"
        )
        _validate_text(values["media_type"], "media_type", self.limits.max_metadata_chars)
        if not values["media_type"]:
            raise RawEvidenceError("invalid_input")
        if values["identity_origin"] == "upstream" and not (
            values["upstream_source_id"] or values["upstream_item_id"]
        ):
            raise RawEvidenceError("identity_invalid")
        if values["identity_origin"] == "local" and not values["source_occurrence_key"]:
            raise RawEvidenceError("identity_invalid")
        if values["captured_at"] is None:
            values["captured_at"] = _now_iso()
        return values


def _validate_owned_root(
    value: str | Path | None,
    forbidden_roots: Iterable[str | Path],
) -> Path:
    candidate = _canonical_absolute(value, "evidence_root")
    for forbidden in forbidden_roots:
        forbidden_path = _canonical_absolute(forbidden, "forbidden_root")
        if _same_or_within(candidate, forbidden_path) or _same_or_within(
            forbidden_path, candidate
        ):
            raise RawEvidenceError("root_overlap")
    if candidate.exists() and not candidate.is_dir():
        raise RawEvidenceError("root_invalid")
    return candidate


def _canonical_absolute(value: str | Path | None, label: str) -> Path:
    if value is None or isinstance(value, bool):
        raise RawEvidenceError(f"{label}_invalid")
    try:
        candidate = Path(value).expanduser()
    except (TypeError, ValueError, OSError) as exc:
        raise RawEvidenceError(f"{label}_invalid") from exc
    if not candidate.is_absolute():
        raise RawEvidenceError(f"{label}_not_absolute")
    try:
        _reject_reparse_components(candidate)
        resolved = candidate.resolve(strict=False)
        _reject_reparse_components(resolved)
    except RawEvidenceError:
        raise
    except (OSError, RuntimeError) as exc:
        raise RawEvidenceError(f"{label}_unresolvable") from exc
    return resolved


def _same_or_within(candidate: Path, ancestor: Path) -> bool:
    try:
        candidate.relative_to(ancestor)
        return True
    except ValueError:
        return False


def _reject_reparse_components(path: Path) -> None:
    current = Path(path.anchor) if path.anchor else Path()
    parts = path.parts[1:] if path.anchor else path.parts
    for part in parts:
        current = current / part
        try:
            if current.is_symlink():
                raise RawEvidenceError("path_reparse_unsupported")
            info = current.lstat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise RawEvidenceError("path_inspection_failed") from exc
        if getattr(info, "st_file_attributes", 0) & _REPARSE_ATTRIBUTE:
            raise RawEvidenceError("path_reparse_unsupported")


def _assert_owned_path(path: Path, ancestor: Path | None) -> None:
    if ancestor is None:
        raise RawEvidenceError("storage_unavailable")
    _reject_reparse_components(path)
    try:
        resolved_path = path.resolve(strict=False)
        resolved_ancestor = ancestor.resolve(strict=False)
        resolved_path.relative_to(resolved_ancestor)
    except (ValueError, OSError, RuntimeError) as exc:
        raise RawEvidenceError("path_escape") from exc


def _verify_file(path: Path, expected_hash: str, expected_size: int) -> bool:
    try:
        if not path.is_file() or path.is_symlink():
            return False
        if path.stat().st_size != expected_size:
            return False
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            while True:
                chunk = handle.read(_MAX_READ_CHUNK)
                if not chunk:
                    break
                digest.update(chunk)
        return secrets.compare_digest(digest.hexdigest(), expected_hash)
    except (OSError, ValueError):
        return False


def _chmod_private(path: Path, *, directory: bool = False) -> None:
    try:
        os.chmod(path, 0o700 if directory else 0o600)
    except OSError:
        # Windows ACL enforcement is deployment-specific.  The path remains
        # isolated and the implementation does not claim ACL equivalence.
        return


def _validate_id(value: Any, label: str) -> str:
    if not isinstance(value, str) or _ID_PATTERN.fullmatch(value) is None:
        raise RawEvidenceError(f"{label}_invalid")
    return value


def _validate_text(value: Any, label: str, max_chars: int) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise RawEvidenceError(f"{label}_invalid")
    if len(value) > max_chars:
        raise RawEvidenceError("metadata_too_large")
    return value


def _validate_choice(value: Any, choices: frozenset[str], label: str) -> str:
    if not isinstance(value, str) or value not in choices:
        raise RawEvidenceError(f"{label}_invalid")
    return value


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


__all__ = [
    "FIDELITY_LEVELS",
    "HASH_ALGORITHM",
    "IDENTITY_ORIGINS",
    "LIFECYCLE_STATES",
    "PRIVACY_CLASSES",
    "RawEvidenceError",
    "RawEvidenceLimits",
    "RawEvidenceStore",
    "SCHEMA_VERSION",
    "VERIFICATION_STATES",
]

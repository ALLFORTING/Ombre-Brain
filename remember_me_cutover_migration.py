"""Explicit local operator toolchain for the RM production cutover.

Implementation C is deliberately a control-plane/maintenance module.  It is
not imported by ``server.py`` and it never selects an asset authority.  Every
root, state database, migration identity, and report path is supplied by the
operator.  The legacy source is opened read-only; the RM target is reached
only through the pinned public Remember-Me Core contract.
"""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from contextlib import closing
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import tempfile
from typing import Any

from asset_cutover_state import (
    CutoverState,
    CutoverStateError,
    CutoverStateStore,
    FreezeLease,
    MigrationIdentity,
    MutationCapability,
)
from asset_storage_layout import AssetStorageLayoutError, validate_asset_storage_layout
from asset_migration_state import canonical_path_identity
from remember_me_adapter import (
    EXPECTED_DATA_COMPATIBILITY,
    EXPECTED_MCP_TOOLS,
    EXPECTED_PACKAGE_VERSION,
    EXPECTED_PILLOW_RANGE,
    EXPECTED_SANITIZER_ID,
    RememberMeAdapter,
    inspect_remember_me_contract,
)


TOOL_NAME = "ombre-rm-cutover-migration"
TOOL_VERSION = "1.0.0-c"
MIGRATION_KEY = "ombre-rm-production-cutover"
MIGRATION_VERSION = 1
CHECKPOINT_SCHEMA_VERSION = 1
DEFAULT_BATCH_SIZE = 100
DEFAULT_LEASE_TTL_SECONDS = 60
DEFAULT_MAX_NEW_INDEX_WORK = 100
MAX_BATCH_SIZE = 500
MAX_EXTERNAL_WORK = 10_000
REPORT_SCHEMA_VERSION = 1
PROGRESS_DB_NAME = "migration-progress.sqlite3"
SOURCE_DB_NAME = "assets.sqlite3"
ASSET_ID_RE = re.compile(r"[0-9a-f]{32}\Z")
SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
SAFE_ID_RE = re.compile(r"[A-Za-z0-9_.:-]{1,128}\Z")

EXIT_SUCCESS = 0
EXIT_PREFLIGHT_FAILED = 2
EXIT_MIGRATION_BLOCKED = 3
EXIT_MIGRATION_FAILED = 4
EXIT_RECONCILIATION_FAILED = 5
EXIT_SOURCE_CHANGED = 6
EXIT_LEASE_LOST = 7
EXIT_WORKSPACE_INVALID = 8
EXIT_INTERNAL_ERROR = 9


class CutoverMigrationError(RuntimeError):
    """Path-free, stable operator error."""

    def __init__(self, code: str, *, exit_code: int = EXIT_INTERNAL_ERROR):
        self.code = code
        self.exit_code = exit_code
        super().__init__(code)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _timestamp() -> str:
    return _utc_now().isoformat(timespec="microseconds")


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _hash_json(value: Any) -> str:
    return _sha256_text(_canonical_json(value))


def _atomic_json_write(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, raw = tempfile.mkstemp(prefix=".rm-cutover-", suffix=".json", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, ensure_ascii=True, sort_keys=True, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(raw, path)
    except Exception:
        try:
            os.unlink(raw)
        except OSError:
            pass
        raise


def _read_only_sqlite(path: Path) -> sqlite3.Connection:
    try:
        connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=1)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only = ON")
        connection.execute("PRAGMA busy_timeout = 1000")
        return connection
    except (OSError, sqlite3.Error) as exc:
        raise CutoverMigrationError("legacy_source_unreadable", exit_code=EXIT_PREFLIGHT_FAILED) from exc


def _is_within(root: Path, candidate: Path) -> bool:
    try:
        candidate.resolve().relative_to(root.resolve())
        return True
    except (OSError, RuntimeError, ValueError):
        return False


def _contains_symlink_component(root: Path, candidate: Path) -> bool:
    try:
        relative = candidate.relative_to(root)
    except ValueError:
        return True
    current = root
    for part in relative.parts:
        current = current / part
        try:
            if current.is_symlink():
                return True
        except OSError:
            return True
    return False


def _validate_absolute_file(value: str | Path, code: str) -> Path:
    if isinstance(value, bool):
        raise CutoverMigrationError(code, exit_code=EXIT_WORKSPACE_INVALID)
    try:
        candidate = Path(value).expanduser()
    except (TypeError, ValueError, OSError) as exc:
        raise CutoverMigrationError(code, exit_code=EXIT_WORKSPACE_INVALID) from exc
    if not candidate.is_absolute():
        raise CutoverMigrationError(code + "_not_absolute", exit_code=EXIT_WORKSPACE_INVALID)
    try:
        resolved = candidate.resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise CutoverMigrationError(code, exit_code=EXIT_WORKSPACE_INVALID) from exc
    if resolved == Path(resolved.anchor):
        raise CutoverMigrationError(code, exit_code=EXIT_WORKSPACE_INVALID)
    return resolved


@dataclass(frozen=True)
class MigrationInputs:
    legacy_root: Path
    rm_root: Path
    state_db_path: Path
    report_path: Path
    migration_identity: str = MIGRATION_KEY
    migration_version: int = MIGRATION_VERSION
    expected_package_version: str = EXPECTED_PACKAGE_VERSION
    expected_data_compatibility: str = EXPECTED_DATA_COMPATIBILITY
    expected_sanitizer_id: str = EXPECTED_SANITIZER_ID
    expected_pillow_range: str = EXPECTED_PILLOW_RANGE
    expected_schema_version: int = CHECKPOINT_SCHEMA_VERSION

    @classmethod
    def create(
        cls,
        *,
        legacy_root: str | Path,
        rm_root: str | Path,
        state_db_path: str | Path,
        report_path: str | Path,
        migration_identity: str = MIGRATION_KEY,
        migration_version: int = MIGRATION_VERSION,
        expected_package_version: str = EXPECTED_PACKAGE_VERSION,
        expected_data_compatibility: str = EXPECTED_DATA_COMPATIBILITY,
        expected_sanitizer_id: str = EXPECTED_SANITIZER_ID,
        expected_pillow_range: str = EXPECTED_PILLOW_RANGE,
        expected_schema_version: int = CHECKPOINT_SCHEMA_VERSION,
    ) -> "MigrationInputs":
        legacy = _validate_absolute_file(legacy_root, "legacy_root_invalid")
        rm = _validate_absolute_file(rm_root, "rm_root_invalid")
        state_db = _validate_absolute_file(state_db_path, "state_db_invalid")
        report = _validate_absolute_file(report_path, "report_path_invalid")
        if not isinstance(migration_identity, str) or not SAFE_ID_RE.fullmatch(migration_identity):
            raise CutoverMigrationError("migration_identity_invalid", exit_code=EXIT_WORKSPACE_INVALID)
        if isinstance(migration_version, bool) or not isinstance(migration_version, int) or migration_version < 1:
            raise CutoverMigrationError("migration_version_invalid", exit_code=EXIT_WORKSPACE_INVALID)
        if isinstance(expected_schema_version, bool) or not isinstance(expected_schema_version, int) or expected_schema_version < 1:
            raise CutoverMigrationError("expected_schema_version_invalid", exit_code=EXIT_WORKSPACE_INVALID)
        if state_db.name != "migration.sqlite3":
            raise CutoverMigrationError("state_db_name_invalid", exit_code=EXIT_WORKSPACE_INVALID)
        try:
            layout = validate_asset_storage_layout(legacy, rm, state_db.parent)
        except AssetStorageLayoutError as exc:
            raise CutoverMigrationError(exc.code, exit_code=EXIT_WORKSPACE_INVALID) from exc
        if layout.state_db_path != state_db:
            raise CutoverMigrationError("state_db_layout_mismatch", exit_code=EXIT_WORKSPACE_INVALID)
        if report == legacy or report == rm or report == state_db:
            raise CutoverMigrationError("report_path_collision", exit_code=EXIT_WORKSPACE_INVALID)
        return cls(
            layout.legacy_root,
            layout.rm_root,
            state_db,
            report,
            migration_identity,
            migration_version,
            expected_package_version,
            expected_data_compatibility,
            expected_sanitizer_id,
            expected_pillow_range,
            expected_schema_version,
        )

    @property
    def state_root(self) -> Path:
        return self.state_db_path.parent

    @property
    def progress_db_path(self) -> Path:
        return self.state_root / PROGRESS_DB_NAME

    @property
    def source_identity(self) -> str:
        return canonical_path_identity(self.legacy_root)

    @property
    def target_identity(self) -> str:
        return canonical_path_identity(self.rm_root)

    @property
    def state_db_identity(self) -> str:
        return "state-sha256:" + _sha256_text(str(self.state_db_path).casefold())


@dataclass(frozen=True)
class SourceSnapshot:
    source_generation: int
    source_generation_hash: str
    upper_bound_asset_id: str | None
    asset_count: int
    asset_ids: tuple[str, ...]
    records: dict[str, dict[str, Any]] = field(repr=False)


class ReadOnlyLegacySource:
    """Strict read-only projection of the legacy AssetStore contract."""

    def __init__(self, legacy_root: Path):
        self.data_root = legacy_root.resolve()
        self.db_path = self.data_root / SOURCE_DB_NAME
        if self.db_path.is_symlink() or not self.db_path.is_file():
            raise CutoverMigrationError("legacy_source_missing", exit_code=EXIT_PREFLIGHT_FAILED)
        self.assets_root = (self.data_root / "assets").resolve()
        if (self.data_root / "assets").is_symlink() or not self.assets_root.is_dir():
            raise CutoverMigrationError("legacy_assets_root_missing", exit_code=EXIT_PREFLIGHT_FAILED)

    def _connect(self) -> sqlite3.Connection:
        return _read_only_sqlite(self.db_path)

    def _records(self) -> dict[str, dict[str, Any]]:
        try:
            with closing(self._connect()) as connection:
                columns = {row["name"] for row in connection.execute("PRAGMA table_info(assets)")}
                required = {
                    "asset_id", "source_sha256", "stored_sha256", "stored_relpath",
                    "original_filename", "mime_type", "kind", "decoded_bytes",
                    "stored_bytes", "width", "height", "created_at", "updated_at",
                    "title", "description",
                }
                if not required.issubset(columns):
                    raise CutoverMigrationError("legacy_schema_incompatible", exit_code=EXIT_PREFLIGHT_FAILED)
                rows = connection.execute("SELECT * FROM assets ORDER BY asset_id ASC").fetchall()
                result: dict[str, dict[str, Any]] = {}
                for row in rows:
                    asset_id = row["asset_id"]
                    if not isinstance(asset_id, str) or not ASSET_ID_RE.fullmatch(asset_id):
                        raise CutoverMigrationError("legacy_record_invalid", exit_code=EXIT_PREFLIGHT_FAILED)
                    result[asset_id] = {key: row[key] for key in row.keys()}
                ids = tuple(result)
                if ids:
                    tag_rows = connection.execute(
                        "SELECT asset_id, tag_display, created_at FROM asset_tags ORDER BY asset_id ASC, tag_normalized ASC"
                    ).fetchall()
                else:
                    tag_rows = ()
                for row in tag_rows:
                    if row["asset_id"] in result:
                        result[row["asset_id"]].setdefault("tags", []).append(
                            {"value": row["tag_display"], "created_at": row["created_at"]}
                        )
                for record in result.values():
                    record.setdefault("tags", [])
                return result
        except CutoverMigrationError:
            raise
        except (OSError, sqlite3.Error, TypeError, ValueError) as exc:
            raise CutoverMigrationError("legacy_source_unreadable", exit_code=EXIT_PREFLIGHT_FAILED) from exc

    def get_import_record(self, asset_id: str) -> dict[str, Any] | None:
        if not isinstance(asset_id, str) or not ASSET_ID_RE.fullmatch(asset_id):
            return None
        return self._records().get(asset_id)

    def resolve_file(self, asset_id: str) -> tuple[dict[str, Any], Path] | None:
        record = self.get_import_record(asset_id)
        if record is None:
            return None
        return self._resolve_record_file(record)

    def _resolve_record_file(self, record: dict[str, Any]) -> tuple[dict[str, Any], Path] | None:
        relative = Path(str(record.get("stored_relpath", "")))
        if relative.is_absolute() or ".." in relative.parts:
            raise CutoverMigrationError("legacy_stored_path_invalid", exit_code=EXIT_PREFLIGHT_FAILED)
        raw_candidate = self.data_root / relative
        if _contains_symlink_component(self.data_root, raw_candidate):
            raise CutoverMigrationError("legacy_stored_path_symlink", exit_code=EXIT_PREFLIGHT_FAILED)
        candidate = raw_candidate.resolve(strict=False)
        if not _is_within(self.assets_root, candidate) or not candidate.is_file():
            return None
        return record, candidate

    def snapshot(self) -> SourceSnapshot:
        records = self._records()
        identities: list[dict[str, Any]] = []
        for asset_id in sorted(records):
            record = records[asset_id]
            resolved = self._resolve_record_file(record)
            blob_hash = None
            blob_size = None
            if resolved is not None:
                blob = resolved[1].read_bytes()
                blob_hash = hashlib.sha256(blob).hexdigest()
                blob_size = len(blob)
            identities.append(
                {
                    "asset_id": asset_id,
                    "record": record,
                    "blob_sha256": blob_hash,
                    "blob_size": blob_size,
                }
            )
        generation_hash = _hash_json(identities)
        generation = int(generation_hash[:15], 16)
        ids = tuple(sorted(records))
        return SourceSnapshot(
            generation,
            generation_hash,
            ids[-1] if ids else None,
            len(ids),
            ids,
            records,
        )

    def list_asset_ids(self, *, last_asset_id: str | None, upper_bound_asset_id: str | None, batch_size: int) -> list[str]:
        if batch_size < 1 or batch_size > MAX_BATCH_SIZE:
            raise CutoverMigrationError("batch_size_invalid", exit_code=EXIT_WORKSPACE_INVALID)
        ids = [asset_id for asset_id in sorted(self._records()) if upper_bound_asset_id is None or asset_id <= upper_bound_asset_id]
        if last_asset_id is not None:
            ids = [asset_id for asset_id in ids if asset_id > last_asset_id]
        return ids[:batch_size]

    def blob_bytes(self, asset_id: str) -> bytes:
        resolved = self.resolve_file(asset_id)
        if resolved is None:
            raise CutoverMigrationError("legacy_blob_missing", exit_code=EXIT_MIGRATION_BLOCKED)
        try:
            return resolved[1].read_bytes()
        except OSError as exc:
            raise CutoverMigrationError("legacy_blob_unreadable", exit_code=EXIT_MIGRATION_BLOCKED) from exc

    def assert_generation(self, snapshot: SourceSnapshot) -> None:
        current = self.snapshot()
        if current.source_generation_hash != snapshot.source_generation_hash:
            raise CutoverMigrationError("source_changed_since_checkpoint", exit_code=EXIT_SOURCE_CHANGED)


@dataclass(frozen=True)
class Checkpoint:
    migration_identity: str
    migration_version: int
    source_identity: str
    target_identity: str
    state_db_identity: str
    source_generation: int
    source_generation_hash: str
    upper_bound_asset_id: str | None
    initial_asset_count: int
    last_completed_asset_id: str | None
    status: str
    processed_count: int
    imported_count: int
    skipped_idempotent_count: int
    blocked_asset_id: str | None
    error_code: str | None
    reconciliation_status: str
    verification_status: str
    reindex_status: str
    updated_at: str
    abort_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


class ProgressStore:
    """Durable C checkpoint separate from A's cutover state schema."""

    def __init__(self, path: Path, *, read_only: bool = False):
        self.path = path
        self.read_only = read_only
        if read_only:
            if not path.is_file():
                raise CutoverMigrationError("checkpoint_missing", exit_code=EXIT_PREFLIGHT_FAILED)
            self.connection = _read_only_sqlite(path)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            try:
                self.connection = sqlite3.connect(path, timeout=5)
                self.connection.row_factory = sqlite3.Row
                self.connection.execute("PRAGMA busy_timeout = 5000")
                self.connection.execute(
                    "CREATE TABLE IF NOT EXISTS cutover_checkpoint (singleton INTEGER PRIMARY KEY CHECK(singleton=1), payload TEXT NOT NULL)"
                )
                self.connection.commit()
            except (OSError, sqlite3.Error) as exc:
                raise CutoverMigrationError("checkpoint_unavailable", exit_code=EXIT_INTERNAL_ERROR) from exc

    def close(self) -> None:
        self.connection.close()

    def load(self) -> Checkpoint | None:
        try:
            row = self.connection.execute("SELECT payload FROM cutover_checkpoint WHERE singleton=1").fetchone()
            if row is None:
                return None
            payload = json.loads(row["payload"])
            if payload.get("schema_version") != CHECKPOINT_SCHEMA_VERSION:
                raise CutoverMigrationError("checkpoint_schema_incompatible", exit_code=EXIT_WORKSPACE_INVALID)
            payload.pop("schema_version", None)
            return Checkpoint(**payload)
        except CutoverMigrationError:
            raise
        except (sqlite3.Error, ValueError, TypeError, KeyError) as exc:
            raise CutoverMigrationError("checkpoint_corrupt", exit_code=EXIT_WORKSPACE_INVALID) from exc

    def save(self, checkpoint: Checkpoint) -> None:
        if self.read_only:
            raise CutoverMigrationError("checkpoint_read_only", exit_code=EXIT_INTERNAL_ERROR)
        payload = checkpoint.to_dict()
        payload["schema_version"] = CHECKPOINT_SCHEMA_VERSION
        try:
            self.connection.execute(
                "INSERT INTO cutover_checkpoint(singleton,payload) VALUES(1,?) ON CONFLICT(singleton) DO UPDATE SET payload=excluded.payload",
                (_canonical_json(payload),),
            )
            self.connection.commit()
        except sqlite3.Error as exc:
            self.connection.rollback()
            raise CutoverMigrationError("checkpoint_write_failed", exit_code=EXIT_INTERNAL_ERROR) from exc


class CapabilityBoundRmTarget:
    """Public Core target facade with an A capability at every write boundary."""

    def __init__(self, service: Any, state: CutoverStateStore, capability: MutationCapability):
        self.service = service
        self.state = state
        self.capability = capability

    def _assert_write(self) -> None:
        self.state.assert_privileged_capability(self.capability, purpose="rm-migration-write")

    def import_asset(self, request: Any) -> Any:
        self._assert_write()
        result = self.service.import_asset(request)
        self._assert_write()
        return result

    def reindex_embeddings(self, request: Any) -> Any:
        self._assert_write()
        result = self.service.reindex_embeddings(request)
        self._assert_write()
        return result

    def __getattr__(self, name: str) -> Any:
        return getattr(self.service, name)


def _build_import_request(asset_id: str, record: dict[str, Any], content: bytes, *, dry_run: bool = False) -> Any:
    from remember_me.core import ImportAssetRequest, ImportAssetTag

    return ImportAssetRequest(
        asset_id=asset_id,
        source_sha256=record["source_sha256"],
        stored_sha256=record["stored_sha256"],
        cleaned_bytes=content,
        original_filename=record["original_filename"],
        mime_type=record["mime_type"],
        kind=record["kind"],
        decoded_bytes=record["decoded_bytes"],
        stored_bytes=record["stored_bytes"],
        width=record["width"],
        height=record["height"],
        created_at=record["created_at"],
        updated_at=record["updated_at"],
        title=record["title"],
        description=record["description"],
        tags=tuple(ImportAssetTag(value=tag["value"], created_at=tag["created_at"]) for tag in record["tags"]),
        dry_run=dry_run,
    )


class ProductionImportAdapter:
    """Small production-root adapter preserving the Stage 8G public import contract."""

    def __init__(self, source: ReadOnlyLegacySource, target: CapabilityBoundRmTarget):
        self.source = source
        self.target = target

    def import_asset(self, asset_id: str) -> tuple[str, str | None]:
        from remember_me.core import (
            AssetIdConflict,
            ImportAssetDisposition,
            ImageMimeMismatch,
            ImageValidationError,
            ImportMetadataValidationError,
            InvalidImportRecord,
            StoredShaMismatch,
            StoredShaOwnershipConflict,
            UnsupportedAssetKind,
            UnsupportedImageFormat,
        )

        record = self.source.get_import_record(asset_id)
        if record is None:
            return "rejected", "legacy_asset_missing"
        if record.get("kind") != "image" or record.get("mime_type") not in {"image/jpeg", "image/png"}:
            return "rejected", "unsupported_legacy_asset"
        try:
            content = self.source.blob_bytes(asset_id)
            if hashlib.sha256(content).hexdigest() != record["stored_sha256"] or len(content) != record["stored_bytes"]:
                return "rejected", "stored_sha_mismatch"
            request = _build_import_request(asset_id, record, content)
            result = self.target.import_asset(request)
        except (KeyError, TypeError, ValueError):
            return "rejected", "malformed_legacy_record"
        except AssetIdConflict:
            return "rejected", "asset_id_conflict"
        except StoredShaOwnershipConflict:
            return "rejected", "stored_sha_ownership_conflict"
        except StoredShaMismatch:
            return "rejected", "stored_sha_mismatch"
        except UnsupportedAssetKind:
            return "rejected", "unsupported_legacy_asset"
        except (UnsupportedImageFormat, ImageMimeMismatch, ImageValidationError, ImportMetadataValidationError, InvalidImportRecord):
            return "rejected", "rm_import_validation_failure"
        except CutoverStateError as exc:
            if exc.code in {"freeze_lease_expired", "freeze_lease_invalid", "capability_invalid"}:
                raise CutoverMigrationError("migration_freeze_lost", exit_code=EXIT_LEASE_LOST) from exc
            raise
        disposition = getattr(getattr(result, "disposition", None), "value", "")
        if disposition in {ImportAssetDisposition.IMPORTED.value, "imported"}:
            return "imported", None
        if disposition in {ImportAssetDisposition.SKIPPED_IDEMPOTENT.value, "skipped_idempotent"}:
            return "skipped_idempotent", None
        return "rejected", "rm_import_failure"

    def get_asset(self, asset_id: str) -> Any:
        from remember_me.core import GetAssetRequest
        return self.target.get_asset(GetAssetRequest(asset_id=asset_id))

    def verification(self) -> tuple[Any, list[Any]]:
        from remember_me.core import (
            AssetVerificationPage,
            AssetVerificationSnapshot,
            BeginAssetVerificationRequest,
            ListAssetVerificationPageRequest,
        )

        begin = getattr(self.target, "begin_asset_verification", None)
        page_method = getattr(self.target, "list_asset_verification_page", None)
        if not callable(begin) or not callable(page_method):
            raise CutoverMigrationError("rm_verification_unavailable", exit_code=EXIT_RECONCILIATION_FAILED)
        snapshot = begin(BeginAssetVerificationRequest(kind="image"))
        if not isinstance(snapshot, AssetVerificationSnapshot):
            raise CutoverMigrationError("rm_verification_result_invalid", exit_code=EXIT_RECONCILIATION_FAILED)
        if type(snapshot.total_count) is not int or snapshot.total_count < 0:
            raise CutoverMigrationError("rm_verification_result_invalid", exit_code=EXIT_RECONCILIATION_FAILED)
        cursor = ""
        pages: list[Any] = []
        seen_cursors = {cursor}
        seen_asset_ids: set[str] = set()
        previous_asset_id: str | None = None
        while True:
            page = page_method(ListAssetVerificationPageRequest(snapshot_id=snapshot.snapshot_id, cursor=cursor, limit=500))
            if (
                not isinstance(page, AssetVerificationPage)
                or page.snapshot_id != snapshot.snapshot_id
                or page.generation != snapshot.generation
                or page.total_count != snapshot.total_count
                or type(page.records) is not tuple
                or len(page.records) > 500
                or type(page.has_more) is not bool
                or type(page.next_cursor) is not str
            ):
                raise CutoverMigrationError("rm_verification_result_invalid", exit_code=EXIT_RECONCILIATION_FAILED)
            if page.has_more and len(page.records) != 500:
                raise CutoverMigrationError("rm_verification_cursor_invalid", exit_code=EXIT_RECONCILIATION_FAILED)
            for record in page.records:
                asset_id = getattr(record, "asset_id", None)
                if not isinstance(asset_id, str) or asset_id in seen_asset_ids or (previous_asset_id is not None and asset_id <= previous_asset_id):
                    raise CutoverMigrationError("rm_verification_duplicate_asset", exit_code=EXIT_RECONCILIATION_FAILED)
                seen_asset_ids.add(asset_id)
                previous_asset_id = asset_id
            pages.extend(page.records)
            if not page.has_more:
                if page.next_cursor != "":
                    raise CutoverMigrationError("rm_verification_cursor_invalid", exit_code=EXIT_RECONCILIATION_FAILED)
                break
            if not page.next_cursor or page.next_cursor in seen_cursors:
                raise CutoverMigrationError("rm_verification_cursor_invalid", exit_code=EXIT_RECONCILIATION_FAILED)
            cursor = page.next_cursor
            seen_cursors.add(cursor)
            if len(pages) > snapshot.total_count + 1:
                raise CutoverMigrationError("rm_verification_cursor_invalid", exit_code=EXIT_RECONCILIATION_FAILED)
        if len(pages) != snapshot.total_count:
            raise CutoverMigrationError("rm_verification_incomplete", exit_code=EXIT_RECONCILIATION_FAILED)
        return snapshot, pages

    def verify_blob(self, snapshot: Any, asset_id: str, expected: dict[str, Any], content: bytes) -> Any:
        from remember_me.core import VerifyAssetBlobRequest
        method = getattr(self.target, "verify_asset_blob", None)
        if not callable(method):
            raise CutoverMigrationError("rm_verification_unavailable", exit_code=EXIT_RECONCILIATION_FAILED)
        return method(VerifyAssetBlobRequest(
            snapshot_id=snapshot.snapshot_id,
            asset_id=asset_id,
            expected_sha256=expected["stored_sha256"],
            expected_size=expected["stored_bytes"],
            expected_bytes=content,
        ))

    def complete_verification(self, snapshot: Any) -> Any:
        from remember_me.core import CompleteAssetVerificationRequest
        method = getattr(self.target, "complete_asset_verification", None)
        if not callable(method):
            raise CutoverMigrationError("rm_verification_unavailable", exit_code=EXIT_RECONCILIATION_FAILED)
        return method(CompleteAssetVerificationRequest(snapshot_id=snapshot.snapshot_id))


def _record_comparison(source: dict[str, Any], target: Any) -> list[str]:
    mismatches: list[str] = []
    for field in (
        "asset_id", "source_sha256", "stored_sha256", "original_filename", "mime_type", "kind",
        "decoded_bytes", "stored_bytes", "width", "height", "created_at", "updated_at", "title", "description",
    ):
        if source.get(field) != getattr(target, field, None):
            mismatches.append(field)
    source_tags = sorted((tag.get("value"), tag.get("created_at")) for tag in source.get("tags", []))
    target_tags = sorted((getattr(tag, "value", None), getattr(tag, "created_at", None)) for tag in getattr(target, "tags", ()))
    if source_tags != target_tags:
        mismatches.append("tags")
    return mismatches


def _db_integrity(rm_root: Path) -> dict[str, Any]:
    checked = 0
    failures = 0
    for candidate in sorted(rm_root.rglob("*.sqlite3")) + sorted(rm_root.rglob("*.db")):
        if not candidate.is_file():
            continue
        checked += 1
        try:
            with closing(_read_only_sqlite(candidate)) as connection:
                row = connection.execute("PRAGMA quick_check(1)").fetchone()
                failures += int(row is None or row[0] != "ok")
        except (CutoverMigrationError, sqlite3.Error, OSError):
            failures += 1
    return {"checked": checked, "failures": failures, "status": "passed" if failures == 0 else "failed"}


def _contract_summary(inputs: MigrationInputs) -> dict[str, Any]:
    try:
        actual = inspect_remember_me_contract()
        contract_status = "passed"
        mismatches = []
        expected = {
            "distribution_name": "remember-me",
            "package_version": inputs.expected_package_version,
            "data_compatibility": inputs.expected_data_compatibility,
            "sanitizer_id": inputs.expected_sanitizer_id,
            "pillow_range": inputs.expected_pillow_range,
            "mcp_tools": EXPECTED_MCP_TOOLS,
        }
        for key, value in expected.items():
            if getattr(actual, key, None) != value:
                mismatches.append(key)
        if mismatches:
            contract_status = "failed"
        if inputs.expected_schema_version != CHECKPOINT_SCHEMA_VERSION:
            mismatches.append("schema_version")
            contract_status = "failed"
        return {"status": contract_status, "mismatches": mismatches, "package_version": actual.package_version, "schema_version": CHECKPOINT_SCHEMA_VERSION}
    except Exception as exc:
        return {"status": "failed", "mismatches": ["contract_unavailable"], "package_version": None}


def _check_checkpoint_identity(checkpoint: Checkpoint | None, inputs: MigrationInputs, snapshot: SourceSnapshot | None = None) -> None:
    if checkpoint is None:
        return
    expected = {
        "migration_identity": inputs.migration_identity,
        "migration_version": inputs.migration_version,
        "source_identity": inputs.source_identity,
        "target_identity": inputs.target_identity,
        "state_db_identity": inputs.state_db_identity,
    }
    for field_name, value in expected.items():
        if getattr(checkpoint, field_name) != value:
            raise CutoverMigrationError("checkpoint_identity_mismatch", exit_code=EXIT_WORKSPACE_INVALID)
    if snapshot is not None:
        if checkpoint.source_generation_hash != snapshot.source_generation_hash:
            raise CutoverMigrationError("source_changed_since_checkpoint", exit_code=EXIT_SOURCE_CHANGED)
        if checkpoint.upper_bound_asset_id != snapshot.upper_bound_asset_id or checkpoint.initial_asset_count != snapshot.asset_count:
            raise CutoverMigrationError("source_changed_since_checkpoint", exit_code=EXIT_SOURCE_CHANGED)


def _new_checkpoint(inputs: MigrationInputs, snapshot: SourceSnapshot) -> Checkpoint:
    return Checkpoint(
        inputs.migration_identity, inputs.migration_version, inputs.source_identity, inputs.target_identity,
        inputs.state_db_identity, snapshot.source_generation, snapshot.source_generation_hash,
        snapshot.upper_bound_asset_id, snapshot.asset_count, None, "ready", 0, 0, 0, None, None,
        "not_run", "not_run", "not_run", _timestamp(), None,
    )


def _replace_checkpoint(checkpoint: Checkpoint, **changes: Any) -> Checkpoint:
    payload = checkpoint.to_dict()
    payload.update(changes)
    payload["updated_at"] = _timestamp()
    return Checkpoint(**payload)


def _state_store(inputs: MigrationInputs) -> CutoverStateStore:
    try:
        return CutoverStateStore(inputs.state_db_path)
    except CutoverStateError as exc:
        raise CutoverMigrationError(exc.code, exit_code=EXIT_WORKSPACE_INVALID) from exc


def _migration_identity(inputs: MigrationInputs, snapshot: SourceSnapshot) -> MigrationIdentity:
    return MigrationIdentity(
        migration_key=inputs.migration_identity,
        migration_version=inputs.migration_version,
        source_identity=inputs.source_identity,
        source_generation=snapshot.source_generation,
        target_identity=inputs.target_identity,
    )


def _ensure_rm_ready(state: CutoverStateStore) -> None:
    snapshot = state.get_snapshot()
    if snapshot.state is CutoverState.LEGACY_UNAVAILABLE_RM:
        if snapshot.rm_available:
            raise CutoverMigrationError("rm_readiness_mismatch", exit_code=EXIT_WORKSPACE_INVALID)
        state.set_rm_available(True)
        state.transition(CutoverState.LEGACY_AUTHORITY_RM_READY)
        return
    if snapshot.state is CutoverState.LEGACY_AUTHORITY_RM_READY:
        if not snapshot.rm_available:
            raise CutoverMigrationError("rm_readiness_mismatch", exit_code=EXIT_WORKSPACE_INVALID)
        return
    if snapshot.state in {CutoverState.FROZEN_LEGACY_MIGRATION, CutoverState.FROZEN_READY_FOR_RM_SWITCH}:
        if snapshot.freeze_status == "active":
            raise CutoverMigrationError("freeze_active_resume_required", exit_code=EXIT_LEASE_LOST)
        if snapshot.freeze_status == "expired" and snapshot.lease_id:
            try:
                state.recover_expired_freeze(expected_lease_id=snapshot.lease_id, target_state=CutoverState.LEGACY_AUTHORITY_RM_READY)
            except CutoverStateError as exc:
                raise CutoverMigrationError(exc.code, exit_code=EXIT_LEASE_LOST) from exc
            return
    raise CutoverMigrationError("cutover_state_not_migration_ready", exit_code=EXIT_WORKSPACE_INVALID)


def _acquire_lease(state: CutoverStateStore, inputs: MigrationInputs, snapshot: SourceSnapshot, ttl_seconds: int) -> tuple[FreezeLease, MutationCapability]:
    _ensure_rm_ready(state)
    identity = _migration_identity(inputs, snapshot)
    try:
        lease = state.acquire_freeze(
            expected_state=CutoverState.LEGACY_AUTHORITY_RM_READY,
            frozen_state=CutoverState.FROZEN_LEGACY_MIGRATION,
            ttl_seconds=ttl_seconds,
            migration_identity=identity,
        )
        capability = state.issue_privileged_capability(lease, purpose="rm-migration-write")
        return lease, capability
    except CutoverStateError as exc:
        code = "migration_freeze_lost" if exc.code in {"freeze_lease_busy", "freeze_lease_stale", "freeze_lease_expired"} else exc.code
        raise CutoverMigrationError(code, exit_code=EXIT_LEASE_LOST if code == "migration_freeze_lost" else EXIT_WORKSPACE_INVALID) from exc


def _renew(state: CutoverStateStore, lease: FreezeLease, ttl_seconds: int) -> FreezeLease:
    try:
        return state.renew_freeze(lease, ttl_seconds=ttl_seconds)
    except CutoverStateError as exc:
        raise CutoverMigrationError("migration_freeze_lost", exit_code=EXIT_LEASE_LOST) from exc


def _load_progress(inputs: MigrationInputs, *, read_only: bool = False) -> tuple[ProgressStore, Checkpoint | None]:
    store = ProgressStore(inputs.progress_db_path, read_only=read_only)
    return store, store.load()


def preflight_local(inputs: MigrationInputs) -> dict[str, Any]:
    started = _timestamp()
    issues: list[str] = []
    snapshot: SourceSnapshot | None = None
    progress: ProgressStore | None = None
    checkpoint: Checkpoint | None = None
    state_snapshot: dict[str, Any] | None = None
    try:
        source = ReadOnlyLegacySource(inputs.legacy_root)
        snapshot = source.snapshot()
        for asset_id, record in snapshot.records.items():
            if record.get("kind") != "image" or record.get("mime_type") not in {"image/jpeg", "image/png"}:
                issues.append("unsupported_legacy_asset")
            if not SHA256_RE.fullmatch(str(record.get("stored_sha256", ""))):
                issues.append("legacy_record_invalid")
            try:
                blob = source.blob_bytes(asset_id)
                if len(blob) != record.get("stored_bytes") or hashlib.sha256(blob).hexdigest() != record.get("stored_sha256"):
                    issues.append("legacy_blob_checksum_mismatch")
            except CutoverMigrationError as exc:
                issues.append(exc.code)
        contract = _contract_summary(inputs)
        if contract["status"] != "passed":
            issues.append("remember_me_contract_mismatch")
        if inputs.rm_root.exists() and any(inputs.rm_root.iterdir()):
            try:
                progress, checkpoint = _load_progress(inputs, read_only=True)
                _check_checkpoint_identity(checkpoint, inputs, snapshot)
            except CutoverMigrationError:
                issues.append("target_state_conflict")
        if inputs.state_db_path.exists():
            try:
                state_snapshot = _read_cutover_snapshot(inputs.state_db_path)
            except CutoverMigrationError:
                issues.append("state_db_unreadable")
        if checkpoint is None and inputs.rm_root.exists() and any(inputs.rm_root.iterdir()):
            issues.append("target_not_empty_without_checkpoint")
        status = "success" if not issues else "preflight_failed"
        report = _base_report(inputs, started, status=status, exit_code=EXIT_SUCCESS if not issues else EXIT_PREFLIGHT_FAILED)
        report.update({
            "phase": "preflight-local",
            "contract": contract,
            "source": {
                "asset_count": snapshot.asset_count if snapshot else 0,
                "upper_bound_asset_id": snapshot.upper_bound_asset_id if snapshot else None,
                "source_generation_hash": snapshot.source_generation_hash if snapshot else None,
            },
            "checkpoint": checkpoint.to_dict() if checkpoint else None,
            "cutover_state": state_snapshot,
            "issues": sorted(set(issues)),
        })
        return report
    except CutoverMigrationError as exc:
        return _error_report(inputs, started, "preflight-local", exc)
    except Exception:
        return _error_report(inputs, started, "preflight-local", CutoverMigrationError("internal_error", exit_code=EXIT_INTERNAL_ERROR))
    finally:
        if progress is not None:
            progress.close()


def _create_runtime(inputs: MigrationInputs) -> Any:
    try:
        adapter = RememberMeAdapter()
        return adapter.create_runtime(inputs.rm_root)
    except Exception as exc:
        raise CutoverMigrationError("rm_runtime_unavailable", exit_code=EXIT_MIGRATION_FAILED) from exc


def migrate(
    inputs: MigrationInputs,
    *,
    batch_size: int = DEFAULT_BATCH_SIZE,
    lease_ttl_seconds: int = DEFAULT_LEASE_TTL_SECONDS,
    max_batches: int | None = None,
    resume: bool = False,
    runtime: Any | None = None,
) -> dict[str, Any]:
    started = _timestamp()
    state: CutoverStateStore | None = None
    progress: ProgressStore | None = None
    lease: FreezeLease | None = None
    try:
        if batch_size < 1 or batch_size > MAX_BATCH_SIZE:
            raise CutoverMigrationError("batch_size_invalid", exit_code=EXIT_WORKSPACE_INVALID)
        source = ReadOnlyLegacySource(inputs.legacy_root)
        source_snapshot = source.snapshot()
        progress, checkpoint = _load_progress(inputs)
        _check_checkpoint_identity(checkpoint, inputs, source_snapshot)
        if checkpoint is None:
            checkpoint = _new_checkpoint(inputs, source_snapshot)
            progress.save(checkpoint)
        elif checkpoint.status == "completed":
            report = _base_report(inputs, started, status="success", exit_code=EXIT_SUCCESS)
            report.update({"phase": "migrate", "checkpoint": checkpoint.to_dict(), "cutover_state": _read_cutover_snapshot(inputs.state_db_path) if inputs.state_db_path.exists() else None})
            return report
        elif not resume and checkpoint.status in {"paused", "blocked", "failed"}:
            raise CutoverMigrationError("explicit_resume_required", exit_code=EXIT_MIGRATION_BLOCKED)
        state = _state_store(inputs)
        lease, capability = _acquire_lease(state, inputs, source_snapshot, lease_ttl_seconds)
        if runtime is None:
            runtime = _create_runtime(inputs)
        target = CapabilityBoundRmTarget(runtime.service, state, capability)
        importer = ProductionImportAdapter(source, target)
        cursor = checkpoint.last_completed_asset_id
        processed_since = 0
        while True:
            if max_batches is not None and processed_since // max(batch_size, 1) >= max_batches:
                checkpoint = _replace_checkpoint(checkpoint, status="paused")
                progress.save(checkpoint)
                break
            lease = _renew(state, lease, lease_ttl_seconds)
            source.assert_generation(source_snapshot)
            ids = source.list_asset_ids(last_asset_id=cursor, upper_bound_asset_id=checkpoint.upper_bound_asset_id, batch_size=batch_size)
            if not ids:
                checkpoint = _replace_checkpoint(checkpoint, status="completed", error_code=None)
                progress.save(checkpoint)
                break
            for asset_id in ids:
                lease = _renew(state, lease, lease_ttl_seconds)
                source.assert_generation(source_snapshot)
                disposition, error_code = importer.import_asset(asset_id)
                processed_since += 1
                if disposition == "imported":
                    checkpoint = _replace_checkpoint(checkpoint, last_completed_asset_id=asset_id, status="running", processed_count=checkpoint.processed_count + 1, imported_count=checkpoint.imported_count + 1, error_code=None)
                elif disposition == "skipped_idempotent":
                    checkpoint = _replace_checkpoint(checkpoint, last_completed_asset_id=asset_id, status="running", processed_count=checkpoint.processed_count + 1, skipped_idempotent_count=checkpoint.skipped_idempotent_count + 1, error_code=None)
                else:
                    checkpoint = _replace_checkpoint(checkpoint, status="blocked", blocked_asset_id=asset_id, error_code=error_code or "migration_blocked")
                    progress.save(checkpoint)
                    raise CutoverMigrationError(error_code or "migration_blocked", exit_code=EXIT_MIGRATION_BLOCKED)
                progress.save(checkpoint)
                cursor = asset_id
            source.assert_generation(source_snapshot)
        report = _base_report(inputs, started, status="success" if checkpoint.status == "completed" else "migration_blocked", exit_code=EXIT_SUCCESS if checkpoint.status == "completed" else EXIT_MIGRATION_BLOCKED)
        report.update({"phase": "migrate", "checkpoint": checkpoint.to_dict(), "cutover_state": _read_cutover_snapshot(inputs.state_db_path)})
        return report
    except CutoverMigrationError as exc:
        if progress is not None:
            current = progress.load()
            if current is not None and exc.code == "migration_freeze_lost":
                progress.save(_replace_checkpoint(current, status="failed", error_code=exc.code))
        return _error_report(inputs, started, "migrate", exc, checkpoint=progress.load() if progress is not None else None)
    except Exception:
        return _error_report(inputs, started, "migrate", CutoverMigrationError("internal_error", exit_code=EXIT_INTERNAL_ERROR), checkpoint=progress.load() if progress is not None else None)
    finally:
        if progress is not None:
            progress.close()
        # Ordinary failure intentionally does not release the A freeze.  It is
        # recoverable only through explicit resume or abort.


def reconcile(
    inputs: MigrationInputs,
    *,
    lease_ttl_seconds: int = DEFAULT_LEASE_TTL_SECONDS,
    runtime: Any | None = None,
    resume: bool = False,
) -> dict[str, Any]:
    started = _timestamp()
    state: CutoverStateStore | None = None
    progress: ProgressStore | None = None
    try:
        source = ReadOnlyLegacySource(inputs.legacy_root)
        source_snapshot = source.snapshot()
        progress, checkpoint = _load_progress(inputs)
        if checkpoint is None:
            raise CutoverMigrationError("migration_checkpoint_missing", exit_code=EXIT_RECONCILIATION_FAILED)
        _check_checkpoint_identity(checkpoint, inputs, source_snapshot)
        if checkpoint.status != "completed":
            raise CutoverMigrationError("migration_not_completed", exit_code=EXIT_RECONCILIATION_FAILED)
        state = _state_store(inputs)
        if state.get_snapshot().freeze_status == "active" and not resume:
            raise CutoverMigrationError("freeze_active_resume_required", exit_code=EXIT_LEASE_LOST)
        lease, capability = _acquire_lease(state, inputs, source_snapshot, lease_ttl_seconds)
        if runtime is None:
            runtime = _create_runtime(inputs)
        target = CapabilityBoundRmTarget(runtime.service, state, capability)
        adapter = ProductionImportAdapter(source, target)
        snapshot, target_records = adapter.verification()
        source_records = source_snapshot.records
        target_ids = {getattr(item, "asset_id", None) for item in target_records}
        source_ids = set(source_records)
        summary: Counter[str] = Counter()
        blob_verified = 0
        for item in target_records:
            lease = _renew(state, lease, lease_ttl_seconds)
            asset_id = getattr(item, "asset_id", None)
            if asset_id not in source_records:
                summary["unexpected_target_asset"] += 1
                continue
            mismatches = _record_comparison(source_records[asset_id], item)
            for field_name in mismatches:
                summary[f"{field_name}_mismatch"] += 1
            expected = source_records[asset_id]
            result = adapter.verify_blob(snapshot, asset_id, expected, source.blob_bytes(asset_id))
            if not all(bool(getattr(result, name, False)) for name in ("readable", "matches_expected_sha256", "matches_expected_size", "matches_expected_bytes")):
                summary["blob_mismatch"] += 1
            else:
                blob_verified += 1
        missing = source_ids - target_ids
        if missing:
            summary["missing_target_asset"] += len(missing)
        if getattr(snapshot, "total_count", None) != len(target_records):
            summary["target_inventory_count_mismatch"] += 1
        complete = adapter.complete_verification(snapshot)
        if not bool(getattr(complete, "complete", True)):
            summary["target_verification_incomplete"] += 1
        source.assert_generation(source_snapshot)
        reconciliation_status = "passed" if not summary else "failed"
        checkpoint = _replace_checkpoint(checkpoint, reconciliation_status=reconciliation_status)
        progress.save(checkpoint)
        if reconciliation_status == "passed":
            state.transition(CutoverState.FROZEN_READY_FOR_RM_SWITCH, lease=lease, migration_identity=_migration_identity(inputs, source_snapshot))
        report = _base_report(inputs, started, status="success" if reconciliation_status == "passed" else "reconciliation_failed", exit_code=EXIT_SUCCESS if reconciliation_status == "passed" else EXIT_RECONCILIATION_FAILED)
        report.update({"phase": "reconcile", "checkpoint": checkpoint.to_dict(), "reconciliation": {"status": reconciliation_status, "expected_count": len(source_ids), "target_count": len(target_records), "missing_count": len(missing), "unexpected_count": summary.get("unexpected_target_asset", 0), "blob_verified_count": blob_verified, "fail_categories": dict(sorted(summary.items()))}, "cutover_state": _read_cutover_snapshot(inputs.state_db_path)})
        return report
    except CutoverMigrationError as exc:
        return _error_report(inputs, started, "reconcile", exc, checkpoint=progress.load() if progress is not None else None)
    except CutoverStateError as exc:
        code = "reconciliation_freeze_lost" if exc.code.startswith("freeze_") else exc.code
        return _error_report(inputs, started, "reconcile", CutoverMigrationError(code, exit_code=EXIT_LEASE_LOST if "freeze" in code else EXIT_RECONCILIATION_FAILED), checkpoint=progress.load() if progress is not None else None)
    except Exception:
        return _error_report(inputs, started, "reconcile", CutoverMigrationError("internal_error", exit_code=EXIT_INTERNAL_ERROR), checkpoint=progress.load() if progress is not None else None)
    finally:
        if progress is not None:
            progress.close()


def verify(
    inputs: MigrationInputs,
    *,
    lease_ttl_seconds: int = DEFAULT_LEASE_TTL_SECONDS,
    runtime: Any | None = None,
    resume: bool = False,
) -> dict[str, Any]:
    started = _timestamp()
    progress: ProgressStore | None = None
    try:
        source = ReadOnlyLegacySource(inputs.legacy_root)
        source_snapshot = source.snapshot()
        progress, checkpoint = _load_progress(inputs)
        if checkpoint is None:
            raise CutoverMigrationError("migration_checkpoint_missing", exit_code=EXIT_RECONCILIATION_FAILED)
        _check_checkpoint_identity(checkpoint, inputs, source_snapshot)
        if checkpoint.reconciliation_status != "passed":
            raise CutoverMigrationError("reconciliation_gate_not_passed", exit_code=EXIT_RECONCILIATION_FAILED)
        state = _state_store(inputs)
        if state.get_snapshot().freeze_status == "active" and not resume:
            raise CutoverMigrationError("freeze_active_resume_required", exit_code=EXIT_LEASE_LOST)
        lease, capability = _acquire_lease(state, inputs, source_snapshot, lease_ttl_seconds)
        if runtime is None:
            runtime = _create_runtime(inputs)
        adapter = ProductionImportAdapter(source, CapabilityBoundRmTarget(runtime.service, state, capability))
        snapshot, records = adapter.verification()
        ids = {getattr(item, "asset_id", None) for item in records}
        expected = set(source_snapshot.records)
        failures: Counter[str] = Counter()
        blob_verified = 0
        for item in records:
            asset_id = getattr(item, "asset_id", None)
            if asset_id not in expected:
                failures["unexpected_target_asset"] += 1
                continue
            result = adapter.verify_blob(snapshot, asset_id, source_snapshot.records[asset_id], source.blob_bytes(asset_id))
            if all(bool(getattr(result, name, False)) for name in ("readable", "matches_expected_sha256", "matches_expected_size", "matches_expected_bytes")):
                blob_verified += 1
            else:
                failures["blob_mismatch"] += 1
            lease = _renew(state, lease, lease_ttl_seconds)
        complete = adapter.complete_verification(snapshot)
        if not bool(getattr(complete, "complete", True)):
            failures["verification_incomplete"] += 1
        failures["missing_target_asset"] += len(expected - ids)
        db = _db_integrity(inputs.rm_root)
        if db["failures"]:
            failures["rm_db_integrity"] += db["failures"]
        status = "passed" if not failures else "failed"
        checkpoint = _replace_checkpoint(checkpoint, verification_status=status)
        progress.save(checkpoint)
        report = _base_report(inputs, started, status="success" if status == "passed" else "reconciliation_failed", exit_code=EXIT_SUCCESS if status == "passed" else EXIT_RECONCILIATION_FAILED)
        report.update({"phase": "verify", "checkpoint": checkpoint.to_dict(), "verification": {"status": status, "expected_count": len(expected), "scanned_count": len(records), "blob_verified_count": blob_verified, "fail_categories": dict(sorted(failures.items())), "db_integrity": db}, "cutover_state": _read_cutover_snapshot(inputs.state_db_path)})
        return report
    except CutoverMigrationError as exc:
        return _error_report(inputs, started, "verify", exc, checkpoint=progress.load() if progress is not None else None)
    except Exception:
        return _error_report(inputs, started, "verify", CutoverMigrationError("internal_error", exit_code=EXIT_INTERNAL_ERROR), checkpoint=progress.load() if progress is not None else None)
    finally:
        if progress is not None:
            progress.close()


async def _await_reindex(target: CapabilityBoundRmTarget, request: Any) -> Any:
    return await asyncio.to_thread(target.reindex_embeddings, request)


def reindex(
    inputs: MigrationInputs,
    *,
    max_new_index_work: int = DEFAULT_MAX_NEW_INDEX_WORK,
    lease_ttl_seconds: int = DEFAULT_LEASE_TTL_SECONDS,
    runtime: Any | None = None,
    resume: bool = False,
) -> dict[str, Any]:
    started = _timestamp()
    progress: ProgressStore | None = None
    try:
        if isinstance(max_new_index_work, bool) or not isinstance(max_new_index_work, int) or not 0 <= max_new_index_work <= MAX_EXTERNAL_WORK:
            raise CutoverMigrationError("max_new_index_work_invalid", exit_code=EXIT_WORKSPACE_INVALID)
        source = ReadOnlyLegacySource(inputs.legacy_root)
        source_snapshot = source.snapshot()
        progress, checkpoint = _load_progress(inputs)
        if checkpoint is None:
            raise CutoverMigrationError("migration_checkpoint_missing", exit_code=EXIT_MIGRATION_FAILED)
        _check_checkpoint_identity(checkpoint, inputs, source_snapshot)
        if checkpoint.reconciliation_status != "passed" or checkpoint.verification_status != "passed":
            raise CutoverMigrationError("vector_reindex_gate_not_passed", exit_code=EXIT_MIGRATION_FAILED)
        state = _state_store(inputs)
        if state.get_snapshot().freeze_status == "active" and not resume:
            raise CutoverMigrationError("freeze_active_resume_required", exit_code=EXIT_LEASE_LOST)
        lease, capability = _acquire_lease(state, inputs, source_snapshot, lease_ttl_seconds)
        if runtime is None:
            runtime = _create_runtime(inputs)
        target = CapabilityBoundRmTarget(runtime.service, state, capability)
        provider = getattr(runtime.service, "vector_provider", None)
        provider_enabled = getattr(provider, "enabled", False) is True
        provider_fingerprint = str(getattr(provider, "model_id", "disabled")) if provider is not None else "disabled"
        from remember_me.core import ReindexEmbeddingsRequest
        if max_new_index_work:
            lease = _renew(state, lease, lease_ttl_seconds)
            result = asyncio.run(_await_reindex(target, ReindexEmbeddingsRequest(asset_id="", limit=max_new_index_work)))
            indexed = int(getattr(result, "indexed", 0))
            skipped = int(getattr(result, "skipped", 0))
            failed = int(getattr(result, "failed", 0))
            scanned = int(getattr(result, "scanned", indexed + skipped + failed))
        else:
            indexed = skipped = failed = scanned = 0
        after_fingerprint = str(getattr(provider, "model_id", "disabled")) if provider is not None else "disabled"
        if after_fingerprint != provider_fingerprint:
            raise CutoverMigrationError("vector_provider_changed", exit_code=EXIT_MIGRATION_FAILED)
        target_records = 0
        try:
            target_records = int(getattr(runtime.service, "count_assets", lambda: 0)())
        except Exception:
            target_records = len(source_snapshot.records)
        eligible = len(source_snapshot.records)
        ineligible = max(0, target_records - eligible)
        readiness = "ready" if provider_enabled and failed == 0 else "keyword_only" if not provider_enabled else "failed"
        status = "passed" if readiness in {"ready", "keyword_only"} else "failed"
        checkpoint = _replace_checkpoint(checkpoint, reindex_status=status)
        progress.save(checkpoint)
        report = _base_report(inputs, started, status="success" if status == "passed" else "migration_failed", exit_code=EXIT_SUCCESS if status == "passed" else EXIT_MIGRATION_FAILED)
        report.update({"phase": "reindex", "checkpoint": checkpoint.to_dict(), "vectors": {"status": status, "readiness": readiness, "provider_enabled": provider_enabled, "provider_model_fingerprint": provider_fingerprint, "external_calls": indexed if provider_enabled else 0, "eligible_count": eligible, "ineligible_count": ineligible, "scanned_count": scanned, "indexed_count": indexed, "skipped_count": skipped, "failed_count": failed, "max_new_index_work": max_new_index_work}, "cutover_state": _read_cutover_snapshot(inputs.state_db_path)})
        return report
    except CutoverMigrationError as exc:
        return _error_report(inputs, started, "reindex", exc, checkpoint=progress.load() if progress is not None else None)
    except Exception:
        return _error_report(inputs, started, "reindex", CutoverMigrationError("internal_error", exit_code=EXIT_INTERNAL_ERROR), checkpoint=progress.load() if progress is not None else None)
    finally:
        if progress is not None:
            progress.close()


def _read_cutover_snapshot(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise CutoverMigrationError("state_db_missing", exit_code=EXIT_WORKSPACE_INVALID)
    try:
        with closing(_read_only_sqlite(path)) as connection:
            row = connection.execute("SELECT * FROM cutover_state WHERE singleton=1").fetchone()
            freeze = connection.execute("SELECT lease_id, expires_at FROM cutover_freeze WHERE singleton=1").fetchone()
            if row is None:
                raise CutoverMigrationError("state_db_corrupt", exit_code=EXIT_WORKSPACE_INVALID)
            lease_expires_at = str(freeze["expires_at"]) if freeze else None
            lease_status = row["freeze_status"]
            if lease_expires_at:
                try:
                    lease_status = "active" if datetime.fromisoformat(lease_expires_at) > _utc_now() else "expired"
                except ValueError:
                    lease_status = "ambiguous"
            return {
                "revision": int(row["revision"]),
                "state": row["state"],
                "authority": row["authority"],
                "rm_available": bool(row["rm_available"]),
                "freeze_status": lease_status,
                "lease_id": str(freeze["lease_id"]) if freeze else None,
                "lease_expires_at": lease_expires_at,
                "migration_identity_hash": _hash_json({key: row[key] for key in ("migration_key", "migration_version", "source_identity", "source_generation", "target_identity")}) if row["migration_key"] else None,
            }
    except CutoverMigrationError:
        raise
    except (sqlite3.Error, OSError, TypeError, ValueError) as exc:
        raise CutoverMigrationError("state_db_unreadable", exit_code=EXIT_WORKSPACE_INVALID) from exc


def inspect(inputs: MigrationInputs) -> dict[str, Any]:
    started = _timestamp()
    progress: ProgressStore | None = None
    try:
        state = _read_cutover_snapshot(inputs.state_db_path)
        progress, checkpoint = _load_progress(inputs, read_only=True)
        _check_checkpoint_identity(checkpoint, inputs)
        report = _base_report(inputs, started, status="success", exit_code=EXIT_SUCCESS)
        report.update({
            "phase": "inspect",
            "cutover_state": state,
            "checkpoint": checkpoint.to_dict() if checkpoint else None,
            "authority_switch_implemented": False,
            "production_access_occurred": False,
        })
        return report
    except CutoverMigrationError as exc:
        return _error_report(inputs, started, "inspect", exc, checkpoint=progress.load() if progress is not None else None)
    except Exception:
        return _error_report(inputs, started, "inspect", CutoverMigrationError("internal_error", exit_code=EXIT_INTERNAL_ERROR), checkpoint=progress.load() if progress is not None else None)
    finally:
        if progress is not None:
            progress.close()


def abort(inputs: MigrationInputs, *, reason: str, lease_ttl_seconds: int = DEFAULT_LEASE_TTL_SECONDS) -> dict[str, Any]:
    started = _timestamp()
    progress: ProgressStore | None = None
    try:
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 256:
            raise CutoverMigrationError("abort_reason_invalid", exit_code=EXIT_WORKSPACE_INVALID)
        progress, checkpoint = _load_progress(inputs)
        state = _state_store(inputs)
        snapshot = state.get_snapshot()
        if snapshot.state not in {CutoverState.FROZEN_LEGACY_MIGRATION, CutoverState.FROZEN_READY_FOR_RM_SWITCH}:
            raise CutoverMigrationError("abort_state_invalid", exit_code=EXIT_WORKSPACE_INVALID)
        if checkpoint is not None:
            progress.save(_replace_checkpoint(checkpoint, status="aborted", abort_reason=reason.strip(), error_code="operator_abort"))
        if snapshot.freeze_status == "expired" and snapshot.lease_id:
            state.recover_expired_freeze(expected_lease_id=snapshot.lease_id, target_state=CutoverState.LEGACY_AUTHORITY_RM_READY)
        elif snapshot.freeze_status == "active":
            raise CutoverMigrationError("abort_requires_active_lease", exit_code=EXIT_LEASE_LOST)
        else:
            raise CutoverMigrationError("freeze_lease_missing", exit_code=EXIT_LEASE_LOST)
        report = _base_report(inputs, started, status="success", exit_code=EXIT_SUCCESS)
        report.update({"phase": "abort", "abort_reason": reason.strip(), "cutover_state": _read_cutover_snapshot(inputs.state_db_path), "checkpoint": progress.load().to_dict() if progress.load() else None})
        return report
    except (CutoverMigrationError, CutoverStateError) as exc:
        error = exc if isinstance(exc, CutoverMigrationError) else CutoverMigrationError(exc.code, exit_code=EXIT_LEASE_LOST if "freeze" in exc.code else EXIT_WORKSPACE_INVALID)
        return _error_report(inputs, started, "abort", error, checkpoint=progress.load() if progress is not None else None)
    except Exception:
        return _error_report(inputs, started, "abort", CutoverMigrationError("internal_error", exit_code=EXIT_INTERNAL_ERROR), checkpoint=progress.load() if progress is not None else None)
    finally:
        if progress is not None:
            progress.close()


def _base_report(inputs: MigrationInputs, started: str, *, status: str, exit_code: int) -> dict[str, Any]:
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "tool": TOOL_NAME,
        "tool_version": TOOL_VERSION,
        "phase": None,
        "status": status,
        "exit_code": exit_code,
        "started_at": started,
        "completed_at": _timestamp(),
        "migration_identity_hash": _sha256_text(inputs.migration_identity),
        "migration_version": inputs.migration_version,
        "source_identity": inputs.source_identity,
        "target_identity": inputs.target_identity,
        "state_db_identity": inputs.state_db_identity,
        "freeze_lease_id": None,
        "checkpoint": None,
        "reconciliation": {"status": "not_run", "fail_categories": {}},
        "verification": {"status": "not_run", "fail_categories": {}},
        "vectors": {"status": "not_run", "readiness": "not_run"},
        "errors": [],
        "production_access_occurred": False,
        "authority_switch_implemented": False,
    }


def _error_report(inputs: MigrationInputs, started: str, phase: str, exc: CutoverMigrationError, *, checkpoint: Checkpoint | None = None) -> dict[str, Any]:
    report = _base_report(inputs, started, status="failed", exit_code=exc.exit_code)
    report.update({"phase": phase, "checkpoint": checkpoint.to_dict() if checkpoint else None, "errors": [{"code": exc.code}]})
    return report


def _print_human(report: dict[str, Any]) -> None:
    phase = report.get("phase") or "cutover"
    print("{}: {} (exit {})".format(phase, report.get("status"), report.get("exit_code")))
    checkpoint = report.get("checkpoint") or {}
    if checkpoint:
        print("checkpoint: {} processed={} imported={} skipped={}".format(checkpoint.get("status"), checkpoint.get("processed_count", 0), checkpoint.get("imported_count", 0), checkpoint.get("skipped_idempotent_count", 0)))
    for key in ("reconciliation", "verification", "vectors"):
        section = report.get(key) or {}
        if section.get("status") not in {None, "not_run"}:
            print("{}: {}".format(key, section.get("status")))
    errors = report.get("errors") or []
    if errors:
        print("errors: {}".format(", ".join(str(item.get("code")) for item in errors)))


def _inputs_from_args(args: argparse.Namespace) -> MigrationInputs:
    return MigrationInputs.create(
        legacy_root=args.legacy_root,
        rm_root=args.rm_root,
        state_db_path=args.state_db,
        report_path=args.report,
        migration_identity=args.migration_identity,
        migration_version=args.migration_version,
        expected_package_version=args.expected_package_version,
        expected_data_compatibility=args.expected_data_compatibility,
        expected_sanitizer_id=args.expected_sanitizer_id,
        expected_pillow_range=args.expected_pillow_range,
        expected_schema_version=args.expected_schema_version,
    )


def _add_common_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--legacy-root", required=True, type=Path)
    parser.add_argument("--rm-root", required=True, type=Path)
    parser.add_argument("--state-db", required=True, type=Path)
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument("--migration-identity", required=True)
    parser.add_argument("--migration-version", default=MIGRATION_VERSION, type=int)
    parser.add_argument("--expected-package-version", default=EXPECTED_PACKAGE_VERSION)
    parser.add_argument("--expected-data-compatibility", default=EXPECTED_DATA_COMPATIBILITY)
    parser.add_argument("--expected-sanitizer-id", default=EXPECTED_SANITIZER_ID)
    parser.add_argument("--expected-pillow-range", default=EXPECTED_PILLOW_RANGE)
    parser.add_argument("--expected-schema-version", default=CHECKPOINT_SCHEMA_VERSION, type=int)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=TOOL_NAME)
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name in ("preflight-local", "migrate", "reconcile", "verify", "reindex", "inspect", "abort"):
        sub = subparsers.add_parser(name)
        _add_common_arguments(sub)
        if name == "migrate":
            sub.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
            sub.add_argument("--lease-ttl-seconds", type=int, default=DEFAULT_LEASE_TTL_SECONDS)
            sub.add_argument("--max-batches", type=int)
            sub.add_argument("--resume", action="store_true")
        elif name in {"reconcile", "verify", "reindex"}:
            sub.add_argument("--lease-ttl-seconds", type=int, default=DEFAULT_LEASE_TTL_SECONDS)
            sub.add_argument("--resume", action="store_true")
        if name == "reindex":
            sub.add_argument("--max-new-index-work", type=int, default=DEFAULT_MAX_NEW_INDEX_WORK)
        if name == "abort":
            sub.add_argument("--reason", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        inputs = _inputs_from_args(args)
        if args.command == "preflight-local":
            report = preflight_local(inputs)
        elif args.command == "migrate":
            report = migrate(inputs, batch_size=args.batch_size, lease_ttl_seconds=args.lease_ttl_seconds, max_batches=args.max_batches, resume=args.resume)
        elif args.command == "reconcile":
            report = reconcile(inputs, lease_ttl_seconds=args.lease_ttl_seconds, resume=args.resume)
        elif args.command == "verify":
            report = verify(inputs, lease_ttl_seconds=args.lease_ttl_seconds, resume=args.resume)
        elif args.command == "reindex":
            report = reindex(inputs, max_new_index_work=args.max_new_index_work, lease_ttl_seconds=args.lease_ttl_seconds, resume=args.resume)
        elif args.command == "inspect":
            report = inspect(inputs)
        else:
            report = abort(inputs, reason=args.reason)
        _atomic_json_write(inputs.report_path, report)
        _print_human(report)
        return int(report.get("exit_code", EXIT_INTERNAL_ERROR))
    except CutoverMigrationError as exc:
        # Inputs may be invalid before a report path can be trusted.  Keep the
        # CLI concise and never echo paths or exception details.
        print("{}: failed (exit {})".format(exc.code, exc.exit_code), file=sys.stderr)
        return exc.exit_code
    except Exception:
        print("internal_error: failed (exit {})".format(EXIT_INTERNAL_ERROR), file=sys.stderr)
        return EXIT_INTERNAL_ERROR


__all__ = [
    "CapabilityBoundRmTarget",
    "Checkpoint",
    "CutoverMigrationError",
    "EXIT_INTERNAL_ERROR",
    "EXIT_LEASE_LOST",
    "EXIT_MIGRATION_BLOCKED",
    "EXIT_MIGRATION_FAILED",
    "EXIT_PREFLIGHT_FAILED",
    "EXIT_RECONCILIATION_FAILED",
    "EXIT_SOURCE_CHANGED",
    "EXIT_SUCCESS",
    "EXIT_WORKSPACE_INVALID",
    "MigrationInputs",
    "ProgressStore",
    "ReadOnlyLegacySource",
    "abort",
    "build_parser",
    "inspect",
    "main",
    "migrate",
    "preflight_local",
    "reconcile",
    "reindex",
    "verify",
]


if __name__ == "__main__":
    raise SystemExit(main())

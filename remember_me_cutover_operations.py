"""Operational safety primitives for the Remember-Me production cutover.

Implementation D1 deliberately lives outside ``server.py`` and outside the
Implementation C migration state machine.  The commands in this module are
explicit-root, fail-closed, and read-only with respect to production.  They
can create an operator-selected backup or isolated rehearsal roots, but they
never select an authority, acquire a live freeze, call an embedding provider,
or start a service.
"""

from __future__ import annotations

import argparse
from collections import Counter
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile
from typing import Any, Callable, Iterable, Mapping

from asset_authority import AssetAuthority
from asset_storage_layout import validate_asset_storage_layout
from remember_me_adapter import (
    EXPECTED_DATA_COMPATIBILITY,
    EXPECTED_MCP_TOOLS,
    EXPECTED_PACKAGE_VERSION,
    EXPECTED_PILLOW_RANGE,
    EXPECTED_SANITIZER_ID,
    inspect_remember_me_contract,
    validate_remember_me_contract,
)
from remember_me_cutover_migration import SOURCE_DB_NAME, ReadOnlyLegacySource


TOOL_NAME = "ombre-rm-cutover-operations"
TOOL_VERSION = "1.0.0-d1"
MANIFEST_SCHEMA_VERSION = 1
BACKUP_FORMAT_VERSION = 1
PROFILES = frozenset({"legacy-authoritative", "frozen-ready"})
SHA256_LENGTH = 64
DEFAULT_HEADROOM_BYTES = 512 * 1024 * 1024
DEFAULT_STATE_RESERVE_BYTES = 64 * 1024 * 1024
DEFAULT_VECTOR_RESERVE_BYTES = 0
TOPOLOGY_VALUES = frozenset(
    {
        "SINGLE_PROCESS_CONFIRMED",
        "MULTI_PROCESS_SUPPORTED",
        "MULTI_PROCESS_UNSAFE",
        "UNKNOWN",
    }
)


class CutoverOperationsError(RuntimeError):
    """Stable operator-facing error that never contains source data."""

    def __init__(self, code: str, *, detail: str | None = None) -> None:
        self.code = code
        self.detail = detail
        super().__init__(code)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _json_bytes(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _json_write(path: Path, payload: Mapping[str, Any]) -> None:
    path = path.expanduser().resolve(strict=False)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.d1-tmp")
    try:
        temporary.write_bytes(_json_bytes(dict(payload)) + b"\n")
        os.replace(temporary, path)
    except OSError as exc:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise CutoverOperationsError("report_write_failed") from exc


def _sha256(path: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    try:
        with path.open("rb") as stream:
            while True:
                block = stream.read(1024 * 1024)
                if not block:
                    break
                digest.update(block)
                size += len(block)
    except OSError as exc:
        raise CutoverOperationsError("file_unreadable") from exc
    return size, digest.hexdigest()


def _canonical_path(value: str | Path, code: str) -> Path:
    if isinstance(value, bool):
        raise CutoverOperationsError(code)
    try:
        candidate = Path(value).expanduser()
        if not candidate.is_absolute():
            raise CutoverOperationsError(f"{code}_not_absolute")
        if _has_symlink_component(candidate):
            raise CutoverOperationsError(f"{code}_symlink_unsupported")
        resolved = candidate.resolve(strict=False)
    except CutoverOperationsError:
        raise
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        raise CutoverOperationsError(code) from exc
    if resolved == Path(resolved.anchor):
        raise CutoverOperationsError(code)
    return resolved


def _is_within(parent: Path, candidate: Path, *, strict: bool = False) -> bool:
    try:
        relative = candidate.resolve(strict=False).relative_to(parent.resolve(strict=False))
    except (OSError, RuntimeError, ValueError):
        return False
    return bool(relative.parts) if strict else True


def _has_symlink_component(path: Path) -> bool:
    current = Path(path.anchor)
    try:
        for part in path.parts[1:]:
            current /= part
            if current.is_symlink():
                return True
    except OSError:
        return True
    return False


def _validate_managed_roots(
    legacy_root: str | Path,
    rm_root: str | Path,
    state_root: str | Path,
) -> tuple[Path, Path, Path]:
    legacy = _canonical_path(legacy_root, "legacy_root")
    rm = _canonical_path(rm_root, "rm_root")
    state = _canonical_path(state_root, "state_root")
    if any(_has_symlink_component(item) for item in (legacy, rm, state)):
        raise CutoverOperationsError("managed_root_symlink_unsupported")
    try:
        validate_asset_storage_layout(legacy, rm, state)
    except Exception as exc:
        raise CutoverOperationsError("storage_layout_invalid") from exc
    return legacy, rm, state


def _validate_new_root(path: str | Path, code: str) -> Path:
    candidate = _canonical_path(path, code)
    if _has_symlink_component(candidate):
        raise CutoverOperationsError(f"{code}_symlink_unsupported")
    if candidate.exists():
        if not candidate.is_dir():
            raise CutoverOperationsError(f"{code}_not_directory")
        try:
            if any(candidate.iterdir()):
                raise CutoverOperationsError(f"{code}_not_empty")
        except OSError as exc:
            raise CutoverOperationsError(f"{code}_unreadable") from exc
    return candidate


def _relative(path: Path, root: Path) -> str:
    return path.resolve(strict=False).relative_to(root.resolve(strict=False)).as_posix()


def _safe_relative(value: str) -> Path:
    if not isinstance(value, str):
        raise CutoverOperationsError("manifest_path_invalid")
    candidate = Path(value)
    if candidate.is_absolute() or ".." in candidate.parts or not value:
        raise CutoverOperationsError("manifest_path_invalid")
    return candidate


def _iter_regular_files(root: Path) -> Iterable[tuple[Path, str]]:
    if not root.exists():
        return ()
    if root.is_symlink():
        raise CutoverOperationsError("managed_root_symlink_unsupported")
    found: list[tuple[Path, str]] = []
    try:
        for current, dirs, files in os.walk(root, topdown=True, followlinks=False):
            current_path = Path(current)
            dirs[:] = sorted(dirs)
            for directory in list(dirs):
                if (current_path / directory).is_symlink():
                    raise CutoverOperationsError("managed_root_symlink_unsupported")
            for name in sorted(files):
                source = current_path / name
                if source.is_symlink() or not source.is_file():
                    raise CutoverOperationsError("managed_file_invalid")
                found.append((source, _relative(source, root)))
    except OSError as exc:
        raise CutoverOperationsError("managed_root_unreadable") from exc
    return tuple(found)


def _secret_material(relative: str) -> bool:
    lowered = relative.lower()
    name = Path(lowered).name
    return (
        name in {".env", ".env.local", "credentials", "credentials.json"}
        or any(token in name for token in ("token", "secret", "password"))
        or name.endswith((".pem", ".key", ".p12", ".pfx"))
    )


def _sqlite_path(path: Path) -> str:
    return f"{path.as_uri()}?mode=ro"


def _sqlite_info(path: Path, *, digest: bool = True) -> dict[str, Any]:
    if not path.is_file() or path.is_symlink():
        raise CutoverOperationsError("sqlite_missing")
    try:
        with closing(sqlite3.connect(_sqlite_path(path), uri=True, timeout=5)) as connection:
            connection.execute("PRAGMA query_only = ON")
            quick = connection.execute("PRAGMA quick_check").fetchone()[0]
            if quick != "ok":
                raise CutoverOperationsError("sqlite_integrity_failed")
            tables = [
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' "
                    "AND name NOT LIKE 'sqlite_%' ORDER BY name"
                )
            ]
            counts: dict[str, int] = {}
            for table in tables:
                safe = table.replace('"', '""')
                counts[table] = int(
                    connection.execute(f'SELECT count(*) FROM "{safe}"').fetchone()[0]
                )
            schema = [
                list(row)
                for row in connection.execute(
                    "SELECT type,name,tbl_name,sql FROM sqlite_master "
                    "WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name,tbl_name"
                )
            ]
            page_size = int(connection.execute("PRAGMA page_size").fetchone()[0])
            page_count = int(connection.execute("PRAGMA page_count").fetchone()[0])
            user_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    except CutoverOperationsError:
        raise
    except (OSError, sqlite3.Error, TypeError, ValueError) as exc:
        raise CutoverOperationsError("sqlite_unreadable") from exc
    size, file_digest = _sha256(path) if digest else (path.stat().st_size, None)
    return {
        "path": path.name,
        "size_bytes": size,
        "sha256": file_digest,
        "quick_check": "ok",
        "page_size": page_size,
        "page_count": page_count,
        "user_version": user_version,
        "tables": tables,
        "row_counts": counts,
        "schema_sha256": hashlib.sha256(_json_bytes(schema)).hexdigest(),
    }


def _snapshot_sqlite(source: Path, destination: Path) -> dict[str, Any]:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        raise CutoverOperationsError("backup_destination_collision")
    try:
        with closing(sqlite3.connect(_sqlite_path(source), uri=True, timeout=5)) as source_conn:
            source_conn.execute("PRAGMA query_only = ON")
            with closing(sqlite3.connect(destination)) as target_conn:
                source_conn.backup(target_conn, pages=64)
                target_conn.execute("PRAGMA journal_mode = DELETE")
                target_conn.commit()
    except (OSError, sqlite3.Error, TypeError, ValueError) as exc:
        raise CutoverOperationsError("sqlite_snapshot_failed") from exc
    info = _sqlite_info(destination)
    info["snapshot_method"] = "sqlite_backup_api"
    return info


def _copy_file(source: Path, destination: Path) -> dict[str, Any]:
    if destination.exists():
        raise CutoverOperationsError("backup_destination_collision")
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        shutil.copyfile(source, destination)
    except OSError as exc:
        raise CutoverOperationsError("file_copy_failed") from exc
    size, digest = _sha256(destination)
    return {"size_bytes": size, "sha256": digest, "copy_method": "streamed_file_copy"}


def _asset_blob_records(db_path: Path) -> tuple[list[dict[str, Any]], str | None]:
    if not db_path.is_file():
        return [], None
    try:
        with closing(sqlite3.connect(_sqlite_path(db_path), uri=True, timeout=5)) as connection:
            connection.row_factory = sqlite3.Row
            tables = {
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            if "assets" not in tables:
                return [], None
            columns = {
                str(row[1])
                for row in connection.execute("PRAGMA table_info(assets)")
            }
            required = {"asset_id", "stored_relpath", "stored_bytes", "stored_sha256"}
            if not required.issubset(columns):
                return [], None
            rows = connection.execute(
                "SELECT asset_id,stored_relpath,stored_bytes,stored_sha256 "
                "FROM assets ORDER BY asset_id"
            ).fetchall()
    except (OSError, sqlite3.Error, TypeError, ValueError) as exc:
        raise CutoverOperationsError("asset_records_unreadable") from exc
    return [
        {
            "asset_id": str(row[0]),
            "relative_path": str(row[1]).replace("\\", "/"),
            "size_bytes": int(row[2]),
            "sha256": str(row[3]),
        }
        for row in rows
    ], "assets"


def _blob_audit(root: Path, db_path: Path) -> dict[str, Any]:
    assets_root = root / "assets"
    records, table = _asset_blob_records(db_path)
    referenced: dict[str, dict[str, Any]] = {}
    invalid_records: list[str] = []
    for record in records:
        relative = record["relative_path"]
        candidate = Path(relative)
        if candidate.is_absolute() or ".." in candidate.parts or not relative.startswith("assets/"):
            invalid_records.append(record["asset_id"])
            continue
        referenced[relative] = record
    actual: dict[str, dict[str, Any]] = {}
    if assets_root.exists():
        for source, relative in _iter_regular_files(assets_root):
            if ".tmp" in Path(relative).parts:
                continue
            full_relative = f"assets/{relative}"
            size, digest = _sha256(source)
            actual[full_relative] = {"size_bytes": size, "sha256": digest}
    missing = sorted(set(referenced) - set(actual))
    unexpected = sorted(set(actual) - set(referenced)) if table else sorted(actual)
    mismatched = sorted(
        relative
        for relative in set(referenced).intersection(actual)
        if referenced[relative]["size_bytes"] != actual[relative]["size_bytes"]
        or referenced[relative]["sha256"] != actual[relative]["sha256"]
    )
    blobs = []
    for relative in sorted(set(referenced).union(actual)):
        record = referenced.get(relative, {})
        observed = actual.get(relative, {})
        blobs.append(
            {
                "asset_id": record.get("asset_id"),
                "relative_path": relative,
                "size_bytes": observed.get("size_bytes", record.get("size_bytes")),
                "sha256": observed.get("sha256", record.get("sha256")),
                "status": (
                    "ok" if relative in referenced and relative in actual
                    and relative not in mismatched else
                    "missing" if relative in missing else
                    "unexpected" if relative in unexpected else "invalid"
                ),
            }
        )
    return {
        "asset_table": table,
        "referenced_count": len(referenced),
        "actual_count": len(actual),
        "missing": missing,
        "unexpected": unexpected,
        "mismatched": mismatched,
        "invalid_records": invalid_records,
        "blobs": blobs,
        "status": "PASS" if not (missing or unexpected or mismatched or invalid_records) else "FAIL",
    }


def _component_files(
    source_root: Path,
    namespace: str,
    destination_root: Path,
    *,
    legacy_scope: bool = False,
) -> tuple[list[dict[str, Any]], list[dict[str, str]], list[dict[str, Any]]]:
    """Capture one component, snapshotting every SQLite file through SQLite."""
    entries: list[dict[str, Any]] = []
    exclusions: list[dict[str, str]] = []
    sqlite_infos: list[dict[str, Any]] = []
    if not source_root.exists():
        return entries, exclusions, sqlite_infos
    candidates = list(_iter_regular_files(source_root))
    if legacy_scope:
        candidates = [
            item for item in candidates
            if not item[1].startswith(("state/", "remember-me/"))
            and (item[1] == SOURCE_DB_NAME or item[1].startswith("assets/")
            or item[1].lower().endswith((".sqlite", ".sqlite3", ".db"))
            )
        ]
    sqlite_suffixes = (".sqlite", ".sqlite3", ".db")
    for source, relative in candidates:
        if _secret_material(relative):
            exclusions.append({"relative_path": f"{namespace}/{relative}", "reason": "credential_material"})
            continue
        if relative.endswith(("-wal", "-shm", "-journal")):
            exclusions.append({"relative_path": f"{namespace}/{relative}", "reason": "sqlite_sidecar"})
            continue
        destination = destination_root / namespace / Path(*Path(relative).parts)
        if source.suffix.lower() in sqlite_suffixes:
            info = _snapshot_sqlite(source, destination)
            info["source_relative_path"] = relative
            sqlite_infos.append(info)
            entries.append(
                {
                    "relative_path": f"{namespace}/{relative}",
                    "entry_type": "sqlite_snapshot",
                    "category": "sqlite",
                    "size_bytes": info["size_bytes"],
                    "sha256": info["sha256"],
                    "page_size": info["page_size"],
                    "page_count": info["page_count"],
                    "user_version": info["user_version"],
                    "schema_sha256": info["schema_sha256"],
                }
            )
        else:
            copied = _copy_file(source, destination)
            entries.append(
                {
                    "relative_path": f"{namespace}/{relative}",
                    "entry_type": "regular",
                    "category": "blob" if relative.startswith("assets/") else "metadata",
                    "size_bytes": copied["size_bytes"],
                    "sha256": copied["sha256"],
                }
            )
    return entries, exclusions, sqlite_infos


def _read_state_snapshot(state_db: Path) -> dict[str, Any]:
    if not state_db.is_file():
        raise CutoverOperationsError("state_db_missing")
    try:
        with closing(sqlite3.connect(_sqlite_path(state_db), uri=True, timeout=5)) as connection:
            connection.row_factory = sqlite3.Row
            if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise CutoverOperationsError("state_integrity_failed")
            row = connection.execute(
                "SELECT * FROM cutover_state WHERE singleton=1"
            ).fetchone()
            freeze = connection.execute(
                "SELECT lease_id,expires_at FROM cutover_freeze WHERE singleton=1"
            ).fetchone()
            if row is None:
                raise CutoverOperationsError("state_schema_invalid")
            expires = str(freeze["expires_at"]) if freeze else None
            status = str(row["freeze_status"])
            if expires:
                try:
                    status = "active" if datetime.fromisoformat(expires) > datetime.now(timezone.utc) else "expired"
                except ValueError:
                    status = "ambiguous"
            identity = {
                key: row[key]
                for key in (
                    "migration_key", "migration_version", "source_identity",
                    "source_generation", "target_identity",
                )
            }
            return {
                "revision": int(row["revision"]),
                "state": str(row["state"]),
                "authority": str(row["authority"]),
                "rm_available": bool(row["rm_available"]),
                "freeze_status": status,
                "lease_id_present": bool(freeze and freeze["lease_id"]),
                "lease_expires_at_present": bool(expires),
                "migration_identity_hash": hashlib.sha256(_json_bytes(identity)).hexdigest()
                if row["migration_key"] else None,
            }
    except CutoverOperationsError:
        raise
    except (OSError, sqlite3.Error, TypeError, ValueError) as exc:
        raise CutoverOperationsError("state_unreadable") from exc


def _contract_info() -> dict[str, Any]:
    try:
        actual = validate_remember_me_contract(inspect_remember_me_contract())
        return {
            "status": "PASS",
            "distribution": actual.distribution_name,
            "package_version": actual.package_version,
            "data_compatibility": actual.data_compatibility,
            "sanitizer_id": actual.sanitizer_id,
            "pillow_range": actual.pillow_range,
            "mcp_tools_sha256": hashlib.sha256(_json_bytes(list(actual.mcp_tools))).hexdigest(),
            "mcp_tool_count": len(actual.mcp_tools),
            "expected": {
                "package_version": EXPECTED_PACKAGE_VERSION,
                "data_compatibility": EXPECTED_DATA_COMPATIBILITY,
                "sanitizer_id": EXPECTED_SANITIZER_ID,
                "pillow_range": EXPECTED_PILLOW_RANGE,
                "mcp_tool_count": len(EXPECTED_MCP_TOOLS),
            },
        }
    except Exception:
        return {
            "status": "FAIL",
            "package_version": None,
            "data_compatibility": None,
            "sanitizer_id": None,
            "pillow_range": None,
        }


def _manifest_digest(manifest: Mapping[str, Any]) -> str:
    without_digest = dict(manifest)
    without_digest.pop("manifest_sha256", None)
    return hashlib.sha256(_json_bytes(without_digest)).hexdigest()


def _validate_manifest(manifest: Any) -> dict[str, Any]:
    if not isinstance(manifest, dict):
        raise CutoverOperationsError("manifest_invalid")
    if manifest.get("schema_version") != MANIFEST_SCHEMA_VERSION or manifest.get("backup_format_version") != BACKUP_FORMAT_VERSION:
        raise CutoverOperationsError("manifest_version_invalid")
    digest = manifest.get("manifest_sha256")
    if not isinstance(digest, str) or len(digest) != SHA256_LENGTH or _manifest_digest(manifest) != digest:
        raise CutoverOperationsError("manifest_digest_invalid")
    for field in (
        "profile", "created_at", "source_ids", "package_contract", "state",
        "state_db_relative_path", "components", "sqlite", "exclusions",
        "blob_manifest",
    ):
        if field not in manifest:
            raise CutoverOperationsError("manifest_invalid")
    if manifest["profile"] not in PROFILES:
        raise CutoverOperationsError("manifest_profile_invalid")
    _safe_relative(manifest["state_db_relative_path"])
    entries = manifest.get("entries")
    if not isinstance(entries, list):
        raise CutoverOperationsError("manifest_entries_invalid")
    paths = []
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("entry_type") not in {"regular", "sqlite_snapshot"}:
            raise CutoverOperationsError("manifest_entries_invalid")
        path = entry.get("relative_path")
        _safe_relative(path)
        if (
            not isinstance(entry.get("size_bytes"), int)
            or isinstance(entry.get("size_bytes"), bool)
            or entry["size_bytes"] < 0
            or not isinstance(entry.get("category"), str)
        ):
            raise CutoverOperationsError("manifest_entries_invalid")
        if not isinstance(entry.get("sha256"), str) or len(entry["sha256"]) != SHA256_LENGTH:
            raise CutoverOperationsError("manifest_entries_invalid")
        if entry["entry_type"] == "sqlite_snapshot" and any(
            not isinstance(entry.get(field), int) or isinstance(entry.get(field), bool)
            or entry[field] < 0
            for field in ("page_size", "page_count", "user_version")
        ):
            raise CutoverOperationsError("manifest_entries_invalid")
        paths.append(path)
    if paths != sorted(paths) or len(paths) != len(set(paths)):
        raise CutoverOperationsError("manifest_entries_invalid")
    return manifest


def _load_manifest(backup_root: Path) -> dict[str, Any]:
    path = backup_root / "manifest.json"
    try:
        raw = path.read_bytes()
        manifest = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CutoverOperationsError("manifest_unreadable") from exc
    if raw != _json_bytes(manifest) + b"\n":
        raise CutoverOperationsError("manifest_not_canonical")
    return _validate_manifest(manifest)


def verify_backup(backup_root: str | Path) -> dict[str, Any]:
    root = _canonical_path(backup_root, "backup_root")
    manifest = _load_manifest(root)
    failures: list[str] = []
    for entry in manifest["entries"]:
        target = root / _safe_relative(entry["relative_path"])
        if not target.is_file() or target.is_symlink():
            failures.append("missing:" + entry["relative_path"])
            continue
        size, digest = _sha256(target)
        if size != entry["size_bytes"] or digest != entry["sha256"]:
            failures.append("hash:" + entry["relative_path"])
        if entry["entry_type"] == "sqlite_snapshot":
            try:
                info = _sqlite_info(target)
                for key in ("page_size", "page_count", "user_version", "schema_sha256"):
                    if info[key] != entry[key]:
                        failures.append("sqlite:" + entry["relative_path"])
                        break
            except CutoverOperationsError:
                failures.append("sqlite:" + entry["relative_path"])
    for blob in manifest.get("blob_manifest", []):
        if blob.get("status") != "ok":
            failures.append("blob:" + str(blob.get("relative_path", "unknown")))
    return {
        "status": "PASS" if not failures else "FAIL",
        "backup_root": str(root),
        "manifest_sha256": manifest["manifest_sha256"],
        "entry_count": len(manifest["entries"]),
        "failures": failures,
    }


def _root_bytes(root: Path) -> int:
    total = 0
    if not root.exists():
        return 0
    for source, _ in _iter_regular_files(root):
        try:
            total += source.stat().st_size
        except OSError as exc:
            raise CutoverOperationsError("managed_root_unreadable") from exc
    return total


def _source_summary(root: Path, namespace: str) -> dict[str, Any]:
    database = root / SOURCE_DB_NAME
    sqlite_files = sorted(
        path for path, relative in _iter_regular_files(root)
        if path.suffix.lower() in {".sqlite", ".sqlite3", ".db"}
        and not relative.endswith(("-wal", "-shm", "-journal"))
    ) if root.exists() else []
    db_info = _sqlite_info(database) if database.is_file() else None
    blob = _blob_audit(root, database) if root.exists() else {
        "status": "FAIL", "missing": ["assets/"], "unexpected": [],
        "mismatched": [], "invalid_records": [], "blobs": [],
        "referenced_count": 0, "actual_count": 0, "asset_table": None,
    }
    return {
        "namespace": namespace,
        "present": root.is_dir(),
        "root_bytes": _root_bytes(root),
        "database": db_info,
        "sqlite_files": [path.name for path in sqlite_files],
        "blob_manifest": blob,
        "asset_count": int((db_info or {}).get("row_counts", {}).get("assets", 0)),
    }


def _target_classification(rm: dict[str, Any], state: dict[str, Any] | None) -> str:
    if not rm["present"] or (rm["asset_count"] == 0 and not rm["sqlite_files"]):
        return "empty"
    if state and state.get("state") in {"frozen_legacy_migration", "frozen_ready_for_rm_switch"}:
        return "resumable_partial"
    if rm["asset_count"] and state and state.get("rm_available"):
        return "completed_or_ready"
    return "conflicting"


def classify_topology(
    *,
    worker_count: int | None = None,
    multiprocess: bool | None = None,
    shared_state: bool | None = None,
    service_instances: int | None = None,
) -> dict[str, Any]:
    """Classify only from explicit operator evidence; never infer safety."""
    if any(value is not None and isinstance(value, bool) for value in (worker_count, service_instances)):
        return {"classification": "UNKNOWN", "reason": "invalid_topology_evidence"}
    if multiprocess is True and shared_state is False:
        classification = "MULTI_PROCESS_UNSAFE"
    elif multiprocess is True and shared_state is True:
        classification = "MULTI_PROCESS_SUPPORTED"
    elif multiprocess is False and worker_count == 1 and (service_instances in (None, 1)):
        classification = "SINGLE_PROCESS_CONFIRMED"
    else:
        classification = "UNKNOWN"
    return {
        "classification": classification,
        "worker_count": worker_count,
        "multiprocess": multiprocess,
        "shared_state": shared_state,
        "service_instances": service_instances,
        "evidence_required": True,
    }


def _vector_readiness(
    rm_root: Path,
    *,
    embedding_enabled: str = "unknown",
    expected_model_id: str | None = None,
) -> dict[str, Any]:
    if embedding_enabled == "false":
        return {
            "status": "KEYWORD_ONLY",
            "external_calls": 0,
            "vector_count": 0,
            "model_ids": [],
            "expected_model_id": expected_model_id,
            "provider_calls": False,
        }
    database = rm_root / SOURCE_DB_NAME
    if not database.is_file():
        return {"status": "UNKNOWN", "external_calls": 0, "reason": "rm_database_missing"}
    models: Counter[str] = Counter()
    vector_count = 0
    try:
        with closing(sqlite3.connect(_sqlite_path(database), uri=True, timeout=5)) as connection:
            tables = [row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )]
            for table in tables:
                columns = {row[1] for row in connection.execute(f'PRAGMA table_info("{str(table).replace(chr(34), chr(34) * 2)}")')}
                model_column = "model" if "model" in columns else "model_id" if "model_id" in columns else None
                vector_column = "embedding" if "embedding" in columns else "vector" if "vector" in columns else None
                if model_column and vector_column:
                    safe = str(table).replace('"', '""')
                    for row in connection.execute(f'SELECT "{model_column}", count(*) FROM "{safe}" GROUP BY "{model_column}"'):
                        models[str(row[0])] += int(row[1])
                    vector_count += sum(models.values()) - vector_count
    except (OSError, sqlite3.Error, TypeError, ValueError):
        return {"status": "UNKNOWN", "external_calls": 0, "reason": "vector_schema_unreadable"}
    if embedding_enabled != "true":
        status = "UNKNOWN"
    elif expected_model_id and vector_count > 0 and set(models) == {expected_model_id}:
        status = "READY"
    else:
        status = "NOT_READY"
    return {
        "status": status,
        "external_calls": 0,
        "vector_count": vector_count,
        "model_ids": sorted(models),
        "expected_model_id": expected_model_id,
        "provider_calls": False,
    }


def _disk_evidence(
    roots: tuple[Path, Path, Path],
    *,
    estimated_vector_bytes: int | None = None,
    headroom_bytes: int = DEFAULT_HEADROOM_BYTES,
) -> dict[str, Any]:
    legacy, rm, state = roots
    try:
        usage = shutil.disk_usage(legacy)
    except OSError as exc:
        raise CutoverOperationsError("disk_usage_unavailable") from exc
    owned = {"legacy": _root_bytes(legacy), "remember_me": _root_bytes(rm), "state": _root_bytes(state)}
    estimated_copy = owned["legacy"] + owned["state"] + DEFAULT_STATE_RESERVE_BYTES
    vector_uncertainty = estimated_vector_bytes is None
    estimated_vector = DEFAULT_VECTOR_RESERVE_BYTES if vector_uncertainty else max(0, estimated_vector_bytes)
    required_min = estimated_copy + estimated_vector + max(0, headroom_bytes)
    return {
        "total_bytes": usage.total,
        "free_bytes": usage.free,
        "used_bytes": usage.used,
        "owned_bytes": owned,
        "estimated_rm_copy_bytes": estimated_copy,
        "estimated_vector_bytes": estimated_vector,
        "vector_space_uncertain": vector_uncertainty,
        "safety_headroom_bytes": max(0, headroom_bytes),
        "required_min_bytes": required_min,
        "status": "PASS" if required_min <= usage.free else "FAIL",
    }


def preflight(
    *,
    legacy_root: str | Path,
    rm_root: str | Path,
    state_db: str | Path,
    report: str | Path | None = None,
    backup_root: str | Path | None = None,
    embedding_enabled: str = "unknown",
    expected_model_id: str | None = None,
    estimated_vector_bytes: int | None = None,
    headroom_bytes: int = DEFAULT_HEADROOM_BYTES,
    topology: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Run a read-only local production preflight; no runtime is opened."""
    started = _utc_now()
    legacy, rm, state_root = _validate_managed_roots(
        legacy_root, rm_root, Path(state_db).expanduser().resolve(strict=False).parent
    )
    state_path = _canonical_path(state_db, "state_db")
    state_info = _read_state_snapshot(state_path)
    legacy_info = _source_summary(legacy, "legacy")
    rm_info = _source_summary(rm, "remember-me")
    contract = _contract_info()
    vector = _vector_readiness(
        rm, embedding_enabled=embedding_enabled, expected_model_id=expected_model_id
    )
    topo = classify_topology(**dict(topology or {}))
    disk = _disk_evidence(
        (legacy, rm, state_root),
        estimated_vector_bytes=estimated_vector_bytes,
        headroom_bytes=headroom_bytes,
    )
    backup_info = {"status": "NOT_PROVIDED"}
    if backup_root is not None:
        backup_info = verify_backup(backup_root)
    state_name = state_info.get("state")
    migration_complete = state_name in {"frozen_ready_for_rm_switch", "frozen_rm_acceptance", "rm_authority_open"}
    payload: dict[str, Any] = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "tool": TOOL_NAME,
        "tool_version": TOOL_VERSION,
        "phase": "preflight",
        "started_at": started,
        "completed_at": _utc_now(),
        "read_only": True,
        "external_calls": 0,
        "production_access_occurred": False,
        "authority_switch_implemented": False,
        "roots": {"legacy": str(legacy), "remember_me": str(rm), "state": str(state_root)},
        "target_classification": _target_classification(rm_info, state_info),
        "legacy": legacy_info,
        "remember_me": rm_info,
        "state": state_info,
        "contract": contract,
        "vectors": vector,
        "disk": disk,
        "topology": topo,
        "backup": backup_info,
        "gates": {
            "storage_layout": True,
            "state_healthy": True,
            "legacy_authority_active": state_info.get("authority") == AssetAuthority.LEGACY.value,
            "freeze_held": state_info.get("freeze_status") == "active",
            "migration_complete": migration_complete,
            "contract_exact": contract.get("status") == "PASS",
            "legacy_blobs_consistent": legacy_info["blob_manifest"]["status"] == "PASS",
            "rm_blobs_consistent": rm_info["blob_manifest"]["status"] == "PASS" if rm_info["present"] else True,
            "disk_acceptable": disk["status"] == "PASS",
            "topology_safe": topo["classification"] in {"SINGLE_PROCESS_CONFIRMED", "MULTI_PROCESS_SUPPORTED"},
        },
    }
    if report is not None:
        _json_write(_canonical_path(report, "report"), payload)
    return payload


def _safe_destination(backup_root: Path, roots: tuple[Path, Path, Path]) -> None:
    if backup_root.exists():
        raise CutoverOperationsError("backup_destination_exists")
    if _has_symlink_component(backup_root):
        raise CutoverOperationsError("backup_destination_symlink_unsupported")
    if any(_is_within(root, backup_root) or _is_within(backup_root, root) for root in roots):
        raise CutoverOperationsError("backup_destination_managed_root_collision")


def create_backup(
    *,
    profile: str,
    legacy_root: str | Path,
    rm_root: str | Path,
    state_db: str | Path,
    destination: str | Path,
    report: str | Path | None = None,
) -> dict[str, Any]:
    if profile not in PROFILES:
        raise CutoverOperationsError("backup_profile_invalid")
    state_path = _canonical_path(state_db, "state_db")
    roots = _validate_managed_roots(legacy_root, rm_root, state_path.parent)
    legacy, rm, state_root = roots
    if not legacy.is_dir() or not state_path.is_file():
        raise CutoverOperationsError("backup_source_missing")
    if profile == "frozen-ready" and not rm.is_dir():
        raise CutoverOperationsError("rm_source_missing")
    backup_root = _canonical_path(destination, "backup_destination")
    _safe_destination(backup_root, roots)
    parent = backup_root.parent
    parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".rm-cutover-d1-", dir=parent))
    try:
        for namespace in ("legacy", "remember-me", "state", "reports"):
            (staging / namespace).mkdir(parents=True, exist_ok=True)
        entries: list[dict[str, Any]] = []
        exclusions: list[dict[str, str]] = []
        sqlite_infos: list[dict[str, Any]] = []
        for source, namespace, legacy_scope in (
            (legacy, "legacy", True),
            (rm, "remember-me", False),
            (state_root, "state", False),
        ):
            component_entries, component_exclusions, component_sqlite = _component_files(
                source, namespace, staging, legacy_scope=legacy_scope
            )
            entries.extend(component_entries)
            exclusions.extend(component_exclusions)
            sqlite_infos.extend(component_sqlite)
        entries.sort(key=lambda item: item["relative_path"])
        exclusions.sort(key=lambda item: item["relative_path"])
        blob_manifest: list[dict[str, Any]] = []
        for namespace, source, db in (
            ("legacy", legacy, legacy / SOURCE_DB_NAME),
            ("remember-me", rm, rm / SOURCE_DB_NAME),
        ):
            audit = _blob_audit(source, db) if source.exists() else {"status": "PASS", "blobs": []}
            if audit.get("status") == "FAIL":
                raise CutoverOperationsError(f"{namespace.replace('-', '_')}_blob_audit_failed")
            for blob in audit.get("blobs", []):
                blob = dict(blob)
                blob["namespace"] = namespace
                blob_manifest.append(blob)
        state_info = _read_state_snapshot(state_path)
        contract = _contract_info()
        if contract["status"] != "PASS":
            raise CutoverOperationsError("remember_me_contract_invalid")
        manifest: dict[str, Any] = {
            "schema_version": MANIFEST_SCHEMA_VERSION,
            "backup_format_version": BACKUP_FORMAT_VERSION,
            "tool": TOOL_NAME,
            "tool_version": TOOL_VERSION,
            "profile": profile,
            "created_at": _utc_now(),
            "source_ids": {
                "legacy": hashlib.sha256(str(legacy).encode()).hexdigest(),
                "remember_me": hashlib.sha256(str(rm).encode()).hexdigest(),
                "state": hashlib.sha256(str(state_root).encode()).hexdigest(),
            },
            "package_contract": contract,
            "state": state_info,
            "state_db_relative_path": _relative(state_path, state_root),
            "components": {
                "legacy": {"present": legacy.is_dir(), "bytes": _root_bytes(legacy)},
                "remember_me": {"present": rm.is_dir(), "bytes": _root_bytes(rm)},
                "state": {"present": state_root.is_dir(), "bytes": _root_bytes(state_root)},
            },
            "sqlite": sorted(sqlite_infos, key=lambda item: item["source_relative_path"]),
            "entries": entries,
            "exclusions": exclusions,
            "blob_manifest": sorted(blob_manifest, key=lambda item: (item.get("namespace", ""), item.get("relative_path", ""))),
            "cutover_state": {
                "authority": state_info.get("authority"),
                "state": state_info.get("state"),
                "freeze_status": state_info.get("freeze_status"),
                "migration_identity_hash": state_info.get("migration_identity_hash"),
            },
        }
        manifest["manifest_sha256"] = _manifest_digest(manifest)
        (staging / "manifest.json").write_bytes(_json_bytes(manifest) + b"\n")
        # Windows does not reliably support replacing a directory with a
        # newly named directory.  The destination was checked above and
        # copytree still refuses a concurrent destination collision.
        shutil.copytree(staging, backup_root)
        shutil.rmtree(staging)
        staging = None  # type: ignore[assignment]
        verification = verify_backup(backup_root)
        result = {
            "schema_version": MANIFEST_SCHEMA_VERSION,
            "tool": TOOL_NAME,
            "tool_version": TOOL_VERSION,
            "phase": "backup",
            "status": verification["status"],
            "profile": profile,
            "backup_root": str(backup_root),
            "manifest_sha256": manifest["manifest_sha256"],
            "entry_count": len(entries),
            "blob_count": len(blob_manifest),
            "verification": verification,
            "production_access_occurred": False,
            "authority_switch_implemented": False,
        }
        if report is not None:
            _json_write(_canonical_path(report, "report"), result)
        return result
    except CutoverOperationsError:
        raise
    except (OSError, ValueError, TypeError) as exc:
        raise CutoverOperationsError("backup_failed") from exc
    finally:
        if staging is not None and staging.exists():
            shutil.rmtree(staging, ignore_errors=True)


def restore_backup(
    *,
    backup_root: str | Path,
    legacy_root: str | Path,
    rm_root: str | Path,
    state_root: str | Path,
    report: str | Path | None = None,
) -> dict[str, Any]:
    source = _canonical_path(backup_root, "backup_root")
    manifest = _load_manifest(source)
    destinations = (
        _validate_new_root(legacy_root, "legacy_restore_root"),
        _validate_new_root(rm_root, "rm_restore_root"),
        _validate_new_root(state_root, "state_restore_root"),
    )
    if len(set(destinations)) != 3:
        raise CutoverOperationsError("restore_roots_overlap")
    if any(_is_within(source, path) or _is_within(path, source) for path in destinations):
        raise CutoverOperationsError("restore_backup_collision")
    namespace_roots = {"legacy": destinations[0], "remember-me": destinations[1], "state": destinations[2]}
    for path in destinations:
        path.mkdir(parents=True, exist_ok=True)
    try:
        for entry in manifest["entries"]:
            relative = _safe_relative(entry["relative_path"])
            namespace = relative.parts[0]
            if namespace not in namespace_roots:
                raise CutoverOperationsError("manifest_namespace_invalid")
            source_file = source / relative
            target = namespace_roots[namespace] / Path(*relative.parts[1:])
            if not source_file.is_file() or target.exists():
                raise CutoverOperationsError("restore_file_invalid")
            copied = _copy_file(source_file, target)
            if copied["sha256"] != entry["sha256"] or copied["size_bytes"] != entry["size_bytes"]:
                raise CutoverOperationsError("restore_hash_mismatch")
        state_relative = _safe_relative(manifest["state_db_relative_path"])
        restored_state_db = destinations[2] / Path(*state_relative.parts)
        verification = verify_restored(
            manifest=manifest,
            legacy_root=destinations[0],
            rm_root=destinations[1],
            state_root=destinations[2],
            state_db=restored_state_db,
        )
        result = {
            "schema_version": MANIFEST_SCHEMA_VERSION,
            "tool": TOOL_NAME,
            "tool_version": TOOL_VERSION,
            "phase": "restore",
            "status": verification["status"],
            "source_backup": str(source),
            "restored_roots": {"legacy": str(destinations[0]), "remember_me": str(destinations[1]), "state": str(destinations[2])},
            "verification": verification,
            "production_access_occurred": False,
            "authority_switch_implemented": False,
        }
        if report is not None:
            _json_write(_canonical_path(report, "report"), result)
        return result
    except CutoverOperationsError:
        raise


def verify_restored(
    *,
    manifest: Mapping[str, Any],
    legacy_root: Path,
    rm_root: Path,
    state_root: Path,
    state_db: Path,
) -> dict[str, Any]:
    failures: list[str] = []
    for entry in manifest["entries"]:
        relative = _safe_relative(entry["relative_path"])
        roots = {"legacy": legacy_root, "remember-me": rm_root, "state": state_root}
        target = roots[relative.parts[0]] / Path(*relative.parts[1:])
        if not target.is_file() or target.is_symlink():
            failures.append("missing:" + entry["relative_path"])
            continue
        size, digest = _sha256(target)
        if size != entry["size_bytes"] or digest != entry["sha256"]:
            failures.append("hash:" + entry["relative_path"])
    try:
        legacy_reader = ReadOnlyLegacySource(legacy_root)
        legacy_count = legacy_reader.snapshot().asset_count
    except Exception:
        legacy_count = None
        failures.append("legacy_reader")
    try:
        state_info = _read_state_snapshot(state_db)
    except CutoverOperationsError:
        state_info = None
        failures.append("state_reader")
    rm_summary = _source_summary(rm_root, "remember-me")
    if rm_summary["present"] and rm_summary["database"] is not None:
        try:
            from remember_me_adapter import RememberMeAdapter
            # This is an isolated restored root only; no server/global runtime.
            runtime = RememberMeAdapter().create_runtime(rm_root)
            rm_count = len(runtime.repository.list_assets_for_search())
            rm_reader = "PASS"
        except Exception:
            rm_count = None
            rm_reader = "FAIL"
            failures.append("remember_me_reader")
    else:
        rm_count = 0
        rm_reader = "NOT_APPLICABLE"
    return {
        "status": "PASS" if not failures else "FAIL",
        "failures": failures,
        "manifest_hashes": "PASS" if not any(item.startswith(("missing:", "hash:")) for item in failures) else "FAIL",
        "sqlite_integrity": "PASS" if not any(item.endswith("reader") for item in failures) else "FAIL",
        "legacy_reader": "PASS" if legacy_count is not None else "FAIL",
        "remember_me_reader": rm_reader,
        "state_reader": "PASS" if state_info is not None else "FAIL",
        "legacy_asset_count": legacy_count,
        "remember_me_asset_count": rm_count,
        "rm_authoritative_capable": bool(rm_reader in {"PASS", "NOT_APPLICABLE"} and state_info is not None),
        "external_calls": 0,
    }


ACCEPTANCE_CHECK_NAMES = (
    "rm_runtime_healthy",
    "rm_data_root_persists",
    "rm_healthy",
    "persistence_reopen",
    "authority_consistency",
    "mcp_backend_selected",
    "dashboard_backend_selected",
    "mcp_dashboard_routing",
    "metadata_get",
    "search",
    "view",
    "inspect",
    "download",
    "dashboard_list",
    "dashboard_detail",
    "dashboard_image",
    "dashboard_thumbnail",
    "authorization_privacy",
    "mutation_freeze_active",
    "dashboard_upload_rejected",
    "dashboard_update_rejected",
    "dashboard_delete_rejected",
    "mcp_upload_rejected",
    "mcp_update_rejected",
    "public_reindex_rejected",
    "direct_ordinary_rm_write_rejected",
    "rm_failure_no_legacy_fallback",
    "legacy_not_authoritative",
    "restart_state_durable",
    # Compatibility aliases retained for the D1 acceptance callers.
    "metadata_read",
    "search_read",
    "view_read",
    "inspect_read",
    "download_read",
    "dashboard_write_rejected",
    "mcp_write_rejected",
    "direct_rm_write_rejected",
    "no_legacy_fallback",
    "no_legacy_route",
    "tickets_recreated_across_restart",
)


def acceptance_check_spec() -> dict[str, Any]:
    return {
        "status": "READY",
        "authority_switch_implemented": True,
        "requires_frozen_state": True,
        "checks": list(ACCEPTANCE_CHECK_NAMES),
        "result_states": ["PASS", "FAIL", "INCOMPLETE"],
    }


def run_frozen_acceptance_checks(
    *,
    state: Mapping[str, Any],
    checks: Mapping[str, Callable[[], Any] | bool] | None = None,
) -> dict[str, Any]:
    """Evaluate D2-invoked callbacks without changing state or routing."""
    callback_map = checks or {}
    results: dict[str, dict[str, Any]] = {}
    for name in ACCEPTANCE_CHECK_NAMES:
        if name == "authority_consistency":
            value: Any = state.get("authority") == AssetAuthority.RM.value
        elif name == "no_legacy_route":
            value = state.get("authority") == AssetAuthority.RM.value
        elif name in {"dashboard_write_rejected", "mcp_write_rejected", "direct_rm_write_rejected"}:
            value = callback_map.get(name)
        else:
            value = callback_map.get(name)
        try:
            observed = value() if callable(value) else value
        except Exception:
            observed = False
        status = "PASS" if observed is True or (isinstance(observed, Mapping) and observed.get("status") == "PASS") else "FAIL" if observed is False or (isinstance(observed, Mapping) and observed.get("status") == "FAIL") else "INCOMPLETE"
        results[name] = {"status": status}
    state_pass = state.get("authority") == AssetAuthority.RM.value and bool(state.get("frozen"))
    overall = "PASS" if state_pass and all(item["status"] == "PASS" for item in results.values()) else "FAIL" if any(item["status"] == "FAIL" for item in results.values()) or not state_pass else "INCOMPLETE"
    return {
        "status": overall,
        "checks": results,
        "state_prerequisite": "PASS" if state_pass else "FAIL",
        "authority_switch_implemented": True,
        "production_access_occurred": False,
    }


READINESS_GATES = (
    "dependency_exact",
    "storage_layout",
    "state_healthy",
    "freeze_held",
    "legacy_authority_active",
    "migration_complete",
    "reconciliation_exact",
    "verification_passed",
    "vector_profile",
    "backup_verified",
    "disk_acceptable",
    "topology_safe",
    "stale_authority_clear",
)


def evaluate_readiness(evidence: Mapping[str, Any]) -> dict[str, Any]:
    """Pure readiness decision; this function cannot perform a transition."""
    def truth(*keys: str) -> bool | None:
        value: Any = evidence
        for key in keys:
            if not isinstance(value, Mapping) or key not in value:
                return None
            value = value[key]
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.upper() in {"PASS", "READY", "YES", "TRUE", "EXACT", "COMPLETE"}
        return None

    gate_values: dict[str, bool | None] = {
        "dependency_exact": truth("dependency_exact") if "dependency_exact" in evidence else truth("contract", "status"),
        "storage_layout": truth("storage_layout") if "storage_layout" in evidence else truth("gates", "storage_layout"),
        "state_healthy": truth("state_healthy") if "state_healthy" in evidence else truth("gates", "state_healthy"),
        "freeze_held": truth("freeze_held") if "freeze_held" in evidence else truth("gates", "freeze_held"),
        "legacy_authority_active": truth("legacy_authority_active") if "legacy_authority_active" in evidence else truth("gates", "legacy_authority_active"),
        "migration_complete": truth("migration_complete") if "migration_complete" in evidence else truth("gates", "migration_complete"),
        "reconciliation_exact": truth("reconciliation_exact"),
        "verification_passed": truth("verification_passed"),
        "vector_profile": truth("vector_profile") if "vector_profile" in evidence else truth("vectors", "status"),
        "backup_verified": truth("backup_verified") if "backup_verified" in evidence else truth("backup", "status"),
        "disk_acceptable": truth("disk_acceptable") if "disk_acceptable" in evidence else truth("disk", "status"),
        "topology_safe": truth("topology_safe") if "topology_safe" in evidence else truth("gates", "topology_safe"),
        "stale_authority_clear": truth("stale_authority_clear"),
    }
    if evidence.get("required_vector_profile") == "keyword_only" and evidence.get("vectors", {}).get("status") == "KEYWORD_ONLY":
        gate_values["vector_profile"] = True
    blockers = [name for name, value in gate_values.items() if value is not True]
    result = {
        "READY_FOR_AUTHORITY_SWITCH": "YES" if not blockers else "NO",
        "status": "PASS" if not blockers else "FAIL",
        "hard_gates": {name: ("PASS" if value is True else "FAIL" if value is False else "UNKNOWN") for name, value in gate_values.items()},
        "blocking_gates": blockers,
        "authority_switch_implemented": True,
        "production_access_occurred": False,
    }
    return result


def _add_common_roots(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--legacy-root", required=True, type=Path)
    parser.add_argument("--rm-root", required=True, type=Path)
    parser.add_argument("--state-db", required=True, type=Path)


def _main_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=TOOL_NAME)
    sub = parser.add_subparsers(dest="command", required=True)
    backup = sub.add_parser("backup")
    backup.add_argument("--profile", choices=sorted(PROFILES), required=True)
    _add_common_roots(backup)
    backup.add_argument("--destination", required=True, type=Path)
    backup.add_argument("--report", type=Path)
    verify = sub.add_parser("verify-backup")
    verify.add_argument("--backup", required=True, type=Path)
    restore = sub.add_parser("restore")
    restore.add_argument("--backup", required=True, type=Path)
    restore.add_argument("--legacy-root", required=True, type=Path)
    restore.add_argument("--rm-root", required=True, type=Path)
    restore.add_argument("--state-root", required=True, type=Path)
    restore.add_argument("--report", type=Path)
    pre = sub.add_parser("preflight")
    _add_common_roots(pre)
    pre.add_argument("--report", type=Path)
    pre.add_argument("--backup-root", type=Path)
    pre.add_argument("--embedding-enabled", choices=("true", "false", "unknown"), default="unknown")
    pre.add_argument("--expected-model-id")
    pre.add_argument("--estimated-vector-bytes", type=int)
    pre.add_argument("--headroom-bytes", type=int, default=DEFAULT_HEADROOM_BYTES)
    pre.add_argument("--worker-count", type=int)
    pre.add_argument("--multiprocess", choices=("true", "false"))
    pre.add_argument("--shared-state", choices=("true", "false"))
    pre.add_argument("--service-instances", type=int)
    gate = sub.add_parser("readiness-gate")
    gate.add_argument("--evidence", required=True, type=Path)
    gate.add_argument("--report", type=Path)
    accept = sub.add_parser("acceptance-checks")
    accept.add_argument("--evidence", required=True, type=Path)
    accept.add_argument("--report", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _main_parser().parse_args(argv)
    try:
        if args.command == "backup":
            result = create_backup(
                profile=args.profile, legacy_root=args.legacy_root, rm_root=args.rm_root,
                state_db=args.state_db, destination=args.destination, report=args.report,
            )
        elif args.command == "verify-backup":
            result = verify_backup(args.backup)
        elif args.command == "restore":
            result = restore_backup(
                backup_root=args.backup, legacy_root=args.legacy_root, rm_root=args.rm_root,
                state_root=args.state_root, report=args.report,
            )
        elif args.command == "preflight":
            topology = {
                "worker_count": args.worker_count,
                "multiprocess": None if args.multiprocess is None else args.multiprocess == "true",
                "shared_state": None if args.shared_state is None else args.shared_state == "true",
                "service_instances": args.service_instances,
            }
            result = preflight(
                legacy_root=args.legacy_root, rm_root=args.rm_root, state_db=args.state_db,
                report=args.report, backup_root=args.backup_root,
                embedding_enabled=args.embedding_enabled, expected_model_id=args.expected_model_id,
                estimated_vector_bytes=args.estimated_vector_bytes, headroom_bytes=args.headroom_bytes,
                topology=topology,
            )
        else:
            evidence = json.loads(args.evidence.read_text(encoding="utf-8"))
            if args.command == "readiness-gate":
                result = evaluate_readiness(evidence)
            else:
                result = run_frozen_acceptance_checks(
                    state=evidence.get("state", {}), checks=evidence.get("checks", {})
                )
            if args.report:
                _json_write(_canonical_path(args.report, "report"), result)
        print(json.dumps(result, ensure_ascii=True, sort_keys=True, indent=2))
        return 0 if result.get("status") in {"PASS", "READY"} else 2
    except CutoverOperationsError as exc:
        print(json.dumps({"status": "FAIL", "error": exc.code}, sort_keys=True))
        return 2


if __name__ == "__main__":
    sys.exit(main())


__all__ = [
    "ACCEPTANCE_CHECK_NAMES",
    "CutoverOperationsError",
    "READINESS_GATES",
    "acceptance_check_spec",
    "classify_topology",
    "create_backup",
    "evaluate_readiness",
    "main",
    "preflight",
    "restore_backup",
    "run_frozen_acceptance_checks",
    "verify_backup",
    "verify_restored",
]

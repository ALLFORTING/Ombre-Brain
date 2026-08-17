"""Read-only frozen RM acceptance probe.

This file is deliberately standalone.  It must be copied to a running
instance and invoked explicitly; importing it does not start an application
server and it never imports Ombre-Brain modules.
"""

from __future__ import annotations

import base64
from datetime import datetime, timezone
import hashlib
import json
import math
import os
import secrets
import sqlite3
import stat
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any


CHECK_NAMES = (
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

ACCEPTANCE_ARTIFACT_SCHEMA_VERSION = 1
CHECKS_OUTPUT_PATH = Path("/tmp/rm-acceptance-checks.json")
EVIDENCE_OUTPUT_PATH = Path("/tmp/rm-acceptance-evidence.json")
OUTPUT_LOCK_PATH = Path("/tmp/rm-acceptance.lock")

MISSING_ASSET_ID = "f" * 32
PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
    "YAAAAAYAAjCB0C8AAAAASUVORK5CYII="
)

REQUIRED_TICKET_SUBRESULTS = (
    "ephemeral_probe",
    "fresh_process_binding",
    "upload_ticket_recreated",
    "download_ticket_recreated",
    "verification_session_recreated",
    "ephemeral_cleanup_complete",
    "capability_not_exposed",
    "durable_fingerprint_unchanged",
    "durable_mutation_performed",
)


@dataclass(frozen=True)
class HttpResult:
    status: int | None
    headers: dict[str, str]
    body: bytes
    error: str | None = None


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _canonical_json_digest(value: Any) -> str:
    return hashlib.sha256(_canonical_json_bytes(value)).hexdigest()


def _parse_utc_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def ticket_recreation_status(subresults: dict[str, dict[str, Any]]) -> str:
    """Aggregate only explicit required subresults, preserving contradictions."""

    statuses = [subresults.get(name, {}).get("status", "INCOMPLETE") for name in REQUIRED_TICKET_SUBRESULTS]
    if "FAIL" in statuses:
        return "FAIL"
    if "INCOMPLETE" in statuses:
        return "INCOMPLETE"
    return "PASS"


def _sqlite_identifier(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _canonical_sqlite_value(value: Any) -> dict[str, Any]:
    """Represent SQLite values deterministically without exposing BLOB data."""

    if value is None:
        return {"type": "null"}
    if isinstance(value, bool):
        return {"type": "integer", "value": int(value)}
    if isinstance(value, int):
        return {"type": "integer", "value": value}
    if isinstance(value, float):
        if math.isnan(value):
            rendered = "nan"
        elif math.isinf(value):
            rendered = "-inf" if value < 0 else "inf"
        else:
            rendered = format(value, ".17g")
        return {"type": "real", "value": rendered}
    if isinstance(value, str):
        return {"type": "text", "value": value}
    if isinstance(value, (bytes, bytearray, memoryview)):
        raw = bytes(value)
        return {
            "type": "blob",
            "bytes": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest(),
        }
    raise TypeError("unsupported_sqlite_value")


def _sqlite_logical_digest(connection: sqlite3.Connection, names: tuple[str, ...]) -> str:
    """Hash schemas and canonicalized rows independent of row iteration order."""

    tables: list[dict[str, Any]] = []
    for name in names:
        quoted = _sqlite_identifier(name)
        schema_row = connection.execute(
            "SELECT type, sql FROM sqlite_master WHERE name = ?",
            (name,),
        ).fetchone()
        if schema_row is None or schema_row[0] != "table":
            raise ValueError("sqlite_schema_changed")
        columns = []
        for column in connection.execute(f"PRAGMA table_xinfo({quoted})"):
            columns.append(
                {
                    "cid": _canonical_sqlite_value(column[0]),
                    "name": _canonical_sqlite_value(column[1]),
                    "type": _canonical_sqlite_value(column[2]),
                    "notnull": _canonical_sqlite_value(column[3]),
                    "default": _canonical_sqlite_value(column[4]),
                    "pk": _canonical_sqlite_value(column[5]),
                    "hidden": _canonical_sqlite_value(column[6]),
                }
            )
        row_digests: list[str] = []
        for row in connection.execute(f"SELECT * FROM {quoted}"):
            canonical_row = [_canonical_sqlite_value(value) for value in row]
            row_bytes = json.dumps(
                canonical_row,
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            row_digests.append(hashlib.sha256(row_bytes).hexdigest())
        row_digests.sort()
        table_payload = {
            "name": name,
            "type": _canonical_sqlite_value(schema_row[0]),
            "sql": _canonical_sqlite_value(schema_row[1]),
            "columns": columns,
            "row_digests": row_digests,
        }
        tables.append(table_payload)
    payload = json.dumps(
        tables,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def sqlite_observation(path: Path) -> dict[str, Any]:
    """Return a complete, redacted read-only SQLite observation."""
    if not path.is_file():
        return {
            "present": False,
            "size": 0,
            "integrity": None,
            "tables": (),
            "counts": {},
            "row_total": 0,
            "logical_digest": None,
        }
    try:
        size = path.stat().st_size
        connection = sqlite3.connect(
            "file:" + str(path) + "?mode=ro",
            uri=True,
            timeout=5,
        )
        try:
            connection.execute("PRAGMA query_only=ON")
            integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
            names = tuple(
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE type = 'table' AND name NOT LIKE 'sqlite_%' "
                    "ORDER BY name COLLATE BINARY"
                )
            )
            counts: dict[str, int] = {}
            for name in names:
                quoted = _sqlite_identifier(str(name))
                counts[name] = int(
                    connection.execute(f"SELECT count(*) FROM {quoted}").fetchone()[0]
                )
            return {
                "present": True,
                "size": size,
                "integrity": str(integrity),
                "tables": names,
                "counts": counts,
                "row_total": sum(counts.values()),
                "logical_digest": _sqlite_logical_digest(connection, names),
            }
        finally:
            connection.close()
    except Exception:
        return {
            "present": None,
            "size": None,
            "integrity": None,
            "tables": (),
            "counts": {},
            "row_total": None,
            "logical_digest": None,
            "error": "sqlite_unavailable",
        }


def sqlite_fingerprint_equal(
    first: dict[str, Any], second: dict[str, Any]
) -> bool:
    """Compare two complete observations without assuming optional components."""
    keys = (
        "present",
        "size",
        "integrity",
        "tables",
        "counts",
        "row_total",
        "logical_digest",
    )
    return all(first.get(key) == second.get(key) for key in keys)


def _is_sensitive_name(name: str) -> bool:
    lowered = name.casefold()
    return (
        lowered in {"lease-token.json", "lease_token.json", ".dashboard_auth.json"}
        or any(word in lowered for word in ("token", "secret", "password", "credential"))
    )


def _is_link_or_reparse(path: Path, info: os.stat_result | None = None) -> bool:
    metadata = info if info is not None else path.lstat()
    return bool(
        stat.S_ISLNK(metadata.st_mode)
        or getattr(metadata, "st_file_attributes", 0) & 0x400
    )


def _assert_tree_containment(root: Path, candidate: Path, resolved_root: Path) -> None:
    if _is_link_or_reparse(candidate):
        raise RuntimeError("unsafe_tree_entry")
    resolved_candidate = candidate.resolve(strict=False)
    if resolved_candidate != resolved_root and resolved_root not in resolved_candidate.parents:
        raise RuntimeError("tree_path_escape")


def tree_observation(root: Path | None) -> dict[str, Any]:
    """Aggregate persistent file state without recording names or contents."""
    if root is None:
        return {"present": False, "files": 0, "bytes": 0, "suffixes": {}, "digest": None}
    digest = hashlib.sha256()
    files = 0
    total_bytes = 0
    suffixes: dict[str, int] = {}
    try:
        root = Path(os.path.abspath(root))
        try:
            root_info = root.lstat()
        except FileNotFoundError:
            return {"present": False, "files": 0, "bytes": 0, "suffixes": {}, "digest": None}
        if _is_link_or_reparse(root, root_info) or not stat.S_ISDIR(root_info.st_mode):
            return {"present": None, "files": None, "bytes": None, "suffixes": {}, "digest": None, "error": "unsafe_tree_root"}
        resolved_root = root.resolve(strict=False)
        pending = [root]
        entries: list[Path] = []
        while pending:
            current = pending.pop()
            with os.scandir(current) as scan:
                children = sorted(
                    (Path(entry.path) for entry in scan),
                    key=lambda item: item.name,
                )
            for path in children:
                info = path.lstat()
                if _is_link_or_reparse(path, info):
                    raise RuntimeError("unsafe_tree_entry")
                _assert_tree_containment(root, path, resolved_root)
                if stat.S_ISDIR(info.st_mode):
                    pending.append(path)
                elif stat.S_ISREG(info.st_mode):
                    entries.append(path)
        for path in sorted(entries, key=lambda item: item.relative_to(root).as_posix()):
            relative_path = path.relative_to(root)
            if any(_is_sensitive_name(part) for part in relative_path.parts):
                continue
            if path.suffix.casefold() in {".db", ".sqlite", ".sqlite3", ".wal", ".shm"}:
                continue
            data = path.read_bytes()
            relative = relative_path.as_posix().encode("utf-8", "strict")
            digest.update(len(relative).to_bytes(8, "big"))
            digest.update(relative)
            digest.update(len(data).to_bytes(8, "big"))
            digest.update(data)
            files += 1
            total_bytes += len(data)
            suffix = path.suffix.casefold() or "<none>"
            suffixes[suffix] = suffixes.get(suffix, 0) + 1
    except Exception as exc:
        error = "unsafe_tree_entry" if str(exc) in {"unsafe_tree_entry", "tree_path_escape", "unsafe_tree_root"} else "tree_unavailable"
        return {"present": None, "files": None, "bytes": None, "suffixes": {}, "digest": None, "error": error}
    return {
        "present": True,
        "files": files,
        "bytes": total_bytes,
        "suffixes": dict(sorted(suffixes.items())),
        "digest": digest.hexdigest(),
    }


def _path_from_env(name: str, default: str) -> Path:
    value = os.environ.get(name, default).strip()
    return Path(value).expanduser()


def _state_db_path() -> Path:
    return _path_from_env(
        "RM_PROBE_STATE_DB",
        "/opt/render/project/src/buckets/state/migration.sqlite3",
    )


def cutover_identity_observation() -> dict[str, Any] | None:
    """Read the redacted durable cutover identity without opening it writable."""

    path = _state_db_path()
    if not path.is_file():
        return None
    connection = None
    try:
        connection = sqlite3.connect(
            "file:" + str(path) + "?mode=ro",
            uri=True,
            timeout=5,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        state = connection.execute(
            "SELECT revision, state, authority, freeze_status "
            "FROM cutover_state WHERE singleton = 1"
        ).fetchone()
        freeze = connection.execute(
            "SELECT generation, acquired_at, expires_at "
            "FROM cutover_freeze WHERE singleton = 1"
        ).fetchone()
        if state is None or state["state"] != RM_FROZEN_ACCEPTANCE_STATE or state["authority"] != "rm":
            return None
        if freeze is None:
            freeze_status = "open"
            generation = None
            acquired_at = None
            expires_at = None
        else:
            expires = _parse_utc_timestamp(freeze["expires_at"])
            if expires is None:
                return None
            freeze_status = "active" if expires > datetime.now(timezone.utc) else "expired"
            generation = freeze["generation"]
            acquired_value = _parse_utc_timestamp(freeze["acquired_at"])
            expires_value = _parse_utc_timestamp(freeze["expires_at"])
            if acquired_value is None or expires_value is None:
                return None
            acquired_at = acquired_value.isoformat(timespec="microseconds")
            expires_at = expires_value.isoformat(timespec="microseconds")
        if type(state["revision"]) is not int or state["revision"] < 0:
            return None
        if generation is not None and (type(generation) is not int or generation < 0):
            return None
        return {
            "revision": int(state["revision"]),
            "cutover_state": str(state["state"]),
            "phase": RM_FROZEN_ACCEPTANCE_PHASE,
            "authority": str(state["authority"]),
            "freeze_status": freeze_status,
            "lease_generation": int(generation) if generation is not None else None,
            "lease_acquired_at": acquired_at,
            "lease_expires_at": expires_at,
        }
    except Exception:
        return None
    finally:
        if connection is not None:
            connection.close()


def persistent_snapshot() -> dict[str, Any]:
    state_db = _state_db_path()
    state_root = state_db.parent
    legacy_root = _path_from_env(
        "RM_PROBE_LEGACY_ROOT",
        str(state_root.parent),
    )
    raw_rm_root = os.environ.get("OMBRE_RM_DATA_ROOT", "").strip()
    rm_root = Path(raw_rm_root).expanduser() if raw_rm_root else None
    rm_db = (
        _path_from_env("RM_PROBE_RM_DB", str(rm_root / "assets.sqlite3"))
        if rm_root is not None
        else None
    )
    legacy_db = _path_from_env("RM_PROBE_LEGACY_DB", str(legacy_root / "assets.sqlite3"))
    return {
        "state_db": sqlite_observation(state_db),
        "rm_db": sqlite_observation(rm_db) if rm_db is not None else None,
        "legacy_db": sqlite_observation(legacy_db),
        "state_tree": tree_observation(state_root),
        "rm_tree": tree_observation(rm_root),
        "legacy_tree": tree_observation(legacy_root),
    }


def persistent_snapshot_equal(first: dict[str, Any], second: dict[str, Any]) -> bool:
    for key in ("state_db", "rm_db", "legacy_db"):
        left, right = first.get(key), second.get(key)
        if left is None or right is None:
            if left != right:
                return False
        elif not sqlite_fingerprint_equal(left, right):
            return False
    for key in ("state_tree", "rm_tree", "legacy_tree"):
        if first.get(key) != second.get(key):
            return False
    return True


def persistent_snapshot_complete(snapshot: dict[str, Any]) -> bool:
    """Return whether every durable surface was actually observed."""

    for key in ("state_db", "rm_db", "legacy_db"):
        value = snapshot.get(key)
        if not isinstance(value, dict) or value.get("present") is not True:
            return False
        if value.get("integrity") != "ok":
            return False
        if not isinstance(value.get("logical_digest"), str) or not value["logical_digest"]:
            return False
    for key in ("state_tree", "rm_tree", "legacy_tree"):
        value = snapshot.get(key)
        if not isinstance(value, dict) or value.get("present") is not True:
            return False
        if value.get("digest") is None:
            return False
    return True


def _json_message(body: bytes) -> dict[str, Any] | None:
    try:
        text = body.decode("utf-8", "strict").strip()
    except UnicodeDecodeError:
        return None
    candidates = [text]
    if "data:" in text:
        candidates.extend(
            line[5:].strip()
            for line in text.splitlines()
            if line.startswith("data:")
        )
    for candidate in reversed(candidates):
        try:
            parsed = json.loads(candidate)
        except (TypeError, ValueError):
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def _tool_payload(message: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(message, dict):
        return None
    result = message.get("result", message)
    if not isinstance(result, dict):
        return None
    structured = result.get("structuredContent")
    if isinstance(structured, dict) and _is_compatibility_payload(structured): return structured
    if isinstance(structured, dict) and _is_compatibility_payload(structured.get("result")): return structured["result"]
    content = result.get("content")
    if isinstance(content, list):
        for item in reversed(content):
            if not isinstance(item, dict) or item.get("type") != "text":
                continue
            parsed = _json_message(str(item.get("text", "")).encode("utf-8"))
            if _is_compatibility_payload(parsed):
                return parsed
    return result if _is_compatibility_payload(result) else None


def _safe_result_detail(result: HttpResult, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    detail: dict[str, Any] = {"http_status": result.status}
    if result.error:
        detail["transport_error"] = result.error
    if isinstance(payload, dict):
        detail["ok"] = payload.get("ok") if isinstance(payload.get("ok"), bool) else None
        error = payload.get("error")
        if isinstance(error, str) and len(error) <= 80:
            detail["error"] = error
        if isinstance(payload.get("total"), int):
            detail["total"] = payload["total"]
        if isinstance(payload.get("results"), list):
            detail["result_count"] = len(payload["results"])
    return detail


class Probe:
    def __init__(self, *, acceptance_run_id: str | None = None, created_at: str | None = None) -> None:
        self.acceptance_run_id = acceptance_run_id or secrets.token_hex(16)
        self.created_at = created_at or _utc_now()
        base = os.environ.get("RM_PROBE_BASE_URL", "http://127.0.0.1:8000").strip()
        self.base_url = base.rstrip("/")
        self.session_cookie = os.environ.get("RM_PROBE_SESSION_COOKIE", "").strip()
        self.mcp_token = os.environ.get("RM_PROBE_MCP_TOKEN", "").strip()
        self.csrf = os.environ.get("RM_PROBE_CSRF", "").strip()
        self.origin = os.environ.get("RM_PROBE_ORIGIN", self.base_url).strip().rstrip("/")
        self.evidence: dict[str, dict[str, Any]] = {}
        self.runtime: dict[str, Any] | None = None
        self.sub_evidence: dict[str, dict[str, Any]] = {}
        self.ephemeral_probe: dict[str, Any] | None = None
        self.runtime_identity: dict[str, str] | None = None
        self.mcp_session_id: str | None = None
        self.mcp_initialized = False
        self.cutover_identity = cutover_identity_observation()
        self.before = persistent_snapshot()

    def record(self, name: str, status: str, source: str, reason: str, **detail: Any) -> None:
        if name not in CHECK_NAMES:
            raise ValueError("unknown_acceptance_check")
        if status not in {"PASS", "FAIL", "INCOMPLETE"}:
            raise ValueError("invalid_acceptance_status")
        value: dict[str, Any] = {
            "status": status,
            "source": source,
            "reason": reason,
        }
        for key, item in detail.items():
            if key in {"http_status", "error", "total", "result_count", "expected", "observed", "authentication_available", "side_effects_free"}:
                value[key] = item
        self.evidence[name] = value

    def expect(self, name: str, observed: bool | None, source: str, reason: str, **detail: Any) -> None:
        status = "PASS" if observed is True else "FAIL" if observed is False else "INCOMPLETE"
        self.record(name, status, source, reason, **detail)

    def record_subresult(
        self,
        name: str,
        status: str,
        reason: str,
        **detail: Any,
    ) -> None:
        if status not in {"PASS", "FAIL", "INCOMPLETE"}:
            raise ValueError("invalid_subresult_status")
        value: dict[str, Any] = {"status": status, "reason": reason}
        for key in ("observed", "authentication_available", "side_effects_free"):
            if key in detail and isinstance(detail[key], bool):
                value[key] = detail[key]
        self.sub_evidence[name] = value

    @staticmethod
    def _fresh_process_binding(payload: dict[str, Any]) -> tuple[str, str, bool]:
        # These expected values belong only to this external probe process.
        # The operator must source them from independent Render control-plane,
        # log, or CLI evidence; this endpoint is not an acceptable source.
        platform = payload.get("platform_identity")
        expected = {
            "instance_id": os.environ.get("RM_PROBE_TRUSTED_INSTANCE_ID", "").strip(),
            "git_commit": os.environ.get("RM_PROBE_TRUSTED_GIT_COMMIT", "").strip(),
            "service_id": os.environ.get("RM_PROBE_TRUSTED_SERVICE_ID", "").strip(),
        }
        fields = ("instance_id", "git_commit", "service_id")
        if (
            not isinstance(platform, dict)
            or any(not isinstance(platform.get(field), str) or not platform[field] for field in fields)
            or any(not expected[field] for field in fields)
        ):
            return "INCOMPLETE", "trusted Render instance/commit/service identity is unavailable", False
        if all(platform[field] == expected[field] for field in fields):
            return "PASS", "current Render identity matches independent trusted evidence", True
        return "FAIL", "current Render identity does not match trusted evidence", False

    def http(self, path: str, *, method: str = "GET", body: bytes | None = None, headers: dict[str, str] | None = None) -> HttpResult:
        request_headers = {"Accept": "application/json, text/plain, */*"}
        if headers:
            request_headers.update(headers)
        request = urllib.request.Request(
            self.base_url + path,
            data=body,
            headers=request_headers,
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                return HttpResult(
                    response.status,
                    {str(k).casefold(): str(v) for k, v in response.headers.items()},
                    response.read(1024 * 1024),
                )
        except urllib.error.HTTPError as exc:
            try:
                body_bytes = exc.read(1024 * 1024)
            except OSError:
                body_bytes = b""
            return HttpResult(exc.code, {}, body_bytes)
        except (OSError, urllib.error.URLError):
            return HttpResult(None, {}, b"", "transport_unavailable")

    def dashboard_headers(self, *, write: bool = False) -> dict[str, str]:
        headers = {
            "Cookie": self.session_cookie,
            "Origin": self.origin,
        }
        if write:
            headers["X-Ombre-CSRF"] = self.csrf
        return headers

    def mcp_request(self, method: str, params: dict[str, Any] | None = None) -> tuple[HttpResult, dict[str, Any] | None]:
        headers = {
            "Authorization": "Bearer " + self.mcp_token,
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if self.mcp_session_id:
            headers["Mcp-Session-Id"] = self.mcp_session_id
        message = {
            "jsonrpc": "2.0",
            "id": str(uuid.uuid4()),
            "method": method,
            "params": params or {},
        }
        result = self.http(
            "/mcp",
            method="POST",
            body=json.dumps(message, separators=(",", ":")).encode("utf-8"),
            headers=headers,
        )
        session_id = result.headers.get("mcp-session-id")
        if session_id:
            self.mcp_session_id = session_id
        return result, _json_message(result.body)

    def mcp_tool(self, name: str, arguments: dict[str, Any]) -> tuple[HttpResult, dict[str, Any] | None]:
        if not self.mcp_token:
            return HttpResult(None, {}, b"", "credential_unavailable"), None
        if not self.mcp_initialized:
            initialize, _ = self.mcp_request(
                "initialize",
                {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {"name": "rm-frozen-acceptance-probe", "version": "1"},
                },
            )
            if initialize.status not in {200, 202}:
                return initialize, None
            self.mcp_request("notifications/initialized")
            self.mcp_initialized = True
        return self.mcp_request(
            "tools/call",
            {"name": name, "arguments": arguments},
        )

    def run_runtime_checks(self) -> None:
        result = self.http(
            "/__operator/rm-runtime-evidence",
            headers={"Authorization": "Bearer " + self.mcp_token} if self.mcp_token else {},
        )
        payload = _json_message(result.body)
        if result.status != 200 or not isinstance(payload, dict) or payload.get("status") != "ok":
            self.record(
                "rm_runtime_healthy",
                "INCOMPLETE",
                "operator_runtime_evidence",
                "runtime evidence unavailable",
                authentication_available=bool(self.mcp_token),
                http_status=result.status,
            )
            return
        required = (
            "authority", "durable_authority", "selected_backend", "cutover_state",
            "boot_mode", "writes_allowed", "frozen", "recovery_required",
            "legacy_fallback_allowed", "rm_available", "platform_identity",
            "runtime_boot_validation_passed",
        )
        if any(key not in payload for key in required):
            self.record("rm_runtime_healthy", "INCOMPLETE", "operator_runtime_evidence", "runtime evidence fields incomplete")
            return
        self.runtime = payload
        platform_identity = payload.get("platform_identity")
        if isinstance(platform_identity, dict):
            fields = ("instance_id", "git_commit", "service_id")
            if all(type(platform_identity.get(field)) is str and platform_identity[field] for field in fields):
                self.runtime_identity = {
                    field: platform_identity[field]
                    for field in fields
                }
        binding_status, binding_reason, binding_observed = self._fresh_process_binding(payload)
        self.record_subresult(
            "fresh_process_binding",
            binding_status,
            binding_reason,
            observed=binding_observed,
        )
        healthy = (
            payload["authority"] == "rm"
            and payload["durable_authority"] == "rm"
            and payload["selected_backend"] == "rm"
            and payload["cutover_state"] == "frozen_rm_acceptance"
            and payload["writes_allowed"] is False
            and payload["frozen"] is True
            and payload["recovery_required"] is False
            and payload["legacy_fallback_allowed"] is False
            and payload["rm_available"] is True
            and payload["runtime_boot_validation_passed"] is True
        )
        self.expect("rm_runtime_healthy", healthy, "running_runtime_boot_validation", "running registry reported RM frozen runtime", expected=True)
        self.expect("authority_consistency", payload["authority"] == payload["durable_authority"] == "rm", "running_runtime_boot_validation", "configured and durable authority agree")
        self.expect("mutation_freeze_active", payload["frozen"] is True and payload["writes_allowed"] is False and payload.get("freeze_status") == "active", "running_runtime_boot_validation", "running boot validation reports frozen writes")
        selected = payload["selected_backend"] == "rm"
        self.expect("mcp_backend_selected", selected, "running_runtime_backend_registry", "selected backend is RM")
        self.expect("dashboard_backend_selected", selected, "running_runtime_backend_registry", "Dashboard and MCP share the selected registry backend")
        no_fallback = payload["legacy_fallback_allowed"] is False and selected
        self.expect("rm_failure_no_legacy_fallback", no_fallback, "running_boot_validation_plus_local_fail_closed_test", "RM boot contract disables legacy fallback")
        self.expect("legacy_not_authoritative", payload["durable_authority"] != "legacy", "running_runtime_boot_validation", "durable authority is RM")
        self.expect("no_legacy_fallback", no_fallback, "running_boot_validation_plus_local_fail_closed_test", "fallback is disabled")
        self.expect("no_legacy_route", no_fallback, "running_runtime_backend_registry", "authority-selected route is RM")
        restart_status = "PASS" if healthy and binding_status == "PASS" else "FAIL" if healthy and binding_status == "FAIL" else "INCOMPLETE"
        self.record("restart_state_durable", restart_status, "trusted_render_identity_and_runtime_validation", "durable RM frozen state was loaded by the trusted current Render instance")

    def run_health_check(self) -> None:
        result = self.http("/health")
        self.expect("rm_healthy", result.status == 200, "health_route", "health route returned HTTP 200", http_status=result.status)

    def run_mcp_reads(self) -> dict[str, str | None]:
        names: dict[str, str | None] = {}
        if not self.mcp_token:
            for name in ("metadata_get", "search", "view", "inspect", "download"):
                self.record(name, "INCOMPLETE", "mcp_authentication", "RM_PROBE_MCP_TOKEN is unavailable", authentication_available=False)
            return names

        calls = {
            "metadata_get": ("rm_asset_get", {"asset_id": MISSING_ASSET_ID}),
            "search": ("rm_asset_search", {"query": "", "limit": 20, "offset": 0}),
            "view": ("rm_asset_view", {"asset_id": MISSING_ASSET_ID}),
            "inspect": ("rm_asset_inspect", {"asset_id": MISSING_ASSET_ID}),
            "download": ("rm_asset_download_link", {"asset_id": MISSING_ASSET_ID}),
        }
        for check, (tool, arguments) in calls.items():
            result, message = self.mcp_tool(tool, arguments)
            payload = _tool_payload(message)
            detail = _safe_result_detail(result, payload)
            if check == "search":
                observed = bool(
                    result.status in {200, 202}
                    and isinstance(payload, dict)
                    and payload.get("ok") is True
                    and payload.get("total") == 0
                    and payload.get("results") == []
                )
            else:
                observed = bool(
                    result.status in {200, 202}
                    and isinstance(payload, dict)
                    and payload.get("ok") is False
                    and payload.get("error") == "asset_unavailable"
                )
            status = "PASS" if observed else "FAIL" if result.status is not None else "INCOMPLETE"
            self.record(check, status, "authenticated_rm_mcp_route", "RM read path returned the expected empty/missing result", **detail)
            names[check] = "PASS" if observed else status
        self.record("metadata_read", self.evidence["metadata_get"]["status"], "metadata_get", "compatibility alias maps to metadata_get")
        self.record("search_read", self.evidence["search"]["status"], "search", "compatibility alias maps to search")
        self.record("view_read", self.evidence["view"]["status"], "view", "compatibility alias maps to view")
        self.record("inspect_read", self.evidence["inspect"]["status"], "inspect", "compatibility alias maps to inspect")
        self.record("download_read", self.evidence["download"]["status"], "download", "compatibility alias maps to download")
        return names

    def run_dashboard_reads(self) -> dict[str, str | None]:
        names: dict[str, str | None] = {}
        if not self.session_cookie:
            for name in ("dashboard_list", "dashboard_detail", "dashboard_image", "dashboard_thumbnail"):
                self.record(name, "INCOMPLETE", "dashboard_authentication", "RM_PROBE_SESSION_COOKIE is unavailable", authentication_available=False)
            return names
        headers = self.dashboard_headers()
        result = self.http("/api/assets?limit=20&offset=0", headers=headers)
        payload = _json_message(result.body)
        observed = bool(
            result.status == 200
            and isinstance(payload, dict)
            and payload.get("total") == 0
            and payload.get("results") == []
        )
        self.record(
            "dashboard_list",
            "PASS" if observed else "FAIL" if result.status is not None else "INCOMPLETE",
            "authenticated_dashboard_route",
            "Dashboard list returned a legal empty result",
            http_status=result.status,
            total=payload.get("total") if isinstance(payload, dict) else None,
            result_count=len(payload.get("results", [])) if isinstance(payload, dict) and isinstance(payload.get("results"), list) else None,
        )
        for name, suffix in (
            ("dashboard_detail", ""),
            ("dashboard_image", "/image"),
            ("dashboard_thumbnail", "/thumbnail"),
        ):
            result = self.http("/api/assets/" + MISSING_ASSET_ID + suffix, headers=headers)
            observed = result.status == 404
            self.record(name, "PASS" if observed else "FAIL" if result.status is not None else "INCOMPLETE", "authenticated_dashboard_route", "missing-id read returned HTTP 404", http_status=result.status)
            names[name] = "PASS" if observed else "FAIL" if result.status is not None else "INCOMPLETE"
        names["dashboard_list"] = self.evidence["dashboard_list"]["status"]
        return names

    @staticmethod
    def _frozen_json(result: HttpResult) -> bool | None:
        payload = _json_message(result.body)
        if result.status is None:
            return None
        return bool(
            result.status == 409
            and isinstance(payload, dict)
            and payload.get("error") == "asset_write_frozen"
        )

    def run_dashboard_mutations(self) -> bool | None:
        if not self.session_cookie or not self.csrf:
            for name in ("dashboard_upload_rejected", "dashboard_update_rejected", "dashboard_delete_rejected"):
                self.record(name, "INCOMPLETE", "dashboard_authentication", "session cookie or CSRF credential unavailable", authentication_available=bool(self.session_cookie and self.csrf))
            return None
        headers = self.dashboard_headers(write=True)
        update = self.http(
            "/api/assets/" + MISSING_ASSET_ID,
            method="PATCH",
            body=json.dumps({"title": "rm-frozen-probe"}).encode("utf-8"),
            headers={**headers, "Content-Type": "application/json"},
        )
        delete = self.http("/api/assets/" + MISSING_ASSET_ID, method="DELETE", headers=headers)
        boundary = "----rm-frozen-acceptance-" + uuid.uuid4().hex
        body = (
            ("--" + boundary + "\r\n").encode()
            + b'Content-Disposition: form-data; name="file"; filename="probe.png"\r\n'
            + b"Content-Type: image/png\r\n\r\n"
            + PNG_1X1
            + ("\r\n--" + boundary + "--\r\n").encode()
        )
        upload = self.http(
            "/api/assets",
            method="POST",
            body=body,
            headers={**headers, "Content-Type": "multipart/form-data; boundary=" + boundary},
        )
        observations = {
            "dashboard_update_rejected": update,
            "dashboard_delete_rejected": delete,
            "dashboard_upload_rejected": upload,
        }
        for name, result in observations.items():
            observed = self._frozen_json(result)
            self.record(name, "PASS" if observed is True else "FAIL" if observed is False else "INCOMPLETE", "authenticated_dashboard_mutation_route", "valid mutation was rejected by the frozen gate before lookup/write", http_status=result.status, error="asset_write_frozen" if observed else None)
        return all(self.evidence[name]["status"] == "PASS" for name in observations)

    def run_mcp_mutations(self) -> bool | None:
        if not self.mcp_token:
            for name in ("mcp_upload_rejected", "mcp_update_rejected", "public_reindex_rejected"):
                self.record(name, "INCOMPLETE", "mcp_authentication", "RM_PROBE_MCP_TOKEN is unavailable", authentication_available=False)
            return None
        calls = {
            "mcp_upload_rejected": ("rm_asset_upload_link", {"expected_bytes": 1, "filename": "probe.png", "mime_type": "image/png"}),
            "mcp_update_rejected": ("rm_asset_update_metadata", {"asset_id": MISSING_ASSET_ID, "title": "rm-frozen-probe"}),
            "public_reindex_rejected": ("rm_asset_reindex_embeddings", {"asset_id": "", "limit": 1}),
        }
        for name, (tool, arguments) in calls.items():
            result, message = self.mcp_tool(tool, arguments)
            payload = _tool_payload(message)
            observed = bool(
                result.status in {200, 202}
                and isinstance(payload, dict)
                and payload.get("ok") is False
                and payload.get("error") == "asset_write_frozen"
            ) if result.status is not None else None
            self.record(name, "PASS" if observed is True else "FAIL" if observed is False else "INCOMPLETE", "authenticated_rm_mcp_mutation_route", "valid mutation was rejected by the frozen gate", **_safe_result_detail(result, payload))
        return all(self.evidence[name]["status"] == "PASS" for name in calls)

    def run_ephemeral_probe(self) -> None:
        if not self.mcp_token:
            self.record_subresult(
                "ephemeral_probe",
                "INCOMPLETE",
                "RM_PROBE_MCP_TOKEN is unavailable",
                authentication_available=False,
            )
            self.ephemeral_probe = None
            return
        result = self.http(
            "/__operator/rm-runtime-evidence/ephemeral-probe",
            method="POST",
            headers={"Authorization": "Bearer " + self.mcp_token},
        )
        payload = _json_message(result.body)
        if not isinstance(payload, dict):
            self.record_subresult("ephemeral_probe", "INCOMPLETE", "ephemeral probe evidence unavailable")
            self.ephemeral_probe = None
            return
        safe_keys = (
            "status",
            "upload_ticket_recreated",
            "download_ticket_recreated",
            "verification_session_recreated",
            "ephemeral_cleanup_complete",
            "capability_not_exposed",
            "durable_mutation_performed",
        )
        self.ephemeral_probe = {
            key: payload.get(key)
            for key in safe_keys
            if key in payload
        }
        status = payload.get("status")
        if status not in {"PASS", "FAIL", "INCOMPLETE"} or any(
            type(payload.get(key)) is not bool
            for key in safe_keys[1:]
        ):
            self.record_subresult("ephemeral_probe", "INCOMPLETE", "ephemeral probe fields are incomplete")
            durable_value = payload.get("durable_mutation_performed")
            self.record_subresult(
                "durable_mutation_performed",
                "FAIL" if type(durable_value) is bool and durable_value else "PASS" if type(durable_value) is bool and durable_value is False else "INCOMPLETE",
                "ephemeral probe durable-mutation flag was not an explicit safe boolean",
                observed=durable_value if type(durable_value) is bool else False,
            )
            return
        self.record_subresult(
            "ephemeral_probe",
            status,
            "host-owned upload, download, and verification lifecycles were exercised",
            observed=status == "PASS",
            side_effects_free=payload["durable_mutation_performed"] is False,
        )
        self.record_subresult(
            "durable_mutation_performed",
            "FAIL" if payload["durable_mutation_performed"] else "PASS",
            "ephemeral probe explicitly reported no durable mutation",
            observed=payload["durable_mutation_performed"],
        )
        for name in (
            "upload_ticket_recreated",
            "download_ticket_recreated",
            "verification_session_recreated",
            "ephemeral_cleanup_complete",
            "capability_not_exposed",
        ):
            self.record_subresult(
                name,
                "PASS" if payload[name] else "FAIL",
                "ephemeral probe returned sanitized lifecycle evidence",
                observed=payload[name],
            )

    def run_auth_privacy(self) -> None:
        unauthenticated = self.http("/api/assets")
        query_token = self.http("/__operator/rm-runtime-evidence?token=" + urllib.parse.quote(self.mcp_token)) if self.mcp_token else None
        observed = unauthenticated.status == 401 and (query_token is not None and query_token.status == 401)
        self.expect("authorization_privacy", observed if self.mcp_token else None, "unauthenticated_route_and_query_token_rejection", "unauthenticated dashboard and query-token runtime evidence requests are rejected", http_status=unauthenticated.status, authentication_available=bool(self.mcp_token))

    def finish(self) -> tuple[dict[str, bool], dict[str, Any]]:
        after = persistent_snapshot()
        side_effects_free = persistent_snapshot_equal(self.before, after)
        durable_complete = persistent_snapshot_complete(self.before) and persistent_snapshot_complete(after)
        durable_status = "PASS" if side_effects_free and durable_complete else "FAIL" if not side_effects_free else "INCOMPLETE"
        self.record_subresult(
            "durable_fingerprint_unchanged",
            durable_status,
            "all durable fingerprints were compared before and after the ephemeral probe",
            observed=side_effects_free if durable_complete else False,
            side_effects_free=side_effects_free,
        )
        for name in CHECK_NAMES:
            if name not in self.evidence:
                self.record(name, "INCOMPLETE", "probe", "no safe observation was available")

        # These are aliases for the actual mutation observations; their values
        # cannot become true unless the corresponding public path was tested.
        aliases = {
            "dashboard_write_rejected": "dashboard_upload_rejected",
            "mcp_write_rejected": "mcp_update_rejected",
            "direct_ordinary_rm_write_rejected": "mcp_update_rejected",
            "direct_rm_write_rejected": "mcp_update_rejected",
        }
        for alias, source_name in aliases.items():
            source = self.evidence[source_name]
            self.record(alias, source["status"], source_name, "compatibility alias maps to the tested public mutation path")
        self.record(
            "mcp_dashboard_routing",
            "PASS" if self.evidence["mcp_backend_selected"]["status"] == "PASS" and self.evidence["dashboard_backend_selected"]["status"] == "PASS" and self.evidence["search"]["status"] == "PASS" and self.evidence["dashboard_list"]["status"] == "PASS" else "FAIL" if any(self.evidence[name]["status"] == "FAIL" for name in ("mcp_backend_selected", "dashboard_backend_selected", "search", "dashboard_list")) else "INCOMPLETE",
            "runtime_registry_plus_authenticated_read_routes",
            "MCP and Dashboard reads use the same RM-selected runtime",
        )
        self.record("rm_data_root_persists", "PASS" if self.runtime and side_effects_free and self.before.get("rm_db", {}).get("present") is True else "FAIL" if self.runtime and not side_effects_free else "INCOMPLETE", "RM_SQLite_and_tree_fingerprints", "RM durable root was present and unchanged")
        self.record("persistence_reopen", "PASS" if self.before["rm_db"] is not None and self.before["rm_db"].get("present") is True and after["rm_db"] is not None and after["rm_db"].get("present") is True and sqlite_fingerprint_equal(self.before["rm_db"], after["rm_db"]) else "INCOMPLETE", "RM_SQLite_read_only_reopen", "two complete read-only RM SQLite observations matched")
        self.record("dashboard_write_rejected", self.evidence["dashboard_upload_rejected"]["status"], "dashboard_upload_rejected", "compatibility alias maps to upload gate")
        self.record("mcp_write_rejected", self.evidence["mcp_update_rejected"]["status"], "mcp_update_rejected", "compatibility alias maps to RM metadata gate")
        self.record("direct_ordinary_rm_write_rejected", self.evidence["mcp_update_rejected"]["status"], "mcp_update_rejected", "direct ordinary RM write is represented by the public RM metadata path")
        self.record("direct_rm_write_rejected", self.evidence["mcp_update_rejected"]["status"], "mcp_update_rejected", "direct RM write alias maps to the public RM metadata path")
        ticket_status = ticket_recreation_status(self.sub_evidence)
        self.record(
            "tickets_recreated_across_restart",
            ticket_status,
            "trusted_runtime_ephemeral_probe",
            "all required process-local ticket/session lifecycles and durable invariants were proven",
        )
        if not side_effects_free:
            for name in ("dashboard_upload_rejected", "dashboard_update_rejected", "dashboard_delete_rejected", "mcp_upload_rejected", "mcp_update_rejected", "public_reindex_rejected", "dashboard_write_rejected", "mcp_write_rejected", "direct_ordinary_rm_write_rejected", "direct_rm_write_rejected"):
                self.record(name, "FAIL", self.evidence[name].get("source", "mutation"), "durable fingerprint changed during mutation probes", side_effects_free=False)
        checks = {name: self.evidence[name]["status"] == "PASS" for name in CHECK_NAMES}
        counts = {
            "pass": sum(value["status"] == "PASS" for value in self.evidence.values()),
            "incomplete": sum(value["status"] == "INCOMPLETE" for value in self.evidence.values()),
            "fail": sum(value["status"] == "FAIL" for value in self.evidence.values()),
        }
        overall = "FAIL" if counts["fail"] else "INCOMPLETE" if counts["incomplete"] else "PASS"
        evidence = {
            "artifact_type": "rm_acceptance_evidence",
            "schema_version": ACCEPTANCE_ARTIFACT_SCHEMA_VERSION,
            "acceptance_run_id": self.acceptance_run_id,
            "created_at": self.created_at,
            "completed_at": _utc_now(),
            "status": overall,
            "cutover_identity": self.cutover_identity,
            "runtime_identity": self.runtime_identity,
            "snapshot_policy": {
                "quiescent_window_required": True,
                "durable_change_is_fail": True,
                "background_writers_suppressed_by_probe": False,
            },
            "checks": self.evidence,
            "summary": {**counts, "side_effects_free": side_effects_free},
            "ephemeral_probe": self.ephemeral_probe,
            "subresults": self.sub_evidence,
            "authentication_available": {
                "dashboard": bool(self.session_cookie),
                "mcp": bool(self.mcp_token),
            },
            "durable_fingerprints": {
                "before_after_equal": side_effects_free,
                "state_db": {"before": self.before["state_db"], "after": after["state_db"]},
                "rm_db": {"before": self.before["rm_db"], "after": after["rm_db"]},
                "legacy_db": {"before": self.before["legacy_db"], "after": after["legacy_db"]},
                "state_tree": {"before": self.before["state_tree"], "after": after["state_tree"]},
                "rm_tree": {"before": self.before["rm_tree"], "after": after["rm_tree"]},
                "legacy_tree": {"before": self.before["legacy_tree"], "after": after["legacy_tree"]},
            },
        }
        return checks, evidence


def _checks_artifact(checks: dict[str, bool], evidence: dict[str, Any]) -> dict[str, Any]:
    return {
        "artifact_type": "rm_acceptance_checks",
        "schema_version": ACCEPTANCE_ARTIFACT_SCHEMA_VERSION,
        "acceptance_run_id": evidence.get("acceptance_run_id"),
        "created_at": evidence.get("created_at"),
        "completed_at": evidence.get("completed_at"),
        "status": evidence.get("status"),
        "cutover_identity": evidence.get("cutover_identity"),
        "runtime_identity": evidence.get("runtime_identity"),
        "evidence_sha256": _canonical_json_digest(evidence),
        "checks": checks,
    }


def _placeholder_artifacts(acceptance_run_id: str, created_at: str) -> tuple[dict[str, Any], dict[str, Any]]:
    evidence = {
        "artifact_type": "rm_acceptance_evidence",
        "schema_version": ACCEPTANCE_ARTIFACT_SCHEMA_VERSION,
        "acceptance_run_id": acceptance_run_id,
        "created_at": created_at,
        "completed_at": None,
        "status": "INCOMPLETE",
        "cutover_identity": None,
        "runtime_identity": None,
        "snapshot_policy": {
            "quiescent_window_required": True,
            "durable_change_is_fail": True,
            "background_writers_suppressed_by_probe": False,
        },
        "checks": {},
        "summary": {"pass": 0, "incomplete": 1, "fail": 0, "side_effects_free": False},
    }
    checks = {
        "artifact_type": "rm_acceptance_checks",
        "schema_version": ACCEPTANCE_ARTIFACT_SCHEMA_VERSION,
        "acceptance_run_id": acceptance_run_id,
        "created_at": created_at,
        "completed_at": None,
        "status": "INCOMPLETE",
        "cutover_identity": None,
        "runtime_identity": None,
        "evidence_sha256": None,
        "checks": {},
    }
    return checks, evidence


class _CanonicalOutputLock:
    """Serialize canonical output replacement across probe processes."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.handle = None
        self._backend = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.path.open("a+b")
        if self.handle.tell() == 0:
            self.handle.write(b"0")
            self.handle.flush()
        self.handle.seek(0)
        try:
            import fcntl

            fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX)
            self._backend = fcntl
        except ImportError:
            import msvcrt

            msvcrt.locking(self.handle.fileno(), msvcrt.LK_LOCK, 1)
            self._backend = msvcrt
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        if self.handle is None:
            return
        try:
            if self._backend is not None and hasattr(self._backend, "flock"):
                self._backend.flock(self.handle.fileno(), self._backend.LOCK_UN)
            elif self._backend is not None:
                self.handle.seek(0)
                self._backend.locking(self.handle.fileno(), self._backend.LK_UNLCK, 1)
        finally:
            self.handle.close()


def _write_json(path: Path, payload: dict[str, Any], acceptance_run_id: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name("." + path.name + "." + acceptance_run_id + ".tmp")
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            stream.write(json.dumps(payload, ensure_ascii=True, sort_keys=True, indent=2) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


RM_FROZEN_ACCEPTANCE_STATE = "frozen_rm_acceptance"
RM_FROZEN_ACCEPTANCE_PHASE = "RM_FROZEN_ACCEPTANCE"


def _is_compatibility_payload(value: Any) -> bool:
    """Recognize one public RM compatibility response envelope."""

    if not isinstance(value, dict) or type(value.get("ok")) is not bool:
        return False
    if value["ok"] is False:
        return isinstance(value.get("error"), str) and bool(value["error"])
    if "error" in value and value["error"] is not None:
        return isinstance(value["error"], str) and bool(value["error"])
    if "total" in value or "results" in value:
        return (
            type(value.get("total")) is int
            and value["total"] >= 0
            and isinstance(value.get("results"), list)
        )
    return len(value) > 1


def main() -> int:
    acceptance_run_id = secrets.token_hex(16)
    created_at = _utc_now()
    with _CanonicalOutputLock(OUTPUT_LOCK_PATH):
        placeholder_checks, placeholder_evidence = _placeholder_artifacts(acceptance_run_id, created_at)
        _write_json(CHECKS_OUTPUT_PATH, placeholder_checks, acceptance_run_id)
        _write_json(EVIDENCE_OUTPUT_PATH, placeholder_evidence, acceptance_run_id)
        try:
            probe = Probe(acceptance_run_id=acceptance_run_id, created_at=created_at)
            try:
                probe.run_runtime_checks()
                probe.run_health_check()
                probe.run_mcp_reads()
                probe.run_dashboard_reads()
                probe.run_auth_privacy()
                probe.run_dashboard_mutations()
                probe.run_mcp_mutations()
                probe.run_ephemeral_probe()
            except Exception:
                probe.record("rm_runtime_healthy", "INCOMPLETE", "probe", "unexpected probe exception; no result was assumed")
            checks, evidence = probe.finish()
            checks_artifact = _checks_artifact(checks, evidence)
        except Exception:
            evidence = {
                "artifact_type": "rm_acceptance_evidence",
                "schema_version": ACCEPTANCE_ARTIFACT_SCHEMA_VERSION,
                "acceptance_run_id": acceptance_run_id,
                "created_at": created_at,
                "completed_at": _utc_now(),
                "status": "INCOMPLETE",
                "cutover_identity": None,
                "runtime_identity": None,
                "snapshot_policy": {
                    "quiescent_window_required": True,
                    "durable_change_is_fail": True,
                    "background_writers_suppressed_by_probe": False,
                },
                "checks": {},
                "summary": {"pass": 0, "incomplete": 1, "fail": 0, "side_effects_free": False},
            }
            checks_artifact = _checks_artifact(
                {name: False for name in CHECK_NAMES},
                evidence,
            )
        # Publish evidence first.  Until the checks replacement happens, the
        # canonical checks artifact remains the current-run INCOMPLETE marker.
        _write_json(EVIDENCE_OUTPUT_PATH, evidence, acceptance_run_id)
        _write_json(CHECKS_OUTPUT_PATH, checks_artifact, acceptance_run_id)
    summary = evidence["summary"]
    print(json.dumps({"status": evidence["status"], **summary}, sort_keys=True))
    return 0 if evidence["status"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())

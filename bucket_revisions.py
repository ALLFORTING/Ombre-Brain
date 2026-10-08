"""Full bucket revisions and the two-stage restore executor.

Every user-visible bucket write first captures the complete pre-image (body
plus frontmatter) into ``bucket_revisions``. Background runtime writes (touch,
decay, delivery bookkeeping) are intentionally not captured. The legacy
``bucket_history`` table is left untouched and keeps serving ``breath(as_of)``.

Restores never edit history. Restoring an existing bucket first captures its
current version as a ``pre_restore`` revision, so a restore is itself undoable.
Resurrecting a deleted bucket keeps its original ``bucket_id``; the durable
``ob_bucket_restorations`` row covers the completed delete records it replaces
so that only this path can bring a deleted identity back.
"""

from __future__ import annotations

import copy
import difflib
import hashlib
import json
import logging
import os
import sqlite3
from contextlib import closing
from datetime import date, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import frontmatter

from bucket_write_lock import bucket_write_scope
from maintenance_write_gate import guarded_async_mutation, guarded_mutation
from utils import now_iso, safe_path, sanitize_name

logger = logging.getLogger("ombre_brain.revisions")

REVISION_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS bucket_revisions (
    revision_id INTEGER PRIMARY KEY AUTOINCREMENT,
    bucket_id TEXT NOT NULL,
    content TEXT NOT NULL,
    metadata_json TEXT NOT NULL,
    relative_path TEXT,
    op_kind TEXT NOT NULL,
    op_id TEXT,
    sealed_at_capture INTEGER NOT NULL CHECK (sealed_at_capture IN (0, 1)),
    captured_at TEXT NOT NULL,
    preimage_sha256 TEXT NOT NULL,
    baseline_key TEXT UNIQUE
)
"""
REVISION_INDEX_SQL = (
    "CREATE INDEX IF NOT EXISTS idx_bucket_revisions_bucket "
    "ON bucket_revisions(bucket_id, revision_id)"
)
RESTORATION_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS ob_bucket_restorations (
    restore_id TEXT PRIMARY KEY,
    bucket_id TEXT NOT NULL,
    mode TEXT NOT NULL CHECK (mode IN ('existing', 'resurrect')),
    source_ref TEXT NOT NULL,
    covered_delete_ids_json TEXT NOT NULL DEFAULT '[]',
    plan_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('pending', 'published', 'completed', 'failed')),
    published_guard_json TEXT,
    pre_revision_id INTEGER,
    report_json TEXT NOT NULL DEFAULT '{}',
    actor TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    completed_at TEXT
)
"""
RESTORATION_ACTIVE_INDEX_SQL = (
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_bucket_restorations_active "
    "ON ob_bucket_restorations(bucket_id) WHERE status IN ('pending', 'published')"
)

# Runtime fields always keep their current value on restore; restoring a
# bucket must not rewind activation, delivery bookkeeping or derived caches.
RUNTIME_FIELDS = (
    "last_active", "activation_count", "trigger_last_seen",
    "tg_summary", "tg_summary_source_hash", "tg_summary_updated_at",
    "_ob_import_operations",
)
RUNTIME_TAGS = ("compressed",)
RELATION_FIELDS = ("related_buckets", "superseded_by", "superseded_at", "supersedes")

# Durable journals whose unfinished records may still apply an effect to a
# bucket identity later. Several executors admit such effects without a source
# incarnation, so a resurrect is refused while any of them still references the
# deleted id; the records themselves are never edited or removed.
PENDING_EFFECT_QUERIES = (
    ("import_operation", "ob_import_operations",
     "SELECT operation_key FROM ob_import_operations WHERE status='planned' "
     "AND (target_bucket_id=?1 OR result_id=?1 OR instr(payload_json, ?1) > 0)"),
    ("keyed_request", "ob_s4_requests",
     "SELECT operation_id FROM ob_s4_requests WHERE status<>'completed' "
     "AND (instr(payload_json, ?1) > 0 OR instr(plan_json, ?1) > 0 OR instr(resolutions_json, ?1) > 0)"),
    ("merge_operation", "merge_operations",
     "SELECT operation_id FROM merge_operations WHERE status<>'complete' "
     "AND (target_id=?1 OR source_id=?1 OR instr(plan_json, ?1) > 0)"),
    ("digest_operation", "digest_operations",
     "SELECT operation_id FROM digest_operations WHERE status<>'complete' AND instr(plan_json, ?1) > 0"),
    ("archive_session_operation", "ob_archive_session_operations",
     "SELECT operation_id FROM ob_archive_session_operations WHERE status<>'completed' AND bucket_id=?1"),
    ("relation_operation", "ob_related_operations",
     "SELECT operation_id FROM ob_related_operations "
     "WHERE coalesce(json_extract(payload, '$.status'), '')<>'complete' AND instr(payload, ?1) > 0"),
    ("confirmed_delete", "ob_confirmed_delete_operations",
     "SELECT delete_id FROM ob_confirmed_delete_operations WHERE status<>'completed' AND bucket_id=?1"),
)
# A keyed request that completed without applying a child (e.g. relation-only
# trace) leaves that child 'planned' for good; its completed parent makes it final.
COMPLETED_PARENT_CHILD = (" AND NOT EXISTS (SELECT 1 FROM ob_s4_requests r WHERE r.status='completed' "
                          "AND instr(r.plan_json, ob_import_operations.operation_key) > 0)")


class RevisionCaptureError(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class RestoreError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _json_default(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return str(value)


def _metadata_json(metadata: dict) -> str:
    return json.dumps(metadata, ensure_ascii=False, sort_keys=True, default=_json_default)


def _sealed(metadata: dict) -> bool:
    try:
        return int(metadata.get("sealed", 0) or 0) == 1
    except (TypeError, ValueError):
        return False


def revision_snapshot(post: frontmatter.Post, relative_path: str | None = None) -> dict:
    """Freeze one pre-image; the caller must hold the bucket writer mutex."""
    metadata_json = _metadata_json(dict(post.metadata))
    content = str(post.content or "")
    return {
        "content": content,
        "metadata_json": metadata_json,
        "relative_path": relative_path,
        "sealed": 1 if _sealed(dict(post.metadata)) else 0,
        "preimage_sha256": hashlib.sha256(
            json.dumps([content, metadata_json], ensure_ascii=False).encode("utf-8")
        ).hexdigest(),
    }


def parse_revision_ref(ref: Any) -> tuple[str, int]:
    """Accept ``r<id>`` (revision) or ``h<id>`` (legacy body-only history)."""
    text = str(ref or "").strip().lower()
    kind = "r"
    if text[:1] in ("r", "h"):
        kind, text = text[0], text[1:]
    if not text.isdigit() or int(text) < 1:
        raise RestoreError("revision_ref_invalid")
    return kind, int(text)


def _related_ids(metadata: dict) -> list[str]:
    from related_integrity import parse_related
    try:
        return list(parse_related(metadata).ids)
    except Exception:
        return []


def _supersedes_ids(metadata: dict) -> list[str]:
    raw = metadata.get("supersedes")
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        return []
    return list(dict.fromkeys(str(item).strip() for item in raw if str(item).strip()))


def _forward(metadata: dict) -> str:
    value = metadata.get("superseded_by")
    return value.strip() if isinstance(value, str) else ""


def _content_diff(before: str, after: str, limit: int = 40) -> list[str]:
    lines = list(difflib.unified_diff(
        before.splitlines(), after.splitlines(), "current", "restored", lineterm="", n=1))
    if len(lines) > limit:
        lines = lines[:limit] + [f"... ({len(lines) - limit} more diff lines)"]
    return lines


def _metadata_diff(before: dict, after: dict) -> list[dict]:
    changes = []
    for key in sorted(set(before) | set(after)):
        if key in RUNTIME_FIELDS:
            continue
        old, new = before.get(key), after.get(key)
        if _metadata_json({"v": old}) != _metadata_json({"v": new}):
            changes.append({"field": key, "current": old, "restored": new})
    return changes


class BucketRevisionMixin:
    """Mixed into BucketManager; relies on its storage helpers."""

    # ---------------------------------------------------------------- schema
    @staticmethod
    def _init_revision_schema(conn) -> None:
        conn.execute(REVISION_TABLE_SQL)
        conn.execute(REVISION_INDEX_SQL)
        conn.execute(RESTORATION_TABLE_SQL)
        conn.execute(RESTORATION_ACTIVE_INDEX_SQL)

    # --------------------------------------------------------------- capture
    def _relative_bucket_path(self, file_path) -> str | None:
        try:
            return Path(file_path).resolve().relative_to(Path(self.base_dir).resolve()).as_posix()
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _insert_revision(conn, bucket_id: str, snapshot: dict, op_kind: str,
                         op_id: str | None = None, *, captured_at: str | None = None,
                         baseline_key: str | None = None) -> int:
        """Insert inside the caller's transaction; replayed op_ids reuse their row."""
        if op_id is not None:
            latest = conn.execute(
                "SELECT revision_id, preimage_sha256, op_id FROM bucket_revisions "
                "WHERE bucket_id=? ORDER BY revision_id DESC LIMIT 1", (bucket_id,)).fetchone()
            if latest is not None and latest[1] == snapshot["preimage_sha256"] and latest[2] == op_id:
                return int(latest[0])
        return int(conn.execute(
            """INSERT INTO bucket_revisions
               (bucket_id, content, metadata_json, relative_path, op_kind, op_id,
                sealed_at_capture, captured_at, preimage_sha256, baseline_key)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (bucket_id, snapshot["content"], snapshot["metadata_json"],
             snapshot.get("relative_path"), op_kind, op_id, int(snapshot["sealed"]),
             captured_at or now_iso(), snapshot["preimage_sha256"], baseline_key),
        ).lastrowid)

    @guarded_mutation("bucket_revision_write")
    def record_revision_snapshots(self, items) -> list[int]:
        """Persist frozen pre-images in one transaction; raising refuses the write."""
        with bucket_write_scope(self.base_dir), closing(sqlite3.connect(self.history_db_path)) as conn:
            ids = [self._insert_revision(conn, bucket_id, snapshot, op_kind, op_id)
                   for bucket_id, snapshot, op_kind, op_id in items]
            conn.commit()
        return ids

    @guarded_mutation("bucket_revision_write")
    def write_with_revision(self, bucket_id: str, snapshot: dict, op_kind: str, op_id, write):
        """Insert a pre-image, run ``write``, and commit only if the write succeeded.

        A failed write rolls the revision back, so a refused write leaves the
        history database byte-identical. A failed insert raises before ``write``.
        """
        with bucket_write_scope(self.base_dir), closing(sqlite3.connect(self.history_db_path)) as conn:
            try:
                self._insert_revision(conn, bucket_id, snapshot, op_kind, op_id)
            except Exception as exc:
                raise RevisionCaptureError("revision_capture_failed") from exc
            try:
                result = write()
            except BaseException:
                conn.rollback()
                raise
            conn.commit()
        return result

    def record_revision(self, bucket_id: str, post: frontmatter.Post, op_kind: str,
                        op_id: str | None = None, *, file_path=None) -> int:
        """Persist one full pre-image before a write; raising refuses the write."""
        snapshot = revision_snapshot(post, self._relative_bucket_path(file_path) if file_path else None)
        return self.record_revision_snapshots([(bucket_id, snapshot, op_kind, op_id)])[0]

    def _capture_current_revision(self, bucket_id: str, op_kind: str, op_id: str | None = None) -> bool:
        """Capture the on-disk version before a write that bypasses update()."""
        with bucket_write_scope(self.base_dir):
            path = self._find_bucket_file(bucket_id)
            if not path:
                return True
            try:
                self.record_revision(bucket_id, frontmatter.load(path), op_kind, op_id, file_path=path)
            except Exception as exc:
                logger.error("Refusing write because revision capture failed for %s: %s", bucket_id, exc)
                return False
        return True

    @staticmethod
    def _derive_revision_op(explicit, operation_key, content_change_type):
        if explicit:
            kind, op_id = (list(explicit) + [None])[:2]
            return str(kind), op_id
        if operation_key:
            if operation_key.startswith("merge:"):
                step = operation_key.split(":")[2] if operation_key.count(":") >= 2 else ""
                return ("merge_target" if step == "target" else "merge_relation"), operation_key
            if operation_key.startswith("digest:"):
                return "digest", operation_key
            return content_change_type or "update", operation_key
        return content_change_type or "update", None

    # ------------------------------------------------------------------ read
    @staticmethod
    def _decode_revision(row) -> dict:
        item = dict(row)
        item["metadata"] = json.loads(item.pop("metadata_json") or "{}")
        item["ref"] = f"r{item['revision_id']}"
        item["sealed_at_capture"] = int(item["sealed_at_capture"])
        return item

    def _open_history_read(self):
        path = Path(self.history_db_path).resolve()
        if not path.exists():
            return None
        conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        return conn

    @staticmethod
    def _table_exists(conn, name: str) -> bool:
        return conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None

    def get_revision(self, revision_id: int) -> dict | None:
        conn = self._open_history_read()
        if conn is None:
            return None
        with closing(conn):
            if not self._table_exists(conn, "bucket_revisions"):
                return None
            row = conn.execute("SELECT * FROM bucket_revisions WHERE revision_id=?",
                               (int(revision_id),)).fetchone()
        return self._decode_revision(row) if row else None

    def list_bucket_revisions(self, bucket_id: str, limit: int = 50) -> list[dict]:
        limit = max(1, min(int(limit or 50), 200))
        conn = self._open_history_read()
        if conn is None:
            return []
        with closing(conn):
            if not self._table_exists(conn, "bucket_revisions"):
                return []
            rows = conn.execute(
                "SELECT * FROM bucket_revisions WHERE bucket_id=? ORDER BY revision_id DESC LIMIT ?",
                (bucket_id, limit)).fetchall()
        return [self._decode_revision(row) for row in rows]

    def get_history_row(self, history_id: int) -> dict | None:
        conn = self._open_history_read()
        if conn is None:
            return None
        with closing(conn):
            row = conn.execute(
                "SELECT id, bucket_id, old_content, changed_at, change_type FROM bucket_history WHERE id=?",
                (int(history_id),)).fetchone()
        return dict(row) if row else None

    def list_history_rows(self, bucket_id: str, limit: int = 50) -> list[dict]:
        limit = max(1, min(int(limit or 50), 200))
        conn = self._open_history_read()
        if conn is None:
            return []
        with closing(conn):
            rows = conn.execute(
                "SELECT id, bucket_id, old_content, changed_at, change_type FROM bucket_history "
                "WHERE bucket_id=? ORDER BY id DESC LIMIT ?", (bucket_id, limit)).fetchall()
        return [dict(row) for row in rows]

    def pending_effects(self, bucket_id: str) -> list[dict]:
        """Unfinished journal records that still reference ``bucket_id``."""
        conn = self._open_history_read()
        if conn is None:
            return []
        found = []
        with closing(conn):
            for kind, table, sql in PENDING_EFFECT_QUERIES:
                if not self._table_exists(conn, table):
                    continue
                if kind == "import_operation" and self._table_exists(conn, "ob_s4_requests"):
                    sql += COMPLETED_PARENT_CHILD
                found.extend({"kind": kind, "id": str(row[0])}
                             for row in conn.execute(sql, (bucket_id,)).fetchall())
        return found

    def restoration_rows(self, bucket_id: str | None = None) -> list[dict]:
        conn = self._open_history_read()
        if conn is None:
            return []
        with closing(conn):
            if not self._table_exists(conn, "ob_bucket_restorations"):
                return []
            sql, args = "SELECT * FROM ob_bucket_restorations", []
            if bucket_id is not None:
                sql += " WHERE bucket_id=?"
                args.append(bucket_id)
            rows = conn.execute(sql + " ORDER BY created_at, restore_id", args).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            for field in ("covered_delete_ids", "plan", "published_guard", "report"):
                raw = item.pop(field + "_json")
                item[field] = json.loads(raw) if raw else None
            result.append(item)
        return result

    # ------------------------------------------------------------- planning
    def _supersession_truth(self, bucket_id: str) -> list[str]:
        """Reverse lists follow current forward pointers, never stale copies."""
        from related_integrity import scan_relation_store
        inventory = scan_relation_store(self.base_dir)
        return sorted(identity for identity, endpoint in inventory.endpoints.items()
                      if identity != bucket_id and _forward(endpoint.metadata) == bucket_id)

    def _resurrect_path(self, bucket_id: str, metadata: dict, revisions: list[dict]) -> str:
        base = Path(self.base_dir).resolve()
        for item in revisions:
            relative = item.get("relative_path")
            if relative:
                candidate = (base / relative).resolve()
                try:
                    candidate.relative_to(base)
                except ValueError:
                    continue
                if candidate.suffix == ".md" and candidate.name[:-3].endswith(bucket_id):
                    return str(candidate)
        bucket_type = metadata.get("type")
        type_dir = {"permanent": self.permanent_dir, "feel": self.feel_dir,
                    "archived": self.archive_dir}.get(bucket_type, self.dynamic_dir)
        domain = metadata.get("domain") or ["未分类"]
        primary = "沉淀物" if bucket_type == "feel" else sanitize_name(str(domain[0])) or "未分类"
        name = sanitize_name(str(metadata.get("name") or "")) if metadata.get("name") else ""
        filename = f"{name}_{bucket_id}.md" if name and name != bucket_id else f"{bucket_id}.md"
        return str(safe_path(os.path.join(type_dir, primary), filename))

    def plan_restore(self, bucket_id: str, ref: str) -> dict:
        """Read-only: describe exactly what a restore would write and skip."""
        bucket_id = str(bucket_id or "").strip()
        if not bucket_id:
            raise RestoreError("bucket_id_required")
        kind, number = parse_revision_ref(ref)
        source = self.get_revision(number) if kind == "r" else self.get_history_row(number)
        if source is None or source["bucket_id"] != bucket_id:
            raise RestoreError("revision_not_found")
        if any(row["status"] in ("pending", "published") for row in self.restoration_rows(bucket_id)):
            raise RestoreError("restore_in_progress")
        revisions = self.list_bucket_revisions(bucket_id, limit=200)
        path = self._find_bucket_file(bucket_id)
        mode = "existing" if path else "resurrect"
        current_post = frontmatter.load(path) if path else None
        current_meta = dict(current_post.metadata) if current_post else {}
        current_content = str(current_post.content or "") if current_post else ""
        current_sha = hashlib.sha256(Path(path).read_bytes()).hexdigest() if path else None

        if kind == "h":
            if mode == "resurrect":
                raise RestoreError("history_row_has_no_metadata")
            source_meta = copy.deepcopy(current_meta)
            source_content = str(source["old_content"] or "")
            source_sealed = None  # unknown for legacy rows
            source_digest = hashlib.sha256(source_content.encode("utf-8")).hexdigest()
        else:
            source_meta = copy.deepcopy(source["metadata"])
            source_content = str(source["content"] or "")
            source_sealed = bool(source["sealed_at_capture"])
            source_digest = source["preimage_sha256"]

        if mode == "existing":
            context_sealed = _sealed(current_meta)
        else:
            context_sealed = bool(revisions and revisions[0]["sealed_at_capture"])
        result_sealed = bool(source_sealed) or context_sealed

        metadata = copy.deepcopy(source_meta)
        metadata["id"] = bucket_id
        if mode == "existing":
            for field in RUNTIME_FIELDS:
                if field in current_meta:
                    metadata[field] = copy.deepcopy(current_meta[field])
                else:
                    metadata.pop(field, None)
            tags = metadata.get("tags")
            if isinstance(tags, list):
                current_tags = current_meta.get("tags") if isinstance(current_meta.get("tags"), list) else []
                tags = [tag for tag in tags if tag not in RUNTIME_TAGS]
                tags += [tag for tag in RUNTIME_TAGS if tag in current_tags]
                metadata["tags"] = tags
            for field in ("related_buckets", "superseded_by", "superseded_at"):
                if field in current_meta:
                    metadata[field] = copy.deepcopy(current_meta[field])
                else:
                    metadata.pop(field, None)
        else:
            for field in ("related_buckets", "superseded_by", "superseded_at"):
                metadata.pop(field, None)
        if result_sealed:
            metadata["sealed"] = 1
        if metadata.get("pinned") or metadata.get("protected"):
            metadata["importance"] = 10
        truth = self._supersession_truth(bucket_id)
        if truth:
            metadata["supersedes"] = truth
        else:
            metadata.pop("supersedes", None)

        from related_integrity import scan_relation_store
        inventory = scan_relation_store(self.base_dir)
        skipped: list[dict] = []
        wanted_related = _related_ids(source_meta)
        apply_related = []
        for identity in wanted_related:
            if identity == bucket_id:
                continue
            if identity in inventory.endpoints:
                apply_related.append(identity)
            else:
                skipped.append({"kind": "related", "target": identity, "reason": "target_missing"})
        for identity in _supersedes_ids(source_meta):
            if identity not in truth:
                skipped.append({"kind": "supersedes", "target": identity,
                                "reason": "target_missing" if identity not in inventory.endpoints
                                else "target_no_longer_points_here"})
        wanted_forward = _forward(source_meta)
        forward = {"superseded_by": None, "superseded_at": None, "apply": False}
        current_forward = _forward(current_meta) if mode == "existing" else ""
        if wanted_forward != current_forward:
            target = inventory.endpoints.get(wanted_forward) if wanted_forward not in ("", "none") else None
            if wanted_forward not in ("", "none") and target is None:
                skipped.append({"kind": "superseded_by", "target": wanted_forward, "reason": "target_missing"})
            elif target is not None and _sealed(target.metadata):
                skipped.append({"kind": "superseded_by", "target": wanted_forward, "reason": "target_sealed"})
            else:
                forward = {"superseded_by": wanted_forward or None,
                           "superseded_at": source_meta.get("superseded_at") if wanted_forward else None,
                           "apply": True}

        if mode == "existing":
            target_path = path
            current_type, next_type = current_meta.get("type"), metadata.get("type")
            if current_type != next_type and {current_type, next_type} <= {"dynamic", "permanent"}:
                type_dir = self.permanent_dir if next_type == "permanent" else self.dynamic_dir
                domain = metadata.get("domain") or ["未分类"]
                target_path = str(safe_path(os.path.join(type_dir, sanitize_name(str(domain[0])) or "未分类"),
                                            os.path.basename(path)))
            elif current_type != next_type:
                metadata["type"] = current_type
                skipped.append({"kind": "type", "target": str(next_type), "reason": "lifecycle_move_unsupported"})
        else:
            target_path = self._resurrect_path(bucket_id, metadata, revisions)

        completed_deletes = [row["delete_id"] for row in self.confirmed_delete_rows()
                             if row["bucket_id"] == bucket_id and row["status"] == "completed"]
        covered = {delete_id for row in self.restoration_rows(bucket_id)
                   if row["status"] in ("published", "completed")
                   for delete_id in (row["covered_delete_ids"] or [])}
        pending = self.pending_effects(bucket_id) if mode == "resurrect" else []
        plan = {
            "bucket_id": bucket_id, "ref": f"{kind}{number}", "mode": mode,
            "pending_effects": pending, "blocked": bool(pending),
            "source_digest": source_digest, "current_sha256": current_sha,
            "result_sealed": result_sealed, "content": source_content, "metadata": metadata,
            "target_relative_path": self._relative_bucket_path(target_path),
            "current_relative_path": self._relative_bucket_path(path) if path else None,
            "related": apply_related, "forward": forward, "skipped": skipped,
            "covered_delete_ids": [i for i in completed_deletes if i not in covered] if mode == "resurrect" else [],
            "content_diff": _content_diff(current_content, source_content),
            "metadata_diff": _metadata_diff(current_meta, metadata),
            "body_changed": current_content != source_content,
        }
        return plan

    @staticmethod
    def restore_confirmation_payload(plan: dict) -> dict:
        """The exact facts a confirm_token is bound to."""
        return {key: plan[key] for key in (
            "bucket_id", "ref", "mode", "pending_effects", "source_digest", "current_sha256", "result_sealed",
            "target_relative_path", "related", "forward", "skipped", "covered_delete_ids")} | {
            "metadata_sha256": hashlib.sha256(_metadata_json(plan["metadata"]).encode("utf-8")).hexdigest(),
            "content_sha256": hashlib.sha256(plan["content"].encode("utf-8")).hexdigest()}

    # ------------------------------------------------------------- execution
    def _restoration_update(self, conn, restore_id, **fields):
        assignments, args = [], []
        for key, value in fields.items():
            column = key + "_json" if key in ("report", "published_guard", "covered_delete_ids", "plan") else key
            assignments.append(f"{column}=?")
            args.append(json.dumps(value, ensure_ascii=False, default=_json_default)
                        if column.endswith("_json") else value)
        assignments.append("updated_at=?")
        args.append(now_iso())
        conn.execute("UPDATE ob_bucket_restorations SET " + ", ".join(assignments)
                     + " WHERE restore_id=?", (*args, restore_id))

    @guarded_mutation("bucket_restore_publish")
    def _publish_restore(self, plan: dict, actor: str) -> str:
        """Durably record intent, then publish the restored file under one mutex hold."""
        bucket_id = plan["bucket_id"]
        base = Path(self.base_dir).resolve()
        target = (base / plan["target_relative_path"]).resolve()
        target.relative_to(base)
        with bucket_write_scope(self.base_dir):
            self.assert_confirmed_delete_writable(bucket_id)
            path = self._find_bucket_file(bucket_id)
            if plan["mode"] == "existing":
                if not path or hashlib.sha256(Path(path).read_bytes()).hexdigest() != plan["current_sha256"]:
                    raise RestoreError("restore_plan_stale")
            elif path:
                raise RestoreError("restore_plan_stale")
            elif target.exists():
                raise RestoreError("restore_target_occupied")
            elif self.pending_effects(bucket_id):
                raise RestoreError("resurrect_blocked_by_pending_effects")
            from bucket_manager import _date_only
            post = frontmatter.Post(plan["content"])
            post.metadata.update(copy.deepcopy(plan["metadata"]))
            post["updated_at"] = _date_only()
            restore_id = uuid4().hex
            with closing(sqlite3.connect(self.history_db_path)) as conn:
                conn.execute("PRAGMA synchronous=FULL")
                conn.execute("BEGIN IMMEDIATE")
                pre_revision_id = None
                if path:
                    pre_revision_id = self._insert_revision(
                        conn, bucket_id, revision_snapshot(frontmatter.load(path), self._relative_bucket_path(path)),
                        "pre_restore", restore_id)
                stored_plan = {k: v for k, v in plan.items() if k not in ("content_diff", "metadata_diff")}
                stored_plan["file_sha256"] = hashlib.sha256(frontmatter.dumps(post).encode("utf-8")).hexdigest()
                created = now_iso()
                conn.execute(
                    """INSERT INTO ob_bucket_restorations
                       (restore_id, bucket_id, mode, source_ref, covered_delete_ids_json, plan_json,
                        status, pre_revision_id, report_json, actor, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?)""",
                    (restore_id, bucket_id, plan["mode"], plan["ref"],
                     json.dumps(plan["covered_delete_ids"]),
                     json.dumps(stored_plan, ensure_ascii=False, default=_json_default),
                     pre_revision_id, json.dumps({"skipped": plan["skipped"], "steps": []}, ensure_ascii=False),
                     actor, created, created))
                conn.commit()
                try:
                    os.makedirs(target.parent, exist_ok=True)
                    self._write_post_atomic(str(target), post)
                    if path and Path(path).resolve() != target:
                        os.unlink(path)
                        self._sync_directory(os.path.dirname(path))
                except Exception:
                    self._restoration_update(conn, restore_id, status="failed")
                    conn.commit()
                    raise
                guard = self.delete_admission.capture(bucket_id)
                self._restoration_update(conn, restore_id, status="published", published_guard=guard)
                conn.commit()
        return restore_id

    @guarded_mutation("bucket_restore_report")
    def _restore_step(self, restore_id: str, step: str, *, status: str | None = None,
                      skipped: list[dict] | None = None) -> dict:
        with bucket_write_scope(self.base_dir), closing(sqlite3.connect(self.history_db_path)) as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT report_json FROM ob_bucket_restorations WHERE restore_id=?",
                               (restore_id,)).fetchone()
            report = json.loads(row[0] or "{}")
            report.setdefault("steps", [])
            report.setdefault("skipped", [])
            if step and step not in report["steps"]:
                report["steps"].append(step)
            report["skipped"].extend(skipped or [])
            fields = {"report": report}
            if status is not None:
                fields["status"] = status
                if status == "completed":
                    fields["completed_at"] = now_iso()
            self._restoration_update(conn, restore_id, **fields)
            conn.commit()
        return report

    async def _restore_followups(self, restoration: dict) -> dict:
        """Idempotent derived-state replay after the file is published."""
        from related_integrity import RelatedError
        restore_id, plan = restoration["restore_id"], restoration["plan"]
        bucket_id = plan["bucket_id"]
        done = set((restoration.get("report") or {}).get("steps") or [])
        if "embedding" not in done:
            if not plan["result_sealed"]:
                await self._refresh_ordinary_embedding_best_effort(bucket_id, plan["content"])
            self._restore_step(restore_id, "embedding")
        if "related" not in done:
            skipped = []
            current = await self.get(bucket_id)
            current_related = _related_ids(current["metadata"]) if current else []
            if plan["related"] != current_related:
                try:
                    self.mutate_related(bucket_id, replace=",".join(plan["related"]))
                except RelatedError as exc:
                    skipped = [{"kind": "related", "target": identity, "reason": exc.code}
                               for identity in plan["related"]]
            self._restore_step(restore_id, "related", skipped=skipped)
        if "supersession" not in done:
            skipped = []
            forward = plan["forward"]
            if forward.get("apply"):
                try:
                    ok = await self.update(bucket_id, superseded_by=forward["superseded_by"],
                                           superseded_at=forward["superseded_at"],
                                           _supersession_reverse=True,
                                           _revision_op=("restore_supersession", restore_id))
                    if not ok:
                        skipped = [{"kind": "superseded_by", "target": forward["superseded_by"],
                                    "reason": "update_refused"}]
                except Exception as exc:  # skip and report; never fail the whole restore
                    skipped = [{"kind": "superseded_by", "target": forward["superseded_by"],
                                "reason": getattr(exc, "code", type(exc).__name__)}]
            self._restore_step(restore_id, "supersession", skipped=skipped)
        if "boot_delta" not in done:
            with bucket_write_scope(self.base_dir):
                guard = self.delete_admission.capture(bucket_id)
                self._record_boot_delta_event(bucket_id, "restored",
                                              {"mode": plan["mode"], "source_ref": plan["ref"]},
                                              _expected_source=guard)
            self._restore_step(restore_id, "boot_delta")
        return self._restore_step(restore_id, "", status="completed")

    @guarded_async_mutation("bucket_restore_execute")
    async def execute_restore(self, plan: dict, *, actor: str = "mcp") -> dict:
        """Execute a previously previewed plan; the caller verified its token."""
        if plan.get("blocked"):
            raise RestoreError("resurrect_blocked_by_pending_effects")
        if plan["result_sealed"]:
            await self._delete_ordinary_embedding(plan["bucket_id"])
        restore_id = self._publish_restore(plan, actor)
        restoration = next(row for row in self.restoration_rows(plan["bucket_id"])
                           if row["restore_id"] == restore_id)
        report = await self._restore_followups(restoration)
        return {"restore_id": restore_id, "mode": plan["mode"], "report": report,
                "pre_revision_id": restoration["pre_revision_id"]}

    async def resume_restorations(self, bucket_id: str | None = None) -> list[str]:
        """Finish follow-ups of restores whose file publication is already durable."""
        resumed = []
        for row in self.restoration_rows(bucket_id):
            if row["status"] == "published":
                await self._restore_followups(row)
                resumed.append(row["restore_id"])
        return resumed

    @guarded_mutation("bucket_restore_recover")
    def recover_restorations(self) -> None:
        """Startup: settle publications interrupted between intent and publish."""
        pending = [row for row in self.restoration_rows() if row["status"] == "pending"]
        if not pending:
            return
        with bucket_write_scope(self.base_dir), closing(sqlite3.connect(self.history_db_path)) as conn:
            for row in pending:
                target = Path(self.base_dir) / row["plan"]["target_relative_path"]
                published = (target.exists() and hashlib.sha256(target.read_bytes()).hexdigest()
                             == row["plan"].get("file_sha256"))
                if published:
                    old = row["plan"].get("current_relative_path")
                    old_path = Path(self.base_dir) / old if old else None
                    if old_path and old_path.resolve() != target.resolve() and old_path.exists():
                        os.unlink(old_path)
                    guard = self.delete_admission.capture(row["bucket_id"])
                    self._restoration_update(conn, row["restore_id"], status="published", published_guard=guard)
                else:
                    self._restoration_update(conn, row["restore_id"], status="failed")
            conn.commit()

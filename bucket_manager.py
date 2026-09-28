# ============================================================
# Module: Memory Bucket Manager (bucket_manager.py)
# 模块：记忆桶管理器
#
# CRUD operations, multi-dimensional index search, activation updates
# for memory buckets.
# 记忆桶的增删改查、多维索引搜索、激活更新。
#
# Core design:
# 核心逻辑：
#   - Each bucket = one Markdown file (YAML frontmatter + body)
#     每个记忆桶 = 一个 Markdown 文件
#   - Storage by type: permanent / dynamic / archive
#     存储按类型分目录
#   - Multi-dimensional soft index: domain + valence/arousal + fuzzy text
#     多维软索引：主题域 + 情感坐标 + 文本模糊匹配
#   - Search strategy: domain pre-filter → weighted multi-dim ranking
#     搜索策略：主题域预筛 → 多维加权精排
#   - Emotion coordinates based on Russell circumplex model:
#     情感坐标基于环形情感模型（Russell circumplex）：
#       valence (0~1): 0=negative → 1=positive
#       arousal (0~1): 0=calm → 1=excited
#
# Depended on by: server.py, decay_engine.py
# 被谁依赖：server.py, decay_engine.py
# ============================================================

import os
import math
import logging
import shutil
import sqlite3
import hashlib
import json
import tempfile
import inspect
import copy
import asyncio
import time
import weakref
from contextlib import nullcontext
from related_integrity import (RelationStore, RelatedError, plan_mutation, scan_relation_store,
                               plan_delete, digest as related_digest)
from bucket_write_lock import bucket_write_scope, initialize_bucket_write_lock
from uuid import UUID, uuid4
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Optional

import frontmatter
import yaml
from frontmatter.default_handlers import YAMLHandler
from rapidfuzz import fuzz

from maintenance_write_gate import (
    DEFAULT_WRITE_COORDINATOR,
    guarded_async_mutation,
    guarded_mutation,
    guarded_optional_async_mutation,
)

from utils import (
    DISPLAY_ALIASES,
    apply_display_aliases,
    apply_display_aliases_to_value,
    generate_bucket_id,
    sanitize_name,
    safe_path,
    now_iso,
)

logger = logging.getLogger("ombre_brain.bucket")

_IMPORT_MARKER_FIELD = "_ob_import_operations"
_S4_LOCKS = weakref.WeakValueDictionary()
_S4_LEASE_SECONDS = 60.0
_IMPORT_OPERATION_STATUSES = frozenset({"planned", "applied"})
_BOOT_DELTA_PROFILES = ("talk", "code", "tg")
TODO_SAID_BY_VALUES = frozenset({"ting", "model", "system", "unknown"})
PROVENANCE_KIND_VALUES = frozenset({"unknown", "summary", "inference", "system"})


class _FeelSourceError(ValueError):
    """Stable, redacted diagnosis for the narrow feel/source path."""

    def __init__(self, code):
        self.code = code
        super().__init__(code)

class SupersessionError(ValueError):
    """Redacted failure at the canonical forward-write boundary."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class _SupersessionLoader(yaml.SafeLoader):
    def construct_mapping(self, node, deep=False):
        keys = [self.construct_object(key, deep=deep) for key, _ in node.value]
        if len(set(keys)) != len(keys):
            raise ValueError("duplicate_frontmatter_key")
        return super().construct_mapping(node, deep=deep)


class _SupersessionHandler(YAMLHandler):
    def load(self, fm, **kwargs):
        metadata = yaml.load(fm, Loader=_SupersessionLoader)
        if not isinstance(metadata, dict):
            raise ValueError("unreadable_frontmatter")
        return metadata


def _successor_id_strict(value):
    if value is None:
        return ""
    if not isinstance(value, str):
        raise SupersessionError("supersession_malformed_forward")
    return value.strip()


def _supersedes_for_mutation(metadata):
    value = metadata.get("supersedes", [])
    values = value.split(",") if isinstance(value, str) else value if isinstance(value, list) else []
    return list(dict.fromkeys(str(item).strip() for item in values if str(item).strip()))


def normalize_provenance_kind(raw: Any, *, strict: bool = False) -> str:
    """Return a safe bucket-body provenance classification.

    Existing frontmatter is intentionally permissive: absent or corrupted
    values are read as ``unknown`` and never rewritten.  New explicit writes
    instead fail closed so a caller cannot silently manufacture a label.
    """
    if isinstance(raw, str) and raw in PROVENANCE_KIND_VALUES:
        return raw
    if strict:
        raise ValueError(
            "provenance_kind must be unknown, summary, inference, or system."
        )
    return "unknown"


def canonicalize_todos(raw: Any) -> list[str]:
    """Return todos in the canonical, ordered ``list[str]`` form."""
    if isinstance(raw, list):
        values = [str(item).strip() for item in raw if item is not None and str(item).strip()]
    elif isinstance(raw, dict):
        values = [
            f"{key}: {value}".strip()
            for key, value in raw.items()
            if str(value).strip()
        ]
    elif isinstance(raw, str):
        text = raw.strip()
        if not text:
            return []
        try:
            parsed = json.loads(text)
        except (TypeError, ValueError):
            parsed = None
        if parsed is not None and parsed != raw:
            return canonicalize_todos(parsed)
        values = [
            line.strip().lstrip("-* ").strip()
            for line in text.replace(",", "\n").splitlines()
            if line.strip().lstrip("-* ").strip()
        ]
    elif raw is None:
        values = []
    else:
        text = str(raw).strip()
        values = [text] if text else []
    return list(dict.fromkeys(values))


def automatic_todo_provenance(todos: Any) -> list[dict[str, Any]]:
    """Describe extracted todos without inventing a speaker or source time.

    Current extraction paths do not establish a trustworthy per-todo source
    timestamp. Execution, capture, and retry times are not ``said_at``.
    """
    return [
        {"text": text, "said_by": "unknown", "said_at": None, "source_bucket": None}
        for text in canonicalize_todos(todos)
    ]


def _todo_provenance_record(
    raw: Any,
    *,
    strict: bool,
) -> dict[str, Any] | None:
    """Validate one optional todo-provenance sidecar record.

    ``todos`` deliberately remains a list of text strings.  This sidecar is
    therefore allowed to be missing or malformed on existing files without
    affecting normal todo reads.  Explicit new writes use ``strict=True`` and
    reject malformed values rather than silently recording invented metadata.
    """
    if not isinstance(raw, dict):
        if strict:
            raise ValueError("todo provenance entries must be objects.")
        return None
    # State conflicts must escape even permissive legacy/malformed fallback.
    terminal = _merge_todo_terminal_state({}, raw)
    allowed = {"id", "text", "said_by", "said_at", "source_bucket", "done_at", "dropped_at"}
    unexpected = set(raw) - allowed
    if unexpected:
        if strict:
            raise ValueError("todo provenance entries contain unsupported fields.")
        return None
    todo_id = raw.get("id")
    if "id" in raw and not valid_todo_id(todo_id):
        if strict:
            raise ValueError("todo id must be todo_<uuid>.")
        return None
    text = raw.get("text")
    if not isinstance(text, str) or not text.strip():
        if strict:
            raise ValueError("todo provenance text must be a non-empty string.")
        return None
    said_by = raw.get("said_by", "unknown")
    if not isinstance(said_by, str) or said_by not in TODO_SAID_BY_VALUES:
        if strict:
            raise ValueError(
                "todo provenance said_by must be ting, model, system, or unknown."
            )
        return None
    said_at = raw.get("said_at")
    if said_at is not None:
        if not isinstance(said_at, str) or not said_at.strip():
            if strict:
                raise ValueError("todo provenance said_at must be an ISO-8601 string or null.")
            return None
        said_at = said_at.strip()
        try:
            datetime.fromisoformat(said_at)
        except (TypeError, ValueError):
            if strict:
                raise ValueError("todo provenance said_at must be an ISO-8601 string or null.")
            return None
    source_bucket = raw.get("source_bucket")
    if source_bucket is not None:
        if not isinstance(source_bucket, str):
            if strict:
                raise ValueError("todo provenance source_bucket must be a string or null.")
            return None
        source_bucket = source_bucket.strip() or None
    for field, timestamp in terminal.items():
        if timestamp is not None:
            try:
                if not valid_todo_id(todo_id) or not isinstance(timestamp, str) or not timestamp.strip():
                    raise ValueError
                datetime.fromisoformat(timestamp)
            except (TypeError, ValueError):
                if strict:
                    raise ValueError(f"todo {field} requires an id and an ISO-8601 string or null.")
                return None
    return {
        **terminal,
        **({"id": todo_id} if todo_id is not None else {}),
        "text": text.strip(),
        "said_by": said_by,
        "said_at": said_at,
        "source_bucket": source_bucket,
    }


def valid_todo_id(value: Any) -> bool:
    """Accept the canonical opaque identity format; never derive it from text."""
    if not isinstance(value, str) or not value.startswith("todo_"):
        return False
    try:
        return str(UUID(value[5:])) == value[5:]
    except (ValueError, AttributeError):
        return False


def _todo_provenance_is_known(record: dict[str, Any] | None) -> bool:
    return bool(record) and (
        record.get("said_by") != "unknown"
        or record.get("said_at") is not None
        or record.get("source_bucket") is not None
    )


def _merge_todo_terminal_state(target: dict, source: dict) -> dict:
    """Sticky, mutually exclusive completion/drop; never arbitrate conflicts."""
    fields = ("done_at", "dropped_at")
    result = {}
    for record in (target, source):
        if all(record.get(field) is not None for field in fields):
            raise ValueError("todo terminal conflict: done_at and dropped_at are mutually exclusive.")
    for field in fields:
        first, incoming = target.get(field), source.get(field)
        if first is not None and incoming is not None and first != incoming:
            label = "completion" if field == "done_at" else "drop"
            raise ValueError(f"todo {label} conflict: one id has different {field} values.")
        if field in target or field in source:
            result[field] = first if first is not None else incoming
    if all(result.get(field) is not None for field in fields):
        raise ValueError("todo terminal conflict: done_at and dropped_at are mutually exclusive.")
    return result


def _todo_is_terminal(record: dict) -> bool:
    """Only for validated records; conflict validation precedes projection."""
    return bool(record.get("done_at") or record.get("dropped_at"))


def _merge_todo_attribution(target: dict, source: dict) -> dict:
    """Attribution choices must never erase or arbitrate terminal state."""
    terminal = _merge_todo_terminal_state(target, source)
    identity = target.get("id") or source.get("id")
    target = {**target, **({"id": identity} if identity else {})}
    source = {**source, **({"id": identity} if identity else {})}
    fields = ("said_by", "said_at", "source_bucket")
    if all(target.get(key) == source.get(key) for key in fields):
        result = target
    elif _todo_provenance_is_known(target) and not _todo_provenance_is_known(source):
        result = target
    elif _todo_provenance_is_known(source) and not _todo_provenance_is_known(target):
        result = source
    else:
        result = {**target, "said_by": "unknown", "said_at": None, "source_bucket": None}
    return {**result, **terminal}


def reconcile_todo_provenance(todos: Any, raw_provenance: Any, *, strict: bool = False) -> list[dict[str, Any]]:
    """Canonical identities plus terminal history; never assign legacy IDs."""
    texts = canonicalize_todos(todos)
    if raw_provenance is None:
        return []
    if not isinstance(raw_provenance, list):
        if strict:
            raise ValueError("todo_provenance must be a list.")
        return []
    records = {}
    # Inspect raw state claims before any unsupported-field/text fallback can
    # hide a conflict for one stable identity.
    terminal_claims = {}
    for raw in raw_provenance:
        if isinstance(raw, dict):
            state = _merge_todo_terminal_state({}, raw)
            if valid_todo_id(raw.get("id")):
                identity = raw["id"]
                terminal_claims[identity] = _merge_todo_terminal_state(
                    terminal_claims.get(identity, {}), state)
    for raw in raw_provenance:
        record = _todo_provenance_record(raw, strict=strict)
        if record is None:
            continue
        key = ("id", record["id"]) if "id" in record else ("text", record["text"])
        previous = records.get(key)
        if previous is not None and previous["text"] != record["text"]:
            if strict:
                raise ValueError("todo identity conflict: one id has different texts.")
            continue
        if record["text"] not in texts and not _todo_is_terminal(record):
            if strict:
                raise ValueError("todo provenance text must appear in todos.")
            continue
        if previous is None:
            records[key] = record
        elif "id" in record:
            records[key] = _merge_todo_attribution(previous, record)
    return ([record for text in texts for record in records.values() if record["text"] == text]
            + [record for record in records.values() if record["text"] not in texts])


def prepare_todo_provenance(todos: Any, raw_provenance: Any = None, *,
                            previous_todos: Any = None, previous_provenance: Any = None,
                            references_only: bool = False, assign_ids: bool = True) -> list[dict[str, Any]]:
    """Prepare explicit writes, retaining sticky terminal state and history."""
    texts = canonicalize_todos(todos)
    incoming = reconcile_todo_provenance(texts, raw_provenance, strict=True)
    previous = reconcile_todo_provenance(previous_todos, previous_provenance, strict=True)
    by_id = {r["id"]: r for r in previous if "id" in r}
    output = []
    for text in texts:
        candidates = [r for r in previous if r["text"] == text]
        submitted = [r for r in incoming if r["text"] == text]
        inherited = not submitted
        if inherited:
            submitted = candidates or automatic_todo_provenance([text])
        for record in submitted:
            record = dict(record)
            identity = record.get("id")
            if identity is not None:
                if references_only and identity not in by_id:
                    raise ValueError("todo id is not an existing identity in this bucket.")
                if not references_only and identity in by_id and by_id[identity]["text"] != text:
                    raise ValueError("todo identity conflict: one id has different texts.")
            else:
                matches = [r for r in candidates if "id" in r]
                # A persisted legacy member alongside terminal history must not vanish
                # through text matching or implicit ID assignment.
                legacy_mixed = any(_todo_is_terminal(r) for r in submitted) or (
                    any(_todo_is_terminal(r) for r in candidates) and (inherited or record in candidates))
                if not legacy_mixed:
                    if len(matches) > 1:
                        raise ValueError("ambiguous todo identity: specify an existing id.")
                    if matches:
                        record["id"] = matches[0]["id"]
                    elif assign_ids:
                        record["id"] = "todo_" + str(uuid4())
            if record.get("id") in by_id:
                record.update(_merge_todo_terminal_state(by_id[record["id"]], record))
            output.append(record)
    # Orphaned incoming terminal records are valid history too.
    for record in incoming:
        if record["text"] not in texts and _todo_is_terminal(record):
            record = dict(record)
            if record["id"] in by_id:
                if by_id[record["id"]]["text"] != record["text"]:
                    raise ValueError("todo identity conflict: one id has different texts.")
                record.update(_merge_todo_terminal_state(by_id[record["id"]], record))
            output.append(record)
    submitted_ids = {r.get("id") for r in output if "id" in r}
    output.extend(r for r in previous if _todo_is_terminal(r) and r["id"] not in submitted_ids)
    return reconcile_todo_provenance(texts, output, strict=True)


def merge_todo_provenance(target_todos: Any, target_provenance: Any,
                          source_todos: Any, source_provenance: Any, *,
                          source_is_persisted: bool = False) -> tuple[list[str], list[dict[str, Any]]]:
    """Union identities; distinguish persisted legacy members from proposals."""
    target, source = canonicalize_todos(target_todos), canonicalize_todos(source_todos)
    texts = list(dict.fromkeys(target + source))
    records = reconcile_todo_provenance(target, target_provenance, strict=True)
    incoming = reconcile_todo_provenance(source, source_provenance, strict=True)
    if source_is_persisted:
        records += automatic_todo_provenance([text for text in target
                   if not any(r["text"] == text for r in records)])
        incoming += automatic_todo_provenance([text for text in source
                    if not any(r["text"] == text for r in incoming)])
    for record in incoming:
        record = dict(record)
        if "id" in record:
            matches = [r for r in records if r.get("id") == record["id"]]
        else:
            matches = [r for r in records if r["text"] == record["text"]]
            if source_is_persisted and any(_todo_is_terminal(r) for r in matches):
                matches = [r for r in matches if "id" not in r]
            if len(matches) > 1:
                if _todo_provenance_is_known(record):
                    raise ValueError("ambiguous todo identity: specify an existing id.")
                continue
        if matches:
            previous = matches[0]
            if previous["text"] != record["text"]:
                raise ValueError("todo identity conflict: one id has different texts.")
            if "id" in previous:
                record["id"] = previous["id"]
            records[records.index(previous)] = _merge_todo_attribution(previous, record)
        else:
            records.append(record)
    return texts, reconcile_todo_provenance(texts, records, strict=True)


def active_todo_projection(todos: Any, raw_provenance: Any) -> tuple[list[str], list[dict]]:
    """Derive activity without modifying persistence or inventing IDs."""
    texts = canonicalize_todos(todos)
    records = reconcile_todo_provenance(texts, raw_provenance)
    active_texts, active_records = [], []
    for text in texts:
        matches = [r for r in records if r["text"] == text]
        # Malformed same-text sidecars remain a conservative legacy contribution.
        invalid_legacy = isinstance(raw_provenance, list) and any(
            isinstance(raw, dict) and raw.get("text") == text
            and _todo_provenance_record(raw, strict=False) is None for raw in raw_provenance)
        active = [r for r in matches if not _todo_is_terminal(r)]
        if not matches or invalid_legacy:
            active += automatic_todo_provenance([text])
        if active:
            active_texts.append(text)
            active_records.extend(active)
    return active_texts, active_records


def todo_completion_plan(bucket_id: str, post: Any, todo_id: str, *, operation: str = "todo_done") -> dict:
    """Bind confirmation only to canonical todo state, never unrelated metadata."""
    if not valid_todo_id(todo_id):
        if operation == "todo_drop":
            raise ValueError("todo_drop requires a stable todo_<uuid> ID. 该 todo 为旧格式，无稳定 ID，当前不能单条放弃。")
        raise ValueError("todo_done requires a stable todo_<uuid> ID; legacy todo without an ID cannot be completed.")
    todos = canonicalize_todos(post.get("todos"))
    records = reconcile_todo_provenance(todos, post.get("todo_provenance"), strict=True)
    target = next((r for r in records if r.get("id") == todo_id), None)
    if target is None:
        if operation == "todo_drop":
            raise ValueError("unknown todo ID. 该 todo 为旧格式，无稳定 ID，当前不能单条放弃。")
        raise ValueError("unknown todo ID; legacy todo without a stable ID cannot be located or completed.")
    state = {"todos": todos, "todo_provenance": [
        {**r, "done_at": r.get("done_at"), "dropped_at": r.get("dropped_at")} for r in records]}
    digest = hashlib.sha256(json.dumps(state, ensure_ascii=False, sort_keys=True,
                            separators=(",", ":")).encode("utf-8")).hexdigest()
    return {"bucket_id": bucket_id, "todo_id": todo_id, "target": target,
            "todo_state_sha256": digest}

class BucketIdempotencyError(RuntimeError):
    """Content-free failure for the O5B memory idempotency seam."""

    def __init__(self, code: str = "idempotency_conflict") -> None:
        self.code = code
        super().__init__(code)


def _date_only(value: str | None = None) -> str:
    """Return YYYY-MM-DD, defaulting to today when parsing fails."""
    if value:
        try:
            return datetime.fromisoformat(str(value)).date().isoformat()
        except (ValueError, TypeError):
            pass
    return datetime.now().date().isoformat()


def _is_sealed_bucket(bucket: Any) -> bool:
    """Return True for either a loaded bucket dict or a frontmatter Post."""
    metadata = bucket.get("metadata") if isinstance(bucket.get("metadata"), dict) else bucket
    try:
        return int(metadata.get("sealed", 0) or 0) == 1
    except (TypeError, ValueError):
        return False


class BucketManager:
    """
    Memory bucket manager — entry point for all bucket CRUD operations.
    Buckets are stored as Markdown files with YAML frontmatter for metadata
    and body for content. Natively compatible with Obsidian browsing/editing.
    记忆桶管理器 —— 所有桶的 CRUD 操作入口。
    桶以 Markdown 文件存储，YAML frontmatter 存元数据，正文存内容。
    天然兼容 Obsidian 直接浏览和编辑。
    """

    def __init__(
        self,
        config: dict,
        embedding_engine=None,
        write_coordinator=None,
    ):
        self.write_coordinator = write_coordinator or DEFAULT_WRITE_COORDINATOR
        # --- Read storage paths from config / 从配置中读取存储路径 ---
        self.base_dir = config["buckets_dir"]
        self.permanent_dir = os.path.join(self.base_dir, "permanent")
        self.dynamic_dir = os.path.join(self.base_dir, "dynamic")
        self.archive_dir = os.path.join(self.base_dir, "archive")
        self.feel_dir = os.path.join(self.base_dir, "feel")
        self.history_db_path = os.path.join(self.base_dir, "bucket_history.sqlite3")
        self.fuzzy_threshold = config.get("matching", {}).get("fuzzy_threshold", 50)
        self.max_results = config.get("matching", {}).get("max_results", 5)

        # --- Wikilink config / 双链配置 ---
        wikilink_cfg = config.get("wikilink", {})
        self.wikilink_enabled = wikilink_cfg.get("enabled", True)
        self.wikilink_use_tags = wikilink_cfg.get("use_tags", False)
        self.wikilink_use_domain = wikilink_cfg.get("use_domain", True)
        self.wikilink_use_auto_keywords = wikilink_cfg.get("use_auto_keywords", True)
        self.wikilink_auto_top_k = wikilink_cfg.get("auto_top_k", 8)
        self.wikilink_min_len = wikilink_cfg.get("min_keyword_len", 2)
        self.wikilink_exclude_keywords = set(wikilink_cfg.get("exclude_keywords", []))
        self.wikilink_stopwords = {
            "的", "了", "在", "是", "我", "有", "和", "就", "不", "人",
            "都", "一个", "上", "也", "很", "到", "说", "要", "去",
            "你", "会", "着", "没有", "看", "好", "自己", "这", "他", "她",
            "我们", "你们", "他们", "然后", "今天", "昨天", "明天", "一下",
            "the", "and", "for", "are", "but", "not", "you", "all", "can",
            "had", "her", "was", "one", "our", "out", "has", "have", "with",
            "this", "that", "from", "they", "been", "said", "will", "each",
        }
        self.wikilink_stopwords |= {w.lower() for w in self.wikilink_exclude_keywords}

        # --- Search scoring weights / 检索权重配置 ---
        scoring = config.get("scoring_weights", {})
        self.w_topic = scoring.get("topic_relevance", 4.0)
        self.w_emotion = scoring.get("emotion_resonance", 2.0)
        self.w_time = scoring.get("time_proximity", 1.5)
        self.w_importance = scoring.get("importance", 1.0)
        self.content_weight = scoring.get("content_weight", 1.0)  # body×1, per spec

        # --- Optional embedding engine for pre-filtering / 可选 embedding 引擎，用于预筛候选集 ---
        self.embedding_engine = embedding_engine
        self._init_history_db()
        initialize_bucket_write_lock(self.base_dir)
        self.relation_store = RelationStore(self.base_dir, self.write_coordinator)
        self.recover_related_operations()

    def preview_related(self, source_id, **kwargs):
        return self.relation_store.preview(source_id, **kwargs)

    def mutate_related(self, source_id, **kwargs):
        return self.relation_store.mutate(source_id, **kwargs)

    def apply_related_plan(self, plan, *, operation_key=None):
        if plan.get('kind') == 'repair':
            return self.relation_store.apply_repair(plan)
        if plan.get('kind') != 'relation':
            raise RelatedError('related_plan_stale')
        def validate(inventory):
            request = plan['request']
            replacement = {'replace': request['replace']} if request['replacement'] else {}
            expected = plan_mutation(inventory, request['source'], add=request['add'],
                remove=request['remove'], origin=request['origin'], **replacement)
            if expected != plan:
                raise RelatedError('related_plan_stale')
            return expected
        return self.relation_store.commit(validate, operation_key=operation_key,
                                           request_digest=related_digest(plan['request']))

    def recover_related_operations(self):
        return self.relation_store.recover()

    def preview_related_delete(self, source_id, *, target_id=None):
        return plan_delete(scan_relation_store(self.base_dir), source_id, target_id=target_id)

    async def _delete_ordinary_embedding(self, bucket_id: str) -> None:
        """Delete the local ordinary vector, regardless of provider enablement."""
        if self.embedding_engine is None:
            return
        delete_embedding = getattr(self.embedding_engine, "delete_embedding", None)
        if delete_embedding is None:
            return
        result = delete_embedding(bucket_id)
        if inspect.isawaitable(result):
            await result

    async def _refresh_ordinary_embedding_best_effort(
        self,
        bucket_id: str,
        content: str,
    ) -> None:
        """Refresh an unsealed vector without making provider failure fatal."""
        if not self.embedding_engine or not getattr(self.embedding_engine, "enabled", False):
            return
        try:
            await self.embedding_engine.generate_and_store(bucket_id, content)
        except Exception as exc:
            logger.warning(f"Embedding refresh failed for {bucket_id}: {exc}")

    def _init_history_db(self) -> None:
        """Create the write-ahead bucket history table if needed."""
        os.makedirs(self.base_dir, exist_ok=True)
        with sqlite3.connect(self.history_db_path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS bucket_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    bucket_id TEXT NOT NULL,
                    old_content TEXT NOT NULL,
                    changed_at TEXT NOT NULL,
                    change_type TEXT NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_bucket_history_bucket_id "
                "ON bucket_history(bucket_id)"
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS letters (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    content TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    sealed INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            columns = {
                row[1] for row in conn.execute("PRAGMA table_info(letters)").fetchall()
            }
            if "sealed" not in columns:
                conn.execute(
                    "ALTER TABLE letters ADD COLUMN sealed INTEGER NOT NULL DEFAULT 0"
                )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_letters_created_at "
                "ON letters(created_at)"
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS notes (
                    note_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    author TEXT NOT NULL,
                    via TEXT NOT NULL,
                    text TEXT NOT NULL,
                    sealed INTEGER NOT NULL DEFAULT 0,
                    open_at TEXT,
                    boot_delivered_at TEXT,
                    read_at TEXT,
                    skipped_at TEXT,
                    skipped_reason TEXT,
                    dismissed_at TEXT
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_notes_boot_delivery "
                "ON notes(sealed, open_at, boot_delivered_at, skipped_at, note_id)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_notes_created_at "
                "ON notes(created_at)"
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS boot_delta_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    bucket_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    occurred_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_boot_delta_events_bucket_id "
                "ON boot_delta_events(bucket_id, id)"
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS boot_delta_checkpoint (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                    last_event_id INTEGER NOT NULL,
                    completed_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS boot_delta_profile_checkpoints (
                    profile TEXT PRIMARY KEY,
                    last_event_id INTEGER NOT NULL,
                    completed_at TEXT NOT NULL
                )
                """
            )
            profile_count = conn.execute(
                "SELECT COUNT(*) FROM boot_delta_profile_checkpoints"
            ).fetchone()[0]
            legacy_checkpoint = conn.execute(
                """
                SELECT last_event_id, completed_at
                FROM boot_delta_checkpoint
                WHERE singleton = 1
                """
            ).fetchone()
            if profile_count == 0 and legacy_checkpoint is not None:
                conn.executemany(
                    """
                    INSERT INTO boot_delta_profile_checkpoints
                        (profile, last_event_id, completed_at)
                    VALUES (?, ?, ?)
                    """,
                    [
                        (profile, int(legacy_checkpoint[0]), legacy_checkpoint[1])
                        for profile in _BOOT_DELTA_PROFILES
                    ],
                )

    def _ensure_import_operation_table(self) -> None:
        """Create the lazy O5B operation journal only when capture is used."""

        with sqlite3.connect(self.history_db_path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS ob_import_operations (
                    operation_key TEXT PRIMARY KEY,
                    operation_kind TEXT NOT NULL CHECK (operation_kind IN ('create', 'update')),
                    target_bucket_id TEXT,
                    payload_json TEXT NOT NULL,
                    payload_digest TEXT NOT NULL,
                    result_id TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (status IN ('planned', 'applied')),
                    memory_mutation_id TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            columns = {
                row[1]
                for row in conn.execute(
                    "PRAGMA table_info(ob_import_operations)"
                ).fetchall()
            }
            if "memory_mutation_id" not in columns:
                conn.execute(
                    "ALTER TABLE ob_import_operations "
                    "ADD COLUMN memory_mutation_id TEXT"
                )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_ob_import_operations_target "
                "ON ob_import_operations(target_bucket_id)"
            )

    @staticmethod
    def _canonical_import_payload(payload: dict[str, Any]) -> tuple[str, str]:
        if not isinstance(payload, dict):
            raise BucketIdempotencyError("operation_payload_invalid")
        try:
            serialized = json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        except (TypeError, ValueError) as exc:
            raise BucketIdempotencyError("operation_payload_invalid") from exc
        return serialized, hashlib.sha256(serialized.encode("utf-8")).hexdigest()

    @staticmethod
    def _operation_result_id(operation_key: str) -> str:
        return hashlib.sha256(
            f"ombre-brain:o5b:bucket:{operation_key}".encode("utf-8")
        ).hexdigest()[:32]

    def _get_import_operation(self, operation_key: str) -> dict[str, Any] | None:
        with sqlite3.connect(self.history_db_path) as conn:
            conn.row_factory = sqlite3.Row
            if not conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'ob_import_operations'"
            ).fetchone():
                return None
            row = conn.execute(
                "SELECT * FROM ob_import_operations WHERE operation_key = ?",
                (operation_key,),
            ).fetchone()
        if row is None:
            return None
        result = dict(row)
        try:
            result["payload"] = json.loads(result.pop("payload_json"))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise BucketIdempotencyError("operation_payload_invalid") from exc
        return result

    def _ensure_import_operation(
        self,
        operation_key: str,
        *,
        operation_kind: str | None = None,
        target_bucket_id: str | None = None,
        payload: dict[str, Any] | None = None,
        payload_digest: str | None = None,
        memory_mutation_id: str | None = None,
    ) -> dict[str, Any]:
        if not isinstance(operation_key, str) or not operation_key or len(operation_key) > 128:
            raise BucketIdempotencyError("operation_key_invalid")
        if operation_kind is not None and operation_kind not in {"create", "update"}:
            raise BucketIdempotencyError("operation_kind_invalid")
        if target_bucket_id is not None and not isinstance(target_bucket_id, str):
            raise BucketIdempotencyError("target_bucket_invalid")
        if memory_mutation_id is not None and (
            not isinstance(memory_mutation_id, str)
            or len(memory_mutation_id) != 64
            or any(char not in "0123456789abcdef" for char in memory_mutation_id)
        ):
            raise BucketIdempotencyError("memory_mutation_invalid")
        if payload is not None and payload_digest is None:
            existing = self._get_import_operation(operation_key)
            if existing is not None:
                # Repeated planning reuses stored IDs, but still rejects changed input.
                compared = json.loads(json.dumps(payload))
                stored = existing["payload"]
                requested = compared if operation_kind == "create" else compared.get("kwargs", {})
                persisted = stored if operation_kind == "create" else stored.get("kwargs", {})
                if ("todos" in requested and any(
                    "id" in record for record in persisted.get("todo_provenance", [])
                    if isinstance(record, dict)
                )):
                    requested["todo_provenance"] = prepare_todo_provenance(
                        requested["todos"], requested.get("todo_provenance"),
                        previous_todos=persisted.get("todos"),
                        previous_provenance=persisted["todo_provenance"],
                    )
                payload = compared
            else:
                payload = json.loads(json.dumps(payload))
                values = payload if operation_kind == "create" else payload.get("kwargs", {})
                if "todos" in values or "todo_provenance" in values:
                    previous = None
                    if operation_kind == "update" and target_bucket_id:
                        path = self._find_bucket_file(target_bucket_id)
                        previous = frontmatter.load(path) if path else None
                    values["todo_provenance"] = prepare_todo_provenance(
                        values.get("todos", previous.get("todos") if previous else []),
                        values.get("todo_provenance"),
                        previous_todos=previous.get("todos") if previous else None,
                        previous_provenance=previous.get("todo_provenance") if previous else None,
                    )
        if payload is not None:
            serialized, computed_digest = self._canonical_import_payload(payload)
            if payload_digest is not None and payload_digest != computed_digest:
                raise BucketIdempotencyError("operation_payload_conflict")
            payload_digest = computed_digest
        else:
            serialized = None

        self._ensure_import_operation_table()
        now = now_iso()
        with sqlite3.connect(self.history_db_path) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM ob_import_operations WHERE operation_key = ?",
                (operation_key,),
            ).fetchone()
            if row is None:
                if operation_kind is None or serialized is None or payload_digest is None:
                    conn.rollback()
                    raise BucketIdempotencyError("operation_plan_missing")
                result_id = (
                    self._operation_result_id(operation_key)
                    if operation_kind == "create"
                    else target_bucket_id
                )
                if not result_id:
                    conn.rollback()
                    raise BucketIdempotencyError("target_bucket_invalid")
                conn.execute(
                    """
                    INSERT INTO ob_import_operations (
                        operation_key, operation_kind, target_bucket_id,
                        payload_json, payload_digest, result_id, status,
                        memory_mutation_id, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, 'planned', ?, ?, ?)
                    """,
                    (
                        operation_key,
                        operation_kind,
                        target_bucket_id,
                        serialized,
                        payload_digest,
                        result_id,
                        memory_mutation_id,
                        now,
                        now,
                    ),
                )
            else:
                if operation_kind is not None and row["operation_kind"] != operation_kind:
                    conn.rollback()
                    raise BucketIdempotencyError("operation_payload_conflict")
                if target_bucket_id is not None and row["target_bucket_id"] != target_bucket_id:
                    conn.rollback()
                    raise BucketIdempotencyError("operation_payload_conflict")
                if payload_digest is not None and row["payload_digest"] != payload_digest:
                    conn.rollback()
                    raise BucketIdempotencyError("operation_payload_conflict")
                if memory_mutation_id is not None:
                    existing_mutation_id = row["memory_mutation_id"]
                    if (
                        existing_mutation_id is not None
                        and existing_mutation_id != memory_mutation_id
                    ):
                        conn.rollback()
                        raise BucketIdempotencyError("memory_mutation_conflict")
                    if existing_mutation_id is None:
                        conn.execute(
                            """
                            UPDATE ob_import_operations
                            SET memory_mutation_id = ?, updated_at = ?
                            WHERE operation_key = ?
                            """,
                            (memory_mutation_id, now, operation_key),
                        )
            conn.commit()
        result = self._get_import_operation(operation_key)
        if result is None:
            raise BucketIdempotencyError("operation_not_found")
        return result

    def _mark_import_operation_applied(self, operation_key: str) -> None:
        with sqlite3.connect(self.history_db_path) as conn:
            conn.execute(
                """
                UPDATE ob_import_operations SET status = 'applied', updated_at = ?
                WHERE operation_key = ?
                """,
                (now_iso(), operation_key),
            )

    @staticmethod
    def _operation_marker(post: frontmatter.Post, operation_key: str) -> dict[str, Any] | None:
        markers = post.get(_IMPORT_MARKER_FIELD, [])
        if markers in (None, ""):
            return None
        if not isinstance(markers, list):
            raise BucketIdempotencyError("operation_marker_invalid")
        for marker in markers:
            if not isinstance(marker, dict):
                raise BucketIdempotencyError("operation_marker_invalid")
            if (marker.get("operation_key") == operation_key
                    and marker.get("operation_kind") in {"create", "update"}):
                return marker
        return None

    @classmethod
    def _append_operation_marker(
        cls,
        post: frontmatter.Post,
        *,
        operation_key: str,
        payload_digest: str,
        operation_kind: str,
        memory_mutation_id: str | None = None,
    ) -> bool:
        existing = cls._operation_marker(post, operation_key)
        if existing is not None:
            if existing.get("payload_digest") != payload_digest:
                raise BucketIdempotencyError("operation_payload_conflict")
            if (
                memory_mutation_id is not None
                and existing.get("memory_mutation_id") != memory_mutation_id
            ):
                raise BucketIdempotencyError("memory_mutation_conflict")
            return False
        markers = post.get(_IMPORT_MARKER_FIELD, [])
        if markers in (None, ""):
            markers = []
        if not isinstance(markers, list):
            raise BucketIdempotencyError("operation_marker_invalid")
        marker = {
            "operation_key": operation_key,
            "payload_digest": payload_digest,
            "operation_kind": operation_kind,
        }
        if memory_mutation_id is not None:
            marker["memory_mutation_id"] = memory_mutation_id
        markers.append(marker)
        post[_IMPORT_MARKER_FIELD] = markers
        return True

    @staticmethod
    def _write_post_atomic(file_path: str, post: frontmatter.Post) -> None:
        """Atomically publish an O5B-marked memory file."""
        BucketManager._write_bytes_atomic(file_path, frontmatter.dumps(post).encode("utf-8"))

    @staticmethod
    def _write_bytes_atomic(file_path: str, payload: bytes) -> None:
        """Publish frozen bytes with file and directory persistence barriers."""
        parent = os.path.dirname(file_path)
        fd, temporary = tempfile.mkstemp(
            prefix=f".{os.path.basename(file_path)}.",
            suffix=".tmp",
            dir=parent,
        )
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, file_path)
            BucketManager._sync_directory(parent)
        except Exception:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise

    @staticmethod
    def _sync_directory(path: str) -> None:
        if os.name != "nt":
            fd = os.open(path, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)

    @guarded_mutation("bucket_import_idempotency_plan")
    def plan_import_operation(
        self,
        operation_key: str,
        *,
        operation_kind: str,
        target_bucket_id: str | None = None,
        payload: dict[str, Any],
        memory_mutation_id: str | None = None,
    ) -> dict[str, Any]:
        """Durably plan an O5B operation before any memory file mutation."""

        with bucket_write_scope(self.base_dir):
            return self._ensure_import_operation(
                operation_key,
                operation_kind=operation_kind,
                target_bucket_id=target_bucket_id,
                payload=payload,
                memory_mutation_id=memory_mutation_id,
            )

    @guarded_async_mutation("bucket_import_idempotency_apply")
    async def apply_import_operation(
        self,
        operation_key: str,
        *,
        operation_kind: str | None = None,
        target_bucket_id: str | None = None,
        payload: dict[str, Any] | None = None,
        payload_digest: str | None = None,
        memory_mutation_id: str | None = None,
        _s4_context: dict | None = None,
    ) -> dict[str, Any]:
        """Apply or replay one durable import memory operation."""

        with bucket_write_scope(self.base_dir) if _s4_context is not None else nullcontext():
            if _s4_context is not None:
                self._trace_fence(_s4_context)
            operation = self._ensure_import_operation(
                operation_key,
                operation_kind=operation_kind,
                target_bucket_id=target_bucket_id,
                payload=payload,
                payload_digest=payload_digest,
                memory_mutation_id=memory_mutation_id,
            )
        stored_payload = operation["payload"]
        if operation["operation_kind"] == "create":
            bucket_id = await self.create(
                **stored_payload,
                _o5b_operation_key=operation_key,
                _o5b_payload_digest=operation["payload_digest"],
                _o5c_memory_mutation_id=operation.get("memory_mutation_id"),
            )
            return {"operation_key": operation_key, "result_id": bucket_id, "kind": "create"}

        bucket_id = operation["target_bucket_id"]
        if not bucket_id or not isinstance(stored_payload, dict):
            raise BucketIdempotencyError("operation_payload_invalid")
        update_kwargs = stored_payload.get("kwargs")
        if not isinstance(update_kwargs, dict):
            raise BucketIdempotencyError("operation_payload_invalid")
        applied = await self.update(
            bucket_id,
            **update_kwargs,
            _o5b_operation_key=operation_key,
            _o5b_payload_digest=operation["payload_digest"],
            _o5c_memory_mutation_id=operation.get("memory_mutation_id"),
            **({"_s4_context": _s4_context} if _s4_context is not None else {}),
        )
        if not applied:
            raise BucketIdempotencyError("target_bucket_missing")
        return {"operation_key": operation_key, "result_id": bucket_id, "kind": "update"}

    @staticmethod
    def validate_trace_operation_id(operation_id):
        if operation_id is not None and (
            not isinstance(operation_id, str) or not 1 <= len(operation_id) <= 128
        ):
            raise BucketIdempotencyError("operation_id_invalid")

    def inspect_trace_request(self, operation_id):
        """Read a request without creating a table or inspecting its target."""
        with sqlite3.connect(self.history_db_path) as conn:
            conn.row_factory = sqlite3.Row
            if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='ob_s4_requests'").fetchone():
                return None
            row = conn.execute("SELECT * FROM ob_s4_requests WHERE operation_id=?", (operation_id,)).fetchone()
        if row is None:
            return None
        result = dict(row)
        for field in ("payload", "normalization_context", "plan", "resolutions"):
            result[field] = json.loads(result.pop(field + "_json"))
        return result

    def _ensure_trace_request_table(self):
        self._ensure_import_operation_table()
        with sqlite3.connect(self.history_db_path) as conn:
            schema = """CREATE TABLE IF NOT EXISTS ob_s4_requests (
                operation_id TEXT PRIMARY KEY COLLATE BINARY,
                kind TEXT NOT NULL CHECK(kind IN ('trace','hold','grow')), schema_version INTEGER NOT NULL,
                root_binding TEXT NOT NULL, payload_json TEXT NOT NULL,
                payload_digest TEXT NOT NULL, normalization_context_json TEXT NOT NULL,
                phase TEXT NOT NULL, status TEXT NOT NULL,
                plan_json TEXT NOT NULL DEFAULT '{}', resolutions_json TEXT NOT NULL DEFAULT '{}',
                result_text TEXT, completed_receipt_json TEXT,
                owner_instance TEXT, epoch INTEGER NOT NULL DEFAULT 0,
                lease_until REAL NOT NULL DEFAULT 0, created_at TEXT NOT NULL,
                completed_at TEXT, last_error_code TEXT)"""
            conn.execute('BEGIN IMMEDIATE')
            old = conn.execute("SELECT sql FROM sqlite_master WHERE name='ob_s4_requests'").fetchone()
            if old and "CHECK(kind='trace')" in ''.join(old[0].split()):
                # SQLite cannot ALTER a CHECK. Keep all original values inside
                # one transaction; a crash rolls the entire replacement back.
                before = conn.execute('SELECT * FROM ob_s4_requests ORDER BY operation_id').fetchall()
                conn.execute('ALTER TABLE ob_s4_requests RENAME TO ob_s4_requests_v1')
                conn.execute(schema)
                conn.execute('INSERT INTO ob_s4_requests SELECT * FROM ob_s4_requests_v1')
                if conn.execute('SELECT * FROM ob_s4_requests ORDER BY operation_id').fetchall() != before:
                    raise BucketIdempotencyError('operation_schema_conflict')
                conn.execute('DROP TABLE ob_s4_requests_v1')
            else:
                conn.execute(schema)
            columns = {r[1] for r in conn.execute("PRAGMA table_info(ob_import_operations)")}
            if "effects_json" not in columns:
                conn.execute("ALTER TABLE ob_import_operations ADD COLUMN effects_json TEXT")

    @guarded_mutation("trace_request_claim")
    def _claim_trace_request(self, operation_id, payload, normalization_context, owner):
        if payload.get('kind') != 'trace':
            raise BucketIdempotencyError('operation_id_conflict')
        return self._claim_s4_request(operation_id, payload, normalization_context, owner)

    @guarded_mutation("s4_request_claim")
    def _claim_s4_request(self, operation_id, payload, normalization_context, owner):
        self.validate_trace_operation_id(operation_id)
        kind = payload.get('kind')
        if kind not in ('trace', 'hold', 'grow'):
            raise BucketIdempotencyError('operation_id_conflict')
        serialized, digest = self._canonical_import_payload(payload)
        root = str(Path(self.base_dir).resolve())
        with bucket_write_scope(self.base_dir):
            self._ensure_trace_request_table()
            with sqlite3.connect(self.history_db_path) as conn:
                conn.row_factory = sqlite3.Row
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute("SELECT * FROM ob_s4_requests WHERE operation_id=?", (operation_id,)).fetchone()
                if row is None:
                    if kind == 'trace':
                        path = self._find_bucket_file(payload['bucket_id'])
                        if not path:
                            raise BucketIdempotencyError('target_bucket_missing')
                        post = frontmatter.load(path)
                        if _is_sealed_bucket(post):
                            raise BucketIdempotencyError('unsupported_combination: sealed target')
                        if payload['content'] and post.get('protected'):
                            raise BucketIdempotencyError('content_protected')
                    conn.execute("""INSERT INTO ob_s4_requests
                        (operation_id,kind,schema_version,root_binding,payload_json,payload_digest,
                         normalization_context_json,phase,status,created_at)
                        VALUES (?,?,?, ?,?,?,?,'accepted','pending',?)""",
                        (operation_id, kind, 1 if kind == 'trace' else 2, root, serialized, digest,
                         json.dumps(normalization_context), now_iso()))
                else:
                    if row['kind'] != kind or row['payload_digest'] != digest:
                        raise BucketIdempotencyError("operation_id_conflict")
                    if row['root_binding'] != root:
                        raise BucketIdempotencyError("operation_root_conflict")
                    if row['status'] == 'completed':
                        return {"replay": row['result_text']}
                    if row['owner_instance'] and row['lease_until'] > time.time():
                        return None
                conn.execute("""UPDATE ob_s4_requests SET owner_instance=?, epoch=epoch+1,
                    lease_until=? WHERE operation_id=?""", (owner, time.time() + _S4_LEASE_SECONDS, operation_id))
                epoch = conn.execute("SELECT epoch FROM ob_s4_requests WHERE operation_id=?", (operation_id,)).fetchone()[0]
        return {"operation_id": operation_id, "owner": owner, "epoch": epoch}

    def _trace_fence(self, context):
        request = self.inspect_trace_request(context['operation_id'])
        if (request is None or request['owner_instance'] != context['owner']
                or request['epoch'] != context['epoch'] or request['lease_until'] <= time.time()):
            raise BucketIdempotencyError("operation_claim_stale")
        return request

    @guarded_mutation("trace_request_checkpoint")
    def _trace_checkpoint(self, context, phase=None, *, plan=None, resolutions=None, result=None):
        with bucket_write_scope(self.base_dir):
            request = self._trace_fence(context)
            with sqlite3.connect(self.history_db_path) as conn:
                conn.execute("""UPDATE ob_s4_requests SET phase=?, plan_json=?, resolutions_json=?,
                    result_text=?, completed_receipt_json=?, status=?, completed_at=?, lease_until=?
                    WHERE operation_id=?""",
                    (phase or request['phase'], json.dumps(plan if plan is not None else request['plan']),
                     json.dumps(resolutions if resolutions is not None else request['resolutions']),
                     result if result is not None else request['result_text'],
                     json.dumps(resolutions) if phase == 'completed' else request['completed_receipt_json'],
                     'completed' if phase == 'completed' else 'pending',
                     now_iso() if phase == 'completed' else request['completed_at'],
                     time.time() + _S4_LEASE_SECONDS, context['operation_id']))

    @guarded_mutation("trace_request_release")
    def _release_trace_request(self, context):
        with bucket_write_scope(self.base_dir), sqlite3.connect(self.history_db_path) as conn:
            conn.execute("""UPDATE ob_s4_requests SET owner_instance=NULL,lease_until=0
                WHERE operation_id=? AND owner_instance=? AND epoch=?""",
                (context['operation_id'], context['owner'], context['epoch']))

    async def _trace_heartbeat(self, context):
        while True:
            await asyncio.sleep(_S4_LEASE_SECONDS / 3)
            self._trace_checkpoint(context)

    def _trace_child_key(self, operation_id, step, *, kind='trace'):
        serialized = json.dumps([
            "s4-child-v1", str(Path(self.base_dir).resolve()), kind, operation_id, step],
            ensure_ascii=False, separators=(',', ':'))
        return "s4:" + hashlib.sha256(serialized.encode('utf-8')).hexdigest()

    def _trace_preimage_guard(self, plan):
        path = self._find_bucket_file(plan['target'])
        if not path or hashlib.sha256(Path(path).read_bytes()).hexdigest() != plan['preimage_digest']:
            raise BucketIdempotencyError("operation_state_conflict")

    @staticmethod
    def _trace_alias_value(value, aliases):
        if isinstance(value, str):
            for source, target in aliases:
                value = value.replace(source, target)
        elif isinstance(value, list):
            value = [BucketManager._trace_alias_value(item, aliases) for item in value]
        return value

    @guarded_mutation("trace_request_plan")
    def _plan_trace_request(self, context, planner):
        with bucket_write_scope(self.base_dir):
            request = self._trace_fence(context)
            target = request['payload']['bucket_id']
            path = self._find_bucket_file(target)
            if not path:
                raise BucketIdempotencyError("target_bucket_missing")
            post = frontmatter.load(path)
            updates, response = planner({"content": post.content, "metadata": copy.deepcopy(post.metadata)})
            updates = copy.deepcopy(updates)
            aliases = request['normalization_context']['aliases']
            history_type = updates.pop('_history_change_type', 'replace')
            if 'content' in updates:
                updates['content'] = self._trace_alias_value(updates['content'], aliases)
            if 'todos' in updates:
                updates['todos'] = self._trace_alias_value(canonicalize_todos(updates['todos']), aliases)
                updates['todo_provenance'] = prepare_todo_provenance(
                    updates['todos'], updates.get('todo_provenance'),
                    previous_todos=post.get('todos'), previous_provenance=post.get('todo_provenance'),
                    references_only=updates.pop('_todo_references_only', False))
            for field in ('tags', 'domain'):
                if field in updates:
                    updates[field] = self._trace_alias_value(updates[field], aliases)
            if 'name' in updates:
                updates['name'] = sanitize_name(self._trace_alias_value(updates['name'], aliases))
            delta = []
            if 'content' in updates and updates['content'] != post.content:
                delta.append(['content_updated', {}])
            old_todos, new_todos = canonicalize_todos(post.get('todos')), updates.get('todos', canonicalize_todos(post.get('todos')))
            if new_todos != old_todos:
                delta.append(['todos_updated', {'closed_count': len(set(old_todos)-set(new_todos)),
                                               'opened_count': len(set(new_todos)-set(old_todos))}])
            relation = {'source': target, 'add': request['payload']['related'],
                        'remove': request['payload']['unrelate'], 'origin': 'explicit'}
            if relation['add'] or relation['remove']:
                self.preview_related(target, add=relation['add'], remove=relation['remove'], origin=relation['origin'])
            plan = {'target': target, 'preimage_digest': hashlib.sha256(Path(path).read_bytes()).hexdigest(),
                    'updates': updates, 'logical_time': now_iso(), 'history_type': history_type,
                    'old_content': post.content, 'delta': delta, 'relation': relation,
                    'response': response, 'embedding_input': updates.get('content'),
                    'embedding_model': getattr(self.embedding_engine, 'model', ''),
                    'keys': {step: self._trace_child_key(context['operation_id'], step)
                             for step in ('memory', 'embedding', 'relation')}}
            if request['payload'].get('todo_items') is not None:
                plan['response'] += '\n' + '\n'.join(
                    f"- {r['text']} | todo_id:{r['id']}" for r in updates.get('todo_provenance', []))
            self._trace_checkpoint(context, 'planned', plan=plan)

    @guarded_mutation("trace_effect_commit")
    def _trace_effect_commit(self, context, step):
        """History/delta INSERT and child receipt share one SQLite transaction."""
        with bucket_write_scope(self.base_dir):
            plan = self._trace_fence(context)['plan']
            child = self._ensure_import_operation(plan['keys']['memory'], operation_kind='update',
                target_bucket_id=plan['target'], payload={'kwargs': plan['updates']})
            if step == 'history' and child['status'] != 'applied':
                self._trace_preimage_guard(plan)
            with sqlite3.connect(self.history_db_path) as conn:
                conn.execute('BEGIN IMMEDIATE')
                row = conn.execute('SELECT effects_json FROM ob_import_operations WHERE operation_key=?',
                                   (plan['keys']['memory'],)).fetchone()
                effects = json.loads(row[0] or '{}')
                if step in effects:
                    return effects[step]
                if step == 'history':
                    receipt = {'outcome': 'not_requested'}
                    if 'content' in plan['updates']:
                        receipt = {'history_id': conn.execute("""INSERT INTO bucket_history
                            (bucket_id,old_content,changed_at,change_type) VALUES(?,?,?,?)""",
                            (plan['target'], plan['old_content'], plan['logical_time'], plan['history_type'])).lastrowid}
                elif step == 'delta':
                    receipt = {'event_ids': [self._insert_boot_delta_event(conn, plan['target'], kind,
                        json.dumps(payload, ensure_ascii=False, sort_keys=True), plan['logical_time'])
                        for kind, payload in plan['delta']]}
                else:
                    raise ValueError('trace_effect_invalid')
                effects[step] = receipt
                conn.execute('UPDATE ob_import_operations SET effects_json=? WHERE operation_key=?',
                             (json.dumps(effects), plan['keys']['memory']))
                return receipt

    @guarded_mutation("trace_embedding_commit")
    def _commit_trace_embedding(self, context, candidate):
        with bucket_write_scope(self.base_dir):
            plan = self._trace_fence(context)['plan']
            engine = self.embedding_engine
            receipt = engine.trace_embedding_receipt(plan['keys']['embedding'])
            if receipt is not None:
                return receipt
            path = self._find_bucket_file(plan['target'])
            post = frontmatter.load(path) if path else None
            if (post is None or _is_sealed_bucket(post) or post.content != plan['embedding_input']
                    or post.get('last_active') != plan['logical_time']):
                return {'outcome': 'superseded_before_refresh'}
            return engine.store_trace_embedding(plan['keys']['embedding'], plan['target'], candidate,
                hashlib.sha256(plan['embedding_input'].encode()).hexdigest(),
                plan['embedding_model'], plan['logical_time'])

    @guarded_mutation("trace_relation_commit")
    def _commit_trace_relation(self, context):
        with bucket_write_scope(self.base_dir):
            plan = self._trace_fence(context)['plan']
            request = plan['relation']
            if not request['add'] and not request['remove']:
                return {'status': 'not_requested'}
            return self.relation_store.commit(lambda inv: plan_mutation(inv, request['source'],
                add=request['add'], remove=request['remove'], origin=request['origin']),
                operation_key=plan['keys']['relation'], request_digest=related_digest(request))

    @guarded_async_mutation("trace_request_execute")
    async def execute_trace_request(self, operation_id, payload, normalization_context, planner):
        """Finite trace-only runner. No provider await holds the storage mutex."""
        self.validate_trace_operation_id(operation_id)
        lock_key = (os.getpid(), asyncio.get_running_loop(), str(Path(self.base_dir).resolve()), operation_id)
        lock = _S4_LOCKS.setdefault(lock_key, asyncio.Lock())
        async with lock:
            owner = uuid4().hex
            while (context := self._claim_trace_request(operation_id, payload, normalization_context, owner)) is None:
                await asyncio.sleep(.05)
            if 'replay' in context:
                return context['replay']
            heartbeat = asyncio.create_task(self._trace_heartbeat(context))
            try:
                request = self.inspect_trace_request(operation_id)
                if request['phase'] == 'accepted':
                    self._plan_trace_request(context, planner)
                request = self.inspect_trace_request(operation_id)
                plan, resolutions = request['plan'], request['resolutions']
                phases = ('planned', 'history_committed', 'memory_applied', 'embedding_resolved',
                          'delta_resolved', 'relation_resolved', 'completed')
                phase = phases.index(request['phase'])
                if phase < 1:
                    resolutions['history'] = self._trace_effect_commit(context, 'history')
                    self._trace_checkpoint(context, 'history_committed', resolutions=resolutions)
                if phase < 2:
                    if plan['updates']:
                        await self.apply_import_operation(plan['keys']['memory'], _s4_context=context)
                    resolutions['memory'] = {'outcome': 'applied' if plan['updates'] else 'not_requested'}
                    self._trace_checkpoint(context, 'memory_applied', resolutions=resolutions)
                if phase < 3:
                    engine = self.embedding_engine
                    receipt = engine.trace_embedding_receipt(plan['keys']['embedding']) if engine else None
                    if receipt is None:
                        if plan['embedding_input'] is None:
                            receipt = {'outcome': 'not_requested'}
                        elif not engine or not engine.enabled:
                            receipt = {'outcome': 'disabled'}
                        else:
                            candidate = resolutions.get('embedding_candidate')
                            if candidate is None:
                                try:
                                    candidate = await engine._generate_embedding(plan['embedding_input'], model=plan['embedding_model'])
                                except Exception:
                                    candidate = []
                                resolutions['embedding_candidate'] = candidate
                                self._trace_checkpoint(context, resolutions=resolutions)
                            try:
                                receipt = self._commit_trace_embedding(context, candidate) if candidate else {'outcome': 'failed'}
                            except BucketIdempotencyError:
                                raise
                            except Exception:
                                # An ambiguous storage error is resolved by durable evidence.
                                # If that read also fails, leave the request pending.
                                receipt = engine.trace_embedding_receipt(plan['keys']['embedding']) or {'outcome': 'failed'}
                    resolutions['embedding'] = receipt
                    self._trace_checkpoint(context, 'embedding_resolved', resolutions=resolutions)
                if phase < 4:
                    resolutions['delta'] = self._trace_effect_commit(context, 'delta')
                    self._trace_checkpoint(context, 'delta_resolved', resolutions=resolutions)
                if phase < 5:
                    resolutions['relation'] = self._commit_trace_relation(context)
                    self._trace_checkpoint(context, 'relation_resolved', resolutions=resolutions)
                relation = resolutions['relation']['status']
                result = plan['response']
                for field, ids in (('related', plan['relation']['add']), ('unrelate', plan['relation']['remove'])):
                    if ids:
                        result += f", {field}={','.join(ids)} ({relation})"
                self._trace_checkpoint(context, 'completed', resolutions=resolutions, result=result)
                return result
            finally:
                heartbeat.cancel()
                try:
                    await heartbeat
                except asyncio.CancelledError:
                    pass
                finally:
                    self._release_trace_request(context)

    @guarded_mutation('hold_grow_item_plan')
    def plan_hold_grow_item(self, context, ordinal, values, candidate=None):
        """Freeze only the current hold/grow item, before its first effect."""
        with bucket_write_scope(self.base_dir):
            request = self._trace_fence(context)
            parent = request['plan']
            entry = parent['items'][ordinal]
            if entry.get('plan') is not None:
                return entry['plan']
            keys = {step: self._trace_child_key(context['operation_id'], f'{ordinal}:{step}',
                                              kind=request['kind'])
                    for step in ('memory', 'embedding', 'relation', 'trigger', 'source', 'emotion')}
            aliases = request['normalization_context']['aliases']
            values = copy.deepcopy(values)
            todos = list(dict.fromkeys(self._trace_alias_value(canonicalize_todos(values.get('todos')), aliases)))
            provenance = automatic_todo_provenance(todos)
            logical_time = now_iso()
            updates, delta, preimage = {}, [], None
            reused = candidate is not None
            if reused:
                target = candidate['id']
                path = self._find_bucket_file(target)
                if not path:
                    raise BucketIdempotencyError('target_bucket_missing')
                post = frontmatter.load(path)
                if (_is_sealed_bucket(post) or post.get('type') == 'feel'
                        or post.get('pinned') or post.get('protected')
                        or self._normalize_search_text(post.content) != self._normalize_search_text(values['content'])):
                    raise BucketIdempotencyError('operation_state_conflict')
                date = values.get('trigger_date', '')
                old_date = str(post.get('trigger_date', '') or '').strip()
                if date and old_date and old_date != date:
                    raise ValueError(f"duplicate bucket {target} has trigger_date={old_date}; requested {date} was rejected without writing")
                if date and old_date != date:
                    updates.update(trigger_date=date, trigger_last_seen='')
                old_todos = canonicalize_todos(post.get('todos'))
                if todos:
                    merged, records = merge_todo_provenance(old_todos, post.get('todo_provenance'), todos, provenance)
                    if merged != old_todos or not isinstance(post.get('todos'), list):
                        updates['todos'] = merged
                    if records != reconcile_todo_provenance(old_todos, post.get('todo_provenance')):
                        updates['todo_provenance'] = records
                    if merged != old_todos:
                        delta.append(['todos_updated', {'closed_count': len(set(old_todos)-set(merged)),
                                                        'opened_count': len(set(merged)-set(old_todos))}])
                written = sorted(updates)
                # Assign identities only when legacy reuse would actually write
                # todos/provenance. An unchanged old sidecar is not a backfill.
                if 'todos' in updates or 'todo_provenance' in updates:
                    updates['todo_provenance'] = prepare_todo_provenance(
                        updates.get('todos', old_todos), updates.get('todo_provenance', post.get('todo_provenance')),
                        previous_todos=old_todos, previous_provenance=post.get('todo_provenance'))
                preimage = hashlib.sha256(Path(path).read_bytes()).hexdigest()
                child_payload = {'kwargs': updates}
                relative_path, file_text = None, None
                result_name = post.get('name', target)
                ignored = ['tags', 'importance', 'domain', 'valence', 'arousal', 'name', 'provenance_kind']
            else:
                target = self._operation_result_id(keys['memory'])
                post = self._build_bucket_post(target, values['content'], tags=values.get('tags'),
                    importance=values.get('importance', 5), domain=values.get('domain'),
                    valence=values.get('valence', .5), arousal=values.get('arousal', .3),
                    bucket_type=values.get('bucket_type', 'dynamic'), name=values.get('name'),
                    pinned=values.get('pinned', False), todos=todos, todo_provenance=provenance,
                    provenance_kind=values.get('provenance_kind'), created=logical_time,
                    last_active=logical_time, created_date=_date_only(logical_time), _aliases=aliases)
                child_payload = {'content': post.content, 'metadata': copy.deepcopy(post.metadata)}
                _, child_digest = self._canonical_import_payload(child_payload)
                self._append_operation_marker(post, operation_key=keys['memory'],
                                              operation_kind='create', payload_digest=child_digest)
                type_dir = self.feel_dir if post['type'] == 'feel' else (
                    self.permanent_dir if post['type'] == 'permanent' else self.dynamic_dir)
                domain = '沉淀物' if post['type'] == 'feel' else sanitize_name(post['domain'][0])
                filename = f"{post['name']}_{target}.md" if post['name'] != target else f'{target}.md'
                relative_path = str(Path(safe_path(os.path.join(type_dir, domain), filename)).relative_to(self.base_dir))
                file_text = frontmatter.dumps(post)
                result_name = target
                delta = [['created', {}]]
                written = ['content', 'tags', 'importance', 'domain', 'valence', 'arousal', 'name', 'todos',
                           *(['todo_provenance'] if provenance else []),
                           *(['trigger_date'] if values.get('trigger_date') else [])]
                ignored = []
            _, child_digest = self._canonical_import_payload(child_payload)
            item_plan = dict(target=target, reused=reused, values=values, keys=keys,
                logical_time=logical_time, updates=updates, preimage_digest=preimage,
                child_payload=child_payload, child_digest=child_digest, relative_path=relative_path,
                file_text=file_text, metadata=copy.deepcopy(post.metadata), result_name=result_name,
                written_fields=written, ignored_fields=ignored, delta=delta,
                embedding_input=None if reused else post.content,
                embedding_model=getattr(self.embedding_engine, 'model', ''),
                related_threshold=float(os.environ.get('OMBRE_RELATED_THRESHOLD', '.75') or '.75'),
                related_top_k=3)
            entry['plan'] = item_plan
            self._trace_checkpoint(context, 'items_running', plan=parent)
            return item_plan

    @guarded_mutation('hold_grow_memory_commit')
    def commit_hold_grow_memory(self, context, ordinal):
        with bucket_write_scope(self.base_dir):
            plan = self._trace_fence(context)['plan']['items'][ordinal]['plan']
            child = self._ensure_import_operation(plan['keys']['memory'],
                operation_kind='update' if plan['reused'] else 'create',
                target_bucket_id=plan['target'] if plan['reused'] else None,
                payload=plan['child_payload'], payload_digest=plan['child_digest'])
            path = self._find_bucket_file(plan['target'])
            post = frontmatter.load(path) if path else None
            marker = self._operation_marker(post, plan['keys']['memory']) if post is not None else None
            if marker:
                if marker['payload_digest'] != plan['child_digest']:
                    raise BucketIdempotencyError('operation_payload_conflict')
                self._sync_directory(os.path.dirname(path))
                self._mark_import_operation_applied(plan['keys']['memory'])
                return {'outcome': 'applied'}
            if child['status'] == 'applied':
                raise BucketIdempotencyError('operation_marker_missing')
            if plan['reused']:
                self._trace_preimage_guard(plan)
                if not plan['updates']:
                    return {'outcome': 'not_requested'}
                post.metadata.update(copy.deepcopy(plan['updates']))
                post['last_active'], post['updated_at'] = plan['logical_time'], _date_only(plan['logical_time'])
                self._append_operation_marker(post, operation_key=plan['keys']['memory'],
                                              operation_kind='update', payload_digest=plan['child_digest'])
                payload = frontmatter.dumps(post).encode('utf-8')
            else:
                if path:
                    raise BucketIdempotencyError('idempotency_conflict')
                path = safe_path(self.base_dir, plan['relative_path'])
                os.makedirs(os.path.dirname(path), exist_ok=True)
                payload = plan['file_text'].encode('utf-8')
            self._trace_fence(context)
            self._write_bytes_atomic(path, payload)
            if Path(path).read_bytes() != payload:
                raise BucketIdempotencyError('operation_state_conflict')
            self._mark_import_operation_applied(plan['keys']['memory'])
            return {'outcome': 'applied'}

    @guarded_mutation('hold_grow_delta_commit')
    def commit_hold_grow_delta(self, context, ordinal):
        with bucket_write_scope(self.base_dir):
            plan = self._trace_fence(context)['plan']['items'][ordinal]['plan']
            with sqlite3.connect(self.history_db_path) as conn:
                conn.execute('BEGIN IMMEDIATE')
                row = conn.execute('SELECT effects_json FROM ob_import_operations WHERE operation_key=?',
                                   (plan['keys']['memory'],)).fetchone()
                effects = json.loads(row[0] or '{}')
                if 'delta' not in effects:
                    effects['delta'] = {'event_ids': [self._insert_boot_delta_event(conn, plan['target'],
                        kind, json.dumps(payload, ensure_ascii=False, sort_keys=True), plan['logical_time'])
                        for kind, payload in plan['delta']]}
                    conn.execute('UPDATE ob_import_operations SET effects_json=? WHERE operation_key=?',
                                 (json.dumps(effects), plan['keys']['memory']))
                return effects['delta']

    @guarded_mutation('hold_grow_trigger_commit')
    def commit_hold_grow_trigger(self, context, ordinal):
        with bucket_write_scope(self.base_dir):
            request = self._trace_fence(context)
            plan = request['plan']['items'][ordinal]['plan']
            date = plan['values'].get('trigger_date', '')
            if plan['reused'] or not date:
                return {'outcome': 'not_requested'}
            key = plan['keys']['trigger']
            payload = {'kwargs': {'trigger_date': date, 'trigger_last_seen': ''}}
            _, digest = self._canonical_import_payload(payload)
            child = self._ensure_import_operation(key, operation_kind='update', target_bucket_id=plan['target'],
                                                 payload=payload, payload_digest=digest)
            path = self._find_bucket_file(plan['target'])
            if not path:
                raise BucketIdempotencyError('target_bucket_missing')
            post = frontmatter.load(path)
            marker = self._operation_marker(post, key)
            if marker:
                if marker['payload_digest'] != digest:
                    raise BucketIdempotencyError('operation_payload_conflict')
                self._sync_directory(os.path.dirname(path))
                self._mark_import_operation_applied(key)
                return {'outcome': 'applied'}
            if child['status'] == 'applied':
                raise BucketIdempotencyError('operation_marker_missing')
            if _is_sealed_bucket(post) or str(post.get('trigger_date') or '') not in ('', date):
                raise BucketIdempotencyError('operation_state_conflict')
            post.metadata.update(payload['kwargs'])
            post['last_active'], post['updated_at'] = plan['logical_time'], _date_only(plan['logical_time'])
            self._append_operation_marker(post, operation_key=key, operation_kind='update', payload_digest=digest)
            self._trace_fence(context)
            frozen = frontmatter.dumps(post).encode('utf-8')
            self._write_bytes_atomic(path, frozen)
            if Path(path).read_bytes() != frozen:
                raise BucketIdempotencyError('operation_state_conflict')
            self._mark_import_operation_applied(key)
            return {'outcome': 'applied'}

    def inspect_import_operation(self, operation_key: str) -> dict[str, Any] | None:
        """Inspect an O5B operation and its hidden atomic marker read-only."""

        with sqlite3.connect(self.history_db_path) as conn:
            if not conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' "
                "AND name = 'ob_import_operations'"
            ).fetchone():
                return None
        operation = self._get_import_operation(operation_key)
        if operation is None:
            return None
        result_id = operation.get("result_id")
        file_path = self._find_bucket_file(result_id) if result_id else None
        marker = None
        if file_path:
            try:
                post = frontmatter.load(file_path)
                marker = self._operation_marker(post, operation_key)
            except Exception as exc:
                raise BucketIdempotencyError("operation_marker_invalid") from exc
        return {
            **operation,
            "memory_exists": file_path is not None,
            "memory_path": file_path,
            "marker": marker,
        }

    @guarded_mutation("digest_operation_write")
    def write_digest_operation(
        self, operation_id: str, kind: str, plan: dict, *,
        owner: str, status: str = "running", outputs: dict | None = None,
        completed: list[str] | None = None, recover: bool = False,
    ) -> None:
        """Persist one digest-only execution record before touching memory files."""
        if kind not in {"consolidation", "rebalance"} or status not in {"running", "failed", "complete"}:
            raise ValueError("invalid digest operation")
        with sqlite3.connect(self.history_db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS digest_operations (
                    operation_id TEXT PRIMARY KEY, kind TEXT NOT NULL,
                    plan_json TEXT NOT NULL, owner TEXT NOT NULL,
                    status TEXT NOT NULL, outputs_json TEXT NOT NULL,
                    completed_json TEXT NOT NULL, updated_at TEXT NOT NULL
                )
            """)
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT kind, plan_json, owner, status, outputs_json, completed_json "
                "FROM digest_operations WHERE operation_id = ?",
                (operation_id,),
            ).fetchone()
            plan_json = json.dumps(plan, ensure_ascii=False, sort_keys=True)
            if row is not None and row[:2] != (kind, plan_json):
                raise ValueError("digest operation plan changed")
            if row is not None and row[2] != owner and not recover:
                raise ValueError("digest operation owned by another process")
            if row is not None and row[3] == "complete":
                raise ValueError("digest operation already complete")
            if row is not None and recover and (
                json.loads(row[4]) != (outputs or {})
                or json.loads(row[5]) != (completed or [])
            ):
                raise ValueError("digest operation changed; request a new resume token")
            if row is None:
                conn.execute("""
                    INSERT INTO digest_operations VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, (operation_id, kind, plan_json, owner, status,
                      json.dumps(outputs or {}, ensure_ascii=False),
                      json.dumps(completed or [], ensure_ascii=False), now_iso()))
            else:
                conn.execute("""
                    UPDATE digest_operations SET owner = ?, status = ?, outputs_json = ?,
                        completed_json = ?, updated_at = ? WHERE operation_id = ?
                """, (owner, status, json.dumps(outputs or {}, ensure_ascii=False),
                      json.dumps(completed or [], ensure_ascii=False), now_iso(), operation_id))

    def read_digest_operations(self, *, open_only: bool = False) -> list[dict]:
        with sqlite3.connect(self.history_db_path) as conn:
            table = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'digest_operations'"
            ).fetchone()
            if not table:
                return []
            conn.row_factory = sqlite3.Row
            query = "SELECT * FROM digest_operations"
            if open_only:
                query += " WHERE status != 'complete'"
            rows = conn.execute(query + " ORDER BY updated_at").fetchall()
        return [
            {**dict(row), "plan": json.loads(row["plan_json"]),
             "outputs": json.loads(row["outputs_json"]),
             "completed": json.loads(row["completed_json"])}
            for row in rows
        ]

    @guarded_mutation("bucket_merge_operation_write")
    def write_merge_operation(
        self, operation_id: str, plan: dict, *, status: str,
        completed: list[str], create: bool = False,
    ) -> None:
        """Persist a merge plan and step progress before or after file mutations."""
        if status not in {"running", "failed", "complete"}:
            raise ValueError("invalid merge operation status")
        with sqlite3.connect(self.history_db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS merge_operations (
                    operation_id TEXT PRIMARY KEY, target_id TEXT NOT NULL,
                    source_id TEXT NOT NULL, plan_json TEXT NOT NULL,
                    status TEXT NOT NULL, completed_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
            """)
            conn.execute("BEGIN IMMEDIATE")
            encoded = json.dumps(plan, ensure_ascii=False, sort_keys=True)
            row = conn.execute(
                "SELECT plan_json FROM merge_operations WHERE operation_id = ?",
                (operation_id,),
            ).fetchone()
            if create:
                if row is not None:
                    raise ValueError("merge operation already exists")
                pending = conn.execute(
                    "SELECT operation_id FROM merge_operations WHERE status != 'complete' "
                    "AND (target_id IN (?, ?) OR source_id IN (?, ?)) LIMIT 1",
                    (plan["target_id"], plan["source_id"],
                     plan["target_id"], plan["source_id"]),
                ).fetchone()
                if pending:
                    raise ValueError(f"unfinished merge operation: {pending[0]}")
                conn.execute(
                    "INSERT INTO merge_operations VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (operation_id, plan["target_id"], plan["source_id"], encoded,
                     status, json.dumps(completed), now_iso()),
                )
            else:
                if row is None or row[0] != encoded:
                    raise ValueError("merge operation plan changed or missing")
                conn.execute(
                    "UPDATE merge_operations SET status = ?, completed_json = ?, "
                    "updated_at = ? WHERE operation_id = ?",
                    (status, json.dumps(completed), now_iso(), operation_id),
                )

    def read_merge_operations(self, *, open_only: bool = False) -> list[dict]:
        with sqlite3.connect(self.history_db_path) as conn:
            if not conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' "
                "AND name = 'merge_operations'"
            ).fetchone():
                return []
            conn.row_factory = sqlite3.Row
            query = "SELECT * FROM merge_operations"
            if open_only:
                query += " WHERE status != 'complete'"
            rows = conn.execute(query + " ORDER BY updated_at").fetchall()
        return [
            {**dict(row), "plan": json.loads(row["plan_json"]),
             "completed": json.loads(row["completed_json"])}
            for row in rows
        ]

    @guarded_mutation("bucket_history_write")
    def record_history(self, bucket_id: str, old_content: str, change_type: str) -> None:
        """Persist the old content before a destructive content change."""
        with sqlite3.connect(self.history_db_path) as conn:
            conn.execute(
                """
                INSERT INTO bucket_history
                    (bucket_id, old_content, changed_at, change_type)
                VALUES (?, ?, ?, ?)
                """,
                (bucket_id, old_content or "", now_iso(), change_type),
            )

    @guarded_mutation("boot_delta_event_write")
    def _record_boot_delta_event(
        self,
        bucket_id: str,
        event_type: str,
        payload: dict[str, Any] | None = None,
    ) -> None:
        """Record a compact business change for the next successful boot."""
        serialized = json.dumps(payload or {}, ensure_ascii=False, sort_keys=True)
        with sqlite3.connect(self.history_db_path) as conn:
            self._insert_boot_delta_event(conn, bucket_id, event_type, serialized, now_iso())

    @staticmethod
    def _insert_boot_delta_event(conn, bucket_id, event_type, serialized, occurred_at):
        return conn.execute(
            """
            INSERT INTO boot_delta_events
                (bucket_id, event_type, payload_json, occurred_at)
            VALUES (?, ?, ?, ?)
            """,
            (bucket_id, event_type, serialized, occurred_at),
        ).lastrowid

    def get_boot_delta_checkpoint(self, profile: str = "talk") -> dict[str, Any] | None:
        """Return one profile's last successful boot checkpoint."""
        with sqlite3.connect(self.history_db_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                """
                SELECT last_event_id, completed_at
                FROM boot_delta_profile_checkpoints
                WHERE profile = ?
                """,
                (profile,),
            ).fetchone()
        return dict(row) if row is not None else None

    def get_boot_delta_high_water(self) -> int:
        """Return the current boot-delta event high-water mark without writing."""
        with sqlite3.connect(self.history_db_path) as conn:
            row = conn.execute(
                "SELECT COALESCE(MAX(id), 0) FROM boot_delta_events"
            ).fetchone()
        return int(row[0] or 0)

    def get_boot_delta_events(
        self,
        after_event_id: int,
        through_event_id: int,
    ) -> list[dict[str, Any]]:
        """Read one stable boot-delta event range, oldest first."""
        with sqlite3.connect(self.history_db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT id, bucket_id, event_type, payload_json, occurred_at
                FROM boot_delta_events
                WHERE id > ? AND id <= ?
                ORDER BY id ASC
                """,
                (int(after_event_id), int(through_event_id)),
            ).fetchall()
        events = []
        for row in rows:
            event = dict(row)
            try:
                payload = json.loads(event.pop("payload_json"))
            except (TypeError, ValueError, json.JSONDecodeError):
                payload = {}
            event["payload"] = payload if isinstance(payload, dict) else {}
            events.append(event)
        return events

    @guarded_mutation("boot_delta_checkpoint_write")
    def advance_boot_delta_checkpoint(
        self,
        expected_event_id: int | None,
        next_event_id: int,
        profile: str = "talk",
    ) -> bool:
        """CAS-advance one profile checkpoint after its boot body completes."""
        with sqlite3.connect(self.history_db_path) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """
                SELECT last_event_id
                FROM boot_delta_profile_checkpoints
                WHERE profile = ?
                """,
                (profile,),
            ).fetchone()
            current = int(row["last_event_id"]) if row is not None else None
            if current != expected_event_id:
                conn.rollback()
                return False
            if row is None:
                existing_count = conn.execute(
                    "SELECT COUNT(*) FROM boot_delta_profile_checkpoints"
                ).fetchone()[0]
                if existing_count == 0:
                    conn.executemany(
                        """
                        INSERT INTO boot_delta_profile_checkpoints
                            (profile, last_event_id, completed_at)
                        VALUES (?, ?, ?)
                        """,
                        [
                            (item, int(next_event_id), now_iso())
                            for item in _BOOT_DELTA_PROFILES
                        ],
                    )
                else:
                    conn.execute(
                        """
                        INSERT INTO boot_delta_profile_checkpoints
                            (profile, last_event_id, completed_at)
                        VALUES (?, ?, ?)
                        """,
                        (profile, int(next_event_id), now_iso()),
                    )
            else:
                conn.execute(
                    """
                    UPDATE boot_delta_profile_checkpoints
                    SET last_event_id = ?, completed_at = ?
                    WHERE profile = ?
                    """,
                    (int(next_event_id), now_iso(), profile),
                )
            conn.commit()
        return True

    def get_history(self, bucket_id: str, limit: int = 20) -> list[dict]:
        """Return recent write-ahead snapshots for manual recovery."""
        limit = max(1, min(int(limit or 20), 100))
        with sqlite3.connect(self.history_db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT bucket_id, old_content, changed_at, change_type
                FROM bucket_history
                WHERE bucket_id = ?
                ORDER BY id DESC
                LIMIT ?
                """,
                (bucket_id, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_history_for_bucket_ids(
        self, bucket_ids: Iterable[str]
    ) -> dict[str, list[dict]]:
        """Read ordered content snapshots for an in-memory historical corpus.

        This intentionally exposes only existing write-ahead rows.  It does
        not create tables, materialize snapshots, or otherwise mutate history.
        """
        ids = list(dict.fromkeys(
            str(bucket_id).strip() for bucket_id in bucket_ids if str(bucket_id).strip()
        ))
        if not ids:
            return {}
        placeholders = ", ".join("?" for _ in ids)
        with sqlite3.connect(self.history_db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                f"""
                SELECT id, bucket_id, old_content, changed_at, change_type
                FROM bucket_history
                WHERE bucket_id IN ({placeholders})
                ORDER BY bucket_id ASC, changed_at ASC, id ASC
                """,
                ids,
            ).fetchall()
        history: dict[str, list[dict]] = {bucket_id: [] for bucket_id in ids}
        for row in rows:
            history.setdefault(str(row["bucket_id"]), []).append(dict(row))
        return history

    @guarded_mutation("bucket_letter_write")
    def record_letter(self, content: str, session_id: str, sealed: bool = False) -> None:
        """Persist an inter-window handoff letter outside normal memory buckets."""
        if not content or not content.strip():
            return
        with sqlite3.connect(self.history_db_path) as conn:
            self._insert_letter(conn, content.strip(), session_id or "", sealed, now_iso())

    @staticmethod
    def _insert_letter(conn, content, session_id, sealed, created_at):
        return conn.execute(
            """
            INSERT INTO letters (content, created_at, session_id, sealed)
            VALUES (?, ?, ?, ?)
            """,
            (content, created_at, session_id, 1 if sealed else 0),
        ).lastrowid

    def get_letters(
        self, limit: int = 1, include_sealed: bool = False, *,
        exclude_session_ids: set[str] | None = None,
    ) -> list[dict]:
        """Return latest handoff letters; internal delivery exclusions precede limit."""
        limit = max(1, min(int(limit or 1), 50))
        where = "" if include_sealed else "WHERE sealed = 0"
        with sqlite3.connect(self.history_db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                f"""
                SELECT id, content, created_at, session_id, sealed
                FROM letters
                {where}
                ORDER BY id DESC
                {"" if exclude_session_ids else "LIMIT ?"}
                """,
                () if exclude_session_ids else (limit,),
            )
            letters = []
            for row in rows:
                if exclude_session_ids and row["session_id"] in exclude_session_ids:
                    continue
                letters.append(dict(row))
                if len(letters) == limit:
                    break
        return letters

    def get_letter(self, letter_id: int, include_sealed: bool = False) -> Optional[dict]:
        """Return one handoff letter by exact id, respecting sealed visibility."""
        try:
            letter_id = int(letter_id)
        except (TypeError, ValueError):
            return None
        if letter_id < 1:
            return None
        with sqlite3.connect(self.history_db_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                """
                SELECT id, content, created_at, session_id, sealed
                FROM letters
                WHERE id = ? AND (? OR sealed = 0)
                """,
                (letter_id, bool(include_sealed)),
            ).fetchone()
        return dict(row) if row else None

    @guarded_mutation("bucket_letter_seal")
    def seal_letter(self, letter_id: int, sealed: bool = True) -> bool:
        """Hide or unhide one handoff letter by id."""
        with sqlite3.connect(self.history_db_path) as conn:
            cur = conn.execute(
                "UPDATE letters SET sealed = ? WHERE id = ?",
                (1 if sealed else 0, int(letter_id)),
            )
            return cur.rowcount > 0

    @guarded_mutation("bucket_note_write")
    def record_note(
        self,
        text: str,
        *,
        author: str = "婷",
        via: str = "mcp",
        sealed: bool = False,
        open_at: str = "",
    ) -> int | None:
        """Persist one optional Ting note outside ordinary memory buckets."""
        if not isinstance(text, str) or not text.strip():
            return None
        if not isinstance(author, str) or not author.strip():
            return None
        if not isinstance(via, str) or not via.strip():
            return None
        normalized_open_at = open_at.strip() if isinstance(open_at, str) else ""
        with sqlite3.connect(self.history_db_path) as conn:
            cur = conn.execute(
                """
                INSERT INTO notes (created_at, author, via, text, sealed, open_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    now_iso(),
                    author.strip(),
                    via.strip(),
                    text,
                    1 if sealed else 0,
                    normalized_open_at or None,
                ),
            )
            return int(cur.lastrowid)

    @staticmethod
    def _note_visible_where(include_sealed: bool = False) -> str:
        sealed_clause = "" if include_sealed else "AND sealed = 0"
        return (
            "(open_at IS NULL OR open_at = '' OR open_at <= ?) "
            f"{sealed_clause}"
        )

    def list_notes(
        self,
        limit: int = 20,
        include_sealed: bool = False,
        *,
        available_at: str | None = None,
    ) -> list[dict]:
        """List visible Ting-note history, newest first, without exposing future notes."""
        limit = max(1, min(int(limit or 20), 100))
        available_at = available_at or now_iso()
        where = self._note_visible_where(include_sealed)
        with sqlite3.connect(self.history_db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                f"""
                SELECT note_id, created_at, author, via, text, sealed, open_at,
                       boot_delivered_at, read_at, skipped_at, skipped_reason,
                       dismissed_at
                FROM notes
                WHERE {where}
                ORDER BY note_id DESC
                LIMIT ?
                """,
                (available_at, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_note(
        self,
        note_id: int,
        include_sealed: bool = False,
        *,
        available_at: str | None = None,
    ) -> Optional[dict]:
        """Read one visible Ting note while preserving sealed/future existence hiding."""
        try:
            note_id = int(note_id)
        except (TypeError, ValueError):
            return None
        if note_id < 1:
            return None
        available_at = available_at or now_iso()
        where = self._note_visible_where(include_sealed)
        with sqlite3.connect(self.history_db_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                f"""
                SELECT note_id, created_at, author, via, text, sealed, open_at,
                       boot_delivered_at, read_at, skipped_at, skipped_reason,
                       dismissed_at
                FROM notes
                WHERE note_id = ? AND {where}
                """,
                (note_id, available_at),
            ).fetchone()
        return dict(row) if row else None

    def get_latest_visible_note(self, *, available_at: str | None = None) -> Optional[dict]:
        """Return the latest normally visible historical note for boot status text."""
        notes = self.list_notes(
            limit=1,
            include_sealed=False,
            available_at=available_at,
        )
        return notes[0] if notes else None

    def get_latest_note_delivery_candidate(
        self,
        *,
        available_at: str | None = None,
    ) -> Optional[dict]:
        """Return the newest visible note still eligible for one-time boot delivery."""
        available_at = available_at or now_iso()
        with sqlite3.connect(self.history_db_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                """
                SELECT note_id, created_at, author, via, text, sealed, open_at,
                       boot_delivered_at, read_at, skipped_at, skipped_reason,
                       dismissed_at
                FROM notes
                WHERE sealed = 0
                  AND dismissed_at IS NULL
                  AND (open_at IS NULL OR open_at = '' OR open_at <= ?)
                  AND boot_delivered_at IS NULL
                  AND skipped_at IS NULL
                ORDER BY note_id DESC
                LIMIT 1
                """,
                (available_at,),
            ).fetchone()
        return dict(row) if row else None

    @guarded_mutation("bucket_note_boot_delivery")
    def mark_note_boot_delivered(
        self,
        note_id: int,
        *,
        delivered_at: str | None = None,
    ) -> bool:
        """Deliver one newest eligible note and skip only older eligible predecessors."""
        try:
            note_id = int(note_id)
        except (TypeError, ValueError):
            return False
        delivered_at = delivered_at or now_iso()
        with sqlite3.connect(self.history_db_path) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("BEGIN IMMEDIATE")
            candidate = conn.execute(
                """
                SELECT note_id
                FROM notes
                WHERE sealed = 0
                  AND dismissed_at IS NULL
                  AND (open_at IS NULL OR open_at = '' OR open_at <= ?)
                  AND boot_delivered_at IS NULL
                  AND skipped_at IS NULL
                ORDER BY note_id DESC
                LIMIT 1
                """,
                (delivered_at,),
            ).fetchone()
            if candidate is None or int(candidate["note_id"]) != note_id:
                conn.rollback()
                return False
            conn.execute(
                "UPDATE notes SET boot_delivered_at = ? WHERE note_id = ?",
                (delivered_at, note_id),
            )
            conn.execute(
                """
                UPDATE notes
                SET skipped_at = ?,
                    skipped_reason = 'superseded_by_newer_eligible_note'
                WHERE note_id < ?
                  AND sealed = 0
                  AND dismissed_at IS NULL
                  AND (open_at IS NULL OR open_at = '' OR open_at <= ?)
                  AND boot_delivered_at IS NULL
                  AND skipped_at IS NULL
                """,
                (delivered_at, note_id, delivered_at),
            )
            conn.commit()
        return True

    @guarded_mutation("bucket_note_read")
    def mark_note_read(self, note_id: int, *, read_at: str | None = None) -> bool:
        """Record a successful full get_note read and suppress later automatic delivery."""
        try:
            note_id = int(note_id)
        except (TypeError, ValueError):
            return False
        read_at = read_at or now_iso()
        with sqlite3.connect(self.history_db_path) as conn:
            cur = conn.execute(
                """
                UPDATE notes
                SET read_at = COALESCE(read_at, ?),
                    boot_delivered_at = COALESCE(boot_delivered_at, ?)
                WHERE note_id = ?
                """,
                (read_at, read_at, note_id),
            )
            return cur.rowcount > 0

    @guarded_mutation("bucket_note_dismiss")
    def dismiss_note(
        self,
        note_id: int,
        *,
        expected_note: dict,
        dismissed_at: str | None = None,
    ) -> bool:
        """Dismiss one unchanged note without deleting it or claiming delivery."""
        dismissed_at = dismissed_at or now_iso()
        with sqlite3.connect(self.history_db_path) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """
                SELECT note_id, created_at, author, via, text, sealed, open_at,
                       boot_delivered_at, read_at, skipped_at, skipped_reason,
                       dismissed_at
                FROM notes WHERE note_id = ?
                """,
                (int(note_id),),
            ).fetchone()
            if row is None or dict(row) != expected_note or row["dismissed_at"] is not None:
                conn.rollback()
                return False
            conn.execute(
                "UPDATE notes SET dismissed_at = ? WHERE note_id = ?",
                (dismissed_at, int(note_id)),
            )
            conn.commit()
        return True

    # ---------------------------------------------------------
    # Create a new bucket
    # 创建新桶
    # Write content and metadata into a .md file
    # 将内容和元数据写入一个 .md 文件
    # ---------------------------------------------------------
    @staticmethod
    def _build_bucket_post(
        bucket_id, content, *, tags=None, importance=5, domain=None,
        valence=0.5, arousal=0.3, bucket_type="dynamic", name=None,
        pinned=False, protected=False, sealed=False, topics=None, todos=None,
        todo_provenance=None, provenance_kind=None, created=None,
        last_active=None, created_date=None, _aliases=None,
    ):
        """Pure, shared construction; no storage, index, or operation writes."""
        canonical_todos = canonicalize_todos(todos)
        normalized_provenance_kind = (
            normalize_provenance_kind(provenance_kind, strict=True)
            if provenance_kind is not None else "unknown"
        )
        canonical_todo_provenance = reconcile_todo_provenance(
            canonical_todos, todo_provenance, strict=todo_provenance is not None,
        )
        alias_text = apply_display_aliases if _aliases is None else lambda value: BucketManager._trace_alias_value(value, _aliases)
        alias = apply_display_aliases_to_value if _aliases is None else lambda value: BucketManager._trace_alias_value(value, _aliases)
        content = alias_text(content)
        name = alias_text(name) if name else name
        tags = alias(tags or [])
        domain = alias(domain) if domain else domain
        todos = alias(canonical_todos)
        todo_provenance = prepare_todo_provenance(
            todos,
            [
                {**record, "text": alias_text(record["text"])}
                for record in canonical_todo_provenance
            ],
        )
        bucket_name = sanitize_name(name) if name else bucket_id
        # feel buckets are allowed to have empty domain; others default to ["未分类"]
        if bucket_type == "feel":
            domain = domain if domain is not None else []
        else:
            domain = domain or ["未分类"]
        linked_content = content  # wikilink injection disabled; LLM adds [[]] via prompt

        # --- Pinned/protected buckets: lock importance to 10 ---
        # --- 钉选/保护桶：importance 强制锁定为 10 ---
        if pinned or protected:
            importance = 10

        # --- Build YAML frontmatter metadata / 构建元数据 ---
        today = created_date or _date_only()
        metadata = {
            "id": bucket_id,
            "name": bucket_name,
            "tags": tags,
            "domain": domain,
            "valence": max(0.0, min(1.0, valence)),
            "arousal": max(0.0, min(1.0, arousal)),
            "importance": max(1, min(10, importance)),
            "type": bucket_type,
            "created": created or now_iso(),
            "last_active": last_active or now_iso(),
            "created_at": today,
            "updated_at": today,
            "emotion_history": "[]",
            "related_buckets": "",
            "source_bucket": "",
            "trigger_date": "",
            "trigger_last_seen": "",
            "dormant": False,
            "sealed": 1 if sealed else 0,
            "activation_count": 0,
            "todos": todos,
        }
        if provenance_kind is not None:
            metadata["provenance_kind"] = normalized_provenance_kind
        if todo_provenance:
            metadata["todo_provenance"] = todo_provenance
        if pinned:
            metadata["pinned"] = True
        if protected:
            metadata["protected"] = True
        if topics is not None:
            metadata["topics"] = topics

        # --- Assemble Markdown file (frontmatter + body) ---
        # --- 组装 Markdown 文件 ---
        if pinned:
            metadata["type"] = "permanent"
        post = frontmatter.Post(linked_content, **metadata)
        return post

    @guarded_async_mutation("bucket_create")
    async def create(
        self,
        content: str,
        tags: list[str] = None,
        importance: int = 5,
        domain: list[str] = None,
        valence: float = 0.5,
        arousal: float = 0.3,
        bucket_type: str = "dynamic",
        provenance_kind: str | None = None,
        name: str = None,
        pinned: bool = False,
        protected: bool = False,
        sealed: bool = False,
        topics: list[str] = None,
        todos: list[str] = None,
        todo_provenance: list[dict[str, Any]] | None = None,
        _o5b_operation_key: str | None = None,
        _o5b_payload_digest: str | None = None,
        _o5c_memory_mutation_id: str | None = None,
    ) -> str:
        """
        Create a new memory bucket, return bucket ID.
        创建一个新的记忆桶，返回桶 ID。

        pinned/protected=True: bucket won't be merged, decayed, or have importance changed.
        Importance is locked to 10 for pinned/protected buckets.
        pinned/protected 桶不参与合并与衰减，importance 强制锁定为 10。
        """
        canonical_todos = canonicalize_todos(todos)
        normalized_provenance_kind = (
            normalize_provenance_kind(provenance_kind, strict=True)
            if provenance_kind is not None
            else "unknown"
        )
        canonical_todo_provenance = reconcile_todo_provenance(
            canonical_todos,
            todo_provenance,
            strict=todo_provenance is not None,
        )
        operation = None
        if _o5b_operation_key is not None:
            operation_payload = {
                "content": content,
                "tags": tags or [],
                "importance": importance,
                "domain": domain,
                "valence": valence,
                "arousal": arousal,
                "name": name,
            }
            if todos is not None:
                operation_payload["todos"] = canonical_todos
            if provenance_kind is not None:
                operation_payload["provenance_kind"] = normalized_provenance_kind
            if todo_provenance is not None:
                operation_payload["todo_provenance"] = canonical_todo_provenance
            operation = self._ensure_import_operation(
                _o5b_operation_key,
                operation_kind="create",
                payload=operation_payload,
                payload_digest=_o5b_payload_digest,
                memory_mutation_id=_o5c_memory_mutation_id,
            )
            if operation["operation_kind"] != "create":
                raise BucketIdempotencyError("operation_kind_conflict")
            bucket_id = operation["result_id"]
            canonical_todo_provenance = reconcile_todo_provenance(
                canonical_todos, operation["payload"].get("todo_provenance"), strict=True,
            )
        else:
            bucket_id = generate_bucket_id()
        with bucket_write_scope(self.base_dir):
            if operation is not None:
                existing_path = self._find_bucket_file(bucket_id)
                if existing_path:
                    try:
                        existing_post = frontmatter.load(existing_path)
                    except Exception as exc:
                        raise BucketIdempotencyError("operation_marker_invalid") from exc
                    marker = self._operation_marker(existing_post, _o5b_operation_key)
                    if (
                        marker is None
                        or marker.get("payload_digest") != operation["payload_digest"]
                        or (
                            operation.get("memory_mutation_id") is not None
                            and marker.get("memory_mutation_id")
                            != operation["memory_mutation_id"]
                        )
                    ):
                        raise BucketIdempotencyError("idempotency_conflict")
                    self._mark_import_operation_applied(_o5b_operation_key)
                    return bucket_id
                if operation["status"] == "applied":
                    raise BucketIdempotencyError("target_bucket_missing")

        post = self._build_bucket_post(
            bucket_id, content, tags=tags, importance=importance, domain=domain,
            valence=valence, arousal=arousal, bucket_type=bucket_type, name=name,
            pinned=pinned, protected=protected, sealed=sealed, topics=topics,
            todos=canonical_todos, todo_provenance=canonical_todo_provenance,
            provenance_kind=provenance_kind,
        )
        content, bucket_name, domain = post.content, post["name"], post["domain"]
        if operation is not None:
            self._append_operation_marker(
                post,
                operation_key=_o5b_operation_key,
                payload_digest=operation["payload_digest"],
                operation_kind="create",
                memory_mutation_id=operation.get("memory_mutation_id"),
            )


        # --- Choose directory by type + primary domain ---
        # --- 按类型 + 主题域选择存储目录 ---
        if bucket_type == "permanent" or pinned:
            type_dir = self.permanent_dir
        elif bucket_type == "feel":
            type_dir = self.feel_dir
        else:
            type_dir = self.dynamic_dir
        if bucket_type == "feel":
            primary_domain = "沉淀物"  # feel subfolder name
        else:
            primary_domain = sanitize_name(domain[0]) if domain else "未分类"
        target_dir = os.path.join(type_dir, primary_domain)
        os.makedirs(target_dir, exist_ok=True)

        # --- Filename: readable_name_bucketID.md (Obsidian friendly) ---
        # --- 文件名：可读名称_桶ID.md ---
        if bucket_name and bucket_name != bucket_id:
            filename = f"{bucket_name}_{bucket_id}.md"
        else:
            filename = f"{bucket_id}.md"
        file_path = safe_path(target_dir, filename)

        if sealed:
            try:
                await self._delete_ordinary_embedding(bucket_id)
            except Exception as exc:
                logger.error(
                    "Refusing sealed bucket create because ordinary-vector cleanup "
                    "failed for %s: %s",
                    bucket_id,
                    exc,
                )
                raise RuntimeError("sealed_embedding_cleanup_failed") from exc

        with bucket_write_scope(self.base_dir):
            existing_path = self._find_bucket_file(bucket_id)
            if existing_path:
                if operation is None:
                    raise BucketIdempotencyError("idempotency_conflict")
                marker = self._operation_marker(frontmatter.load(existing_path), _o5b_operation_key)
                if not marker or marker.get("payload_digest") != operation["payload_digest"]:
                    raise BucketIdempotencyError("idempotency_conflict")
                if (operation.get("memory_mutation_id") is not None
                        and marker.get("memory_mutation_id") != operation["memory_mutation_id"]):
                    raise BucketIdempotencyError("memory_mutation_conflict")
                self._mark_import_operation_applied(_o5b_operation_key)
                return bucket_id
            try:
                if operation is not None:
                    self._write_post_atomic(file_path, post)
                    self._mark_import_operation_applied(_o5b_operation_key)
                else:
                    with open(file_path, "w", encoding="utf-8") as f:
                        f.write(frontmatter.dumps(post))
            except OSError as e:
                logger.error(f"Failed to write bucket file / 写入桶文件失败: {file_path}: {e}")
                raise

        logger.info(
            f"Created bucket / 创建记忆桶: {bucket_id} ({bucket_name}) → {primary_domain}/"
            + (" [PINNED]" if pinned else "") + (" [PROTECTED]" if protected else "")
        )
        self._record_boot_delta_event(bucket_id, "created")
        if not sealed:
            await self._refresh_ordinary_embedding_best_effort(bucket_id, content)
        return bucket_id

    # ---------------------------------------------------------
    # Read bucket content
    # 读取桶内容
    # Returns {"id", "metadata", "content", "path"} or None
    # ---------------------------------------------------------
    async def get(self, bucket_id: str) -> Optional[dict]:
        """
        Read a single bucket by ID.
        根据 ID 读取单个桶。
        """
        if not bucket_id or not isinstance(bucket_id, str):
            return None
        file_path = self._find_bucket_file(bucket_id)
        if not file_path:
            return None
        return self._load_bucket(file_path)

    def preview_todo_completion(self, bucket_id: str, todo_id: str) -> dict:
        with bucket_write_scope(self.base_dir):
            path = self._find_bucket_file(bucket_id)
            if not path:
                raise ValueError("bucket not found")
            post = frontmatter.load(path)
            return {**todo_completion_plan(bucket_id, post, todo_id),
                    "bucket_name": post.get("name", bucket_id)}

    @guarded_mutation("bucket_todo_complete")
    def complete_todo(self, bucket_id: str, todo_id: str, authorize) -> dict:
        with bucket_write_scope(self.base_dir):
            path = self._find_bucket_file(bucket_id)
            if not path:
                raise ValueError("bucket not found")
            post = frontmatter.load(path)
            plan = todo_completion_plan(bucket_id, post, todo_id)
            if plan["target"].get("dropped_at"):
                raise ValueError("该 todo 已放弃，不能标记为完成")
            if plan["target"].get("done_at"):
                return {"status": "already_completed", "done_at": plan["target"]["done_at"]}
            if not authorize(plan):
                return {"status": "confirmation_invalid"}
            records = reconcile_todo_provenance(post.get("todos"), post.get("todo_provenance"), strict=True)
            done_at = now_iso()
            for record in records:
                if record.get("id") == todo_id:
                    record.update(_merge_todo_terminal_state(record, {"done_at": done_at}))
            post["todo_provenance"] = records
            try:
                self._write_post_atomic(path, post)
            except OSError:
                return {"status": "write_failed"}
            return {"status": "completed", "done_at": done_at}

    def preview_todo_drop(self, bucket_id: str, todo_id: str) -> dict:
        with bucket_write_scope(self.base_dir):
            path = self._find_bucket_file(bucket_id)
            if not path:
                raise ValueError("bucket not found")
            post = frontmatter.load(path)
            return {**todo_completion_plan(bucket_id, post, todo_id, operation="todo_drop"),
                    "bucket_name": post.get("name", bucket_id)}

    @guarded_mutation("bucket_todo_drop")
    def drop_todo(self, bucket_id: str, todo_id: str, authorize) -> dict:
        with bucket_write_scope(self.base_dir):
            path = self._find_bucket_file(bucket_id)
            if not path:
                raise ValueError("bucket not found")
            post = frontmatter.load(path)
            plan = todo_completion_plan(bucket_id, post, todo_id, operation="todo_drop")
            if plan["target"].get("done_at"):
                raise ValueError("该 todo 已完成，不能标记为放弃")
            if plan["target"].get("dropped_at"):
                return {"status": "already_dropped", "dropped_at": plan["target"]["dropped_at"]}
            if not authorize(plan):
                return {"status": "confirmation_invalid"}
            records = reconcile_todo_provenance(post.get("todos"), post.get("todo_provenance"), strict=True)
            dropped_at = now_iso()
            for record in records:
                if record.get("id") == todo_id:
                    record.update(_merge_todo_terminal_state(record, {"dropped_at": dropped_at}))
            post["todo_provenance"] = records
            try:
                self._write_post_atomic(path, post)
            except OSError:
                return {"status": "write_failed"}
            return {"status": "dropped", "dropped_at": dropped_at}

    # ---------------------------------------------------------
    # Move bucket between directories
    # 在目录间移动桶文件
    # ---------------------------------------------------------
    @guarded_mutation("bucket_move")
    def _move_bucket(self, file_path: str, target_type_dir: str, domain: list[str] = None) -> str:
        """
        Move a bucket file to a new type directory, preserving domain subfolder.
        Returns new file path.
        """
        with bucket_write_scope(self.base_dir):
            primary_domain = sanitize_name(domain[0]) if domain else "未分类"
            target_dir = os.path.join(target_type_dir, primary_domain)
            os.makedirs(target_dir, exist_ok=True)
            filename = os.path.basename(file_path)
            new_path = safe_path(target_dir, filename)
            if os.path.normpath(file_path) != os.path.normpath(new_path):
                os.rename(file_path, new_path)
                logger.info(f"Moved bucket / 移动记忆桶: {filename} → {target_dir}/")
            return new_path

    # ---------------------------------------------------------
    # Update bucket
    # 更新桶
    # Supports: content, tags, importance, valence, arousal, name, resolved
    # ---------------------------------------------------------
    def _supersession_catalog_locked(self):
        """Read a fresh identity catalog, only while the root mutex is held.

        Forward metadata is interpreted lazily along the requested chain. Other
        relation fields never determine either identity or cycle eligibility.
        """
        root = Path(self.base_dir).resolve()
        catalog = {"buckets": {}, "conflicts": [], "unreadable": [], "incomplete": False}
        for location in (self.permanent_dir, self.dynamic_dir, self.archive_dir, self.feel_dir):
            directory = Path(location)
            if not directory.exists() and not directory.is_symlink():
                continue
            if directory.is_symlink() or not directory.is_dir() or not directory.resolve().is_relative_to(root):
                catalog["incomplete"] = True
                continue
            def onerror(_):
                catalog["incomplete"] = True
            for parent, dirs, files in os.walk(directory, followlinks=False, onerror=onerror):
                if any((Path(parent) / name).is_symlink() for name in dirs):
                    catalog["incomplete"] = True
                dirs[:] = [name for name in dirs if not (Path(parent) / name).is_symlink()]
                for filename in files:
                    if not filename.endswith(".md"):
                        continue
                    path = Path(parent) / filename
                    try:
                        if path.is_symlink() or not path.resolve().is_relative_to(root):
                            raise ValueError("unsafe_bucket_path")
                        raw = path.read_text(encoding="utf-8")
                        if not YAMLHandler().detect(raw):
                            raise ValueError("unreadable_frontmatter")
                        post = frontmatter.loads(raw, handler=_SupersessionHandler())
                    except Exception:
                        catalog["unreadable"].append(path.stem)
                        continue
                    identity = post.get("id", path.stem)
                    if (not isinstance(identity, str) or not identity or identity != identity.strip()
                            or not (path.stem == identity or path.stem.endswith("_" + identity))):
                        catalog["conflicts"].append((path.stem, identity))
                        continue
                    catalog["buckets"].setdefault(identity, []).append((str(path), post))
        return catalog

    def _resolve_feel_source_locked(self, source_id):
        """Prove canonical identity using W-9's catalog, without forward traversal."""
        if not isinstance(source_id, str) or not source_id or source_id != source_id.strip():
            raise _FeelSourceError("source_identity_malformed")
        catalog = self._supersession_catalog_locked()
        matches = lambda stem: stem == source_id or stem.endswith("_" + source_id)
        for stem, claim in catalog["conflicts"]:
            if claim == source_id or matches(stem):
                malformed = not isinstance(claim, str) or not claim or claim != claim.strip()
                raise _FeelSourceError(
                    "source_identity_malformed" if malformed else "source_identity_conflict"
                )
        # Only empty metadata can be the parser's silent delimiter fallback.
        # Check those catalog rows too: an unrelated truncated header could
        # conceal another claim to source_id.
        for identity, rows in catalog["buckets"].items():
            for path, post in rows:
                if not post.metadata:
                    try:
                        _SupersessionHandler().split(Path(path).read_text(encoding="utf-8"))
                    except Exception:
                        code = ("source_unreadable" if identity == source_id
                                or matches(Path(path).stem) else "source_identity_ambiguous")
                        raise _FeelSourceError(code) from None
        if any(matches(stem) for stem in catalog["unreadable"]):
            raise _FeelSourceError("source_unreadable")
        if len(catalog["buckets"].get(source_id, [])) > 1:
            raise _FeelSourceError("source_identity_duplicate")
        if any(other != source_id and any(matches(Path(path).stem) for path, _ in rows)
               for other, rows in catalog["buckets"].items()):
            raise _FeelSourceError("source_identity_conflict")
        # An incomplete/unreadable inventory can conceal another claim to this ID.
        if catalog["incomplete"] or catalog["unreadable"]:
            raise _FeelSourceError("source_identity_ambiguous")
        try:
            path, catalog_post = self._resolve_supersession_locked(catalog, source_id)
        except SupersessionError as exc:
            codes = {
                "supersession_target_missing": "source_missing",
                "supersession_bucket_unreadable": "source_unreadable",
                "supersession_duplicate_identity": "source_identity_duplicate",
                "supersession_identity_conflict": "source_identity_conflict",
                "supersession_identity_ambiguous": "source_identity_ambiguous",
            }
            raise _FeelSourceError(codes.get(exc.code, "source_identity_ambiguous")) from None
        try:
            raw = Path(path).read_text(encoding="utf-8")
            handler = _SupersessionHandler()
            # frontmatter.parse otherwise swallows a missing closing delimiter
            # and treats the entire file as a legacy post with no metadata.
            if not handler.detect(raw):
                raise ValueError()
            handler.split(raw)
            post = frontmatter.loads(raw, handler=handler)
        except Exception:
            raise _FeelSourceError("source_unreadable") from None
        if post.metadata != catalog_post.metadata or post.content != catalog_post.content:
            raise _FeelSourceError("source_identity_ambiguous")
        return path, post

    def preview_feel_source(self, source_id):
        """Read-only proof; never return a post/path/catalog for later publication."""
        try:
            with bucket_write_scope(self.base_dir):
                self._resolve_feel_source_locked(source_id)
            return {"status": "valid"}
        except _FeelSourceError as exc:
            return {"status": exc.code}
        except Exception:
            return {"status": "source_identity_ambiguous"}

    @guarded_mutation("bucket_feel_source_mark")
    def mark_feel_source(self, source_id, *, model_valence=None, _s4_effect=None):
        """Publish and verify one marking from fresh locked state; no rollback."""
        attempted = False
        try:
            with bucket_write_scope(self.base_dir):
                if _s4_effect is not None:
                    self._trace_fence(_s4_effect['context'])
                path, current = self._resolve_feel_source_locked(source_id)
                if _s4_effect is not None:
                    _, effect_digest = self._canonical_import_payload({'source_id': source_id, 'model_valence': model_valence})
                    marker = self._operation_marker(current, _s4_effect['key'])
                    if marker:
                        if marker['payload_digest'] != effect_digest:
                            raise BucketIdempotencyError('operation_payload_conflict')
                        self._sync_directory(os.path.dirname(path))
                        return {'status': 'marked', 'mode': 'replayed'}
                already_satisfied = current.get("digested") is True and (
                    model_valence is None or current.get("model_valence") == model_valence
                )
                draft = copy.deepcopy(current)
                draft["digested"] = True
                if model_valence is not None:
                    if not 0 <= model_valence <= 1:
                        return {"status": "write_failed"}
                    draft["model_valence"] = model_valence
                draft["last_active"] = now_iso()
                draft["updated_at"] = _date_only()
                if _s4_effect is not None:
                    draft['last_active'] = _s4_effect['logical_time']
                    draft['updated_at'] = _date_only(_s4_effect['logical_time'])
                    self._append_operation_marker(draft, operation_key=_s4_effect['key'],
                                                  operation_kind='update', payload_digest=effect_digest)
                    self._trace_fence(_s4_effect['context'])
                payload = frontmatter.dumps(draft).encode("utf-8")
                previous_payload = Path(path).read_bytes()
                attempted = True
                publication_unconfirmed = False
                try:
                    # None is the normal primitive return; False or an exception
                    # must be reconciled from disk just like any other outcome.
                    publication_unconfirmed = self._write_bytes_atomic(path, payload) is False
                except Exception:
                    publication_unconfirmed = True
                try:
                    verified_path, catalog_post = self._resolve_feel_source_locked(source_id)
                    if verified_path != path:
                        return {"status": "write_outcome_unknown"}
                    raw = Path(verified_path).read_bytes()
                    verified = frontmatter.loads(raw.decode("utf-8"), handler=_SupersessionHandler())
                    if (verified.content != catalog_post.content
                            or verified.metadata != catalog_post.metadata):
                        return {"status": "write_outcome_unknown"}
                    if (raw != payload or verified.content != draft.content
                            or verified.metadata != draft.metadata):
                        return {"status": "write_failed"}
                    # Same-second retries can already have identical timestamps.
                    # A failed acknowledgement plus unchanged bytes cannot prove
                    # this mutation, even when the old state satisfies the plan.
                    if publication_unconfirmed and previous_payload == payload:
                        return {"status": "write_outcome_unknown"}
                    return {"status": "marked",
                            "mode": "already_satisfied" if already_satisfied else "updated"}
                except Exception:
                    return {"status": "write_outcome_unknown"}
        except BucketIdempotencyError:
            raise
        except _FeelSourceError as exc:
            return {"status": "write_outcome_unknown" if attempted else exc.code}
        except Exception:
            return {"status": "write_outcome_unknown" if attempted else "write_failed"}

    def _resolve_supersession_locked(self, catalog, identity):
        if not isinstance(identity, str) or not identity or identity != identity.strip():
            raise SupersessionError("supersession_identity_ambiguous")
        if catalog["incomplete"]:
            raise SupersessionError("supersession_identity_ambiguous")
        matches = lambda stem: stem == identity or stem.endswith("_" + identity)
        if any(claim == identity or matches(stem) for stem, claim in catalog["conflicts"]):
            raise SupersessionError("supersession_identity_conflict")
        if any(matches(stem) for stem in catalog["unreadable"]):
            raise SupersessionError("supersession_bucket_unreadable")
        records = catalog["buckets"].get(identity, [])
        # A filename that can select a different canonical ID is also ambiguous.
        if any(other != identity and any(matches(Path(path).stem) for path, _ in rows)
               for other, rows in catalog["buckets"].items()):
            raise SupersessionError("supersession_identity_conflict")
        if len(records) > 1:
            raise SupersessionError("supersession_duplicate_identity")
        if not records:
            raise SupersessionError("supersession_target_missing")
        return records[0]

    def _validate_supersession_forward_locked(self, source_id, value, *, catalog=None,
                                               overlay=None, removed=(), allow_same=True):
        # Callers acquire the root mutex before constructing this catalog and
        # retain it through publication. Never accept a preflight catalog here.
        if catalog is None:
            catalog = self._supersession_catalog_locked()
        source = self._resolve_supersession_locked(catalog, source_id)
        requested = _successor_id_strict(value)
        if requested in ("", "none"):
            return catalog, source
        if requested == source_id:
            raise SupersessionError("supersession_self_loop")
        self._resolve_supersession_locked(catalog, requested)
        previous = source[1].get("superseded_by")
        if allow_same and isinstance(previous, str) and previous.strip() == requested:
            return catalog, source
        # An unreadable file may conceal another identity; additions need a
        # complete identity proof. Clear and identical edges need no downstream.
        if catalog["unreadable"]:
            raise SupersessionError("supersession_identity_ambiguous")
        overlay = overlay or {}
        seen = set()
        current = requested
        while current not in ("", "none"):
            if current == source_id or current in seen:
                raise SupersessionError("supersession_cycle")
            if current in removed:
                raise SupersessionError("supersession_target_missing")
            seen.add(current)
            _, post = self._resolve_supersession_locked(catalog, current)
            current = _successor_id_strict(overlay.get(current, post.get("superseded_by")))
        return catalog, source

    def preflight_supersession(self, source_id, value):
        with bucket_write_scope(self.base_dir):
            self._validate_supersession_forward_locked(source_id, value)

    def validate_supersession_rewire(self, source_id, target_id, rewires, *, planning=False):
        """Validate a transient merge overlay; do not publish or persist it."""
        with bucket_write_scope(self.base_dir):
            catalog = self._supersession_catalog_locked()
            self._resolve_supersession_locked(catalog, target_id)
            if planning:
                self._resolve_supersession_locked(catalog, source_id)
                incoming = {identity for identity, rows in catalog["buckets"].items()
                            if identity != source_id and any(
                                isinstance(post.get("superseded_by"), str)
                                and post["superseded_by"].strip() == source_id for _, post in rows)}
                if incoming != set(rewires) or any(value != target_id for value in rewires.values()):
                    raise SupersessionError("supersession_plan_stale")
            for identity, value in rewires.items():
                self._validate_supersession_forward_locked(identity, value, catalog=catalog,
                    overlay=rewires, removed={source_id}, allow_same=False)

    def _plan_supersession_reverse_locked(self, catalog, source_id, source_post, value):
        """Stage only the old/new successor reverse deltas from current posts."""
        requested = _successor_id_strict(value)
        previous = catalog["buckets"][source_id][0][1].get("superseded_by")
        previous = previous.strip() if isinstance(previous, str) else ""
        neighbors = {}
        for identity, adding in ((previous, False), (requested, True)):
            if identity in ("", "none") or (not adding and previous == requested):
                continue
            try:
                path, original = self._resolve_supersession_locked(catalog, identity)
            except SupersessionError:
                if adding:
                    raise
                continue  # Preserve clear for a broken/ambiguous old successor.
            if identity == source_id:
                draft = source_post
            else:
                if identity not in neighbors:
                    neighbors[identity] = (path, copy.deepcopy(original), original)
                draft = neighbors[identity][1]
            reverse = _supersedes_for_mutation(draft.metadata)
            draft["supersedes"] = (list(dict.fromkeys(reverse + [source_id])) if adding
                                   else [item for item in reverse if item != source_id])
            draft["last_active"] = now_iso()
            draft["updated_at"] = _date_only()
        return list(neighbors.values())

    @guarded_async_mutation("bucket_update")
    async def update(self, bucket_id: str, **kwargs) -> bool:
        """
        Update bucket content or metadata fields.
        更新桶的内容或元数据字段。
        """
        s4_context = kwargs.pop('_s4_context', None)
        relation_requested = "related_buckets" in kwargs
        paired_supersession = kwargs.pop("_supersession_reverse", False)
        relation_value = kwargs.pop("related_buckets", None)
        if relation_requested:
            if kwargs.get("_o5b_operation_key") is not None:
                raise RelatedError("related_operation_key_conflict")
            self.preview_related(bucket_id, replace=relation_value)
            if not kwargs:
                self.mutate_related(bucket_id, replace=relation_value)
                return True
        with bucket_write_scope(self.base_dir):
            if s4_context is not None:
                s4_plan = self._trace_fence(s4_context)['plan']
            history_change_type = kwargs.pop("_history_change_type", "replace")
            o5b_operation_key = kwargs.pop("_o5b_operation_key", None)
            o5b_payload_digest = kwargs.pop("_o5b_payload_digest", None)
            o5c_memory_mutation_id = kwargs.pop("_o5c_memory_mutation_id", None)
            operation = None
            if o5b_operation_key is not None:
                operation = self._ensure_import_operation(
                    o5b_operation_key,
                    operation_kind="update",
                    target_bucket_id=bucket_id,
                    payload={"kwargs": dict(kwargs)},
                    payload_digest=o5b_payload_digest,
                    memory_mutation_id=o5c_memory_mutation_id,
                )
                if operation["operation_kind"] != "update":
                    raise BucketIdempotencyError("operation_kind_conflict")
                bucket_id = operation["target_bucket_id"]
                stored_kwargs = operation["payload"].get("kwargs")
                if not isinstance(stored_kwargs, dict):
                    raise BucketIdempotencyError("operation_payload_invalid")
                kwargs = stored_kwargs
            file_path = self._find_bucket_file(bucket_id)
            if not file_path:
                return False

            try:
                post = frontmatter.load(file_path)
            except Exception as e:
                logger.warning(f"Failed to load bucket for update / 加载桶失败: {file_path}: {e}")
                return False

            if operation is not None:
                marker = self._operation_marker(post, o5b_operation_key)
                if marker is not None:
                    if marker.get("payload_digest") != operation["payload_digest"]:
                        raise BucketIdempotencyError("operation_payload_conflict")
                    if (
                        operation.get("memory_mutation_id") is not None
                        and marker.get("memory_mutation_id")
                        != operation["memory_mutation_id"]
                    ):
                        raise BucketIdempotencyError("memory_mutation_conflict")
                    self._mark_import_operation_applied(o5b_operation_key)
                    return True
                if operation["status"] == "applied":
                    raise BucketIdempotencyError("operation_marker_missing")

            if s4_context is not None:
                self._trace_preimage_guard(s4_plan)

        embedding_cleanup_done = False
        preliminary_sealed = int(kwargs.get("sealed", post.get("sealed", 0)) or 0) == 1
        if preliminary_sealed and (not _is_sealed_bucket(post) or "content" in kwargs):
            try:
                await self._delete_ordinary_embedding(bucket_id)
                embedding_cleanup_done = True
            except Exception as exc:
                logger.error("Refusing sealed update: cleanup failed for %s: %s", bucket_id, exc)
                return False

        with bucket_write_scope(self.base_dir):
            if s4_context is not None:
                self._trace_fence(s4_context)
            file_path = self._find_bucket_file(bucket_id)
            if not file_path:
                return False
            post = frontmatter.load(file_path)
            if operation is not None:
                marker = self._operation_marker(post, o5b_operation_key)
                if marker is not None:
                    if marker.get("payload_digest") != operation["payload_digest"]:
                        raise BucketIdempotencyError("operation_payload_conflict")
                    if (
                        operation.get("memory_mutation_id") is not None
                        and marker.get("memory_mutation_id")
                        != operation["memory_mutation_id"]
                    ):
                        raise BucketIdempotencyError("memory_mutation_conflict")
                    self._mark_import_operation_applied(o5b_operation_key)
                    return True
                if operation["status"] == "applied":
                    raise BucketIdempotencyError("operation_marker_missing")

            if s4_context is not None:
                self._trace_preimage_guard(s4_plan)

            reverse_updates = []
            supersession_original = None
            if "superseded_by" in kwargs:
                catalog, (file_path, current_post) = self._validate_supersession_forward_locked(
                    bucket_id, kwargs["superseded_by"])
                post = copy.deepcopy(current_post)
                if paired_supersession:
                    requested = _successor_id_strict(kwargs["superseded_by"])
                    if requested not in ("", "none"):
                        target = self._resolve_supersession_locked(catalog, requested)[1]
                        if _is_sealed_bucket(target):
                            raise SupersessionError("supersession_target_sealed")
                    supersession_original = copy.deepcopy(post)
                    reverse_updates = self._plan_supersession_reverse_locked(
                        catalog, bucket_id, post, kwargs["superseded_by"])

            requested_permanent = kwargs.pop("permanent", None)
            if requested_permanent is not None:
                if requested_permanent not in (0, 1):
                    return False
                if post.get("type") not in ("dynamic", "permanent"):
                    return False
                next_pinned = bool(kwargs.get("pinned", post.get("pinned", False)))
                if requested_permanent == 0 and (next_pinned or post.get("protected")):
                    return False
            original_file_bytes = None
            if requested_permanent is not None or kwargs.get("pinned"):
                with open(file_path, "rb") as original_file:
                    original_file_bytes = original_file.read()

            previous_content = str(post.content or "")
            previous_todos = canonicalize_todos(post.get("todos"))
            previous_todo_provenance = reconcile_todo_provenance(
                previous_todos,
                post.get("todo_provenance"),
            )
            prepared_todos = None
            prepared_provenance = None
            if "todos" in kwargs or "todo_provenance" in kwargs:
                prepared_todos = (
                    (canonicalize_todos(kwargs["todos"]) if s4_context is not None else
                     apply_display_aliases_to_value(canonicalize_todos(kwargs["todos"])))
                    if "todos" in kwargs else previous_todos
                )
                try:
                    incoming_provenance = kwargs.get("todo_provenance")
                    if operation is not None and o5b_operation_key.startswith("o5b:"):
                        incoming_provenance = reconcile_todo_provenance(
                            prepared_todos, incoming_provenance, strict=True,
                        )
                        current_ids = {r["id"] for r in previous_todo_provenance if "id" in r}
                        for record in incoming_provenance:
                            matches = [r for r in previous_todo_provenance
                                       if r["text"] == record["text"] and "id" in r]
                            if record.get("id") not in current_ids and len(matches) == 1:
                                record["id"] = matches[0]["id"]
                    prepared_provenance = prepare_todo_provenance(
                        prepared_todos,
                        incoming_provenance,
                        previous_todos=previous_todos,
                        previous_provenance=post.get("todo_provenance"),
                        references_only=kwargs.pop("_todo_references_only", False),
                    )
                    if operation is not None and o5b_operation_key.startswith("o5b:"):
                        # Attribution edits made after an extraction plan must survive.
                        # A confirmed bucket merge instead writes its planned conflict result.
                        current_by_id = {r["id"]: r for r in previous_todo_provenance if "id" in r}
                        prepared_provenance = [
                            {**r, **{k: current_by_id[r["id"]][k]
                                     for k in ("said_by", "said_at", "source_bucket")}}
                            if r["id"] in current_by_id and _todo_provenance_is_known(current_by_id[r["id"]])
                            else r for r in prepared_provenance
                        ]
                except ValueError as exc:
                    logger.warning("Refusing invalid todo update for %s: %s", bucket_id, exc)
                    return False
            previous_superseded_by = post.get("superseded_by")
            explicit_provenance_kind = (
                normalize_provenance_kind(kwargs["provenance_kind"], strict=True)
                if "provenance_kind" in kwargs
                else None
            )

            if operation is not None:
                self._append_operation_marker(
                    post,
                    operation_key=o5b_operation_key,
                    payload_digest=operation["payload_digest"],
                    operation_kind="update",
                    memory_mutation_id=operation.get("memory_mutation_id"),
                )


            previous_sealed = _is_sealed_bucket(post)
            next_sealed = (
                int(kwargs["sealed"]) == 1
                if "sealed" in kwargs
                else previous_sealed
            )
            content_changed = "content" in kwargs
            cleanup_before_write = next_sealed and (not previous_sealed or content_changed)
            if cleanup_before_write and not embedding_cleanup_done:
                return False

            # --- Pinned/protected buckets: lock importance to 10, ignore importance changes ---
            # --- 钉选/保护桶：importance 不可修改，强制保持 10 ---
            is_pinned = post.get("pinned", False) or post.get("protected", False)
            if is_pinned:
                kwargs.pop("importance", None)  # silently ignore importance update

            # --- Update only fields that were passed in / 只改传入的字段 ---
            if "content" in kwargs:
                try:
                    if s4_context is None:
                        self.record_history(bucket_id, post.content, history_change_type)
                    elif 'history' not in json.loads(operation.get('effects_json') or '{}'):
                        raise BucketIdempotencyError('operation_history_missing')
                except Exception as e:
                    logger.error(
                        f"Refusing content update because history capture failed "
                        f"for {bucket_id}: {e}"
                    )
                    return False
                if s4_context is None:
                    kwargs["content"] = apply_display_aliases(kwargs["content"])
                post.content = kwargs["content"]  # wikilink injection disabled; LLM adds [[]] via prompt
            # A body rewrite invalidates any previous provenance claim unless the
            # caller deliberately provides a replacement classification.
            if explicit_provenance_kind is not None:
                post["provenance_kind"] = explicit_provenance_kind
            elif content_changed:
                post["provenance_kind"] = "unknown"
            if "tags" in kwargs:
                if s4_context is None:
                    kwargs["tags"] = apply_display_aliases_to_value(kwargs["tags"])
                post["tags"] = kwargs["tags"]
            if prepared_todos is not None:
                post["todos"] = prepared_todos
                if prepared_provenance:
                    post["todo_provenance"] = prepared_provenance
                else:
                    post.metadata.pop("todo_provenance", None)
            if "importance" in kwargs:
                post["importance"] = max(1, min(10, int(kwargs["importance"])))
            if "domain" in kwargs:
                if s4_context is None:
                    kwargs["domain"] = apply_display_aliases_to_value(kwargs["domain"])
                post["domain"] = kwargs["domain"]
            if "valence" in kwargs:
                post["valence"] = max(0.0, min(1.0, float(kwargs["valence"])))
            if "arousal" in kwargs:
                post["arousal"] = max(0.0, min(1.0, float(kwargs["arousal"])))
            if "name" in kwargs:
                if s4_context is None:
                    kwargs["name"] = apply_display_aliases(kwargs["name"])
                post["name"] = sanitize_name(kwargs["name"])
            if "resolved" in kwargs:
                post["resolved"] = bool(kwargs["resolved"])
            if "pinned" in kwargs:
                post["pinned"] = bool(kwargs["pinned"])
                if kwargs["pinned"]:
                    post["importance"] = 10  # pinned → lock importance to 10
                    post["type"] = "permanent"
            if requested_permanent is not None:
                post["type"] = "permanent" if requested_permanent else "dynamic"
            if "digested" in kwargs:
                post["digested"] = bool(kwargs["digested"])
            if "model_valence" in kwargs:
                post["model_valence"] = max(0.0, min(1.0, float(kwargs["model_valence"])))
            if "emotion_history" in kwargs:
                post["emotion_history"] = kwargs["emotion_history"]
            if "source_bucket" in kwargs:
                post["source_bucket"] = kwargs["source_bucket"]
            if "trigger_date" in kwargs:
                post["trigger_date"] = kwargs["trigger_date"]
            if "trigger_last_seen" in kwargs:
                post["trigger_last_seen"] = kwargs["trigger_last_seen"]
            if "superseded_by" in kwargs:
                if kwargs["superseded_by"] is None:
                    post.metadata.pop("superseded_by", None)
                else:
                    post["superseded_by"] = str(kwargs["superseded_by"])
            if "superseded_at" in kwargs:
                if kwargs["superseded_at"] is None:
                    post.metadata.pop("superseded_at", None)
                else:
                    post["superseded_at"] = str(kwargs["superseded_at"])
            if "supersedes" in kwargs:
                if kwargs["supersedes"] is None:
                    post.metadata.pop("supersedes", None)
                else:
                    post["supersedes"] = list(
                        dict.fromkeys(
                            str(value).strip()
                            for value in kwargs["supersedes"]
                            if str(value).strip()
                        )
                    )
            if "dormant" in kwargs:
                post["dormant"] = bool(kwargs["dormant"])
            if "sealed" in kwargs:
                post["sealed"] = 1 if int(kwargs["sealed"]) == 1 else 0

            # --- Auto-refresh activation time / 自动刷新激活时间 ---
            post["last_active"] = s4_plan['logical_time'] if s4_context is not None else now_iso()
            post["updated_at"] = _date_only(s4_plan['logical_time']) if s4_context is not None else _date_only()

            applied_supersession = []
            try:
                if paired_supersession or operation is not None or content_changed or prepared_todos is not None or requested_permanent is not None or kwargs.get("pinned"):
                    self._write_post_atomic(file_path, post)
                    if operation is not None:
                        self._mark_import_operation_applied(o5b_operation_key)
                else:
                    with open(file_path, "w", encoding="utf-8") as f:
                        f.write(frontmatter.dumps(post))
                if supersession_original is not None:
                    applied_supersession.append((bucket_id, file_path, supersession_original))
                for neighbor_path, draft, original in reverse_updates:
                    self._write_post_atomic(neighbor_path, draft)
                    applied_supersession.append((original.get("id", Path(neighbor_path).stem),
                                                  neighbor_path, original))
            except OSError as e:
                for restore_id, restore_path, original in reversed(applied_supersession):
                    try:
                        # Even compensation must not recreate an unsafe edge.
                        self._validate_supersession_forward_locked(
                            restore_id, original.get("superseded_by"))
                        self._write_post_atomic(restore_path, original)
                    except (OSError, SupersessionError):
                        logger.warning("Supersession compensation refused or failed for %s", restore_id)
                logger.error(f"Failed to write bucket update / 写入桶更新失败: {file_path}: {e}")
                return False

            # --- Keep lifecycle metadata and directory together. ---
            # NOTE: resolved buckets are NOT auto-archived here.
            # They stay in dynamic/ and decay naturally until score < threshold.
            # 注意：resolved 桶不在此自动归档，留在 dynamic/ 随衰减引擎自然归档。
            domain = post.get("domain", ["未分类"])
            type_dir = (self.permanent_dir if post.get("type") == "permanent" else
                        self.dynamic_dir if post.get("type") == "dynamic" else None)
            if type_dir and original_file_bytes is not None:
                try:
                    self._move_bucket(file_path, type_dir, domain)
                except OSError as exc:
                    if "superseded_by" in kwargs:
                        try:
                            original_post = frontmatter.loads(original_file_bytes.decode("utf-8"))
                            self._validate_supersession_forward_locked(
                                bucket_id, original_post.get("superseded_by"))
                        except SupersessionError:
                            logger.warning("Refusing unsafe forward restoration after lifecycle move failure")
                            return False
                    with open(file_path, "wb") as restore_file:
                        restore_file.write(original_file_bytes)
                    logger.error("Failed to move bucket lifecycle type %s: %s", bucket_id, exc)
                    return False

        if s4_context is not None:
            return True

        if (
            not next_sealed
            and (previous_sealed or content_changed)
        ):
            await self._refresh_ordinary_embedding_best_effort(
                bucket_id,
                post.content,
            )

        if content_changed and str(post.content or "") != previous_content:
            self._record_boot_delta_event(bucket_id, "content_updated")
        current_todos = canonicalize_todos(post.get("todos"))
        # Boot deltas intentionally track todo text only.  Provenance-only
        # sidecar changes are metadata refinements and do not create a new
        # boot-delta event in Phase 5.10.
        if current_todos != previous_todos:
            self._record_boot_delta_event(
                bucket_id,
                "todos_updated",
                {
                    "closed_count": len(set(previous_todos) - set(current_todos)),
                    "opened_count": len(set(current_todos) - set(previous_todos)),
                },
            )
        current_superseded_by = post.get("superseded_by")
        if (
            current_superseded_by != previous_superseded_by
            and str(current_superseded_by or "").strip()
        ):
            self._record_boot_delta_event(
                bucket_id,
                "superseded",
                {"mode": str(current_superseded_by).strip()},
            )

        if relation_requested:
            self.mutate_related(bucket_id, replace=relation_value)
        logger.info(f"Updated bucket / 更新记忆桶: {bucket_id}")
        return True

    @guarded_async_mutation("bucket_tg_summary_refresh")
    async def refresh_tg_summary(
        self,
        bucket_id: str,
        summary: str,
        source_sha256: str,
    ) -> tuple[str, str]:
        """Store a TG summary only when the source body still has the expected hash."""
        with bucket_write_scope(self.base_dir):
            file_path = self._find_bucket_file(bucket_id)
            if not file_path:
                return "missing", ""
            try:
                post = frontmatter.load(file_path)
            except Exception as exc:
                logger.warning(
                    "Failed to load bucket for TG summary refresh %s: %s",
                    bucket_id,
                    exc,
                )
                return "invalid", ""
            if _is_sealed_bucket(post):
                return "sealed", ""

            current_source_sha256 = hashlib.sha256(
                str(post.content or "").encode("utf-8")
            ).hexdigest()
            if current_source_sha256 != source_sha256:
                return "source_hash_mismatch", current_source_sha256

            post["tg_summary"] = summary
            post["tg_summary_source_hash"] = current_source_sha256
            post["tg_summary_updated_at"] = now_iso()
            post["last_active"] = now_iso()
            post["updated_at"] = _date_only()
            try:
                self._write_post_atomic(file_path, post)
            except OSError as exc:
                logger.error(
                    "Failed to write TG summary refresh for %s: %s", bucket_id, exc
                )
                return "write_failed", current_source_sha256
            logger.info("Refreshed TG summary for bucket %s", bucket_id)
            return "updated", current_source_sha256

    # ---------------------------------------------------------
    # Wikilink injection — DISABLED
    # 自动添加 Obsidian 双链 — 已禁用
    # Now handled by LLM prompts (Gemini adds [[]] for proper nouns)
    # 现在由 LLM prompt 处理（Gemini 对人名/地名/专有名词加 [[]]）
    # ---------------------------------------------------------
    # def _apply_wikilinks(self, content, tags, domain, name): ...
    # def _collect_wikilink_keywords(self, content, tags, domain, name): ...
    # def _normalize_keywords(self, keywords): ...
    # def _extract_auto_keywords(self, content): ...

    # ---------------------------------------------------------
    # Delete bucket
    # 删除桶
    # ---------------------------------------------------------
    @guarded_async_mutation("bucket_delete")
    async def delete(
        self,
        bucket_id: str,
        *,
        _allow_sealed: bool = False,
        _expected_todo_state: tuple[Any, Any] | None = None,
        _relation_target: str | None = None,
        _relation_operation_key: str | None = None,
        _relation_expected_inventory: str | None = None,
    ) -> bool:
        """
        Delete a memory bucket file.
        删除指定的记忆桶文件。
        """
        if _relation_operation_key:
            receipt = self.relation_store.lookup(_relation_operation_key)
            if receipt:
                self.relation_store.commit(lambda inv: None,
                    operation_key=_relation_operation_key,
                    request_digest=related_digest({"source": bucket_id, "target": _relation_target}))
                return True
        self.recover_related_operations()
        if not self._find_bucket_file(bucket_id):
            await self._delete_ordinary_embedding(bucket_id)
            return False
        relation_plan = self.preview_related_delete(bucket_id, target_id=_relation_target)
        if (_relation_expected_inventory is not None
                and relation_plan['inventory'] != _relation_expected_inventory):
            raise RelatedError('related_plan_stale')
        file_path = self._find_bucket_file(bucket_id)
        if not file_path:
            try:
                await self._delete_ordinary_embedding(bucket_id)
            except Exception as exc:
                logger.error(
                    "Orphan ordinary-vector cleanup failed for missing bucket %s: %s",
                    bucket_id,
                    exc,
                )
            return False

        with bucket_write_scope(self.base_dir):
            file_path = self._find_bucket_file(bucket_id)
            if not file_path:
                return False
            try:
                post = frontmatter.load(file_path)
                if (_expected_todo_state is not None and
                        (post.get("todos"), post.get("todo_provenance")) != _expected_todo_state):
                    return False
                if (
                    (not _allow_sealed and _is_sealed_bucket(post))
                    or post.get("pinned")
                    or post.get("protected")
                ):
                    logger.warning(
                        "Refusing destructive delete of protected bucket %s",
                        bucket_id,
                    )
                    return False
                self.record_history(bucket_id, post.content, "delete")
            except Exception as exc:
                logger.error(f"Failed to snapshot bucket {bucket_id}: {exc}")
                return False

        try:
            await self._delete_ordinary_embedding(bucket_id)
        except Exception as exc:
            logger.error(
                "Refusing to delete bucket %s because ordinary-vector cleanup failed: %s",
                bucket_id,
                exc,
            )
            return False

        def validate(inventory):
            planned = plan_delete(inventory, bucket_id, target_id=_relation_target)
            if planned != relation_plan:
                raise RelatedError('related_plan_stale')
            return planned
        self.relation_store.commit(validate, operation_key=_relation_operation_key,
            request_digest=related_digest({"source": bucket_id, "target": _relation_target}))

        logger.info(f"Deleted bucket / 删除记忆桶: {bucket_id}")
        return True

    # ---------------------------------------------------------
    # Touch bucket (refresh activation time + increment count)
    # 触碰桶（刷新激活时间 + 累加激活次数）
    # Called on every recall hit; affects decay score.
    # 每次检索命中时调用，影响衰减得分。
    # ---------------------------------------------------------
    @guarded_optional_async_mutation("bucket_touch")
    async def touch(
        self,
        bucket_id: str,
        ripple_ids: set[str] | None = None,
        wake_dormant: bool = False,
    ) -> None:
        """
        Update a bucket's last activation time and count. Wake it only when requested.
        Also triggers time ripple: nearby memories get a slight activation boost.
        更新桶的最后激活时间和激活次数；仅在显式请求时解除休眠。
        同时触发时间涟漪：时间上相邻的记忆轻微唤醒。
        """
        try:
            with bucket_write_scope(self.base_dir):
                file_path = self._find_bucket_file(bucket_id)
                if not file_path:
                    return

                post = frontmatter.load(file_path)
                post["last_active"] = now_iso()
                post["activation_count"] = post.get("activation_count", 0) + 1
                if wake_dormant:
                    post["dormant"] = False

                with open(file_path, "w", encoding="utf-8") as f:
                    f.write(frontmatter.dumps(post))

            # --- Time ripple: boost nearby memories within ±48h ---
            # --- 时间涟漪：±48小时内的记忆轻微唤醒 ---
            current_time = datetime.fromisoformat(str(post.get("created", post.get("last_active", ""))))
            await self._time_ripple(
                bucket_id,
                current_time,
                allowed_ids=ripple_ids,
            )
        except Exception as e:
            logger.warning(f"Failed to touch bucket / 触碰桶失败: {bucket_id}: {e}")
            raise

    @guarded_async_mutation("bucket_dormant")
    async def set_dormant(self, bucket_id: str, dormant: bool = True) -> bool:
        """Set dormant without refreshing last_active or updated_at."""
        with bucket_write_scope(self.base_dir):
            file_path = self._find_bucket_file(bucket_id)
            if not file_path:
                return False
            try:
                post = frontmatter.load(file_path)
                post["dormant"] = bool(dormant)
                with open(file_path, "w", encoding="utf-8") as f:
                    f.write(frontmatter.dumps(post))
                return True
            except Exception as e:
                logger.warning(f"Failed to set dormant for {bucket_id}: {e}")
                return False

    @guarded_optional_async_mutation("bucket_time_ripple")
    async def _time_ripple(
        self,
        source_id: str,
        reference_time: datetime,
        hours: float = 48.0,
        allowed_ids: set[str] | None = None,
    ) -> None:
        """
        Slightly boost activation_count of buckets created/activated near the reference time.
        轻微提升时间相邻桶的激活次数（+0.3），不改 last_active 避免递归唤醒。
        Max 5 buckets rippled per touch to bound I/O.
        """
        all_buckets = await self.list_all(include_archive=False)

        rippled = 0
        max_ripple = 5
        for bucket in all_buckets:
            if rippled >= max_ripple:
                break
            if bucket["id"] == source_id:
                continue
            if allowed_ids is not None and bucket["id"] not in allowed_ids:
                continue
            meta = bucket.get("metadata", {})
            # Skip pinned/permanent/feel
            if meta.get("pinned") or meta.get("protected") or meta.get("type") in ("permanent", "feel"):
                continue

            created_str = meta.get("created", meta.get("last_active", ""))
            try:
                created = datetime.fromisoformat(str(created_str))
                delta_hours = abs((reference_time - created).total_seconds()) / 3600
            except (ValueError, TypeError):
                continue

            if delta_hours <= hours:
                # Boost activation_count by 0.3 (fractional), don't change last_active
                with bucket_write_scope(self.base_dir):
                    file_path = self._find_bucket_file(bucket["id"])
                    if not file_path:
                        continue
                    try:
                        post = frontmatter.load(file_path)
                        current_count = post.get("activation_count", 1)
                        # Store as float for fractional increments; calculate_score handles it
                        post["activation_count"] = round(current_count + 0.3, 1)
                        with open(file_path, "w", encoding="utf-8") as f:
                            f.write(frontmatter.dumps(post))
                        rippled += 1
                    except Exception:
                        logger.warning("Failed to persist time ripple for %s", bucket["id"])
                        raise

    # ---------------------------------------------------------
    # Multi-dimensional search (core feature)
    # 多维搜索（核心功能）
    #
    # Strategy: domain pre-filter → weighted multi-dim ranking
    # 策略：主题域预筛 → 多维加权精排
    #
    # Ranking formula:
    #   total = topic(×w_topic) + emotion(×w_emotion)
    #           + time(×w_time) + importance(×w_importance)
    #
    # Per-dimension scores (normalized to 0~1):
    #   topic     = rapidfuzz weighted match (name/tags/domain/body)
    #   emotion   = 1 - Euclidean distance (query v/a vs bucket v/a)
    #   time      = e^(-0.02 × days) (recent memories first)
    #   importance = importance / 10
    # ---------------------------------------------------------
    async def search(
        self,
        query: str,
        limit: int = None,
        domain_filter: list[str] = None,
        query_valence: float = None,
        query_arousal: float = None,
        include_dormant: bool = False,
        include_sealed: bool = False,
        candidate_buckets: list[dict] = None,
        trace: dict | None = None,
        include_semantic: bool = True,
    ) -> list[dict]:
        """
        Multi-dimensional indexed search for memory buckets.
        多维索引搜索记忆桶。

        domain_filter: pre-filter by domain (None = search all)
        query_valence/arousal: emotion coordinates for resonance scoring
        """
        if trace is not None:
            trace.clear()
            trace.update({
                "query": query,
                "candidate_source": (
                    "candidate_buckets"
                    if candidate_buckets is not None
                    else "active_buckets"
                ),
                "semantic": {
                    "enabled": bool(
                        self.embedding_engine
                        and getattr(self.embedding_engine, "enabled", False)
                    ),
                    "status": "not_run",
                },
                "candidates": [],
                "ranking": [],
            })

        if not query or not query.strip():
            if trace is not None:
                trace["status"] = "empty_query"
            return []

        query = apply_display_aliases(query)

        limit = limit or self.max_results
        all_buckets = (
            list(candidate_buckets)
            if candidate_buckets is not None
            else await self.list_all(include_archive=False)
        )

        if not all_buckets:
            if trace is not None:
                trace["status"] = "no_candidates"
            return []

        trace_entries = {}
        if trace is not None:
            for bucket in all_buckets:
                bid = str(bucket.get("id", ""))
                trace_entries[bid] = {
                    "id": bid,
                    "candidate_origin": trace["candidate_source"],
                    "eligible": True,
                    "admitted": False,
                    "entered_ranking": False,
                }

        # --- Layer 1: domain pre-filter (fast scope reduction) ---
        # --- 第一层：主题域预筛（快速缩小范围）---
        if candidate_buckets is not None:
            candidates = all_buckets
        elif domain_filter:
            filter_set = {d.lower() for d in domain_filter}
            candidates = [
                b for b in all_buckets
                if {d.lower() for d in b["metadata"].get("domain", [])} & filter_set
            ]
            # Fall back to full search if pre-filter yields nothing
            # 预筛为空则回退全量搜索
            if not candidates:
                candidates = all_buckets
        else:
            candidates = all_buckets

        if trace is not None:
            candidate_ids = {str(bucket.get("id", "")) for bucket in candidates}
            for bid, entry in trace_entries.items():
                if bid not in candidate_ids:
                    entry["eligible"] = False
                    entry["exclusion_reasons"] = ["domain_filter"]

        if not include_dormant:
            before_dormant = candidates
            candidates = [
                b for b in candidates
                if not b.get("metadata", {}).get("dormant", False)
            ]
            if trace is not None:
                retained_ids = {str(bucket.get("id", "")) for bucket in candidates}
                for bucket in before_dormant:
                    bid = str(bucket.get("id", ""))
                    if bid not in retained_ids:
                        entry = trace_entries.get(bid)
                        if entry is not None:
                            entry["eligible"] = False
                            entry.setdefault("exclusion_reasons", []).append("dormant")

        if not include_sealed:
            before_sealed = candidates
            candidates = [
                bucket for bucket in candidates
                if not _is_sealed_bucket(bucket)
            ]
            if trace is not None:
                retained_ids = {str(bucket.get("id", "")) for bucket in candidates}
                for bucket in before_sealed:
                    bid = str(bucket.get("id", ""))
                    if bid not in retained_ids:
                        entry = trace_entries.get(bid)
                        if entry is not None:
                            entry["eligible"] = False
                            entry.setdefault("exclusion_reasons", []).append("sealed")

        # --- Layer 1.5: semantic recall for hybrid keyword/vector ranking ---
        # --- 第1.5层：语义召回，与关键词分数混合排序 ---
        vector_scores = {}
        semantic_before_error = ""
        if include_semantic and self.embedding_engine and self.embedding_engine.enabled:
            semantic_before_error = str(
                getattr(self.embedding_engine, "last_error", "") or ""
            )
            try:
                if candidate_buckets is None:
                    candidate_ids = None
                    if not include_sealed:
                        candidate_ids = {
                            str(bucket["id"])
                            for bucket in all_buckets
                            if not _is_sealed_bucket(bucket)
                        }
                    vector_results = await self.embedding_engine.search_similar(
                        query,
                        top_k=50,
                        **(
                            {"candidate_ids": candidate_ids}
                            if candidate_ids is not None
                            else {}
                        ),
                    )
                else:
                    candidate_ids = {str(bucket["id"]) for bucket in candidates}
                    vector_results = await self.embedding_engine.search_similar(
                        query,
                        top_k=50,
                        candidate_ids=candidate_ids,
                    )
                vector_scores = dict(vector_results)
            except Exception as e:
                logger.warning(f"Embedding pre-filter failed, using fuzzy only / embedding 预筛失败: {e}")

        if trace is not None:
            semantic_after_error = str(
                getattr(self.embedding_engine, "last_error", "") or ""
            ) if self.embedding_engine else ""
            if not include_semantic:
                trace["semantic"] = {
                    "enabled": False,
                    "status": "disabled_for_historical_corpus",
                }
            elif not self.embedding_engine or not self.embedding_engine.enabled:
                trace["semantic"] = {"enabled": False, "status": "disabled"}
            elif semantic_after_error and semantic_after_error != semantic_before_error:
                trace["semantic"] = {
                    "enabled": True,
                    "status": "provider_error",
                    "error_code": semantic_after_error,
                }
            elif vector_scores:
                trace["semantic"] = {
                    "enabled": True,
                    "status": "available",
                    "matched_count": len(vector_scores),
                }
            else:
                trace["semantic"] = {
                    "enabled": True,
                    "status": "unavailable_or_empty_index",
                }

        # --- Layer 2: weighted multi-dim ranking ---
        # --- 第二层：多维加权精排 ---
        scored = []
        for bucket in candidates:
            meta = bucket.get("metadata", {})

            try:
                # Dim 1: topic relevance (fuzzy text, 0~1)
                topic_score = self._calc_topic_score(query, bucket)
                exact_score = self._calc_exact_match_score(query, bucket)
                exact_name_match = self._is_exact_name_match(query, bucket)
                semantic_score = max(
                    0.0,
                    min(1.0, float(vector_scores.get(bucket["id"], 0.0))),
                )

                # Dim 2: emotion resonance (coordinate distance, 0~1)
                emotion_score = self._calc_emotion_score(
                    query_valence, query_arousal, meta
                )

                # Dim 3: time proximity (exponential decay, 0~1)
                time_score = self._calc_time_score(meta)

                # Dim 4: importance (direct normalization)
                importance_score = max(1, min(10, int(meta.get("importance", 5)))) / 10.0

                # --- Weighted sum / 加权求和 ---
                total = (
                    topic_score * self.w_topic
                    + emotion_score * self.w_emotion
                    + time_score * self.w_time
                    + importance_score * self.w_importance
                )
                # Normalize to 0~100 for readability
                weight_sum = self.w_topic + self.w_emotion + self.w_time + self.w_importance
                normalized = (total / weight_sum) * 100 if weight_sum > 0 else 0

                trace_entry = (
                    trace_entries.get(str(bucket.get("id", "")))
                    if trace is not None
                    else None
                )
                if trace_entry is not None:
                    trace_entry["scores"] = {
                        "fuzzy_lexical": round(topic_score, 4),
                        "exact_match": round(exact_score, 4),
                        "emotion": round(emotion_score, 4),
                        "time": round(time_score, 4),
                        "importance": round(importance_score, 4),
                        "semantic": round(semantic_score, 4),
                    }
                    trace_entry["exact_name_match"] = exact_name_match
                    trace_entry["pre_penalty_score"] = round(normalized, 2)
                    trace_entry["semantic_threshold"] = semantic_score >= 0.42
                    trace_entry["threshold"] = self.fuzzy_threshold

                # Threshold check uses raw (pre-penalty) score so resolved buckets
                # 阈值用原始分数判定，确保 resolved 桶在关键词命中时仍可被搜出
                # remain reachable by keyword (penalty applied only to ranking).
                if normalized >= self.fuzzy_threshold or semantic_score >= 0.42:
                    # Resolved buckets get ranking penalty (but still reachable by keyword)
                    # 已解决的桶仅在排序时降权
                    hybrid_score = normalized
                    if semantic_score:
                        hybrid_score = (
                            normalized * 0.65 + semantic_score * 100 * 0.35
                        )
                    if meta.get("resolved", False):
                        hybrid_score *= 0.3
                    superseded_factor = (
                        0.1 if str(meta.get("superseded_by", "") or "").strip() else 1.0
                    )
                    hybrid_score *= superseded_factor
                    if trace_entry is not None:
                        trace_entry["admitted"] = True
                        trace_entry["resolved"] = bool(meta.get("resolved", False))
                        trace_entry["ranking_penalty"] = (
                            0.3 if meta.get("resolved", False) else 1.0
                        )
                        trace_entry["superseded_factor"] = superseded_factor
                        trace_entry["final_ranking_score"] = round(hybrid_score, 2)
                        trace_entry["match_tier"] = (
                            3 if exact_score >= 0.95
                            else 2 if exact_score > 0
                            else 1
                        )
                    bucket["exact_name_match"] = exact_name_match
                    bucket["score"] = round(hybrid_score, 2)
                    bucket["semantic_score"] = round(semantic_score, 4)
                    bucket["vector_match"] = semantic_score >= 0.42 and not exact_score
                    if exact_score >= 0.95:
                        bucket["match_tier"] = 3
                    elif exact_score > 0:
                        bucket["match_tier"] = 2
                    else:
                        bucket["match_tier"] = 1
                    scored.append(bucket)
                elif trace_entry is not None:
                    trace_entry["exclusion_reasons"] = ["threshold"]
            except Exception as e:
                logger.warning(
                    f"Scoring failed for bucket {bucket.get('id', '?')} / "
                    f"桶评分失败: {e}"
                )
                continue

        scored.sort(
            key=lambda x: (
                x["exact_name_match"], x.get("match_tier", 0), x["score"]
            ),
            reverse=True,
        )
        if trace is not None:
            for rank, bucket in enumerate(scored, start=1):
                trace_entry = trace_entries.get(str(bucket.get("id", "")))
                if trace_entry is not None:
                    trace_entry["entered_ranking"] = True
                    trace_entry["rank"] = rank
            trace["status"] = "ok"
            trace["candidate_count"] = len(all_buckets)
            trace["eligible_count"] = len(candidates)
            trace["admitted_count"] = len(scored)
            trace["candidates"] = list(trace_entries.values())
            trace["ranking"] = [str(bucket.get("id", "")) for bucket in scored[:limit]]
        return scored[:limit]

    # ---------------------------------------------------------
    # Topic relevance sub-score:
    # name(×3) + domain(×2.5) + tags(×2) + body(×1)
    # 文本相关性子分：桶名(×3) + 主题域(×2.5) + 标签(×2) + 正文(×1)
    # ---------------------------------------------------------
    def _calc_topic_score(self, query: str, bucket: dict) -> float:
        """
        Calculate text dimension relevance score (0~1).
        计算文本维度的相关性得分。
        """
        exact_score = self._calc_exact_match_score(query, bucket)
        if exact_score:
            return exact_score

        meta = bucket.get("metadata", {})
        query_text = self._normalize_search_text(query)
        name_score = fuzz.partial_ratio(
            query_text, self._normalize_search_text(meta.get("name", ""))
        ) / 100
        domains = meta.get("domain", [])
        if isinstance(domains, str):
            domains = [domains]
        domain_score = max(
            (
                fuzz.partial_ratio(query_text, self._normalize_search_text(domain)) / 100
                for domain in domains
            ),
            default=0,
        )
        keyword_score = max(
            (
                fuzz.ratio(query_text, self._normalize_search_text(keyword)) / 100
                for keyword in self._metadata_keywords(meta)
            ),
            default=0,
        )
        body = " ".join([
            str(meta.get("summary", "")),
            str(bucket.get("content", "")[:2000]),
        ])
        content_score = fuzz.partial_ratio(
            query_text, self._normalize_search_text(body)
        ) / 100

        fuzzy = (
            name_score * 0.30
            + domain_score * 0.20
            + keyword_score * 0.30
            + content_score * 0.20
        )
        return min(0.69, fuzzy)

    @staticmethod
    def _normalize_search_text(value) -> str:
        """Normalize without tokenizing, preserving one-character Chinese names."""
        return "".join(str(value or "").casefold().split())

    def _is_exact_name_match(self, query: str, bucket: dict) -> bool:
        """Prioritize whole normalized names without changing retrieval scores."""
        query_text = self._normalize_search_text(apply_display_aliases(query))
        name = bucket.get("metadata", {}).get("name", "")
        return bool(query_text) and query_text == self._normalize_search_text(
            apply_display_aliases(name or "")
        )

    def _metadata_keywords(self, meta: dict) -> list[str]:
        keywords = meta.get("keywords", [])
        tags = meta.get("tags", [])
        if isinstance(keywords, str):
            keywords = [part.strip() for part in keywords.split(",") if part.strip()]
        if isinstance(tags, str):
            tags = [part.strip() for part in tags.split(",") if part.strip()]
        return [
            str(item)
            for item in list(keywords or []) + list(tags or [])
            if str(item).strip()
        ]

    def _calc_exact_match_score(self, query: str, bucket: dict) -> float:
        """Exact keywords rank highest; content/summary matches rank second."""
        query_text = self._normalize_search_text(query)
        if not query_text:
            return 0.0
        meta = bucket.get("metadata", {})
        keywords = [
            self._normalize_search_text(item)
            for item in self._metadata_keywords(meta)
        ]
        if query_text in keywords:
            return 1.0
        if any(query_text in keyword for keyword in keywords):
            return 0.95

        searchable = " ".join([
            str(meta.get("name", "")),
            str(meta.get("summary", "")),
            str(bucket.get("content", "")),
        ])
        if query_text in self._normalize_search_text(searchable):
            return 0.85
        return 0.0

    # ---------------------------------------------------------
    # Emotion resonance sub-score:
    # Based on Russell circumplex Euclidean distance
    # 情感共鸣子分：基于环形情感模型的欧氏距离
    # No emotion in query → neutral 0.5 (doesn't affect ranking)
    # ---------------------------------------------------------
    def _calc_emotion_score(
        self, q_valence: float, q_arousal: float, meta: dict
    ) -> float:
        """
        Calculate emotion resonance score (0~1, closer = higher).
        计算情感共鸣度（0~1，越近越高）。
        """
        if q_valence is None or q_arousal is None:
            return 0.5  # No emotion coordinates → neutral / 无情感坐标时给中性分

        try:
            b_valence = float(meta.get("valence", 0.5))
            b_arousal = float(meta.get("arousal", 0.3))
        except (ValueError, TypeError):
            return 0.5

        # Euclidean distance, max sqrt(2) ≈ 1.414
        dist = math.sqrt((q_valence - b_valence) ** 2 + (q_arousal - b_arousal) ** 2)
        return max(0.0, 1.0 - dist / 1.414)

    # ---------------------------------------------------------
    # Time proximity sub-score:
    # More recent activation → higher score
    # 时间亲近子分：距上次激活越近分越高
    # ---------------------------------------------------------
    def _calc_time_score(self, meta: dict) -> float:
        """
        Calculate time proximity score (0~1, more recent = higher).
        计算时间亲近度。
        """
        last_active_str = meta.get("last_active", meta.get("created", ""))
        try:
            last_active = datetime.fromisoformat(str(last_active_str))
            days = max(0.0, (datetime.now() - last_active).total_seconds() / 86400)
        except (ValueError, TypeError):
            days = 30
        return math.exp(-0.02 * days)

    # ---------------------------------------------------------
    # List all buckets
    # 列出所有桶
    # ---------------------------------------------------------
    async def list_all(self, include_archive: bool = False) -> list[dict]:
        """
        Recursively walk directories (including domain subdirs), list all buckets.
        递归遍历目录（含域子目录），列出所有记忆桶。
        """
        buckets = []

        dirs = [self.permanent_dir, self.dynamic_dir, self.feel_dir]
        if include_archive:
            dirs.append(self.archive_dir)

        for dir_path in dirs:
            if not os.path.exists(dir_path):
                continue
            for root, _, files in os.walk(dir_path):
                for filename in files:
                    if not filename.endswith(".md"):
                        continue
                    file_path = os.path.join(root, filename)
                    bucket = self._load_bucket(file_path)
                    if bucket:
                        buckets.append(bucket)

        return buckets

    # ---------------------------------------------------------
    # Statistics (counts per category + total size)
    # 统计信息（各分类桶数量 + 总体积）
    # ---------------------------------------------------------
    async def get_stats(self) -> dict:
        """
        Return memory bucket statistics (including domain subdirs).
        返回记忆桶的统计数据。
        """
        stats = {
            "permanent_count": 0,
            "dynamic_count": 0,
            "archive_count": 0,
            "feel_count": 0,
            "total_size_kb": 0.0,
            "domains": {},
        }

        for subdir, key in [
            (self.permanent_dir, "permanent_count"),
            (self.dynamic_dir, "dynamic_count"),
            (self.archive_dir, "archive_count"),
            (self.feel_dir, "feel_count"),
        ]:
            if not os.path.exists(subdir):
                continue
            for root, _, files in os.walk(subdir):
                for f in files:
                    if f.endswith(".md"):
                        stats[key] += 1
                        fpath = os.path.join(root, f)
                        try:
                            stats["total_size_kb"] += os.path.getsize(fpath) / 1024
                        except OSError:
                            pass
                        # Per-domain counts / 每个域的桶数量
                        domain_name = os.path.basename(root)
                        if domain_name != os.path.basename(subdir):
                            stats["domains"][domain_name] = stats["domains"].get(domain_name, 0) + 1

        return stats

    # ---------------------------------------------------------
    # Archive bucket (move from permanent/dynamic into archive)
    # 归档桶（从 permanent/dynamic 移入 archive）
    # Called by decay engine to simulate "forgetting"
    # 由衰减引擎调用，模拟"遗忘"
    # ---------------------------------------------------------
    @guarded_async_mutation("bucket_archive")
    async def archive(self, bucket_id: str) -> bool:
        """
        Move a bucket into the archive directory (preserving domain subdirs).
        将指定桶移入归档目录（保留域子目录结构）。
        """
        with bucket_write_scope(self.base_dir):
            file_path = self._find_bucket_file(bucket_id)
            if not file_path:
                return False

            try:
                # Read once, get domain info and update type / 一次性读取
                post = frontmatter.load(file_path)
                domain = post.get("domain", ["未分类"])
                primary_domain = sanitize_name(domain[0]) if domain else "未分类"
                archive_subdir = os.path.join(self.archive_dir, primary_domain)
                os.makedirs(archive_subdir, exist_ok=True)

                dest = safe_path(archive_subdir, os.path.basename(file_path))

                # Update type marker then move file / 更新类型标记后移动文件
                post["type"] = "archived"
                with open(file_path, "w", encoding="utf-8") as f:
                    f.write(frontmatter.dumps(post))

                # Use shutil.move for cross-filesystem safety
                # 使用 shutil.move 保证跨文件系统安全
                shutil.move(file_path, str(dest))
            except Exception as e:
                logger.error(
                    f"Failed to archive bucket / 归档桶失败: {bucket_id}: {e}"
                )
                return False

            logger.info(f"Archived bucket / 归档记忆桶: {bucket_id} → archive/{primary_domain}/")
            return True

    # ---------------------------------------------------------
    # Internal: find bucket file across all three directories
    # 内部：在三个目录中查找桶文件
    # ---------------------------------------------------------
    def _find_bucket_file(self, bucket_id: str) -> Optional[str]:
        """
        Recursively search permanent/dynamic/archive for a bucket file
        matching the given ID.
        在 permanent/dynamic/archive 中递归查找指定 ID 的桶文件。
        """
        if not bucket_id:
            return None
        for dir_path in [self.permanent_dir, self.dynamic_dir, self.archive_dir, self.feel_dir]:
            if not os.path.exists(dir_path):
                continue
            for root, _, files in os.walk(dir_path):
                for fname in files:
                    if not fname.endswith(".md"):
                        continue
                    # Match by exact ID segment in filename
                    # 通过文件名中的 ID 片段精确匹配
                    name_part = fname[:-3]  # remove .md
                    if name_part == bucket_id or name_part.endswith(f"_{bucket_id}"):
                        return os.path.join(root, fname)
        return None

    # ---------------------------------------------------------
    # Internal: load bucket data from .md file
    # 内部：从 .md 文件加载桶数据
    # ---------------------------------------------------------
    def _load_bucket(self, file_path: str) -> Optional[dict]:
        """
        Parse a Markdown file and return structured bucket data.
        解析 Markdown 文件，返回桶的结构化数据。
        """
        try:
            post = frontmatter.load(file_path)
            metadata = dict(post.metadata)
            # O5B operation markers are durable write-control metadata only;
            # never expose them through ordinary memory reads/search results.
            metadata.pop(_IMPORT_MARKER_FIELD, None)
            if "name" in metadata:
                metadata["name"] = apply_display_aliases(metadata["name"])
            if "tags" in metadata:
                metadata["tags"] = apply_display_aliases_to_value(metadata["tags"])
            if "summary" in metadata:
                metadata["summary"] = apply_display_aliases_to_value(
                    metadata["summary"]
                )
            metadata.setdefault("created_at", _date_only(metadata.get("created")))
            metadata.setdefault(
                "updated_at",
                _date_only(metadata.get("updated_at") or metadata.get("last_active") or metadata.get("created")),
            )
            metadata.setdefault("dormant", False)
            metadata.setdefault("sealed", 0)
            metadata.setdefault("source_bucket", "")
            metadata.setdefault("trigger_date", "")
            metadata.setdefault("trigger_last_seen", "")
            metadata["provenance_kind"] = normalize_provenance_kind(
                metadata.get("provenance_kind")
            )
            metadata["sealed"] = 1 if int(metadata.get("sealed", 0) or 0) == 1 else 0
            return {
                "id": post.get("id", Path(file_path).stem),
                "metadata": metadata,
                "content": apply_display_aliases(post.content),
                "path": file_path,
            }
        except Exception as e:
            logger.warning(
                f"Failed to load bucket file / 加载桶文件失败: {file_path}: {e}"
            )
            return None

    @guarded_async_mutation("bucket_alias_cleanup")
    async def clean_display_aliases(self) -> dict:
        """Persist display aliases across all bucket files without changing dates."""
        changed = []
        scanned = 0
        replacements = 0
        for base_dir in (
            self.permanent_dir,
            self.dynamic_dir,
            self.archive_dir,
            self.feel_dir,
        ):
            if not os.path.exists(base_dir):
                continue
            for root, _, files in os.walk(base_dir):
                for filename in files:
                    if not filename.endswith(".md"):
                        continue
                    scanned += 1
                    with bucket_write_scope(self.base_dir):
                        path = os.path.join(root, filename)
                        try:
                            post = frontmatter.load(path)
                        except Exception as exc:
                            logger.warning("Alias cleanup could not read %s: %s", path, exc)
                            continue

                        if _is_sealed_bucket(post):
                            continue

                        original_content = post.content
                        original_name = post.get("name")
                        original_tags = post.get("tags")
                        post.content = apply_display_aliases(post.content)
                        # Alias cleanup can rewrite the body. It is not a
                        # provenance-preserving metadata operation.
                        if str(post.content) != str(original_content):
                            post["provenance_kind"] = "unknown"
                        if original_name is not None:
                            post["name"] = apply_display_aliases(original_name)
                        if original_tags is not None:
                            post["tags"] = apply_display_aliases_to_value(original_tags)

                        before = (
                            str(original_content)
                            + str(original_name or "")
                            + str(original_tags or "")
                        )
                        after = (
                            str(post.content)
                            + str(post.get("name", ""))
                            + str(post.get("tags", ""))
                        )
                        file_replacements = sum(
                            before.count(source) for source in DISPLAY_ALIASES
                        )
                        if before == after:
                            continue

                        temp_path = f"{path}.alias-clean.tmp"
                        try:
                            with open(temp_path, "w", encoding="utf-8") as handle:
                                handle.write(frontmatter.dumps(post))
                            os.replace(temp_path, path)
                        except OSError as exc:
                            logger.error("Alias cleanup could not write %s: %s", path, exc)
                            try:
                                if os.path.exists(temp_path):
                                    os.remove(temp_path)
                            except OSError:
                                pass
                            continue

                        bucket_id = str(post.get("id", Path(path).stem))
                        replacements += file_replacements
                        changed.append({
                            "id": bucket_id,
                            "name": str(post.get("name", bucket_id)),
                            "replacements": file_replacements,
                        })
                    if original_content != post.content:
                        await self._refresh_ordinary_embedding_best_effort(
                            bucket_id,
                            post.content,
                        )

        remaining = 0
        for base_dir in (
            self.permanent_dir,
            self.dynamic_dir,
            self.archive_dir,
            self.feel_dir,
        ):
            if not os.path.exists(base_dir):
                continue
            for root, _, files in os.walk(base_dir):
                for filename in files:
                    if not filename.endswith(".md"):
                        continue
                    try:
                        post = frontmatter.load(os.path.join(root, filename))
                    except Exception:
                        continue
                    searchable = (
                        str(post.content)
                        + str(post.get("name", ""))
                        + str(post.get("tags", ""))
                    )
                    remaining += sum(
                        searchable.count(source) for source in DISPLAY_ALIASES
                    )
        return {
            "scanned": scanned,
            "changed_count": len(changed),
            "replacements": replacements,
            "remaining": remaining,
            "changed": changed,
        }

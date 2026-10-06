# ============================================================
# Module: Shared stateless helpers (server_common.py)
# 模块：公共无状态工具函数
#
# Bucket metadata readers, date / ID / todo / emotion parsing, structured
# filter matching, and small display helpers used across server.py.
# 桶元数据读取、日期 / ID / todo / 情绪解析、结构化过滤匹配与小型显示工具。
#
# Stateless: no config, runtime components, process-local tables or clock
# reads. This module must never import server; server.py re-exports every
# name defined here.
# 无状态：不读配置、运行时组件、进程内状态表，也不读当前时间。
# 本模块禁止 import server；server.py 会重新导出这里定义的全部名字。
#
# Depended on by: server.py, server_boot_format.py
# 被谁依赖：server.py、server_boot_format.py
# ============================================================

import json as _json_lib
import os
from archive_session_operations import ArchiveSessionError
from bucket_manager import (
    canonicalize_todos,
    normalize_provenance_kind,
    reconcile_todo_provenance,
)
from datetime import datetime
from dehydrator import AnalysisParseError
from openai import APIConnectionError, APITimeoutError
from related_integrity import parse_related


def _canonical_body_name(content: str) -> str:
    """Use at most 20 Unicode characters from the stored body as a title."""
    return " ".join(str(content).split())[:20]


def _provider_failure_category(exc: BaseException) -> str:
    """Map an exception chain to a small public reason without exposing details."""
    current: BaseException | None = exc
    while current is not None:
        if isinstance(current, AnalysisParseError):
            return "parse_error"
        if getattr(current, "status_code", None) == 429:
            return "rate_limited"
        if isinstance(current, (APIConnectionError, APITimeoutError)):
            return "connection_error"
        current = current.__cause__
    if "API 不可用" in str(exc) or "OMBRE_API_KEY" in str(exc):
        return "provider_unconfigured"
    return "provider_error"


def _bucket_date(meta: dict, *keys: str) -> str:
    """Return the first available bucket date as YYYY-MM-DD."""
    for key in keys:
        value = meta.get(key)
        if not value:
            continue
        try:
            return datetime.fromisoformat(str(value)).date().isoformat()
        except (ValueError, TypeError):
            continue
    return ""


def _bucket_topic(meta: dict) -> str:
    domains = meta.get("domain", []) or meta.get("domains", [])
    if isinstance(domains, list) and domains:
        return ",".join(str(d) for d in domains if d)
    if isinstance(domains, str):
        return domains
    return "未分类"


def _bucket_emotion(meta: dict) -> str:
    try:
        val = float(meta.get("valence", 0.5))
        aro = float(meta.get("arousal", 0.3))
    except (ValueError, TypeError):
        val, aro = 0.5, 0.3
    return f"V{val:.1f}/A{aro:.1f}"


def _superseded_by_id(metadata: dict) -> str:
    """Return the normalized successor marker; absent/empty metadata means active."""
    value = metadata.get("superseded_by", "") if isinstance(metadata, dict) else ""
    return str(value or "").strip()


def _supersedes_ids(metadata: dict) -> list[str]:
    """Read legacy-tolerant reverse supersession metadata as unique bucket IDs."""
    value = metadata.get("supersedes", []) if isinstance(metadata, dict) else []
    if isinstance(value, str):
        values = _parse_csv_ids(value)
    elif isinstance(value, list):
        values = [str(item).strip() for item in value if str(item).strip()]
    else:
        values = []
    return list(dict.fromkeys(values))


def _bucket_display_icon(meta: dict, *, protected_as_pinned: bool = False) -> str:
    """Share the pulse type/status icons while keeping query pins literal."""
    if int(meta.get("sealed", 0) or 0) == 1:
        return "🔒"
    if meta.get("pinned") or (protected_as_pinned and meta.get("protected")):
        return "📌"
    if meta.get("type") == "permanent":
        return "📦"
    if meta.get("type") == "feel":
        return "🫧"
    if meta.get("type") == "archived":
        return "🗄️"
    if meta.get("resolved", False):
        return "✅"
    return "💭"


def _is_recent_bucket(bucket: dict, cutoff: str | None, *, exact_day: bool = False) -> bool:
    if not cutoff:
        return True
    updated = _bucket_date(bucket.get("metadata", {}), "updated_at", "last_active", "created")
    return bool(updated and (updated == cutoff if exact_day else updated >= cutoff))


def _parse_date_filter(value: str, parameter: str) -> str:
    value = (value or "").strip()
    if not value:
        return ""
    try:
        return datetime.strptime(value, "%Y-%m-%d").date().isoformat()
    except ValueError as exc:
        raise ValueError(f"{parameter} must use YYYY-MM-DD format.") from exc


def _parse_optional_date(value: str, parameter: str) -> str | None:
    value = (value or "").strip()
    if not value:
        return ""
    return _parse_date_filter(value, parameter)


def _is_in_date_range(
    bucket: dict,
    date_from: str = "",
    date_to: str = "",
) -> bool:
    if not date_from and not date_to:
        return True
    updated = _bucket_date(
        bucket.get("metadata", {}),
        "updated_at",
        "last_active",
        "created",
    )
    if not updated:
        return False
    return (not date_from or updated >= date_from) and (
        not date_to or updated <= date_to
    )


def _parse_resonance(value: str) -> tuple[float, float] | None:
    value = (value or "").strip()
    if not value:
        return None
    try:
        raw_v, raw_a = [part.strip() for part in value.split(",", 1)]
        target = (float(raw_v), float(raw_a))
    except (ValueError, TypeError) as exc:
        raise ValueError("resonance must use 'v,a' format, both between 0 and 1.") from exc
    if not (0 <= target[0] <= 1 and 0 <= target[1] <= 1):
        raise ValueError("resonance values must be between 0 and 1.")
    return target


def _resonance_distance(bucket: dict, target: tuple[float, float]) -> float:
    meta = bucket.get("metadata", {})
    raw_valence = meta.get("valence")
    raw_arousal = meta.get("arousal")
    valence = float(0.5 if raw_valence is None else raw_valence)
    arousal = float(0.3 if raw_arousal is None else raw_arousal)
    return ((valence - target[0]) ** 2 + (arousal - target[1]) ** 2) ** 0.5


def _parse_csv_ids(value: str) -> list[str]:
    return [part.strip() for part in (value or "").split(",") if part.strip()]


def _normalize_archive_topics(topics: list[str] | None) -> list[str]:
    """Normalize structured archive topics without changing their labels."""
    if topics is None:
        return []
    if not isinstance(topics, list) or any(not isinstance(item, str) for item in topics):
        raise ValueError("topics must be a list of strings.")

    normalized = []
    seen = set()
    for item in topics:
        topic = item.strip()
        if not topic or topic in seen:
            continue
        seen.add(topic)
        normalized.append(topic)
    return normalized


def _structured_metadata_values(metadata: dict, field: str) -> list[str]:
    """Return safe structured values without stringifying malformed metadata."""
    raw = metadata.get(field, [])
    if field == "tags" and isinstance(raw, str):
        raw = [part.strip() for part in raw.split(",") if part.strip()]
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, str)]


def _is_test_bucket(bucket: dict) -> bool:
    """Use the existing exact tag identity only at delivery entry points."""
    return "test" in _structured_metadata_values(bucket.get("metadata", {}), "tags")


def _matches_any_structured_filter(
    bucket: dict,
    field: str,
    values: list[str],
) -> bool:
    if not values:
        return True
    metadata = bucket.get("metadata", {})
    stored_values = _structured_metadata_values(metadata, field)
    wanted = set(values)
    return any(item in wanted for item in stored_values)


def _breath_recency_key(bucket: dict) -> tuple[str, str]:
    """Return the canonical deterministic breath recency key."""
    metadata = bucket.get("metadata", {})
    return (
        _bucket_date(
            metadata,
            "updated_at",
            "last_active",
            "created_at",
            "created",
        ),
        str(bucket.get("id", "")),
    )


def _normalize_todos(raw) -> list[str]:
    if isinstance(raw, list):
        return [str(item).strip() for item in raw if str(item).strip()]
    if isinstance(raw, dict):
        return [
            f"{key}: {value}".strip()
            for key, value in raw.items()
            if str(value).strip()
        ]
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return []
        try:
            parsed = _json_lib.loads(text)
        except (ValueError, TypeError):
            parsed = None
        if parsed is not None and parsed != raw:
            return _normalize_todos(parsed)
        return [
            line.strip().lstrip("-* ").strip()
            for line in text.replace(",", "\n").splitlines()
            if line.strip().lstrip("-* ").strip()
        ]
    return [str(raw).strip()] if raw is not None and str(raw).strip() else []


def _canonical_todos(raw) -> list[str]:
    """Normalize legacy todo shapes and return an ordered, deduplicated list."""
    return canonicalize_todos(_normalize_todos(raw))


def _parse_explicit_provenance_kind(value) -> str | None:
    """Validate a public assertion without treating an omitted value as one."""
    if value is None or value == "":
        return None
    return normalize_provenance_kind(value, strict=True)


def _structured_todo_items(todo_items) -> tuple[list[str], list[dict]]:
    """Validate MCP todo_items without changing legacy todos semantics."""
    if not isinstance(todo_items, list):
        raise ValueError("todo_items 必须是数组。")
    raw_todos = []
    for item in todo_items:
        if not isinstance(item, dict):
            raise ValueError("todo_items 的每一项必须是对象。")
        text = item.get("text")
        if not isinstance(text, str) or not text.strip():
            raise ValueError("todo_items.text 必须是非空字符串。")
        for field in ("done_at", "dropped_at"):
            if field in item:
                raise ValueError(f"todo_items.{field} is server-generated and cannot be supplied.")
        raw_todos.append(text.strip())
    todos = _canonical_todos(raw_todos)
    try:
        provenance = reconcile_todo_provenance(
            todos,
            todo_items,
            strict=True,
        )
    except ValueError as exc:
        raise ValueError(f"todo_items 无效：{exc}") from exc
    return todos, provenance


def _parse_emotion_history(raw) -> list[dict]:
    if isinstance(raw, list):
        history = raw
    elif isinstance(raw, str) and raw.strip():
        try:
            history = _json_lib.loads(raw)
        except Exception:
            history = []
    else:
        history = []
    return [item for item in history if isinstance(item, dict)]


def _encode_emotion_history(history: list[dict]) -> str:
    return _json_lib.dumps(history[-20:], ensure_ascii=False, separators=(",", ":"))


def _read_emotion_timeline_for_write(path: str) -> list[dict]:
    def invalid_constant(_value):
        raise ValueError()
    def unique_keys(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError()
            result[key] = value
        return result
    try:
        with open(path, "r", encoding="utf-8") as handle:
            timeline = _json_lib.load(handle, parse_constant=invalid_constant, object_pairs_hook=unique_keys)
    except FileNotFoundError:
        if os.path.lexists(path):
            raise ArchiveSessionError("archive_emotion_timeline_invalid") from None
        return []
    except (OSError, ValueError):
        raise ArchiveSessionError("archive_emotion_timeline_invalid") from None
    if (os.path.islink(path) or not isinstance(timeline, list)
            or any(not isinstance(item, dict) for item in timeline)):
        raise ArchiveSessionError("archive_emotion_timeline_invalid")
    return timeline


def _related_ids(meta: dict) -> list[str]:
    return parse_related(meta).require_safe()


def _metadata_restore_value(metadata: dict, field: str):
    """Return a BucketManager.update-compatible value that restores field presence."""
    return metadata.get(field) if field in metadata else None


def _split_search_results(matches: list[dict], max_results: int) -> tuple[list[dict], list[dict], int]:
    """Return all pinned matches plus a separately limited non-pinned result set."""
    pinned = [
        bucket for bucket in matches
        if bucket.get("metadata", {}).get("pinned")
        or bucket.get("metadata", {}).get("protected")
    ]
    regular = [
        bucket for bucket in matches
        if bucket not in pinned
    ]
    pinned.sort(key=lambda bucket: float(bucket.get("score", 0)), reverse=True)
    regular.sort(key=lambda bucket: float(bucket.get("score", 0)), reverse=True)
    hidden_count = max(0, len(regular) - max_results)
    return pinned, regular[:max_results], hidden_count


def _is_sealed(bucket: dict) -> bool:
    """Return True when a bucket is manually sealed."""
    return int(bucket.get("metadata", {}).get("sealed", 0) or 0) == 1

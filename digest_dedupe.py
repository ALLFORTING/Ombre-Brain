"""Pure local embedding duplicate scan used by the digest MCP tool."""

import json
import os
import sqlite3
from pathlib import Path

import numpy as np
import yaml


def _read_frontmatter_fields(
    file_path: str,
    wanted_fields: set[str],
) -> dict[str, object] | None:
    """Read selected top-level YAML scalars without reading a bucket body."""
    try:
        with open(file_path, "r", encoding="utf-8") as handle:
            if handle.readline().strip() != "---":
                return None
            fields: dict[str, object] = {}
            for raw_line in handle:
                if raw_line.strip() in ("---", "..."):
                    return fields
                key, separator, raw_value = raw_line.partition(":")
                if (
                    not separator
                    or key != key.lstrip()
                    or key not in wanted_fields
                ):
                    continue
                try:
                    fields[key] = yaml.safe_load(raw_value)
                except yaml.YAMLError:
                    fields[key] = raw_value.strip()
    except OSError:
        return None
    return None


def _is_sealed(value: object) -> bool:
    """Fail closed when a frontmatter sealed value cannot be interpreted."""
    try:
        return int(value or 0) == 1
    except (TypeError, ValueError):
        return True


def _is_dormant(value: object) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _display_label(value: object, fallback: str, limit: int = 160) -> str:
    text = " ".join(str(value or "").split())
    return text[:limit] if text else fallback


def _bucket_metadata_index(
    bucket_roots: tuple[str, ...],
    *,
    include_display: bool = True,
) -> tuple[dict[str, dict], dict[str, int]]:
    """Index access frontmatter and, when allowed, unsealed display frontmatter."""
    records: dict[str, dict] = {}
    counts = {"buckets": 0, "sealed": 0, "metadata_unreadable": 0}
    for base_dir in bucket_roots:
        if not os.path.exists(base_dir):
            continue
        for root, _, filenames in os.walk(base_dir):
            for filename in filenames:
                if not filename.endswith(".md"):
                    continue
                file_path = os.path.join(root, filename)
                fallback_id = Path(file_path).stem
                counts["buckets"] += 1
                access = _read_frontmatter_fields(file_path, {"id", "sealed"})
                if access is None:
                    counts["sealed"] += 1
                    counts["metadata_unreadable"] += 1
                    records[fallback_id] = {"sealed": True}
                    continue

                bucket_id = str(access.get("id") or fallback_id)
                if _is_sealed(access.get("sealed", 0)):
                    counts["sealed"] += 1
                    records[bucket_id] = {"sealed": True}
                    continue

                if not include_display:
                    records[bucket_id] = {"sealed": False}
                    continue

                display = _read_frontmatter_fields(
                    file_path,
                    {"name", "summary", "dormant"},
                ) or {}
                records[bucket_id] = {
                    "sealed": False,
                    "name": _display_label(display.get("name"), bucket_id),
                    "summary": _display_label(
                        display.get("summary") or display.get("name"),
                        bucket_id,
                        limit=120,
                    ),
                    "dormant": _is_dormant(display.get("dormant", False)),
                }
    return records, counts


def _read_embedding_rows(db_path: str, model: str) -> tuple[list[tuple[str, str]], list[tuple[str, int]]]:
    """Read vectors through SQLite read-only mode and enumerate model counts."""
    database_path = Path(db_path)
    if not database_path.is_file():
        raise RuntimeError("embedding_database_missing")
    database_uri = f"{database_path.resolve().as_uri()}?mode=ro"
    with sqlite3.connect(database_uri, uri=True) as conn:
        conn.execute("PRAGMA query_only = ON")
        model_counts = [
            (str(stored_model or ""), int(count))
            for stored_model, count in conn.execute(
                "SELECT model, COUNT(*) FROM embeddings GROUP BY model ORDER BY model"
            ).fetchall()
        ]
        rows = [
            (str(bucket_id), str(embedding_json))
            for bucket_id, embedding_json in conn.execute(
                "SELECT bucket_id, embedding FROM embeddings WHERE model = ?",
                (model,),
            ).fetchall()
        ]
    return rows, model_counts


def run_dedupe_scan(
    *,
    bucket_roots: tuple[str, ...],
    excluded_archive_roots: tuple[str, ...] = (),
    db_path: str,
    model: str,
    limit: int = 30,
) -> str:
    """Return a local-only duplicate report. It performs no bucket or DB mutation."""
    try:
        limit = max(0, min(int(limit), 500))
    except (TypeError, ValueError):
        return "limit 必须是整数。"

    bucket_records, bucket_counts = _bucket_metadata_index(bucket_roots)
    excluded_archive_records, excluded_archive_counts = _bucket_metadata_index(
        excluded_archive_roots,
        include_display=False,
    )
    embedding_rows, model_counts = _read_embedding_rows(db_path, model)

    orphan_rows = 0
    sealed_vector_rows = 0
    invalid_vector_rows = 0
    usable_entries: list[tuple[str, np.ndarray]] = []
    for bucket_id, embedding_json in embedding_rows:
        record = bucket_records.get(bucket_id)
        if record is None:
            if bucket_id in excluded_archive_records:
                continue
            orphan_rows += 1
            continue
        if record.get("sealed", True):
            sealed_vector_rows += 1
            continue
        try:
            vector = np.asarray(json.loads(embedding_json), dtype=np.float64)
        except (TypeError, ValueError, json.JSONDecodeError):
            invalid_vector_rows += 1
            continue
        if vector.ndim != 1 or vector.size == 0 or not np.isfinite(vector).all():
            invalid_vector_rows += 1
            continue
        usable_entries.append((bucket_id, vector))

    by_dimension: dict[int, list[int]] = {}
    for index, (_, vector) in enumerate(usable_entries):
        by_dimension.setdefault(int(vector.size), []).append(index)

    pair_left: list[np.ndarray] = []
    pair_right: list[np.ndarray] = []
    pair_scores: list[np.ndarray] = []
    for indexes in by_dimension.values():
        if len(indexes) < 2:
            continue
        matrix = np.vstack([usable_entries[index][1] for index in indexes])
        norms = np.linalg.norm(matrix, axis=1)
        nonzero = norms > 0
        if nonzero.sum() < 2:
            continue
        local_indexes = np.asarray(indexes, dtype=np.int64)[nonzero]
        normalized = matrix[nonzero] / norms[nonzero, np.newaxis]
        similarity = np.clip(normalized @ normalized.T, -1.0, 1.0)
        left, right = np.triu_indices(len(local_indexes), k=1)
        pair_left.append(local_indexes[left])
        pair_right.append(local_indexes[right])
        pair_scores.append(similarity[left, right])

    scores = np.concatenate(pair_scores) if pair_scores else np.asarray([], dtype=np.float64)
    left_indexes = np.concatenate(pair_left) if pair_left else np.asarray([], dtype=np.int64)
    right_indexes = np.concatenate(pair_right) if pair_right else np.asarray([], dtype=np.int64)
    distribution = (
        ("0.95+", int(np.count_nonzero(scores >= 0.95))),
        ("0.90-0.95", int(np.count_nonzero((scores >= 0.90) & (scores < 0.95)))),
        ("0.85-0.90", int(np.count_nonzero((scores >= 0.85) & (scores < 0.90)))),
        ("0.80-0.85", int(np.count_nonzero((scores >= 0.80) & (scores < 0.85)))),
        ("0.75-0.80", int(np.count_nonzero((scores >= 0.75) & (scores < 0.80)))),
    )

    lines = [
        "=== digest embedding 查重（只读）===",
        f"当前模型: {model}",
        f"向量: N={len(usable_entries)}（当前模型行={len(embedding_rows)}，sealed 跳过={sealed_vector_rows}，无效跳过={invalid_vector_rows}）",
        f"桶: M={bucket_counts['buckets']}（sealed={bucket_counts['sealed']}，元数据不可读={bucket_counts['metadata_unreadable']}）",
        f"归档桶排除: {excluded_archive_counts['buckets']}",
        f"差额: K=M-N={bucket_counts['buckets'] - len(usable_entries)}",
        f"孤儿向量行: {orphan_rows}",
        "embeddings 表 model 分布:",
    ]
    lines.extend(f"- {stored_model or '(empty)'}: {count}" for stored_model, count in model_counts)
    lines.append("相似度分布（同维、有效、非 sealed 向量对）:")
    lines.extend(f"- {label}: {count}" for label, count in distribution)
    lines.append(f"成对清单（按相似度降序，最多 {limit} 对）:")

    if not len(scores) or limit == 0:
        lines.append("- 无可输出的向量对。")
        return "\n".join(lines)

    ordered = np.argsort(scores)[::-1][:limit]
    for rank, pair_index in enumerate(ordered, start=1):
        left_id = usable_entries[int(left_indexes[pair_index])][0]
        right_id = usable_entries[int(right_indexes[pair_index])][0]
        left_record = bucket_records[left_id]
        right_record = bucket_records[right_id]
        lines.append(
            f"{rank}. {scores[pair_index]:.6f} | "
            f"{left_id} name={left_record['name']!r} summary={left_record['summary']!r} dormant={left_record['dormant']} "
            f"<-> {right_id} name={right_record['name']!r} summary={right_record['summary']!r} dormant={right_record['dormant']}"
        )
    return "\n".join(lines)

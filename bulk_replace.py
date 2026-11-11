"""Literal bulk text replacement: rule validation, planning and rendering.

Pure functions only (no file, SQLite or network access). BucketManager supplies
read-only snapshots and performs the guarded writes; server.py owns the MCP
tool and the one-shot confirmation token.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

FIELDS = ("content", "name", "tags", "todos")
DEFAULT_FIELDS = "content,name,tags,todos"
SCOPES = ("buckets", "letters")
PROVENANCE_CHOICES = ("unknown", "keep", "summary", "inference", "system")
MAX_RULES = 500
MAX_TEXT = 2000
MAX_HITS_SHOWN_PER_BUCKET = 20


class BulkReplaceError(ValueError):
    """Invalid request; the message is safe to show to the operator."""


def normalize_rules(rules: Any) -> list[dict]:
    if not isinstance(rules, list) or not rules:
        raise BulkReplaceError("rules 必须是非空列表，每条形如 {from, to, bucket_ids(可选)}。")
    if len(rules) > MAX_RULES:
        raise BulkReplaceError(f"规则最多 {MAX_RULES} 条，请分批。")
    normalized = []
    for index, raw in enumerate(rules, 1):
        label = f"规则 #{index}"
        if not isinstance(raw, dict):
            raise BulkReplaceError(f"{label} 必须是对象。")
        extra = set(raw) - {"from", "to", "bucket_ids"}
        if extra:
            raise BulkReplaceError(f"{label} 含不支持的键：{', '.join(sorted(map(str, extra)))}。")
        source, target = raw.get("from"), raw.get("to")
        if not isinstance(source, str) or not source:
            raise BulkReplaceError(f"{label} 的 from 必须是非空字符串。")
        if not isinstance(target, str):
            raise BulkReplaceError(f"{label} 的 to 必须是字符串（空串表示删除）。")
        if len(source) > MAX_TEXT or len(target) > MAX_TEXT:
            raise BulkReplaceError(f"{label} 的 from/to 不能超过 {MAX_TEXT} 字。")
        if source == target:
            raise BulkReplaceError(f"{label} 的 from 与 to 相同。")
        scope = raw.get("bucket_ids")
        if scope is not None:
            if (not isinstance(scope, list) or not scope
                    or any(not isinstance(item, str) or not item.strip() for item in scope)):
                raise BulkReplaceError(f"{label} 的 bucket_ids 必须是非空的桶 id 字符串列表。")
            scope = list(dict.fromkeys(item.strip() for item in scope))
        normalized.append({"from": source, "to": target, "bucket_ids": scope})
    return normalized


def normalize_fields(raw: Any) -> list[str]:
    text = DEFAULT_FIELDS if raw is None or str(raw).strip() == "" else str(raw)
    selected = list(dict.fromkeys(part.strip().lower() for part in text.split(",") if part.strip()))
    bad = [name for name in selected if name not in FIELDS]
    if bad or not selected:
        raise BulkReplaceError("fields 只能是 content、name、tags、todos 的逗号组合。")
    return [name for name in FIELDS if name in selected]


def normalize_scope(raw: Any) -> list[str]:
    text = "buckets,letters" if raw is None or str(raw).strip() == "" else str(raw)
    selected = list(dict.fromkeys(part.strip().lower() for part in text.split(",") if part.strip()))
    if not selected or any(name not in SCOPES for name in selected):
        raise BulkReplaceError("scope 只能是 buckets、letters 的逗号组合。")
    return [name for name in SCOPES if name in selected]


def normalize_provenance(raw: Any) -> str:
    value = str(raw or "keep").strip().lower()
    if value not in PROVENANCE_CHOICES:
        raise BulkReplaceError("provenance 只能是 " + "、".join(PROVENANCE_CHOICES) + "。")
    return value


def _apply_text(text: str, indexed_rules: list[tuple[int, dict]], context: int):
    """Apply rules in order; hits record the text each rule actually saw."""
    hits = []
    for index, rule in indexed_rules:
        needle = rule["from"]
        start = 0
        while True:
            pos = text.find(needle, start)
            if pos < 0:
                break
            end = pos + len(needle)
            hits.append({
                "rule": index,
                "before": text[max(0, pos - context):pos],
                "lead": pos - context > 0,
                "after": text[end:end + context],
                "trail": end + context < len(text),
            })
            start = end
        if start:
            # split/join == str.replace for non-overlapping literals.
            text = rule["to"].join(text.split(needle))
    return text, hits


def plan_bucket(source: dict, rules: list[dict], fields: list[str], context: int) -> dict | None:
    """Return the per-bucket change set, or None when nothing would change."""
    bucket_id = source["id"]
    indexed = [(i, r) for i, r in enumerate(rules, 1)
               if r["bucket_ids"] is None or bucket_id in r["bucket_ids"]]
    if not indexed:
        return None
    changes: dict[str, Any] = {}
    hits: list[dict] = []
    if "content" in fields and isinstance(source.get("content"), str):
        new, found = _apply_text(source["content"], indexed, context)
        if new != source["content"]:
            changes["content"] = new
            hits += [dict(h, field="content") for h in found]
    if "name" in fields and isinstance(source.get("name"), str):
        new, found = _apply_text(source["name"], indexed, max(context, len(source["name"])))
        if new != source["name"]:
            changes["name"] = new
            hits += [dict(h, field="name") for h in found]
    tags = source.get("tags")
    if "tags" in fields and isinstance(tags, (list, str)):
        items = [tags] if isinstance(tags, str) else list(tags)
        new_items, tag_hits, touched = [], [], False
        for item in items:
            if not isinstance(item, str):
                new_items.append(item)
                continue
            new, found = _apply_text(item, indexed, max(context, len(item)))
            if new != item:
                touched = True
                tag_hits += [dict(h, field="tags") for h in found]
            new_items.append(new)
        if touched:
            # Two tags may collapse into one; keep order, drop the repeat.
            merged = list(dict.fromkeys(new_items)) if all(
                isinstance(i, str) for i in new_items) else new_items
            changes["tags"] = merged[0] if isinstance(tags, str) else merged
            hits += tag_hits
    todos = source.get("todos")
    if "todos" in fields and isinstance(todos, list):
        new_todos, todo_hits, touched = [], [], False
        for item in todos:
            if not isinstance(item, str):
                new_todos.append(item)
                continue
            new, found = _apply_text(item, indexed, max(context, len(item)))
            if new != item:
                touched = True
                todo_hits += [dict(h, field="todos") for h in found]
            new_todos.append(new)
        if touched:
            changes["todos"] = new_todos
            hits += todo_hits
            # Keep the sidecar texts in step; id, said_by, said_at, source_bucket stay as is.
            records = source.get("todo_provenance")
            if isinstance(records, list):
                new_records = []
                for record in records:
                    if isinstance(record, dict) and isinstance(record.get("text"), str):
                        record = dict(record, text=_apply_text(record["text"], indexed, 0)[0])
                    new_records.append(record)
                if new_records != records:
                    changes["todo_provenance"] = new_records
    if not changes:
        return None
    return {"id": bucket_id, "name": source.get("name"), "label": source.get("label"),
            "pre": source["preimage_sha256"], "changes": changes, "hits": hits}


def _count(value: Any, needle: str) -> int:
    if isinstance(value, str):
        return value.count(needle)
    if isinstance(value, list):
        return sum(_count(item, needle) for item in value)
    if isinstance(value, dict):
        return sum(_count(item, needle) for item in value.values())
    return 0


def build_plan(scan: dict, rules: list[dict], fields: list[str], provenance: str,
               context: int = 12, scope: list[str] | None = None) -> dict:
    scope = list(scope or SCOPES)
    sources = scan["sources"]
    buckets = []
    for source in sources:
        planned = plan_bucket(source, rules, fields, context)
        if planned is not None:
            buckets.append(planned)
    buckets.sort(key=lambda item: item["id"])
    stats = []
    present = {source["id"] for source in sources}
    for index, rule in enumerate(rules, 1):
        hit_buckets = [b for b in buckets if any(h["rule"] == index for h in b["hits"])]
        stats.append({
            "index": index, "from": rule["from"], "to": rule["to"],
            "hits": sum(1 for b in buckets for h in b["hits"] if h["rule"] == index),
            "buckets": len(hit_buckets),
            "scope_missing": [i for i in (rule["bucket_ids"] or []) if i not in present],
        })
    untouched: dict[str, int] = {}
    for source in sources:
        for key, value in (source.get("extra") or {}).items():
            if "todos" in fields and key in ("todos", "todo_provenance"):
                continue
            total = sum(_count(value, r["from"]) for r in rules
                        if r["bucket_ids"] is None or source["id"] in r["bucket_ids"])
            if total:
                untouched[key] = untouched.get(key, 0) + total
    payload = {
        "rules": [{"from": r["from"], "to": r["to"], "bucket_ids": r["bucket_ids"]} for r in rules],
        "fields": fields,
        "scope": scope,
        "provenance": provenance,
        "buckets": [{"id": b["id"], "pre": b["pre"],
                     "post": hashlib.sha256(json.dumps(
                         [b["changes"].get(k) for k in (*FIELDS, "todo_provenance")], ensure_ascii=False
                     ).encode("utf-8")).hexdigest()} for b in buckets],
    }
    return {"rules": rules, "fields": fields, "scope": scope, "provenance": provenance,
            "buckets": buckets, "stats": stats, "untouched_fields": untouched, "payload": payload,
            "skipped_sealed": scan["skipped_sealed"],
            "skipped_sealed_letters": scan.get("skipped_sealed_letters", 0),
            "unreadable": scan["unreadable"],
            "duplicate_ids": scan["duplicate_ids"], "scanned": len(sources)}


def _flat(text: str) -> str:
    return "↵".join("".join(text.split("\r")).split("\n"))


def _render_hit(hit: dict, rules: list[dict]) -> str:
    rule = rules[hit["rule"] - 1]
    where = hit["field"]
    return (f"  [{where} #{hit['rule']}] {'…' if hit['lead'] else ''}{_flat(hit['before'])}"
            f"【{_flat(rule['from'])} → {_flat(rule['to'])}】{_flat(hit['after'])}"
            f"{'…' if hit['trail'] else ''}")


def render_preview(plan: dict, plan_hash: str, token: str | None, ttl: int,
                   page: int, page_size: int) -> str:
    buckets, rules = plan["buckets"], plan["rules"]
    total_hits = sum(len(b["hits"]) for b in buckets)
    by_field = {k: sum(1 for b in buckets for h in b["hits"] if h["field"] == k) for k in FIELDS}
    lines = ["bulk_replace preview: no changes made.",
             f"plan_hash: {plan_hash}",
             f"规则 {len(rules)} 条；范围 {','.join(plan['scope'])}；字段 {','.join(plan['fields'])}；"
             f"正文改动后 provenance_kind: "
             f"{'保持不变' if plan['provenance'] == 'keep' else plan['provenance']}（信不带该字段）",
             f"扫描未封存对象 {plan['scanned']} 个（桶和信）；命中 {len(buckets)} 个，共 {total_hits} 处"
             f"（content {by_field['content']} / name {by_field['name']} / tags {by_field['tags']} / todos {by_field['todos']}）",
             f"跳过：sealed 桶 {plan['skipped_sealed']} 个、sealed 信 {plan['skipped_sealed_letters']} 封"
             f"（不读取、不显示）；无法读取 {plan['unreadable']} 个；重复 id {plan['duplicate_ids']} 个"]
    lines.append("规则统计：")
    for stat in plan["stats"]:
        note = "" if stat["hits"] else "  ← 没有命中"
        lines.append(f"  #{stat['index']} 「{_flat(stat['from'])}」→「{_flat(stat['to'])}」："
                     f"{stat['hits']} 处 / {stat['buckets']} 个桶{note}")
        if stat["scope_missing"]:
            lines.append("      bucket_ids 里读不到（不存在、已封存或不在扫描范围）："
                         + ", ".join(stat["scope_missing"][:20]))
    if plan["untouched_fields"]:
        lines.append("提示：下列 frontmatter 字段里还有 from 的字面命中，本入口不会改："
                     + "；".join(f"{k} {v} 处" for k, v in sorted(plan["untouched_fields"].items())))
    pages = max(1, -(-len(buckets) // page_size))
    page = min(max(1, page), pages)
    lo = (page - 1) * page_size
    lines.append(f"--- 第 {page}/{pages} 页（桶 {lo + 1 if buckets else 0}-{min(lo + page_size, len(buckets))}"
                 f" / {len(buckets)}）---")
    for bucket in buckets[lo:lo + page_size]:
        title = bucket.get("label") or bucket["name"] or ""
        lines.append(f"[{bucket['id']}] {_flat(str(title))}（{len(bucket['hits'])} 处）")
        for hit in bucket["hits"][:MAX_HITS_SHOWN_PER_BUCKET]:
            lines.append(_render_hit(hit, rules))
        if len(bucket["hits"]) > MAX_HITS_SHOWN_PER_BUCKET:
            lines.append(f"  …另有 {len(bucket['hits']) - MAX_HITS_SHOWN_PER_BUCKET} 处未展开")
    if token:
        lines.append(f"有效期 {ttl} 秒，一次性；用完全相同的 rules/fields/provenance、dry_run=False 和 "
                     "confirm_token 执行。规则、命中桶或桶内容有任何变化都会作废，需要重新预览。")
        lines.append(f"confirm_token: {token}")
    else:
        lines.append("没有可改动的内容，未签发 confirm_token。")
    return "\n".join(lines)

"""Anchors: dated "keep this in mind in every window" notes shown by boot.

An anchor is an ordinary bucket tagged ANCHOR_TAG with extra metadata that is
only written when used (never defaulted by the bucket builder):

* anchor_until      YYYY-MM-DD; valid through the whole of that day (Asia/Shanghai)
* anchor_ended_at   date Ting ended it manually
* anchor_log        [{"at": "YYYY-MM-DDTHH:MM", "text": "..."}] window records

Expiry and ending are decided at read time. Boot only reads anchors; nothing is
consumed or marked seen, so every window and profile sees the same full text.
"""
from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

from utils import count_tokens_approx

ANCHOR_TAG = "锚点"
ANCHOR_MAX_ACTIVE = 4
ANCHOR_BODY_MAX_CHARS = 300
ANCHOR_NOTE_MAX_CHARS = 80
ANCHOR_SHOWN_NOTES = 5
ANCHOR_TG_TOKEN_CAP = 2200  # TG boot is 4000 tokens in total; leave room for the other sections
ANCHOR_NOTE_DEDUPE_MINUTES = 10
ANCHOR_END = "end"
ANCHOR_FIELDS = ("anchor_until", "anchor_ended_at", "anchor_log")

_TZ = ZoneInfo("Asia/Shanghai")


def shanghai_now() -> datetime:
    return datetime.now(_TZ)


def parse_anchor_date(value) -> date:
    """Strict YYYY-MM-DD."""
    text = str(value or "").strip()
    try:
        parsed = date.fromisoformat(text)
    except ValueError:
        raise ValueError("anchor_until must be YYYY-MM-DD, or 'end' to end the anchor.") from None
    if parsed.isoformat() != text:
        raise ValueError("anchor_until must be YYYY-MM-DD, or 'end' to end the anchor.")
    return parsed


def _until(meta: dict):
    try:
        return date.fromisoformat(str(meta.get("anchor_until") or "").strip())
    except ValueError:
        return None


def is_anchor(meta: dict) -> bool:
    return bool(meta.get("anchor_until"))


def anchor_state(meta: dict, today: date) -> str:
    """none / active / ended / expired."""
    until = _until(meta)
    if until is None:
        return "none"
    if meta.get("anchor_ended_at"):
        return "ended"
    return "active" if until >= today else "expired"


def is_visible_active(bucket: dict, today: date, *, is_sealed, is_test, superseded_by) -> bool:
    meta = bucket.get("metadata", {})
    return (
        anchor_state(meta, today) == "active"
        and not is_sealed(bucket)
        and not is_test(bucket)
        and not superseded_by(meta)
    )


def collect_active(buckets: list[dict], today: date, **filters) -> list[dict]:
    found = [b for b in buckets if is_visible_active(b, today, **filters)]
    found.sort(key=lambda b: (str(b["metadata"].get("anchor_until")), str(b["metadata"].get("created", "")), str(b["id"])))
    return found


def clean_note(text: str) -> str:
    note = " ".join(str(text or "").split())
    if not note:
        raise ValueError("anchor_note is empty.")
    if len(note) > ANCHOR_NOTE_MAX_CHARS:
        raise ValueError(f"anchor_note must be at most {ANCHOR_NOTE_MAX_CHARS} characters (got {len(note)}).")
    return note


def make_log_entry(text: str, now: datetime | None = None) -> dict:
    now = now or shanghai_now()
    return {"at": now.strftime("%Y-%m-%dT%H:%M"), "text": clean_note(text)}


def append_log(existing, entry: dict):
    """Return the new log, or None when it duplicates the previous entry within the dedupe window."""
    log = [e for e in (existing or []) if isinstance(e, dict)]
    if log:
        last = log[-1]
        try:
            gap = (datetime.fromisoformat(entry["at"]) - datetime.fromisoformat(str(last.get("at")))).total_seconds()
        except ValueError:
            gap = None
        if last.get("text") == entry["text"] and gap is not None and 0 <= gap < ANCHOR_NOTE_DEDUPE_MINUTES * 60:
            return None
    return log + [entry]


def _fmt_at(at: str) -> str:
    try:
        return datetime.fromisoformat(str(at)).strftime("%m-%d %H:%M")
    except ValueError:
        return str(at)


def format_log_lines(log, limit: int | None = None) -> list[str]:
    entries = [e for e in (log or []) if isinstance(e, dict)]
    return [f"- {_fmt_at(e.get('at'))} {e.get('text', '')}" for e in (entries if limit is None else entries[-limit:])]


def days_left_text(until: date, today: date) -> str:
    left = (until - today).days
    return "今天最后一天" if left == 0 else f"还剩 {left} 天"


def format_anchor(bucket: dict, today: date, shown_notes: int = ANCHOR_SHOWN_NOTES) -> str:
    meta = bucket["metadata"]
    until = _until(meta)
    head = (f"[bucket_id:{bucket['id']}] 有效至 {until.strftime('%m-%d')}（{days_left_text(until, today)}）")
    parts = [head, str(bucket.get("content", "")).strip()]
    log = [e for e in (meta.get("anchor_log") or []) if isinstance(e, dict)]
    if log:
        parts.append("窗口记录：")
        if len(log) > shown_notes:
            parts.append(f"（更早 {len(log) - shown_notes} 条，dream(detail_ids=\"{bucket['id']}\") 看全部）")
        if shown_notes > 0:
            parts.extend(format_log_lines(log, shown_notes))
    return "\n".join(parts)


ANCHORS_HEADER = "=== boot: 锚点 ===\n"


def format_section(active: list[dict], today: date, token_cap: int | None = None) -> str:
    """Boot section text; empty string when no anchor is active (section omitted).

    Anchor bodies are always shown in full. Under a token cap (TG), the number of
    recent window records shown per anchor shrinks (5 -> 0) until the section fits;
    older records are folded into a one-line pointer to dream.
    """
    if not active:
        return ""
    for shown in range(ANCHOR_SHOWN_NOTES, -1, -1):
        text = ANCHORS_HEADER + "\n---\n".join(format_anchor(b, today, shown) for b in active)
        if token_cap is None or shown == 0 or count_tokens_approx(text) <= token_cap:
            return text


def breath_tag(meta: dict, today: date) -> str:
    """Prefix for breath results, e.g. '[锚点 至 11-07] '; empty for non-anchors."""
    state = anchor_state(meta, today)
    until = _until(meta)
    if state == "none" or until is None:
        return ""
    if state == "active":
        return f"[锚点 至 {until.strftime('%m-%d')}] "
    return f"[锚点已{'结束' if state == 'ended' else '到期'}] "


def dream_lines(meta: dict, today: date) -> str:
    """Full anchor fields for dream: until, ended_at, every log entry."""
    until = _until(meta)
    if until is None:
        return ""
    state = anchor_state(meta, today)
    label = {"active": "有效", "ended": "已手动结束", "expired": "已到期"}[state]
    lines = [f"锚点：anchor_until={until.isoformat()}（{label}）"
             + (f" anchor_ended_at={meta.get('anchor_ended_at')}" if meta.get("anchor_ended_at") else "")]
    log = format_log_lines(meta.get("anchor_log"))
    lines.append("anchor_log：" + ("无" if not log else ""))
    lines.extend(log)
    return "\n".join(lines)

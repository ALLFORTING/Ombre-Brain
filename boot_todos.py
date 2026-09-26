"""Read-only active todo identities and budget-independent daily boot pages."""

from dataclasses import dataclass
from datetime import date, datetime, timezone
from math import gcd
from typing import Callable
from zoneinfo import ZoneInfo

from bucket_manager import active_todo_projection
from utils import count_tokens_approx


PROFILE_TODOS = {
    "talk": (3, 3, 120),
    "code": (2, 3, 100),
    "tg": (1, 2, 40),
}
TEXT_TRUNCATION = "…[正文已截]"


@dataclass(frozen=True)
class TodoItem:
    key: tuple[str, str, str]
    bucket_id: str
    todo_id: str | None
    text: str
    importance: int


@dataclass(frozen=True)
class TodoPage:
    candidates: tuple[TodoItem, ...]
    priority: tuple[TodoItem, ...]
    rotation: tuple[TodoItem, ...]
    start: int
    stride: int
    text_chars: int


@dataclass(frozen=True)
class TodoFit:
    entries: tuple[str, ...]
    shown: int
    hidden: int
    header: str
    text: str


def shanghai_date(now: datetime | None = None) -> date:
    """Do not substitute the host timezone if IANA data is unavailable."""
    instant = now if now is not None else datetime.now(timezone.utc)
    if instant.tzinfo is None:
        raise ValueError("Shanghai date requires an aware instant.")
    return instant.astimezone(ZoneInfo("Asia/Shanghai")).date()


def rotation_stride(size: int, target: int) -> int:
    if size <= 1:
        return 1
    return min(
        (step for step in range(1, size) if gcd(step, size) == 1),
        key=lambda step: (abs(step - target), step),
    )


def active_display_candidates(buckets: list[dict]) -> tuple[TodoItem, ...]:
    """Reuse the completion authority; collapse only legacy display duplicates."""
    items: dict[tuple[str, str, str], TodoItem] = {}
    bucket_states = {}
    for bucket in buckets:
        meta = bucket.get("metadata", {})
        if int(meta.get("sealed", 0) or 0) == 1 or meta.get("resolved", False):
            continue
        bucket_id = str(bucket["id"])
        state = (meta.get("todos"), meta.get("todo_provenance"), meta.get("importance", 0))
        if bucket_id in bucket_states and bucket_states[bucket_id] != state:
            raise ValueError("conflicting bucket identity in todo snapshot")
        bucket_states[bucket_id] = state
        _, records = active_todo_projection(meta.get("todos"), meta.get("todo_provenance"))
        importance = int(meta.get("importance", 0) or 0)
        for record in records:
            todo_id = record.get("id")
            text = record["text"]
            key = (bucket_id, "id", todo_id) if todo_id else (bucket_id, "legacy", text)
            item = TodoItem(key, bucket_id, todo_id, text, importance)
            if key in items and items[key] != item:
                raise ValueError("conflicting todo display identity")
            items[key] = item
    return tuple(sorted(items.values(), key=lambda item: (-item.importance, item.key)))


def todo_page(candidates: tuple[TodoItem, ...], profile: str, day: date) -> TodoPage:
    anchor_count, target, text_chars = PROFILE_TODOS[profile]
    priority = candidates[:anchor_count]
    tail = tuple(sorted(candidates[anchor_count:], key=lambda item: item.key))
    stride = rotation_stride(len(tail), target)
    start = day.toordinal() * stride % len(tail) if tail else 0
    return TodoPage(candidates, priority, tail[start:] + tail[:start], start, stride, text_chars)


def _row(item: TodoItem, text_chars: int) -> str:
    # Escape line breaks for display only; never use this text as an identity key.
    text = item.text.translate({13: "\\r", 10: "\\n"})
    if len(text) > text_chars:
        text = text[:text_chars - len(TEXT_TRUNCATION)] + TEXT_TRUNCATION
    legacy = " 旧格式；不能单条 todo_done" if item.todo_id is None else ""
    return (
        f"- [bucket_id:{item.bucket_id}] [todo_id:{item.todo_id or 'null'}] "
        f"importance:{item.importance} | {text}{legacy}"
    )


def _header(total: int, shown: int) -> str:
    explanation = (
        "其余未完成事项参与每日轮转；未显示 ≠ 已完成。"
        if shown < total else "本次已显示全部未完成事项。"
    )
    return (
        "=== boot: 未完结 todos ===\n"
        f"共 {total} 项未完成 | 本次显示 {shown} | 未显示 {total - shown}\n"
        + explanation
    )


def fit_todos(
    page: TodoPage,
    budget: int,
    *,
    measure: Callable[[str], int] = count_tokens_approx,
) -> TodoFit:
    """Return atomic rows and matching counts; a summary is never sliced.

    If even the summary exceeds budget, the global fitter must use its reserved
    notice space. ``measure`` can account for the entire final composition.
    """
    total = len(page.candidates)

    def result(selected: list[tuple[TodoItem, str]]) -> TodoFit:
        header = _header(total, len(selected))
        entries = tuple(row for _, row in selected)
        text = header + ("\n" + "\n".join(entries) if entries else "")
        return TodoFit(entries, len(entries), total - len(entries), header, text)

    full = result([(item, _row(item, page.text_chars)) for item in page.candidates])
    if measure(full.text) <= budget:
        return full
    selected: list[tuple[TodoItem, str]] = []
    minimum_chars = len(TEXT_TRUNCATION) + 1

    def add(item: TodoItem, *, compact: bool = False) -> bool:
        low = minimum_chars
        full_row = _row(item, page.text_chars)
        compact_row = _row(item, low)
        full_cost = measure(result(selected + [(item, full_row)]).text)
        compact_cost = measure(result(selected + [(item, compact_row)]).text)
        # Removing a truncation marker can make the complete body cheaper than
        # the shortest marked body, particularly for short English text.
        if full_cost <= budget and (not compact or full_cost <= compact_cost):
            selected.append((item, full_row))
            return True
        if compact_cost > budget:
            return False
        # Full text was checked separately. Keep the search inside the marked
        # prefix range, where its cost increases with the retained body.
        high = minimum_chars if compact else min(
            page.text_chars, len(item.text.translate({13: "\\r", 10: "\\n"})) - 1,
        )
        while low < high:
            middle = (low + high + 1) // 2
            if measure(result(selected + [(item, _row(item, middle))]).text) <= budget:
                low = middle
            else:
                high = middle - 1
        selected.append((item, _row(item, low)))
        return True

    # Reserve one real rotation row before priority can exhaust the budget.
    seed = None
    for index, item in enumerate(page.rotation):
        if add(item, compact=True):
            seed = index
            break
    rotation_seed = selected[0] if selected else None
    for item in page.priority:
        add(item)
    if rotation_seed is not None:
        selected.remove(rotation_seed)
        # Restore as much seed text as fits, without losing the priority rows.
        add(rotation_seed[0])
    if seed is not None:
        for item in page.rotation[seed + 1:]:
            add(item)
    # Items before seed could not fit even alone; no second scan is necessary.
    if len(selected) == total:
        by_key = {item.key: row for item, row in selected}
        selected = [(item, by_key[item.key]) for item in page.candidates]
    return result(selected)

# ============================================================
# Module: Boot formatting helpers (server_boot_format.py)
# 模块：boot 排版工具
#
# Session/note previews, truncation notices, TG summary display, profile
# filters, and the section budget fitter used by the boot tool.
# 会话与留言预览、截断提示、TG 压缩版显示、profile 过滤，以及 boot 的分段预算分配。
#
# Stateless: helpers that read bucket_mgr or the clock stay in server.py.
# This module must never import server; server.py re-exports every name
# defined here. The boot tool itself stays in server.py.
# 无状态：需要读 bucket_mgr 或当前时间的函数留在 server.py。
# 本模块禁止 import server；server.py 会重新导出这里定义的全部名字；boot 工具本身留在 server.py。
#
# Depended on by: server.py
# 被谁依赖：server.py
# ============================================================

from boot_todos import TodoPage, fit_todos
from datetime import datetime
from utils import count_tokens_approx, strip_wikilinks
from server_common import _is_sealed


BOOT_TRUNCATION_NOTICE_TOKENS = 160
BOOT_PROFILE_CODE_ROOTS = frozenset({"项目", "工程", "工具", "环境", "部署"})
BOOT_PROFILE_TG_MIN_IMPORTANCE = 8


def _extract_session_summary(content: str, max_chars: int | None = 700) -> str:
    """Extract the Summary section from an archived session bucket."""
    text = strip_wikilinks(content or "").strip()
    marker = "## Summary"
    if marker in text:
        text = text.split(marker, 1)[1].strip()
        if "\n## " in text:
            text = text.split("\n## ", 1)[0].strip()
    return text[:max_chars].strip() if max_chars is not None else text


def _format_note_preview(text: str, limit: int = 80) -> str:
    """Make a bounded one-line preview without changing the stored note body."""
    compact = " ".join((text or "").split())
    return compact[:limit] + ("…" if len(compact) > limit else "")


def _parse_note_open_at(value: str) -> str:
    """Normalize an optional local open_at timestamp for note visibility."""
    value = (value or "").strip()
    if not value:
        return ""
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("open_at must use ISO local date/time format.") from exc
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone().replace(tzinfo=None)
    return parsed.isoformat(timespec="seconds")


def _note_delivery_state(note: dict) -> str:
    if note.get("dismissed_at"):
        return "dismissed"
    if note.get("skipped_at"):
        return "skipped_for_delivery"
    if note.get("boot_delivered_at"):
        return "boot_delivered"
    return "pending"


def _format_bucket_truncation_notice(bucket_id: str, shown: int, total: int) -> str:
    return (
        f"[…已截断：bucket {bucket_id}，显示 {shown} / {total} 字符；"
        f"完整内容可用 dream(detail_ids=\"{bucket_id}\") 读取]"
    )


def _format_boot_preview(
    bucket: dict,
    max_chars: int,
    *,
    show_truncation: bool = False,
) -> str:
    """Return a display-safe bucket preview with an optional truncation marker."""
    content = strip_wikilinks(bucket.get("content", "")).strip()
    preview = content[:max_chars]
    if show_truncation and len(content) > len(preview):
        bucket_id = str(bucket.get("id", ""))
        return f"{preview}\n{_format_bucket_truncation_notice(bucket_id, len(preview), len(content))}"
    return preview


def _format_tg_summary_refresh_notice(bucket_id: str, state: str, source_hash: str) -> str:
    """Tell the caller how to refresh a missing or stale TG summary safely."""
    label = "尚未生成" if state == "missing" else "已过期"
    return (
        f"[…TG summary {label}：bucket {bucket_id}；当前原文 source_hash:{source_hash}；"
        f"请先用 dream(detail_ids=\"{bucket_id}\") 读取全文，再按 refresh_tg_summary "
        "的 generation contract 生成并保存压缩版]"
    )


def _format_tg_summary_preview(bucket: dict, source_hash: str, summary: str) -> str:
    """Render a valid caller-generated TG summary with a source-of-truth reminder."""
    bucket_id = str(bucket.get("id", ""))
    return (
        f"[TG 压缩版：bucket {bucket_id}；source_hash:{source_hash}]\n"
        f"{summary}\n"
        f"[这是压缩版；原 bucket 是唯一真实来源；需要细节时用 "
        f"dream(detail_ids=\"{bucket_id}\") 读取全文]"
    )


def _profile_metadata_labels(bucket: dict) -> set[str]:
    """Return normalized structured labels without inspecting bucket prose."""
    meta = bucket.get("metadata", {})
    labels = []
    for key in ("domain", "tags", "topics"):
        value = meta.get(key, [])
        if isinstance(value, str):
            value = [value]
        if isinstance(value, list):
            labels.extend(str(item).strip() for item in value if str(item).strip())
    return {label.split("/", 1)[0].casefold() for label in labels}


def _profile_is_code_context(bucket: dict) -> bool:
    return bool(_profile_metadata_labels(bucket) & BOOT_PROFILE_CODE_ROOTS)


def _profile_is_global_constraint(bucket: dict) -> bool:
    meta = bucket.get("metadata", {})
    return bool(
        meta.get("pinned")
        or meta.get("protected")
        or int(meta.get("importance", 0) or 0) >= 9
    )


def _profile_allows_bucket(bucket: dict, profile: str) -> bool:
    """Conservatively filter display-only profile sections from metadata."""
    if _is_sealed(bucket):
        return False
    if profile == "talk":
        return True
    if profile == "code":
        return _profile_is_global_constraint(bucket) or _profile_is_code_context(bucket)
    return _profile_is_global_constraint(bucket) or (
        int(bucket.get("metadata", {}).get("importance", 0) or 0)
        >= BOOT_PROFILE_TG_MIN_IMPORTANCE
    )


def _prefix_within_token_budget(text: str, token_budget: int) -> str:
    """Return the longest text prefix that fits the approximate token budget."""
    if token_budget <= 0:
        return ""
    if count_tokens_approx(text) <= token_budget:
        return text
    low, high = 0, len(text)
    while low < high:
        middle = (low + high + 1) // 2
        if count_tokens_approx(text[:middle]) <= token_budget:
            low = middle
        else:
            high = middle - 1
    return text[:low].rstrip()


def _fit_sections_to_budget(
    sections: list[tuple[str, str, str]],
    max_tokens: int,
    *,
    minimum_chars: dict[str, int] | None = None,
    atomic_sections: set[str] | None = None,
    omission_item_refs: dict[str, list[str]] | None = None,
    omission_item_ends: dict[str, list[tuple[str, int]]] | None = None,
    truncation_notice_tokens: int = BOOT_TRUNCATION_NOTICE_TOKENS,
    return_sections: bool = False,
    todo_display: TodoPage | None = None,
) -> str | tuple[str, dict[str, str]]:
    """Fit named sections in priority order and report every omitted block."""
    if not sections:
        return ("", {}) if return_sections else ""
    minimum_chars = minimum_chars or {}
    atomic_sections = atomic_sections or set()
    omission_item_refs = omission_item_refs or {}
    omission_item_ends = omission_item_ends or {}
    total_tokens = (
        count_tokens_approx("\n\n".join(text for _, _, text in sections))
        if todo_display is not None
        else sum(count_tokens_approx(text) for _, _, text in sections)
    )
    if total_tokens <= max_tokens:
        body = "\n\n".join(text for _, _, text in sections)
        return (body, {key: text for key, _, text in sections}) if return_sections else body

    # Per-section rounding and separators must fit too. The existing notice
    # reserve also protects a complete todo summary if no content slot survives.
    content_budget = max(0, max_tokens - truncation_notice_tokens - (
        len(sections) if todo_display is not None else 0
    ))
    requested_tokens = {}
    for key, _, text in sections:
        minimum = max(0, minimum_chars.get(key, 0))
        if key == "triggers":
            requested_tokens[key] = count_tokens_approx(text)
        elif minimum > 0:
            requested_tokens[key] = count_tokens_approx(text[:minimum])

    reserved_tokens = {}
    remaining_reserve = content_budget
    # If every guarantee fits, later sections keep their full reservation.
    # Otherwise the same pass assigns the available budget in output order.
    for key, _, _ in sections:
        requested = requested_tokens.get(key, 0)
        reserved = min(requested, remaining_reserve)
        if key in requested_tokens:
            reserved_tokens[key] = reserved
        remaining_reserve -= reserved
    output = []
    emitted_sections: dict[str, str] = {}
    complete = []
    partial = []
    omitted = []

    def _omission_detail(
        key: str,
        display_name: str,
        emitted_text: str,
        *,
        partial_output: bool,
    ) -> str:
        refs = omission_item_refs.get(key, [])
        if not refs:
            return display_name
        if key in omission_item_ends:
            ends = omission_item_ends[key]
            emitted_refs = [ref for ref, end in ends if end <= len(emitted_text)]
            omitted_refs = [ref for ref, end in ends if end > len(emitted_text)]
            complete_count = len(emitted_refs)
        else:
            emitted_refs = [ref for ref in refs if ref in emitted_text]
            omitted_refs = [ref for ref in refs if ref not in emitted_text]
            complete_count = len(emitted_refs)
            if partial_output and emitted_refs:
                last_emitted = emitted_refs[-1]
                if last_emitted not in omitted_refs:
                    omitted_refs.append(last_emitted)
                    complete_count -= 1
        omitted_label = "、".join(omitted_refs) or "正文尾部"
        continuation = ""
        if key == "mailbox":
            letter_ids = [
                ref.split(":", 1)[1]
                for ref in omitted_refs
                if ref.startswith("letter_id:")
            ]
            if letter_ids:
                continuation = (
                    "；完整内容请用 "
                    + "、".join(
                        f"get_letter(letter_id={letter_id})"
                        for letter_id in letter_ids
                    )
                    + " 读取"
                )
        return (
            f"{display_name}（原 {len(refs)} 项，完整输出 {complete_count} 项；"
            f"省略/截断：{omitted_label}{continuation}）"
        )

    used = 0
    priority_exhausted = False
    todo_index = None
    todo_summary_tokens = 0

    for index, (key, display_name, text) in enumerate(sections):
        later_reserve = sum(
            reserved_tokens.get(later_key, 0)
            for later_key, _, _ in sections[index + 1 :]
        )
        is_reserved = key in reserved_tokens
        if priority_exhausted and not is_reserved:
            omitted.append(
                _omission_detail(key, display_name, "", partial_output=False)
            )
            continue

        available = max(0, content_budget - used - later_reserve)
        if key == "todos" and todo_display is not None:
            fitted = fit_todos(todo_display, available)
            todo_index = len(output)
            output.append(fitted.text)
            emitted_sections[key] = fitted.text
            if count_tokens_approx(fitted.text) <= available:
                used += count_tokens_approx(fitted.text)
            else:
                todo_summary_tokens = count_tokens_approx(fitted.text) + 1
            if fitted.hidden:
                priority_exhausted = True
            continue
        section_tokens = count_tokens_approx(text)
        if section_tokens <= available:
            output.append(text)
            emitted_sections[key] = text
            complete.append(display_name)
            used += section_tokens
            continue

        if key in atomic_sections:
            omitted.append(
                _omission_detail(key, display_name, "", partial_output=False)
            )
            priority_exhausted = True
            continue

        prefix = _prefix_within_token_budget(text, available)
        if prefix:
            output.append(prefix)
            emitted_sections[key] = prefix
            partial.append(
                _omission_detail(key, display_name, prefix, partial_output=True)
            )
            used += count_tokens_approx(prefix)
        else:
            omitted.append(
                _omission_detail(key, display_name, "", partial_output=False)
            )
        priority_exhausted = True

    notice_lines = ["已按 boot 预算截断："]
    if partial:
        notice_lines.append("- 部分截断：" + "、".join(partial))
    if omitted:
        notice_lines.append("- 未输出：" + "、".join(omitted))
    notice = "\n".join(notice_lines)
    notice_budget = max(0, truncation_notice_tokens - todo_summary_tokens)
    if count_tokens_approx(notice) > notice_budget:
        if omission_item_refs and (todo_display is None or notice_budget >= 100):
            overflow = (
                "\n- 截断说明的 ID 清单仅列前缀；未完整输出的钉选项"
                "另见完整 TG summary recovery receipts。"
                if "pinned" in omission_item_ends else
                "\n- 省略 ID 清单超出本次 TG 预算；仅列出前缀，"
                "未列出的稳定 ID 无法在当前紧凑输出中完整列出。"
            )
            notice = (
                _prefix_within_token_budget(
                    notice,
                    max(0, notice_budget - count_tokens_approx(overflow)),
                )
                + overflow
            )
        else:
            notice = _prefix_within_token_budget(
                notice,
                notice_budget,
            )
    output.append(notice)
    if todo_index is not None:
        def _measure_final_todos(text: str) -> int:
            candidate_output = list(output)
            candidate_output[todo_index] = text
            return count_tokens_approx("\n\n".join(candidate_output))

        # Reclaim unused lower-section/notice budget without changing any other
        # emitted text. The page was captured before fitting and CAS retries.
        fitted = fit_todos(todo_display, max_tokens, measure=_measure_final_todos)
        output[todo_index] = fitted.text
        emitted_sections["todos"] = fitted.text
    body = "\n\n".join(output)
    return (body, emitted_sections) if return_sections else body


def _boot_delta_locator(bucket: dict) -> str:
    """Return a bounded stable locator without exposing bucket content."""
    meta = bucket.get("metadata", {})
    name = str(meta.get("name", bucket.get("id", ""))).strip()
    if len(name) > 80:
        name = name[:77].rstrip() + "..."
    return f"[bucket_id:{bucket['id']}] {name or bucket['id']}"

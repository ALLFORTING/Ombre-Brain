import hashlib
import importlib
import re
import sqlite3
import sys
from datetime import date
from pathlib import Path
from unittest.mock import AsyncMock

import frontmatter
import pytest


@pytest.fixture
def server(tmp_path, monkeypatch):
    monkeypatch.setenv("OMBRE_BUCKETS_DIR", str(tmp_path / "buckets"))
    monkeypatch.setenv("OMBRE_RESPONSE_SEAL", "r17-test-seal")
    monkeypatch.setenv("OMBRE_RM_RUNTIME_ENABLED", "0")
    monkeypatch.setenv("OMBRE_RM_HOOK_SKIP", "1")
    monkeypatch.delenv("OMBRE_API_KEY", raising=False)
    monkeypatch.delenv("OMBRE_EMBEDDING_API_KEY", raising=False)
    sys.modules.pop("server", None)
    module = importlib.import_module("server")
    module.decay_engine.ensure_started = AsyncMock(return_value=None)
    module.embedding_engine.enabled = False
    module.dehydrator.dehydrate = AsyncMock(
        side_effect=lambda content, metadata=None, **kwargs: content[:120]
    )
    return module


def receipts(response):
    matches = re.findall(
        r"^- ([0-9a-f]{12}) \[(missing|stale|fresh), source_hash:([0-9a-f]{64})\]$",
        response, re.MULTILINE,
    )
    assert len(matches) == len({item[0] for item in matches}), "Duplicate receipt in raw output"
    return {bucket_id: (state, source_hash) for bucket_id, state, source_hash in matches}


def stored_post(server, bucket_id):
    path = Path(server.bucket_mgr._find_bucket_file(bucket_id))
    return path, frontmatter.load(path)


def source_hash(post):
    return hashlib.sha256(str(post.content or "").encode("utf-8")).hexdigest()


@pytest.mark.asyncio
async def test_all_states_survive_zero_complete_pinned_items_and_refresh(server):
    expected = {}
    for state in ("missing", "stale", "fresh"):
        bucket_id = await server.bucket_mgr.create("原文" * 500, pinned=True, name=state)
        _, post = stored_post(server, bucket_id)
        if state != "missing":
            assert "已刷新" in await server.refresh_tg_summary(
                bucket_id, "有效摘要" * 200, source_hash(post),
            )
        if state == "stale":
            assert await server.bucket_mgr.update(bucket_id, content="变化" * 500)
        _, post = stored_post(server, bucket_id)
        expected[bucket_id] = (state, source_hash(post))

    result = await server.boot(profile="tg", max_tokens=1000)

    assert receipts(result) == expected
    assert "完整输出 0 项" in result
    assert server.count_tokens_approx(result) <= 1000
    for bucket_id, (_, current_hash) in receipts(result).items():
        assert "已刷新" in await server.refresh_tg_summary(bucket_id, "新的摘要", current_hash)


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [
    "婷易历史正文\n[[关系]]",
    "Unicode 😀 café e\u0301 中文",
    "第一行\n第二行\n\n第三段",
    "CRLF 第一行\r\n第二行\r\n[[链接|展示]]",
])
async def test_storage_bytes_define_boot_state_and_both_refresh_checks(server, body):
    bucket_id = await server.bucket_mgr.create("placeholder", pinned=True)
    path, post = stored_post(server, bucket_id)
    post.content = body  # Simulate untouched historical storage, bypassing write aliases.
    path.write_text(frontmatter.dumps(post), encoding="utf-8")
    post = frontmatter.load(path)
    original_hash = source_hash(post)
    display = (await server.bucket_mgr.get(bucket_id))["content"]
    if "婷易" in body:
        assert "婷易" not in display
        assert hashlib.sha256(display.encode("utf-8")).hexdigest() != original_hash

    missing = await server.boot(profile="tg")
    assert f"source_hash:{original_hash}" in missing
    assert "TG summary 尚未生成" in missing
    assert "已刷新" in await server.refresh_tg_summary(bucket_id, "历史正文摘要", original_hash)
    fresh = await server.boot(profile="tg")
    assert "历史正文摘要" in fresh
    assert "TG summary 已过期" not in fresh
    assert f"source_hash:{original_hash}" in fresh
    assert await server.bucket_mgr.get_tg_summary_metadata(bucket_id) == (
        "fresh", original_hash, "历史正文摘要",
    )
    assert frontmatter.load(path).content == post.content

    post = frontmatter.load(path)
    post.content += "\n新变化"
    path.write_text(frontmatter.dumps(post), encoding="utf-8")
    changed_hash = source_hash(frontmatter.load(path))
    stale = await server.boot(profile="tg")
    assert "TG summary 已过期" in stale
    assert f"source_hash:{changed_hash}" in stale
    rejected = await server.refresh_tg_summary(bucket_id, "不能保存", original_hash)
    assert "原文已变化" in rejected
    assert f"source_hash:{changed_hash}" in rejected
    assert frontmatter.load(path).get("tg_summary") == "历史正文摘要"
    assert "已刷新" in await server.refresh_tg_summary(bucket_id, "新摘要", changed_hash)


@pytest.mark.asyncio
async def test_receipt_hash_rejects_body_change_after_boot(server):
    bucket_id = await server.bucket_mgr.create("长" * 900, pinned=True)
    response = await server.boot(profile="tg", max_tokens=1000)
    old_hash = receipts(response)[bucket_id][1]
    assert await server.bucket_mgr.update(bucket_id, content="之后改变")
    assert "原文已变化" in await server.refresh_tg_summary(bucket_id, "不能保存", old_hash)
    _, post = stored_post(server, bucket_id)
    assert "tg_summary" not in post


def synthetic_pinned():
    header = "=== boot: 开机索引 ===\n"
    lines = []
    items = []
    end = len(header)
    for i in range(3):
        bucket_id = f"{i + 1:012x}"
        current_hash = hashlib.sha256(f"source{i}".encode()).hexdigest()
        # Another item's ID inside prose must never count as complete output.
        line = f"[bucket_id:{bucket_id}]\n" + "正文" * 120 + " bucket_id:000000000003"
        lines.append(line)
        end += len(line)
        items.append((bucket_id, "missing", current_hash, end))
        end += len("\n---\n")
    return header + "\n---\n".join(lines), items


def test_receipt_reservation_registers_new_omissions_until_stable(server, monkeypatch):
    text, items = synthetic_pinned()
    real_fit = server._fit_sections_to_budget
    histories = []

    def capture(*args, **kwargs):
        body, emitted = real_fit(*args, **kwargs)
        histories.append({bid for bid, _, _, end in items if end > len(emitted.get("pinned", ""))})
        return body, emitted

    monkeypatch.setattr(server, "_fit_sections_to_budget", capture)
    cascade_found = False
    partial_found = False
    for budget in range(800, 1400, 5):
        histories.clear()
        body, emitted = server._fit_tg_boot_sections(
            [("pinned", "钉选索引", text)], budget,
            recovery_items=items,
            omission_item_refs={"pinned": [f"bucket_id:{bid}" for bid, _, _, _ in items]},
            omission_item_ends={"pinned": [(f"bucket_id:{bid}", end) for bid, _, _, end in items]},
            truncation_notice_tokens=100,
        )
        expected = {bid for bid, _, _, end in items if end > len(emitted.get("pinned", ""))}
        assert set(receipts(body)) == expected
        assert server.count_tokens_approx(body) <= budget
        cascade_found |= any(a < b for a, b in zip(histories, histories[1:]))
        prefix = emitted.get("pinned", "")
        partial_found |= bool(prefix) and any(f"[bucket_id:{bid}]" in prefix for bid in expected)
    assert cascade_found, "Fixture must actually exercise a newly omitted item after reserving receipts"
    assert partial_found, "A half item must still have its own complete receipt"


def test_omission_count_uses_exact_item_end_at_boundary(server):
    text, items = synthetic_pinned()
    first_end = items[0][3]
    budget = server.count_tokens_approx(text[:first_end])
    # Give enough room for exactly one item, plus the independently reserved notice.
    body, emitted = server._fit_sections_to_budget(
        [("pinned", "钉选索引", text)], budget + 100,
        omission_item_refs={"pinned": [f"bucket_id:{bid}" for bid, _, _, _ in items]},
        omission_item_ends={"pinned": [(f"bucket_id:{bid}", end) for bid, _, _, end in items]},
        truncation_notice_tokens=100, return_sections=True,
    )
    assert len(emitted["pinned"]) >= first_end
    assert len(emitted["pinned"]) < items[1][3]
    assert "完整输出 1 项" in body


@pytest.mark.asyncio
@pytest.mark.parametrize("long_seal", [False, True])
async def test_receipts_over_4000_emit_every_hash_without_consuming_ordinary_sections(
    server, monkeypatch, long_seal,
):
    if long_seal:
        monkeypatch.setenv("OMBRE_RESPONSE_SEAL", "\u9a8c" * 3000)
    await server.boot(profile="tg")
    checkpoint_before = server.bucket_mgr.get_boot_delta_checkpoint(profile="tg")
    await server.leave_note("R17_PENDING_NOTE")
    await server.archive_session("carrier", letter="R17_PENDING_LETTER")
    expected = {}
    for i in range(200):
        bucket_id = await server.bucket_mgr.create(
            "超长原文" * 200, pinned=True, name=f"pinned {i}", todos=["R17_PENDING_TODO"],
        )
        _, post = stored_post(server, bucket_id)
        expected[bucket_id] = ("missing", source_hash(post))
    trigger_id = next(iter(expected))
    assert await server.bucket_mgr.update(trigger_id, trigger_date=date.today().isoformat())
    with sqlite3.connect(server.bucket_mgr.history_db_path) as conn:
        letters_before = conn.execute("SELECT * FROM letters").fetchall()
    expected[trigger_id] = ("missing", source_hash(stored_post(server, trigger_id)[1]))

    result = await server.boot(profile="tg")

    assert receipts(result) == expected
    receipt_lines = "\n".join(line for line in result.splitlines() if line.startswith("- "))
    assert server.count_tokens_approx(receipt_lines) > 4000
    assert server.count_tokens_approx(result) > 4000
    assert server.TG_RECOVERY_OVER_BUDGET in result
    assert result.startswith("boot profile: tg\n\n" + server.TG_RECOVERY_HEADER)
    assert result.endswith("seal: " + server._response_seal())
    assert result.count(server.TG_RECOVERY_OVER_BUDGET) == 1
    for marker in ("R17_PENDING_NOTE", "R17_PENDING_LETTER", "R17_PENDING_TODO", "=== boot: 增量摘要 ==="):
        assert marker not in result
    assert server.bucket_mgr.get_boot_delta_checkpoint(profile="tg") == checkpoint_before
    assert (await server.bucket_mgr.get(trigger_id))["metadata"]["trigger_last_seen"] == ""
    with sqlite3.connect(server.bucket_mgr.history_db_path) as conn:
        row = conn.execute("SELECT boot_delivered_at, skipped_at, read_at FROM notes").fetchone()
        assert row == (None, None, None)
        assert conn.execute("SELECT * FROM letters").fetchall() == letters_before
    assert (await server.bucket_mgr.get(trigger_id))["metadata"]["todos"] == ["R17_PENDING_TODO"]


@pytest.mark.asyncio
async def test_receipts_and_counts_use_only_existing_visible_pinned_set(server):
    visible = await server.bucket_mgr.create("可见原文" * 200, pinned=True)
    hidden = []
    hidden.append(await server.bucket_mgr.create("SEALED_MARKER", pinned=True, sealed=True))
    for tags in (["test", "audit"], ["audit", "test"], ["test"]):
        hidden.append(await server.bucket_mgr.create("TEST_MARKER", pinned=True, tags=tags))
    string_tag = await server.bucket_mgr.create("STRING_TEST_MARKER", pinned=True)
    path, post = stored_post(server, string_tag)
    post["tags"] = "audit, test"
    path.write_text(frontmatter.dumps(post), encoding="utf-8")
    hidden.append(string_tag)
    archived = await server.bucket_mgr.create("ARCHIVE_MARKER", pinned=True)
    await server.bucket_mgr.archive(archived)
    hidden.append(archived)
    hidden.append(await server.bucket_mgr.create("ORDINARY_MARKER", importance=3))

    response = await server.boot(profile="tg", max_tokens=1000)

    assert set(receipts(response)) == {visible}
    assert "钉选索引（原 1 项，完整输出 0 项" in response
    for bucket_id in hidden:
        assert bucket_id not in response
    for marker in ("SEALED_MARKER", "TEST_MARKER", "STRING_TEST_MARKER", "ARCHIVE_MARKER", "ORDINARY_MARKER"):
        assert marker not in response


@pytest.mark.asyncio
async def test_sufficient_budget_preserves_existing_sections_and_display(server):
    await server.leave_note("visible note")
    await server.archive_session("carrier", letter="visible letter")
    bucket_id = await server.bucket_mgr.create("[[可见]]正文", pinned=True, todos=["visible todo"])
    response = await server.boot(profile="tg")
    assert not receipts(response)
    assert server.TG_RECOVERY_HEADER not in response
    assert "已按 boot 预算截断" not in response
    assert f"[bucket_id:{bucket_id}]" in response
    assert "可见正文" in response
    assert "[[可见]]" not in response
    headings = ["婷留言", "增量摘要", "今日浮现", "最新信箱", "未完结 todos", "开机索引"]
    positions = [response.index(f"=== boot: {heading} ===") for heading in headings]
    assert positions == sorted(positions)
    assert server.count_tokens_approx(response) <= 4000


def delivery_snapshot(server):
    with sqlite3.connect(server.bucket_mgr.history_db_path) as conn:
        notes = conn.execute("SELECT boot_delivered_at, skipped_at, read_at FROM notes").fetchall()
        letters = conn.execute("SELECT * FROM letters").fetchall()
    return server.bucket_mgr.get_boot_delta_checkpoint(profile="tg"), notes, letters


@pytest.mark.asyncio
async def test_long_seal_without_omissions_preserves_body_and_real_delivery(server, monkeypatch):
    await server.boot(profile="tg")
    await server.leave_note("R17_FULL_NOTE")
    await server.archive_session("carrier", letter="R17_FULL_LETTER")
    bucket_id = await server.bucket_mgr.create(
        "R17_FULL_TRIGGER", pinned=True, todos=["R17_FULL_TODO"],
    )
    assert await server.bucket_mgr.update(bucket_id, trigger_date=date.today().isoformat())
    _, _, letters_before = delivery_snapshot(server)
    high_water = server.bucket_mgr.get_boot_delta_high_water()
    monkeypatch.setenv("OMBRE_RESPONSE_SEAL", "\u9a8c" * 3000)
    real_fit = server._fit_sections_to_budget
    fitted_sections = []

    def capture(sections, budget, **kwargs):
        # This is the unchanged baseline ordinary-content allowance, regardless
        # of the runtime seal length. With no omissions the sections pass whole.
        assert budget == 4000 - 40
        fitted_sections.append(sections)
        return real_fit(sections, budget, **kwargs)

    monkeypatch.setattr(server, "_fit_sections_to_budget", capture)
    result = await server.boot(profile="tg")

    assert len(fitted_sections) == 1
    expected_body = "\n\n".join(text for _, _, text in fitted_sections[0])
    assert result == server._with_response_seal("boot profile: tg\n\n" + expected_body)
    assert not receipts(result)
    assert server.TG_RECOVERY_HEADER not in result
    assert server.TG_RECOVERY_OVER_BUDGET not in result
    assert server.count_tokens_approx(expected_body) <= 4000
    assert server.count_tokens_approx(result) > 4000
    for marker in ("R17_FULL_NOTE", "R17_FULL_LETTER", "R17_FULL_TRIGGER", "R17_FULL_TODO"):
        assert marker in result
    checkpoint, notes, letters = delivery_snapshot(server)
    assert checkpoint["last_event_id"] == high_water
    assert notes[0][0] is not None and notes[0][1:] == (None, None)
    assert letters == letters_before
    bucket = await server.bucket_mgr.get(bucket_id)
    assert bucket["metadata"]["trigger_last_seen"] == date.today().isoformat()
    assert bucket["metadata"]["todos"] == ["R17_FULL_TODO"]


@pytest.mark.asyncio
async def test_seal_only_overflow_with_receipts_has_no_recovery_notice_or_false_delivery(server, monkeypatch):
    await server.boot(profile="tg")
    monkeypatch.setenv("OMBRE_RESPONSE_SEAL", "\u9a8c" * 3000)
    # The full note is eligible but cannot fit after receipt/notice reservation.
    # Reserved later sections may still fit and must consume only if delivered.
    await server.leave_note("R17_PENDING_NOTE" + "\u957f" * 400)
    await server.archive_session("carrier", letter="R17_PENDING_LETTER" * 1000)
    expected = {}
    for i in range(5):
        bucket_id = await server.bucket_mgr.create(
            "\u6b63\u6587" * 400, pinned=True, todos=["R17_PENDING_TODO"],
        )
        expected[bucket_id] = ("missing", source_hash(stored_post(server, bucket_id)[1]))
    trigger_id = next(iter(expected))
    await server.bucket_mgr.update(trigger_id, trigger_date=date.today().isoformat())
    before = delivery_snapshot(server)
    result = await server.boot(profile="tg", max_tokens=1000)

    assert receipts(result) == expected
    assert result.endswith("seal: " + "\u9a8c" * 3000)
    body = result.removeprefix("boot profile: tg\n\n").rsplit("\n\nseal: ", 1)[0]
    assert server.count_tokens_approx(body) <= 1000
    assert server.count_tokens_approx(result) > 1000
    assert server.TG_RECOVERY_OVER_BUDGET not in result
    checkpoint, notes, letters = delivery_snapshot(server)
    assert (notes, letters) == before[1:]
    if "=== boot: \u589e\u91cf\u6458\u8981 ===" in result:
        assert checkpoint["last_event_id"] > before[0]["last_event_id"]
    else:
        assert checkpoint == before[0]
    bucket = await server.bucket_mgr.get(trigger_id)
    assert bucket["metadata"]["trigger_last_seen"] == ""
    assert bucket["metadata"]["todos"] == ["R17_PENDING_TODO"]
    for marker in ("R17_PENDING_NOTE", "R17_PENDING_LETTER"):
        assert marker not in result


@pytest.mark.asyncio
async def test_storage_final_check_rejects_change_after_refresh_precheck(server, monkeypatch):
    bucket_id = await server.bucket_mgr.create("original", pinned=True)
    current_hash = source_hash(stored_post(server, bucket_id)[1])
    real_refresh = server.bucket_mgr.refresh_tg_summary
    changed_hash = None

    async def change_before_write(*args):
        nonlocal changed_hash
        path, post = stored_post(server, bucket_id)
        post.content = "changed after precheck [[link]]"
        path.write_text(frontmatter.dumps(post), encoding="utf-8")
        changed_hash = source_hash(frontmatter.load(path))
        return await real_refresh(*args)

    monkeypatch.setattr(server.bucket_mgr, "refresh_tg_summary", change_before_write)
    result = await server.refresh_tg_summary(bucket_id, "must not save", current_hash)
    assert "原文已变化" in result
    assert f"source_hash:{changed_hash}" in result
    assert "tg_summary" not in stored_post(server, bucket_id)[1]


def test_duplicate_recovery_ids_use_first_entry_and_identical_reservation(server):
    text, items = synthetic_pinned()
    bid, state, current_hash, end = items[0]
    duplicate_items = [items[0], (bid, "stale", "f" * 64, len(text)), *items[1:], items[0]]
    unique_body, unique_emitted = server._fit_tg_boot_sections(
        [("pinned", "pinned", text)], 200, recovery_items=items,
        truncation_notice_tokens=100,
    )
    body, emitted = server._fit_tg_boot_sections(
        [("pinned", "pinned", text)], 200, recovery_items=duplicate_items,
        omission_item_refs={"pinned": [f"bucket_id:{item[0]}" for item in duplicate_items]},
        omission_item_ends={"pinned": [(f"bucket_id:{item[0]}", item[3]) for item in duplicate_items]},
        truncation_notice_tokens=100,
    )
    assert (body, emitted) == (unique_body, unique_emitted)
    # Count raw lines before any dictionary conversion can hide duplicates.
    raw_ids = re.findall(r"^- ([0-9a-f]{12}) \[", body, re.MULTILINE)
    assert raw_ids == [item[0] for item in items]
    assert raw_ids.count(bid) == 1
    assert f"- {bid} [{state}, source_hash:{current_hash}]" in body
    assert "source_hash:" + "f" * 64 not in body


@pytest.mark.asyncio
@pytest.mark.parametrize("budget", [1000, 4000])
async def test_boot_deduplicates_visible_pins_before_metadata_and_counts(server, monkeypatch, budget):
    bucket_id = await server.bucket_mgr.create("\u539f\u6587" * 400, pinned=True, name="FIRST_VISIBLE")
    real_list = server.bucket_mgr.list_all
    real_metadata = server.bucket_mgr.get_tg_summary_metadata

    async def duplicate_list(*args, **kwargs):
        buckets = await real_list(*args, **kwargs)
        duplicate = dict(buckets[0])
        duplicate["metadata"] = {**duplicate["metadata"], "name": "LATER_DUPLICATE"}
        return [*buckets, duplicate]

    metadata_reads = AsyncMock(wraps=real_metadata)
    monkeypatch.setattr(server.bucket_mgr, "list_all", duplicate_list)
    monkeypatch.setattr(server.bucket_mgr, "get_tg_summary_metadata", metadata_reads)
    result = await server.boot(profile="tg", max_tokens=budget)

    metadata_reads.assert_awaited_once_with(bucket_id)
    assert "LATER_DUPLICATE" not in result
    if budget == 1000:
        assert re.findall(r"^- " + bucket_id + r" \[", result, re.MULTILINE) == ["- " + bucket_id + " ["]
        assert "\u539f 1 \u9879" in result
    else:
        assert result.count("[bucket_id:" + bucket_id + "] FIRST_VISIBLE") == 1
        assert not receipts(result)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["archive", "test", "outside_tg"])
async def test_refresh_retains_baseline_query_scope_and_raw_mismatch_hash(server, kind):
    bucket_id = await server.bucket_mgr.create("placeholder", importance=3, tags=["test"] if kind == "test" else [])
    path, post = stored_post(server, bucket_id)
    post.content = "\u5a77\u6613 historic source\n[[link]]"
    path.write_text(frontmatter.dumps(post), encoding="utf-8")
    if kind == "archive":
        assert await server.bucket_mgr.archive(bucket_id)
    current_hash = source_hash(stored_post(server, bucket_id)[1])
    result = await server.refresh_tg_summary(bucket_id, "summary", "0" * 64)
    assert "source_hash:" + current_hash in result
    assert "tg_summary" not in stored_post(server, bucket_id)[1]
    assert "\u5df2\u5237\u65b0" in await server.refresh_tg_summary(bucket_id, "summary", current_hash)


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["removed", "sealed", "read_failure"])
async def test_refresh_metadata_unavailable_uses_existing_baseline_outcomes(server, monkeypatch, change):
    bucket_id = await server.bucket_mgr.create("original", pinned=True)
    current_hash = source_hash(stored_post(server, bucket_id)[1])

    async def unavailable(bucket_id):
        if change == "removed":
            stored_post(server, bucket_id)[0].unlink()
        elif change == "sealed":
            assert await server.bucket_mgr.update(bucket_id, sealed=1)
        return None

    writer = AsyncMock(side_effect=AssertionError("Unavailable metadata must not reach a summary write"))
    monkeypatch.setattr(server.bucket_mgr, "get_tg_summary_metadata", unavailable)
    monkeypatch.setattr(server.bucket_mgr, "refresh_tg_summary", writer)
    result = await server.refresh_tg_summary(bucket_id, "summary", current_hash)
    expected = {
        "removed": f"\u672a\u627e\u5230\u8bb0\u5fc6\u6876: {bucket_id}",
        "sealed": f"\u8bb0\u5fc6\u6876\u5df2\u5c01\u5b58\uff0c\u4e0d\u80fd\u5237\u65b0 TG summary: {bucket_id}",
        "read_failure": f"TG summary \u4fdd\u5b58\u5931\u8d25: {bucket_id}",
    }
    assert result == expected[change]
    assert "source_hash:" not in result
    writer.assert_not_awaited()

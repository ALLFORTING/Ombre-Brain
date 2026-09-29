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
    return {
        bucket_id: (state, source_hash)
        for bucket_id, state, source_hash in re.findall(
            r"^- ([0-9a-f]{12}) \[(missing|stale|fresh), source_hash:([0-9a-f]{64})\]$",
            response, re.MULTILINE,
        )
    }


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
        assert server.count_tokens_approx(server._with_response_seal(f"boot profile: tg\n\n{body}")) <= budget
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
async def test_receipts_over_4000_emit_every_hash_without_consuming_ordinary_sections(server):
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


@pytest.mark.asyncio
async def test_envelope_and_long_seal_are_counted_and_cannot_clip_receipts(server, monkeypatch):
    monkeypatch.setenv("OMBRE_RESPONSE_SEAL", "验" * 200)
    expected = {}
    await server.archive_session("carrier", letter="挤占" * 4000)
    for i in range(5):
        bucket_id = await server.bucket_mgr.create("正文" * 400, pinned=True)
        expected[bucket_id] = ("missing", source_hash(stored_post(server, bucket_id)[1]))
    result = await server.boot(profile="tg", max_tokens=1000)
    assert receipts(result) == expected
    assert server.count_tokens_approx(result) <= 1000
    assert result.endswith("seal: " + "验" * 200)
    assert server.TG_RECOVERY_OVER_BUDGET not in result


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

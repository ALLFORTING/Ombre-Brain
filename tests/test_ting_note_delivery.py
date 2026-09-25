import importlib
import sqlite3
import sys
from datetime import datetime, timedelta
from unittest.mock import AsyncMock

import pytest


def _load_server(tmp_path, monkeypatch, seal="test-note-seal"):
    monkeypatch.setenv("OMBRE_BUCKETS_DIR", str(tmp_path / "buckets"))
    monkeypatch.setenv("OMBRE_RESPONSE_SEAL", seal)
    monkeypatch.delenv("OMBRE_API_KEY", raising=False)
    sys.modules.pop("server", None)
    server = importlib.import_module("server")
    server.decay_engine.ensure_started = AsyncMock(return_value=None)
    server.dehydrator.dehydrate = AsyncMock(
        side_effect=lambda content, metadata=None: content[:120]
    )
    return server


def _note_rows(server):
    with sqlite3.connect(server.bucket_mgr.history_db_path) as conn:
        conn.row_factory = sqlite3.Row
        return [
            dict(row)
            for row in conn.execute(
                """
                SELECT note_id, text, sealed, open_at, boot_delivered_at, read_at,
                       skipped_at, skipped_reason, dismissed_at
                FROM notes ORDER BY note_id
                """
            )
        ]


@pytest.mark.asyncio
async def test_boot_always_reports_empty_ting_note_status(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)

    result = await server.boot()

    assert "=== boot: 婷留言 ===\n婷留言：无（暂无历史留言）" in result
    assert result.index("=== boot: 婷留言 ===") < result.index("=== boot: 今日浮现 ===")
    assert _note_rows(server) == []


@pytest.mark.asyncio
async def test_optional_notes_use_real_autoincrement_ids_and_exact_mcp_text(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)

    assert "未创建留言" in await server.leave_note("   ")
    assert "未创建留言" in await server.leave_note("bad", open_at="not-a-date")
    first_text = "逐字保留：不要总结，也不要改写。\n第二行仍在。"
    created = await server.leave_note(first_text)

    assert "note_id:1" in created
    assert first_text[:20] in created
    rows = _note_rows(server)
    assert len(rows) == 1
    assert rows[0]["note_id"] == 1
    assert rows[0]["text"] == first_text
    assert rows[0]["boot_delivered_at"] is None


@pytest.mark.asyncio
async def test_boot_delivers_one_note_once_then_reports_last_actual_note(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    body = "第一封完整留言"
    await server.leave_note(body)

    first = await server.boot()
    second = await server.boot()

    assert body in first
    assert "[note_id:1]" in first
    assert body not in second
    assert "婷留言：无（上次留言 #1，" in second
    rows = _note_rows(server)
    assert rows[0]["boot_delivered_at"] is not None
    assert rows[0]["skipped_at"] is None


@pytest.mark.asyncio
async def test_newest_eligible_note_delivers_and_older_pending_note_is_not_backlogged(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    await server.leave_note("older #1 body")
    await server.leave_note("newer #2 body")

    first = await server.boot()
    second = await server.boot()
    history = await server.list_notes()
    old = await server.get_note(1)

    assert "newer #2 body" in first
    assert "older #1 body" not in first
    assert "older #1 body" not in second
    assert "婷留言：无（上次留言 #2，" in second
    assert "delivery:skipped_for_delivery" in history
    assert "older #1 body" in old
    rows = _note_rows(server)
    assert rows[0]["skipped_at"] is not None
    assert rows[0]["skipped_reason"] == "superseded_by_newer_eligible_note"
    assert rows[1]["boot_delivered_at"] is not None


@pytest.mark.asyncio
async def test_later_note_delivers_without_repeating_prior_delivery(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    await server.leave_note("one")
    assert "one" in await server.boot()
    await server.leave_note("two")

    third_window = await server.boot()
    fourth_window = await server.boot()

    assert "two" in third_window
    assert "one" not in third_window
    assert "two" not in fourth_window
    assert "婷留言：无（上次留言 #2，" in fourth_window


@pytest.mark.asyncio
async def test_sealed_and_future_notes_preserve_existence_hiding_and_future_delivery(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    future = datetime.now() + timedelta(hours=2)
    await server.leave_note("future hidden body", open_at=future.isoformat(timespec="seconds"))
    await server.leave_note("ordinary current body")
    await server.leave_note("sealed hidden body", sealed=True)

    current = await server.boot()
    default_history = await server.list_notes()
    hidden_future = await server.get_note(1)
    hidden_sealed = await server.get_note(3)
    included_sealed = await server.get_note(3, include_sealed=True)

    assert "ordinary current body" in current
    assert "future hidden body" not in current
    assert "sealed hidden body" not in current
    assert "note_id:2" in default_history
    assert "note_id:1" not in default_history
    assert "note_id:3" not in default_history
    assert "note_id not found: 1" in hidden_future
    assert "note_id not found: 3" in hidden_sealed
    assert "sealed hidden body" in included_sealed

    frozen = future + timedelta(seconds=1)

    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return frozen if tz is None else frozen.astimezone(tz)

    monkeypatch.setattr(server, "datetime", FrozenDateTime)
    due = await server.boot()
    rows = _note_rows(server)

    assert "future hidden body" in due
    assert rows[0]["boot_delivered_at"] is not None
    assert rows[0]["skipped_at"] is None
    assert rows[1]["boot_delivered_at"] is not None


@pytest.mark.asyncio
async def test_budget_fallback_never_truncates_or_consumes_note_before_full_get(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    large_body = "长" * 12000
    await server.leave_note(large_body)

    first = await server.boot()
    second = await server.boot()
    rows_before = _note_rows(server)
    full = await server.get_note(1)
    after_read = await server.boot()

    prompt = "婷有新留言 #1，全文请用 get_note(note_id=1) 读取"
    assert prompt in first
    assert prompt in second
    assert large_body not in first
    assert rows_before[0]["boot_delivered_at"] is None
    assert large_body in full
    assert "婷留言：无（上次留言 #1，" in after_read
    rows_after = _note_rows(server)
    assert rows_after[0]["read_at"] is not None
    assert rows_after[0]["boot_delivered_at"] is not None


@pytest.mark.asyncio
async def test_global_boot_budget_keeps_ting_note_atomic_and_pending(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    body = "长" * 525
    await server.leave_note(body)
    await server.bucket_mgr.create("钉" * 2000, name="budget pressure", pinned=True)
    candidate, candidate_id = server._format_ting_note_for_boot(
        datetime.now().isoformat(timespec="seconds"), 1000
    )
    assert candidate_id == 1 and body in candidate

    result = await server.boot(max_tokens=1000)
    row = _note_rows(server)[0]

    assert body not in result
    assert "部分截断：婷留言" not in result
    assert row["boot_delivered_at"] is None
    assert row["skipped_at"] is None


@pytest.mark.asyncio
async def test_failed_final_boot_composition_does_not_deliver_note(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    await server.leave_note("must survive failed boot")

    def fail_fit(*args, **kwargs):
        raise RuntimeError("final composition failed")

    monkeypatch.setattr(server, "_fit_sections_to_budget", fail_fit)
    with pytest.raises(RuntimeError, match="final composition failed"):
        await server.boot()
    assert _note_rows(server)[0]["boot_delivered_at"] is None


@pytest.mark.asyncio
async def test_dismiss_note_requires_exact_one_shot_confirmation(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    await server.leave_note("retain this exact note")

    preview = await server.dismiss_note(1)
    token = preview.split("confirm_token=", 1)[1].split()[0].rstrip("；")
    assert "待确认 dismiss_note" in preview
    assert _note_rows(server)[0]["dismissed_at"] is None
    assert "确认无效" in await server.dismiss_note(1, confirm_token="wrong")

    confirmed = await server.dismiss_note(1, confirm_token=token)
    row = _note_rows(server)[0]
    assert "已 dismiss" in confirmed
    assert row["text"] == "retain this exact note"
    assert row["dismissed_at"] is not None
    assert row["boot_delivered_at"] is None
    assert row["read_at"] is None
    assert "已 dismiss" in await server.dismiss_note(1, confirm_token=token)
    assert "retain this exact note" not in await server.boot()
    assert "delivery:dismissed" in await server.list_notes()


@pytest.mark.asyncio
async def test_dismiss_note_rejects_changed_plan_and_hidden_note(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    await server.leave_note("visible")
    await server.leave_note("sealed", sealed=True)
    await server.leave_note("future", open_at="2099-01-01T00:00:00")

    assert "not found" in await server.dismiss_note(2)
    assert "not found" in await server.dismiss_note(3, include_sealed=True)
    preview = await server.dismiss_note(1)
    token = preview.split("confirm_token=", 1)[1].split()[0].rstrip("；")
    await server.get_note(1)
    assert "确认无效" in await server.dismiss_note(1, confirm_token=token)
    assert _note_rows(server)[0]["dismissed_at"] is None


@pytest.mark.asyncio
async def test_notes_do_not_create_or_enter_memory_buckets(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    await server.leave_note("not a memory retrieval corpus")

    before = await server.bucket_mgr.list_all(include_archive=True)
    breath = await server.breath(query="not a memory retrieval corpus")
    digest = await server.digest(dry_run=True)
    after = await server.bucket_mgr.list_all(include_archive=True)

    assert before == []
    assert after == []
    assert "not a memory retrieval corpus" not in breath
    assert "not a memory retrieval corpus" not in digest
    assert server.dehydrator.dehydrate.await_count == 0


def test_notes_schema_is_separate_from_letters_and_has_delivery_fields(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)

    with sqlite3.connect(server.bucket_mgr.history_db_path) as conn:
        note_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(notes)").fetchall()
        }
        letter_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(letters)").fetchall()
        }

    assert {
        "note_id", "created_at", "author", "via", "text", "sealed", "open_at",
        "boot_delivered_at", "read_at", "skipped_at", "skipped_reason", "dismissed_at",
    } <= note_columns
    assert "note_id" not in letter_columns

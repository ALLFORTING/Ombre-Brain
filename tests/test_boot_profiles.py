import importlib
import sqlite3
import sys
from unittest.mock import AsyncMock

import pytest


@pytest.mark.asyncio
@pytest.mark.parametrize("profile", ["talk", "code", "tg"])
async def test_lifecycle_low_budget_keeps_expiry_pending_and_repeating_items_unconsumed(tmp_path, monkeypatch, profile):
    from tests.test_trigger_memory import _freeze_day, _raw_trigger
    server = _load_server(tmp_path, monkeypatch)
    _freeze_day(server, monkeypatch, "2026-10-18")
    due_ids = []
    for index in range(10):
        bucket_id = await server.bucket_mgr.create("触" * 280 + f"END_REPEAT_{index}", name=f"due-{index}")
        await server.trace(bucket_id, trigger_date="2026-10-18", trigger_repeat="daily")
        due_ids.append(bucket_id)
    expired_ids = []
    for index in range(5):
        bucket_id = await server.bucket_mgr.create("expired body", name=f"expired-{index}")
        await server.trace(bucket_id, trigger_date="2026-10-01")
        expired_ids.append(bucket_id)
    result = await server.boot(profile=profile, max_tokens=1000)
    assert server.count_tokens_approx(result) <= 1000
    assert not all(_raw_trigger(server, bucket_id).get("trigger_expiry_notified_at") for bucket_id in expired_ids)
    consumed = 0
    for index, bucket_id in enumerate(due_ids):
        complete = f"END_REPEAT_{index}" in result
        meta = _raw_trigger(server, bucket_id).metadata
        assert (meta["trigger_date"] == "2026-10-19") is complete
        consumed += complete
    assert consumed < len(due_ids)
    followup = await server.boot(profile=profile, max_tokens=16000)
    # TG's bounded selection may leave extra previews for subsequent boots;
    # collect them until the pending expiry line can be delivered in full.
    for _ in range(4):
        if "已自动收起 5 条过期提醒：" in followup and "另有 2 条" in followup:
            break
        followup = await server.boot(profile=profile, max_tokens=16000)
    assert "已自动收起 5 条过期提醒：" in followup and "另有 2 条" in followup
    assert all(_raw_trigger(server, bucket_id)["trigger_expiry_notified_at"] == "2026-10-18" for bucket_id in expired_ids)
    assert "已自动收起" not in await server.boot(profile=profile)


@pytest.mark.asyncio
@pytest.mark.parametrize("partial", [False, True])
async def test_expiry_receipt_requires_complete_final_line(tmp_path, monkeypatch, partial):
    from tests.test_trigger_memory import _freeze_day, _raw_trigger
    server = _load_server(tmp_path, monkeypatch)
    _freeze_day(server, monkeypatch, "2026-10-18")
    bucket_id = await server.bucket_mgr.create("old expiry", name="receipt-tail-marker")
    await server.trace(bucket_id, trigger_date="2026-10-01")
    original = server._fit_sections_to_budget
    def truncate_receipt(*args, **kwargs):
        body, emitted = original(*args, **kwargs)
        text = emitted["triggers"]
        end = text.index("已自动收起")
        clipped = text[:end + (12 if partial else 0)]
        emitted["triggers"] = clipped
        return body.replace(text, clipped), emitted
    monkeypatch.setattr(server, "_fit_sections_to_budget", truncate_receipt)
    result = await server.boot(max_tokens=1000)
    assert "receipt-tail-marker" not in result
    meta = _raw_trigger(server, bucket_id).metadata
    assert meta["trigger_expired_at"] == "2026-10-18"
    assert not meta.get("trigger_expiry_notified_at")
    monkeypatch.setattr(server, "_fit_sections_to_budget", original)
    assert "receipt-tail-marker" in await server.boot()
    assert _raw_trigger(server, bucket_id)["trigger_expiry_notified_at"] == "2026-10-18"


@pytest.mark.asyncio
async def test_concurrent_boots_deliver_and_advance_one_occurrence_once(tmp_path, monkeypatch):
    import asyncio
    from tests.test_trigger_memory import _freeze_day, _raw_trigger
    server = _load_server(tmp_path, monkeypatch)
    _freeze_day(server, monkeypatch, "2026-10-18")
    bucket_id = await server.bucket_mgr.create("only one occurrence", name="concurrent-reminder")
    await server.trace(bucket_id, trigger_date="2026-10-18", trigger_repeat="daily")
    ready, both_ready = 0, asyncio.Event()
    async def barrier(buckets):
        nonlocal ready
        ready += 1
        if ready == 2:
            both_ready.set()
        await asyncio.wait_for(both_ready.wait(), timeout=5)
        return {}
    monkeypatch.setattr(server, "_trigger_stale_hints", barrier)
    results = await asyncio.gather(server.boot(profile="talk"), server.boot(profile="code"))
    assert sum("concurrent-reminder" in result for result in results) == 1
    meta = _raw_trigger(server, bucket_id).metadata
    assert meta["trigger_date"] == "2026-10-19"
    assert meta["trigger_anchor"] == "2026-10-18"


@pytest.mark.asyncio
async def test_concurrent_trace_rebuilds_final_trigger_output(tmp_path, monkeypatch):
    from tests.test_trigger_memory import _freeze_day, _raw_trigger
    server = _load_server(tmp_path, monkeypatch)
    _freeze_day(server, monkeypatch, "2026-10-18")
    bucket_id = await server.bucket_mgr.create("trace wins", name="stale-preview-must-disappear")
    await server.trace(bucket_id, trigger_date="2026-10-18", trigger_repeat="daily")
    async def reschedule(buckets):
        await server.trace(bucket_id, trigger_date="2026-11-10", trigger_repeat="monthly")
        return {}
    monkeypatch.setattr(server, "_trigger_stale_hints", reschedule)
    result = await server.boot()
    assert "stale-preview-must-disappear" not in result
    meta = _raw_trigger(server, bucket_id).metadata
    assert (meta["trigger_date"], meta["trigger_repeat"], meta["trigger_anchor"]) == (
        "2026-11-10", "monthly", "2026-11-10")
    assert not meta.get("trigger_last_seen")


@pytest.mark.asyncio
async def test_final_delta_cas_recomposition_rechecks_expiry_line_delivery(tmp_path, monkeypatch):
    from tests.test_trigger_memory import _freeze_day, _raw_trigger
    server = _load_server(tmp_path, monkeypatch)
    _freeze_day(server, monkeypatch, "2026-10-18")
    await server.boot()
    bucket_id = await server.bucket_mgr.create("CAS expiry body", name="cas-expiry-marker")
    await server.trace(bucket_id, trigger_date="2026-10-01")
    original = server.bucket_mgr.advance_boot_delta_checkpoint
    calls = []
    def conflict(expected, next_event_id, profile="talk"):
        calls.append(next_event_id)
        if len(calls) == 1:
            original(expected, server.bucket_mgr.get_boot_delta_high_water(), profile=profile)
            return False
        return original(expected, next_event_id, profile=profile)
    monkeypatch.setattr(server.bucket_mgr, "advance_boot_delta_checkpoint", conflict)
    result = await server.boot(max_tokens=1000)
    assert calls
    assert "（无新增变化）" in result
    assert "cas-expiry-marker" in result
    assert _raw_trigger(server, bucket_id)["trigger_expiry_notified_at"] == "2026-10-18"


def _load_server(tmp_path, monkeypatch):
    monkeypatch.setenv("OMBRE_BUCKETS_DIR", str(tmp_path / "buckets"))
    monkeypatch.setenv("OMBRE_RESPONSE_SEAL", "profile-seal")
    monkeypatch.delenv("OMBRE_API_KEY", raising=False)
    monkeypatch.delenv("OMBRE_TRIGGER_STALE_THRESHOLD", raising=False)
    monkeypatch.delenv("OMBRE_TRIGGER_STALE_LEAD", raising=False)
    sys.modules.pop("server", None)
    server = importlib.import_module("server")
    server.decay_engine.ensure_started = AsyncMock(return_value=None)
    server._recall_memory_candidates = AsyncMock(
        return_value={"candidates": [], "semantic": {"status": "disabled"}}
    )
    server.dehydrator.dehydrate = AsyncMock(
        side_effect=lambda content, metadata=None: content[:120]
    )
    return server


@pytest.mark.asyncio
async def test_default_boot_is_talk_and_all_profiles_identify_themselves(
    tmp_path, monkeypatch
):
    server = _load_server(tmp_path, monkeypatch)

    default = await server.boot()
    code = await server.boot(profile="code")
    tg = await server.boot(profile="tg")

    assert default.startswith("boot profile: talk\n")
    assert code.startswith("boot profile: code\n")
    assert tg.startswith("boot profile: tg\n")
    for result in (default, code, tg):
        assert "=== boot: 婷留言 ===" in result
        assert "=== boot: 增量摘要 ===" in result
        assert "=== boot: 今日浮现 ===" in result


@pytest.mark.asyncio
async def test_invalid_profile_is_rejected_without_starting_decay(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)

    result = await server.boot(profile="daily")

    assert result == "profile 必须是 talk、code 或 tg。"
    assert server.decay_engine.ensure_started.await_count == 0


@pytest.mark.asyncio
async def test_code_uses_structured_metadata_for_session_selection(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    code_id = await server.bucket_mgr.create(
        "ordinary archive summary",
        name="code session",
        domain=["session"],
        topics=["项目/OB"],
    )
    personal_id = await server.bucket_mgr.create(
        "项目 words in body must not classify this archive",
        name="personal session",
        domain=["session"],
        topics=["日常/作息"],
    )
    await server.bucket_mgr.archive(code_id)
    await server.bucket_mgr.archive(personal_id)

    code = await server.boot(profile="code")
    talk = await server.boot(profile="talk")

    assert code_id in code
    assert personal_id not in code
    assert code_id in talk and personal_id in talk


@pytest.mark.asyncio
async def test_profile_delta_checkpoints_do_not_consume_each_other(
    tmp_path, monkeypatch
):
    server = _load_server(tmp_path, monkeypatch)
    await server.boot(profile="talk")
    bucket_id = await server.bucket_mgr.create(
        "profile delta body",
        name="profile delta",
        domain=["项目/OB"],
        pinned=True,
    )

    talk = await server.boot(profile="talk")
    code = await server.boot(profile="code")
    tg = await server.boot(profile="tg")

    locator = f"[bucket_id:{bucket_id}] profile delta：新建"
    assert locator in talk
    assert locator in code
    assert locator in tg
    with sqlite3.connect(server.bucket_mgr.history_db_path) as conn:
        rows = conn.execute(
            "SELECT profile, last_event_id FROM boot_delta_profile_checkpoints"
        ).fetchall()
    assert {row[0] for row in rows} == {"talk", "code", "tg"}


def test_legacy_global_delta_checkpoint_seeds_all_profiles(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    with sqlite3.connect(server.bucket_mgr.history_db_path) as conn:
        conn.execute("DELETE FROM boot_delta_profile_checkpoints")
        conn.execute("DELETE FROM boot_delta_checkpoint")
        conn.execute(
            """
            INSERT INTO boot_delta_checkpoint (singleton, last_event_id, completed_at)
            VALUES (1, 17, '2026-09-17T00:00:00')
            """
        )

    server.bucket_mgr._init_history_db()

    assert all(
        server.bucket_mgr.get_boot_delta_checkpoint(profile)["last_event_id"] == 17
        for profile in ("talk", "code", "tg")
    )


@pytest.mark.asyncio
async def test_note_delivery_and_sealed_hiding_remain_global_across_profiles(
    tmp_path, monkeypatch
):
    server = _load_server(tmp_path, monkeypatch)
    await server.leave_note("single cross-profile note")
    sealed_id = await server.bucket_mgr.create(
        "sealed profile body", name="sealed profile", sealed=True, pinned=True
    )

    talk = await server.boot(profile="talk")
    code = await server.boot(profile="code")
    tg = await server.boot(profile="tg")

    assert "single cross-profile note" in talk
    assert "single cross-profile note" not in code
    assert "single cross-profile note" not in tg
    for result in (talk, code, tg):
        assert sealed_id not in result
        assert "sealed profile" not in result


@pytest.mark.asyncio
async def test_tg_is_compact_and_keeps_global_constraint_and_high_todo(
    tmp_path, monkeypatch
):
    server = _load_server(tmp_path, monkeypatch)
    constraint_id = await server.bucket_mgr.create(
        "global constraint body " * 300,
        name="protected operation rule",
        protected=True,
        domain=["日常/作息"],
    )
    high_todo_id = await server.bucket_mgr.create(
        "high todo carrier",
        name="high todo",
        importance=8,
        todos=["must ship"],
    )
    low_todo_id = await server.bucket_mgr.create(
        "low todo carrier",
        name="low todo",
        importance=3,
        todos=["later"],
    )

    talk = await server.boot(profile="talk")
    tg = await server.boot(profile="tg")

    assert constraint_id in tg
    assert high_todo_id in tg
    assert low_todo_id in tg
    assert "=== boot: 最新信箱 ===" in tg
    assert "=== boot: 最近 3 次归档 ===" not in tg
    assert "=== boot: 回声 ===" not in tg
    assert len(tg) < len(talk)
    assert server.count_tokens_approx(tg) <= server.BOOT_PROFILE_CONFIG["tg"]["max_tokens"]


@pytest.mark.asyncio
async def test_tg_mailbox_shows_only_latest_visible_letter_without_sessions(
    tmp_path, monkeypatch
):
    server = _load_server(tmp_path, monkeypatch)
    await server.archive_session("older session", letter="TG_OLDER_VISIBLE_LETTER")
    await server.archive_session("latest session", letter="TG_LATEST_VISIBLE_LETTER")
    await server.archive_session(
        "sealed session", letter="TG_SEALED_LETTER_MUST_NOT_LEAK", sealed=True
    )
    await server.leave_note("TG_FUTURE_NOTE_MUST_NOT_LEAK", open_at="2099-01-01T00:00:00")

    talk = await server.boot(profile="talk")
    code = await server.boot(profile="code")
    tg = await server.boot(profile="tg")

    for result in (talk, code, tg):
        assert "TG_LATEST_VISIBLE_LETTER" in result
        assert "TG_OLDER_VISIBLE_LETTER" not in result
        assert "TG_SEALED_LETTER_MUST_NOT_LEAK" not in result
    assert "TG_FUTURE_NOTE_MUST_NOT_LEAK" not in tg
    assert "=== boot: 最新信箱 ===" in tg
    assert "=== boot: 最近 3 次归档 ===" not in tg
    assert "older session" not in tg
    assert "latest session" not in tg


@pytest.mark.asyncio
async def test_profile_boot_does_not_touch_bucket_files(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    bucket_id = await server.bucket_mgr.create(
        "profile read-only", name="profile read-only", pinned=True
    )
    path = server.bucket_mgr._find_bucket_file(bucket_id)
    before = open(path, "rb").read()

    await server.boot(profile="code")
    await server.boot(profile="tg")

    assert open(path, "rb").read() == before


def _newer_trigger_recall():
    return {
        "semantic": {"status": "available"},
        "candidates": [{
            "id": "newer-reference",
            "metadata": {"name": "newer information", "created": "2099-01-01T10:00:00"},
            "content": "CANDIDATE_BODY_MUST_NOT_LEAK",
            "semantic_score": 0.90,
        }],
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("profile,limit", [("talk", 10), ("code", 10), ("tg", 5)])
async def test_profile_trigger_hints_only_recall_shown_items(tmp_path, monkeypatch, profile, limit):
    from datetime import datetime

    server = _load_server(tmp_path, monkeypatch)
    today = datetime.now().date().isoformat()
    ids = []
    for index in range(12):
        bucket_id = await server.bucket_mgr.create(f"shown reminder {index}", name=f"reminder {index}")
        await server.bucket_mgr.update(bucket_id, trigger_date=today)
        ids.append(bucket_id)
    server._recall_memory_candidates.return_value = _newer_trigger_recall()

    result = await server.boot(profile=profile)

    assert server._recall_memory_candidates.await_count == limit
    assert result.count("⚠ 可能已过时（相似 0.90）：[bucket_id:newer-reference] newer information（2099-01-01）") == limit
    assert "CANDIDATE_BODY_MUST_NOT_LEAK" not in result
    assert server.count_tokens_approx(result) <= server.BOOT_PROFILE_CONFIG[profile]["max_tokens"]
    seen = 0
    for bucket_id in ids:
        metadata = (await server.bucket_mgr.get(bucket_id))["metadata"]
        seen += metadata.get("trigger_last_seen") == today
        assert not metadata.get("resolved", False)
        assert not metadata.get("superseded_by")
    assert seen == limit


@pytest.mark.asyncio
@pytest.mark.parametrize("profile", ["talk", "code", "tg"])
@pytest.mark.parametrize("failure", ["error", "disabled", "empty", "timeout"])
async def test_profile_boot_survives_trigger_recall_failure(tmp_path, monkeypatch, profile, failure):
    import asyncio
    from datetime import datetime

    server = _load_server(tmp_path, monkeypatch)
    today = datetime.now().date().isoformat()
    bucket_id = await server.bucket_mgr.create("FAILURE_PREVIEW_SURVIVES", name="due reminder")
    await server.bucket_mgr.update(bucket_id, trigger_date=today)
    cancelled = []
    if failure == "error":
        server._recall_memory_candidates.side_effect = RuntimeError("PRIVATE_RECALL_FAILURE")
    elif failure == "timeout":
        monkeypatch.setattr(server, "_TRIGGER_HINT_TIMEOUT_SECONDS", 0.01)

        async def recall(content, limit):
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.append(True)

        server._recall_memory_candidates.side_effect = recall
    else:
        server._recall_memory_candidates.return_value = {
            "semantic": {"status": "disabled" if failure == "disabled" else "available"},
            "candidates": [],
        }

    result = await server.boot(profile=profile)

    assert result.startswith(f"boot profile: {profile}\n")
    assert "FAILURE_PREVIEW_SURVIVES" in result
    assert "⚠" not in result and "PRIVATE_RECALL_FAILURE" not in result
    assert (await server.bucket_mgr.get(bucket_id))["metadata"]["trigger_last_seen"] == today
    if failure == "timeout":
        assert cancelled == [True]


@pytest.mark.asyncio
@pytest.mark.parametrize("max_tokens", [1000, 16000])
@pytest.mark.parametrize("cas_conflict", [False, True])
async def test_trigger_hint_budget_and_cas_keep_preview_consumption(
    tmp_path, monkeypatch, max_tokens, cas_conflict
):
    from datetime import datetime

    server = _load_server(tmp_path, monkeypatch)
    await server.boot()
    today = datetime.now().date().isoformat()
    ids = []
    for index in range(8):
        bucket_id = await server.bucket_mgr.create(
            "触" * 280 + f"END_HINT_{index}", name=f"due hint {index}"
        )
        await server.bucket_mgr.update(bucket_id, trigger_date=today)
        ids.append(bucket_id)
    # Keep the same delta text across the two deliveries; real checkpoints still advance.
    monkeypatch.setattr(server, "_format_boot_delta", lambda **kwargs: (
        "=== boot: 增量摘要 ===\n（无新增变化）", kwargs["high_water"]
    ))
    baseline = await server.boot(max_tokens=max_tokens)
    for bucket_id in ids:
        await server.bucket_mgr.update(bucket_id, trigger_last_seen="")
    await server.bucket_mgr.create("checkpoint advance only", tags=["test"])
    server._recall_memory_candidates.reset_mock()
    server._recall_memory_candidates.return_value = _newer_trigger_recall()
    original = server.bucket_mgr.advance_boot_delta_checkpoint
    advances = []

    def concurrent_advance(expected_event_id, next_event_id, profile="talk"):
        advances.append(next_event_id)
        advanced = original(expected_event_id, next_event_id, profile=profile)
        return False if cas_conflict and len(advances) == 1 else advanced

    monkeypatch.setattr(server.bucket_mgr, "advance_boot_delta_checkpoint", concurrent_advance)
    result = await server.boot(max_tokens=max_tokens)

    assert server._recall_memory_candidates.await_count == 8  # CAS never repeats recall.
    assert advances
    assert server.count_tokens_approx(result) <= max_tokens
    if max_tokens == 1000:
        assert result == baseline  # All previews/other sections retain their original budget.
        assert "⚠" not in result
        assert 0 < sum(f"END_HINT_{index}" in result for index in range(8)) < 8
    else:
        assert result.count("⚠ 可能已过时（相似 0.90）：") == 8
    for index, bucket_id in enumerate(ids):
        assert (f"END_HINT_{index}" in baseline) == (f"END_HINT_{index}" in result)
        metadata = (await server.bucket_mgr.get(bucket_id))["metadata"]
        assert (metadata["trigger_last_seen"] == today) is (f"END_HINT_{index}" in result)
        assert not metadata.get("resolved", False)
        assert not metadata.get("superseded_by")


@pytest.mark.asyncio
@pytest.mark.parametrize("profile", ["talk", "code", "tg"])
async def test_profile_boot_suppresses_tied_one_point_zero_hints(tmp_path, monkeypatch, profile):
    from datetime import datetime

    server = _load_server(tmp_path, monkeypatch)
    today = datetime.now().date().isoformat()
    bucket_id = await server.bucket_mgr.create("TIED_HINT_PREVIEW_SURVIVES", name="due reminder")
    await server.bucket_mgr.update(bucket_id, trigger_date=today)
    recall = _newer_trigger_recall()
    recall["candidates"][0]["semantic_score"] = 1.0
    recall["candidates"].append({
        "id": "second-reference",
        "metadata": {"name": "other newer information", "created": "2099-01-01T10:00:00"},
        "content": "OTHER_CANDIDATE_BODY_MUST_NOT_LEAK",
        "semantic_score": 1.0,
    })
    server._recall_memory_candidates.return_value = recall

    result = await server.boot(profile=profile)

    assert "TIED_HINT_PREVIEW_SURVIVES" in result
    assert "⚠ 可能已过时" not in result
    assert "CANDIDATE_BODY_MUST_NOT_LEAK" not in result
    assert "OTHER_CANDIDATE_BODY_MUST_NOT_LEAK" not in result
    assert (await server.bucket_mgr.get(bucket_id))["metadata"]["trigger_last_seen"] == today

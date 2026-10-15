import importlib
import sys
from datetime import datetime, timedelta
from unittest.mock import AsyncMock

import pytest


def _load_server(tmp_path, monkeypatch):
    monkeypatch.setenv("OMBRE_BUCKETS_DIR", str(tmp_path / "buckets"))
    monkeypatch.delenv("OMBRE_API_KEY", raising=False)
    monkeypatch.delenv("OMBRE_TRIGGER_STALE_THRESHOLD", raising=False)
    sys.modules.pop("server", None)
    server = importlib.import_module("server")
    server.decay_engine.ensure_started = AsyncMock(return_value=None)
    server._recall_memory_candidates = AsyncMock(
        return_value={"candidates": [], "semantic": {"status": "disabled"}}
    )
    server.dehydrator.analyze = AsyncMock(
        return_value={
            "domain": ["trigger-test"],
            "valence": 0.5,
            "arousal": 0.3,
            "tags": ["trigger"],
            "suggested_name": "trigger-memory",
        }
    )
    return server


@pytest.mark.asyncio
async def test_boot_surfaces_due_trigger_once_per_day(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    today = datetime.now().date().isoformat()
    result = await server.hold(
        "remember this on trigger day",
        trigger_date=today,
    )
    bucket_id = result.split("→")[-1].split()[0] if "→" in result else ""
    if not bucket_id:
        bucket_id = (await server.bucket_mgr.list_all(include_archive=False))[0]["id"]

    first = await server.boot()
    second = await server.boot()
    bucket = await server.bucket_mgr.get(bucket_id)

    assert "boot: 今日浮现" in first
    assert bucket_id in first
    assert bucket_id not in second
    assert bucket["metadata"]["trigger_last_seen"] == today


@pytest.mark.asyncio
async def test_trace_sets_trigger_and_resolved_hides_it(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    yesterday = (datetime.now().date() - timedelta(days=1)).isoformat()
    bucket_id = await server.bucket_mgr.create(content="trace trigger body")

    await server.trace(bucket_id, trigger_date=yesterday)
    first = await server.boot()
    await server.trace(bucket_id, resolved=1)
    second = await server.boot()

    assert bucket_id in first
    assert bucket_id not in second


@pytest.mark.asyncio
async def test_invalid_trigger_date_is_rejected(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)

    hold_result = await server.hold("invalid trigger", trigger_date="2026/07/20")
    trace_result = await server.trace("missing", trigger_date="July 20")

    assert "trigger_date must use YYYY-MM-DD format" in hold_result
    assert "trigger_date must use YYYY-MM-DD format" in trace_result


@pytest.mark.asyncio
async def test_boot_only_marks_fully_emitted_trigger_items_seen(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    today = datetime.now().date().isoformat()
    ids = []
    for index in range(8):
        bucket_id = await server.bucket_mgr.create(
            ("触" * 280) + f"END_TRIGGER_{index}",
            name=f"due trigger {index}",
        )
        assert await server.bucket_mgr.update(bucket_id, trigger_date=today)
        ids.append(bucket_id)

    first = await server.boot(max_tokens=1000)
    seen = []
    for index, bucket_id in enumerate(ids):
        bucket = await server.bucket_mgr.get(bucket_id)
        complete = f"END_TRIGGER_{index}" in first
        assert (bucket["metadata"]["trigger_last_seen"] == today) is complete
        seen.append(complete)
    assert any(seen) and not all(seen)

    second = await server.boot(max_tokens=16000)
    for index, bucket_id in enumerate(ids):
        if not seen[index]:
            assert f"END_TRIGGER_{index}" in second
            bucket = await server.bucket_mgr.get(bucket_id)
            assert bucket["metadata"]["trigger_last_seen"] == today


@pytest.mark.asyncio
async def test_cas_recomposition_recomputes_trigger_consumption(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    await server.boot()
    today = datetime.now().date().isoformat()
    ids = []
    for index in range(8):
        bucket_id = await server.bucket_mgr.create(
            ("触" * 280) + f"END_CAS_{index}",
            name=f"CAS event {index} " + "变" * 70,
        )
        assert await server.bucket_mgr.update(bucket_id, trigger_date=today)
        ids.append(bucket_id)

    original = server.bucket_mgr.advance_boot_delta_checkpoint
    calls = 0

    def concurrent_advance(expected_event_id, next_event_id, profile="talk"):
        nonlocal calls
        calls += 1
        if calls == 1:
            assert original(
                expected_event_id,
                server.bucket_mgr.get_boot_delta_high_water(),
                profile=profile,
            )
            return False
        return original(expected_event_id, next_event_id, profile=profile)

    monkeypatch.setattr(server.bucket_mgr, "advance_boot_delta_checkpoint", concurrent_advance)
    result = await server.boot(max_tokens=1000)

    assert calls == 1
    assert "（无新增变化）" in result
    for index, bucket_id in enumerate(ids):
        bucket = await server.bucket_mgr.get(bucket_id)
        assert (bucket["metadata"]["trigger_last_seen"] == today) is (
            f"END_CAS_{index}" in result
        )


def _trigger_bucket(bucket_id="reminder", *, created="2026-10-15T09:00:00", **metadata):
    return {
        "id": bucket_id,
        "content": "the original reminder content",
        "metadata": {
            "name": bucket_id,
            "created": created,
            "trigger_date": datetime.now().date().isoformat(),
            "importance": 5,
            **metadata,
        },
    }


def _recall(*candidates, status="available"):
    return {"candidates": list(candidates), "semantic": {"status": status}}


def _candidate(bucket_id="newer", *, score=0.85, created="2026-10-15T10:00:00", **metadata):
    bucket = _trigger_bucket(bucket_id, created=created, **metadata)
    bucket["semantic_score"] = score
    bucket["content"] = "CANDIDATE_BODY_MUST_NOT_LEAK"
    return bucket


@pytest.mark.asyncio
async def test_trigger_hint_uses_semantic_rank_and_preserves_preview(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    reminder = _trigger_bucket()
    lexical = _candidate("lexical", score=0.81)
    lexical["score"] = 99
    semantic = _candidate("semantic", score=0.94, name="best\n\tname")
    semantic["score"] = 1
    server._recall_memory_candidates.return_value = _recall(lexical, semantic)
    update = AsyncMock()
    monkeypatch.setattr(server.bucket_mgr, "update", update)
    baseline = {}

    text, items = await server._format_due_triggers([reminder], baseline_out=baseline)

    assert text == baseline["text"] + "\n⚠ 可能已过时（相似 0.94）：[bucket_id:semantic] best name（2026-10-15）"
    assert "CANDIDATE_BODY_MUST_NOT_LEAK" not in text
    assert "[bucket_id:lexical]" not in text
    assert items == [("reminder", len(text))]
    assert baseline["items"] == [("reminder", len(baseline["text"]))]
    server._recall_memory_candidates.assert_awaited_once_with(reminder["content"], limit=8)
    update.assert_not_awaited()
    assert reminder["metadata"].get("resolved") is None
    assert reminder["metadata"].get("superseded_by") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("marker", ["new-id", "none", "  new-id  "])
async def test_superseded_due_trigger_is_skipped_before_recall(tmp_path, monkeypatch, marker):
    server = _load_server(tmp_path, monkeypatch)
    text, items = await server._format_due_triggers([_trigger_bucket(superseded_by=marker)])
    assert "（今日无到期提醒）" in text
    assert items == []
    server._recall_memory_candidates.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("case", [
    "self", "sealed", "test", "superseded", "dormant", "older", "equal",
    "missing_time", "bad_time", "date_only", "mixed_zone", "low_score",
    "no_score", "bad_score", "nan", "infinity",
])
async def test_trigger_hint_rejects_ineligible_candidate(tmp_path, monkeypatch, case):
    server = _load_server(tmp_path, monkeypatch)
    candidate = _candidate()
    if case == "self":
        candidate["id"] = "reminder"
    elif case in {"sealed", "dormant"}:
        candidate["metadata"][case] = True
    elif case == "test":
        candidate["metadata"]["tags"] = ["test"]
    elif case == "superseded":
        candidate["metadata"]["superseded_by"] = "none"
    elif case in {"older", "equal", "missing_time", "bad_time", "date_only", "mixed_zone"}:
        candidate["metadata"]["created"] = {
            "older": "2026-10-15T08:59:59", "equal": "2026-10-15T09:00:00",
            "missing_time": None, "bad_time": "invalid", "date_only": "2026-10-16",
            "mixed_zone": "2026-10-15T10:00:00+08:00",
        }[case]
    elif case == "no_score":
        candidate.pop("semantic_score")
    else:
        candidate["semantic_score"] = {
            "low_score": 0.6499, "bad_score": "invalid", "nan": float("nan"),
            "infinity": float("inf"),
        }[case]
    server._recall_memory_candidates.return_value = _recall(candidate)
    assert await server._trigger_stale_hint(_trigger_bucket()) == ""


@pytest.mark.asyncio
@pytest.mark.parametrize("created", [None, "invalid", "2026-10-15"])
async def test_trigger_hint_does_not_infer_missing_creation_time(tmp_path, monkeypatch, created):
    server = _load_server(tmp_path, monkeypatch)
    reminder = _trigger_bucket(created=created, created_at="2026-10-14")
    assert await server._trigger_stale_hint(reminder) == ""
    server._recall_memory_candidates.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("created,newer,expected", [
    ("2026-10-15T09:00:00", "2026-10-15T09:00:01", True),
    ("2026-10-15T09:00:00+08:00", "2026-10-15T01:00:01Z", True),
    ("2026-10-15T09:00:00+08:00", "2026-10-15T02:00:00+02:00", False),
    ("2026-10-15T09:00:00+08:00", "2026-10-15T01:00:00Z", False),
    ("2026-10-15T09:00:00+08:00", "2026-10-15T10:00:00", False),
])
async def test_trigger_hint_compares_creation_instants(tmp_path, monkeypatch, created, newer, expected):
    server = _load_server(tmp_path, monkeypatch)
    server._recall_memory_candidates.return_value = _recall(_candidate(created=newer, score=0.65))
    result = await server._trigger_stale_hint(_trigger_bucket(created=created))
    assert bool(result) is expected


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["disabled", "unavailable_or_empty_index", "provider_error"])
async def test_trigger_hint_requires_available_semantics(tmp_path, monkeypatch, status):
    server = _load_server(tmp_path, monkeypatch)
    server._recall_memory_candidates.return_value = _recall(_candidate(), status=status)
    text, _ = await server._format_due_triggers([_trigger_bucket()])
    assert "the original reminder content" in text
    assert "⚠" not in text


@pytest.mark.asyncio
async def test_trigger_hint_no_match_and_recall_failure_are_silent(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    server._recall_memory_candidates.return_value = _recall()
    baseline, _ = await server._format_due_triggers([_trigger_bucket()])
    server._recall_memory_candidates.side_effect = RuntimeError("HIDDEN_PROVIDER_ERROR")
    failed, _ = await server._format_due_triggers([_trigger_bucket()])
    assert failed == baseline
    assert "HIDDEN_PROVIDER_ERROR" not in failed


@pytest.mark.asyncio
async def test_trigger_hint_name_is_bounded_and_one_line(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    server._recall_memory_candidates.return_value = _recall(_candidate(name="长\n" * 100))
    hint = await server._trigger_stale_hint(_trigger_bucket())
    assert hint.count("\n") == 0
    assert hint.endswith("...（2026-10-15）")
    assert len(hint.split("] ", 1)[1].split("（", 1)[0]) <= 80


@pytest.mark.asyncio
async def test_trigger_hint_deadline_discards_partial_results_and_awaits_cancellation(tmp_path, monkeypatch):
    import asyncio

    server = _load_server(tmp_path, monkeypatch)
    monkeypatch.setattr(server, "_TRIGGER_HINT_TIMEOUT_SECONDS", 0.01)
    completed = []

    async def recall(content, limit):
        if server._recall_memory_candidates.await_count == 1:
            return _recall(_candidate())
        try:
            await asyncio.Event().wait()
        finally:
            completed.append("cancelled")

    server._recall_memory_candidates.side_effect = recall
    text, items = await server._format_due_triggers([
        _trigger_bucket("first"), _trigger_bucket("second"), _trigger_bucket("third"),
    ])
    assert "⚠" not in text
    assert len(items) == 3
    assert server._recall_memory_candidates.await_count == 2
    assert completed == ["cancelled"]


@pytest.mark.asyncio
async def test_trigger_hint_propagates_external_cancellation(tmp_path, monkeypatch):
    import asyncio

    server = _load_server(tmp_path, monkeypatch)
    started = asyncio.Event()
    stopped = asyncio.Event()

    async def recall(content, limit):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    server._recall_memory_candidates.side_effect = recall
    task = asyncio.create_task(server._trigger_stale_hints([_trigger_bucket()]))
    try:
        await started.wait()
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert stopped.is_set()


@pytest.mark.asyncio
@pytest.mark.parametrize("max_items,expected", [(5, 5), (10, 10), (99, 10), (0, 0)])
async def test_trigger_hint_recall_is_bounded_after_due_filtering(tmp_path, monkeypatch, max_items, expected):
    server = _load_server(tmp_path, monkeypatch)
    buckets = [
        _trigger_bucket("resolved", resolved=True),
        _trigger_bucket("sealed", sealed=1),
        _trigger_bucket("superseded", superseded_by="none"),
        _trigger_bucket("future", trigger_date="2099-01-01"),
        _trigger_bucket("seen", trigger_last_seen=datetime.now().date().isoformat()),
        *[_trigger_bucket(f"due-{index}") for index in range(12)],
    ]
    text, items = await server._format_due_triggers(buckets, max_items=max_items)
    assert len(items) == expected
    assert server._recall_memory_candidates.await_count == expected
    assert all(bucket_id.startswith("due-") for bucket_id, _ in items)
    assert all(f"[bucket_id:{name}]" not in text for name in ("resolved", "sealed", "superseded", "future", "seen"))


@pytest.mark.asyncio
@pytest.mark.parametrize("score,expected", [(0.6499, False), (0.65, True), (0.6501, True)])
async def test_trigger_hint_default_threshold_boundary(tmp_path, monkeypatch, score, expected):
    server = _load_server(tmp_path, monkeypatch)
    server._recall_memory_candidates.return_value = _recall(_candidate(score=score))
    hint = await server._trigger_stale_hint(_trigger_bucket())
    assert bool(hint) is expected
    if expected:
        assert hint == f"⚠ 可能已过时（相似 {score:.2f}）：[bucket_id:newer] newer（2026-10-15）"


@pytest.mark.asyncio
@pytest.mark.parametrize("threshold,score,expected", [
    ("0.90", 0.85, False), ("0.60", 0.64, True), ("0", 0.0, True),
    ("1", 0.9999, False), ("1", 1.0, True), (" 0.7 ", 0.70, True),
])
async def test_trigger_hint_threshold_override(tmp_path, monkeypatch, threshold, score, expected):
    server = _load_server(tmp_path, monkeypatch)
    monkeypatch.setenv("OMBRE_TRIGGER_STALE_THRESHOLD", threshold)
    server._recall_memory_candidates.return_value = _recall(_candidate(score=score))
    hint = await server._trigger_stale_hint(_trigger_bucket())
    assert bool(hint) is expected
    if expected:
        assert f"（相似 {score:.2f}）：" in hint


@pytest.mark.asyncio
@pytest.mark.parametrize("configured", [None, "", "  ", "broken", "NaN", "nan", "inf", "-inf", "1e999", "-0.01", "1.01"])
async def test_trigger_hint_invalid_threshold_falls_back(tmp_path, monkeypatch, configured):
    server = _load_server(tmp_path, monkeypatch)
    if configured is not None:
        monkeypatch.setenv("OMBRE_TRIGGER_STALE_THRESHOLD", configured)
    assert server._trigger_stale_threshold() == 0.65
    server._recall_memory_candidates.return_value = _recall(_candidate(score=0.6499))
    assert await server._trigger_stale_hint(_trigger_bucket()) == ""
    server._recall_memory_candidates.return_value = _recall(_candidate(score=0.65))
    assert "（相似 0.65）：" in await server._trigger_stale_hint(_trigger_bucket())


@pytest.mark.asyncio
async def test_trigger_hint_reloads_threshold_and_displays_one(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    server._recall_memory_candidates.return_value = _recall(_candidate(score=0.85))
    monkeypatch.setenv("OMBRE_TRIGGER_STALE_THRESHOLD", "0.90")
    assert await server._trigger_stale_hint(_trigger_bucket()) == ""
    monkeypatch.setenv("OMBRE_TRIGGER_STALE_THRESHOLD", "0.80")
    assert "（相似 0.85）：" in await server._trigger_stale_hint(_trigger_bucket())
    server._recall_memory_candidates.return_value = _recall(_candidate(score=1.0))
    assert await server._trigger_stale_hint(_trigger_bucket()) == (
        "⚠ 可能已过时（相似 1.00）：[bucket_id:newer] newer（2026-10-15）"
    )

@pytest.mark.asyncio
@pytest.mark.parametrize("case,score,expected", [
    ("missing", None, False), ("none", None, False),
    ("empty", "", False), ("zero", 0.0, True),
])
async def test_trigger_hint_zero_threshold_requires_explicit_score(
    tmp_path, monkeypatch, case, score, expected
):
    server = _load_server(tmp_path, monkeypatch)
    monkeypatch.setenv("OMBRE_TRIGGER_STALE_THRESHOLD", "0")
    candidate = _candidate(score=score)
    if case == "missing":
        candidate.pop("semantic_score")
    server._recall_memory_candidates.return_value = _recall(candidate)
    hint = await server._trigger_stale_hint(_trigger_bucket())
    assert bool(hint) is expected
    if expected:
        assert "（相似 0.00）：" in hint
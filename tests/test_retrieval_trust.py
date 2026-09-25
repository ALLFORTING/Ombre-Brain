"""R-1/R-2/R-3 regressions for public retrieval and provider fallbacks."""

import importlib
import re
import sqlite3
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest


def _server(tmp_path, monkeypatch):
    monkeypatch.setenv("OMBRE_BUCKETS_DIR", str(tmp_path / "buckets"))
    monkeypatch.delenv("OMBRE_API_KEY", raising=False)
    monkeypatch.delenv("OMBRE_RESPONSE_SEAL", raising=False)
    sys.modules.pop("server", None)
    server = importlib.import_module("server")
    server.decay_engine.ensure_started = AsyncMock(return_value=None)
    server.bucket_mgr.touch = AsyncMock(return_value=True)
    return server


def _bucket(index, *, content=None, sealed=False):
    return {
        "id": f"trust-{index:02d}",
        "content": content or f"canonical body {index} [[link]]",
        "metadata": {
            "name": f"trust-{index}", "tags": ["test"],
            "created_at": "2026-09-24T00:00:00", "updated_at": "2026-09-24T00:00:00",
            "sealed": int(sealed),
        },
    }


def _ids(text):
    return re.findall(r"\[bucket_id:(trust-\d+)\]", text)


def _cursor(text):
    match = re.search(r"^下一页 cursor: (\S+)$", text, re.MULTILINE)
    return match.group(1) if match else ""


@pytest.mark.asyncio
async def test_query_full_returns_canonical_body_and_marks_truncation(tmp_path, monkeypatch):
    server = _server(tmp_path, monkeypatch)
    bucket = _bucket(1, content="canonical [[link]] " + "X" * 120)
    server.bucket_mgr.search = AsyncMock(return_value=[bucket])
    server.dehydrator.dehydrate = AsyncMock(side_effect=AssertionError("full must not dehydrate"))

    full = await server.breath(query="canonical", mode="full", touch=False)
    short = await server.breath(query="canonical", mode="full", max_tokens=5, touch=False)

    assert "[显示=原文] " + bucket["content"] in full
    assert "[显示=原文·已截断]" in short
    assert bucket["content"] not in short
    server.dehydrator.dehydrate.assert_not_awaited()
    server.bucket_mgr.touch.assert_not_awaited()


@pytest.mark.asyncio
async def test_filtered_query_full_returns_canonical_body(tmp_path, monkeypatch):
    server = _server(tmp_path, monkeypatch)
    bucket = _bucket(1)
    server.bucket_mgr.list_all = AsyncMock(return_value=[bucket])
    server.bucket_mgr.search = AsyncMock(return_value=[bucket])
    server.dehydrator.dehydrate = AsyncMock(side_effect=AssertionError("full must not dehydrate"))

    result = await server.breath(query="canonical", tags_filter=["test"], mode="full", touch=False)

    assert "[显示=原文] " + bucket["content"] in result
    server.dehydrator.dehydrate.assert_not_awaited()


@pytest.mark.asyncio
async def test_no_query_full_keeps_existing_dehydration_route(tmp_path, monkeypatch):
    server = _server(tmp_path, monkeypatch)
    await server.bucket_mgr.create(content="legacy surfacing body", pinned=True)
    server.dehydrator.dehydrate = AsyncMock(return_value="legacy composed output")

    result = await server.breath(mode="full", touch=False)

    assert "legacy composed output" in result
    server.dehydrator.dehydrate.assert_awaited()


@pytest.mark.asyncio
async def test_compose_failure_keeps_bucket_and_does_not_cache(tmp_path, monkeypatch):
    server = _server(tmp_path, monkeypatch)
    bucket = _bucket(1)
    server.bucket_mgr.search = AsyncMock(return_value=[bucket])
    server.dehydrator.dehydrate = AsyncMock(side_effect=RuntimeError("provider down"))
    server.dehydrator._set_cached_summary = AsyncMock()
    server._fire_webhook = AsyncMock()

    result = await server.breath(query="canonical", touch=False)

    assert _ids(result) == [bucket["id"]]
    assert "[显示=原文；摘要服务暂不可用] " + bucket["content"] in result
    assert "本次显示 1 / 因组装失败省略 0 / 后续剩余 0" in result
    server.dehydrator._set_cached_summary.assert_not_awaited()
    server.bucket_mgr.touch.assert_not_awaited()
    server._fire_webhook.assert_not_awaited()


@pytest.mark.asyncio
async def test_budget_cursor_consumes_exactly_displayed_id_set(tmp_path, monkeypatch):
    server = _server(tmp_path, monkeypatch)
    buckets = [_bucket(i) for i in range(8)]
    by_id = {bucket["id"]: bucket for bucket in buckets}
    server.bucket_mgr.search = AsyncMock(return_value=buckets)
    server.bucket_mgr.get = AsyncMock(side_effect=by_id.get)
    server.dehydrator.dehydrate = AsyncMock(side_effect=lambda body, meta, **kwargs: (body, "original"))

    pages = []
    cursor = ""
    for _ in range(8):
        page = await server.breath(query="canonical", max_results=3, max_tokens=9, cursor=cursor, touch=False)
        pages.append(page)
        cursor = _cursor(page)
        if not cursor:
            break

    seen = [bucket_id for page in pages for bucket_id in _ids(page)]
    assert seen == [bucket["id"] for bucket in buckets]
    assert len(seen) == len(set(seen))
    assert not cursor
    assert server.bucket_mgr.search.await_count == 1


@pytest.mark.asyncio
async def test_cursor_revalidates_deleted_dormant_and_sealed_candidates(tmp_path, monkeypatch):
    server = _server(tmp_path, monkeypatch)
    buckets = [_bucket(i) for i in range(5)]
    by_id = {bucket["id"]: bucket for bucket in buckets}
    server.bucket_mgr.search = AsyncMock(return_value=buckets)
    server.bucket_mgr.get = AsyncMock(side_effect=by_id.get)
    server.dehydrator.dehydrate = AsyncMock(side_effect=lambda body, meta, **kwargs: (body, "original"))

    first = await server.breath(query="canonical", max_results=1, touch=False)
    del by_id["trust-01"]
    by_id["trust-02"] = _bucket(2, sealed=True)
    by_id["trust-03"] = _bucket(3)
    by_id["trust-03"]["metadata"]["dormant"] = True
    second = await server.breath(query="canonical", max_results=1, touch=False, cursor=_cursor(first))

    assert _ids(first) == ["trust-00"]
    assert _ids(second) == ["trust-04"]
    assert "共匹配 2 / 前页已消费 1 / 本次显示 1" in second
    assert not _cursor(second)


@pytest.mark.asyncio
async def test_summary_composition_stays_serial_and_ordered(tmp_path, monkeypatch):
    import asyncio

    server = _server(tmp_path, monkeypatch)
    server.bucket_mgr.search = AsyncMock(return_value=[_bucket(i) for i in range(4)])
    active = 0
    peak = 0

    async def summarize(body, meta, **kwargs):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0)
        active -= 1
        return body, "original"

    server.dehydrator.dehydrate = AsyncMock(side_effect=summarize)
    result = await server.breath(query="canonical", max_results=4, touch=False)

    assert _ids(result) == [f"trust-{i:02d}" for i in range(4)]
    assert peak == 1


@pytest.mark.asyncio
async def test_unrenderable_bucket_is_counted_as_failed_omission(tmp_path, monkeypatch):
    server = _server(tmp_path, monkeypatch)
    buckets = [_bucket(0), _bucket(1)]
    del buckets[0]["content"]
    server.bucket_mgr.search = AsyncMock(return_value=buckets)
    server.dehydrator.dehydrate = AsyncMock(side_effect=RuntimeError("provider unavailable"))

    result = await server.breath(query="canonical", touch=False)

    assert _ids(result) == ["trust-01"]
    assert "共匹配 2 / 前页已消费 0 / 本次显示 1 / 因组装失败省略 1 / 后续剩余 0" in result


@pytest.mark.asyncio
async def test_first_summary_too_large_reports_counts_without_consuming(tmp_path, monkeypatch):
    server = _server(tmp_path, monkeypatch)
    buckets = [_bucket(0), _bucket(1)]
    server.bucket_mgr.search = AsyncMock(return_value=buckets)
    server.dehydrator.dehydrate = AsyncMock(return_value=("very long summary " * 50, "summary"))

    result = await server.breath(query="canonical", max_tokens=1, max_results=1, touch=False)

    assert not _ids(result)
    assert "max_tokens 过小" in result
    assert "共匹配 2 / 前页已消费 0 / 本次显示 0 / 因组装失败省略 0 / 后续剩余 2" in result
    assert _cursor(result)


@pytest.mark.asyncio
async def test_sealed_is_absent_from_public_count(tmp_path, monkeypatch):
    server = _server(tmp_path, monkeypatch)
    server.bucket_mgr.search = AsyncMock(return_value=[_bucket(1), _bucket(2, sealed=True)])
    server.dehydrator.dehydrate = AsyncMock(side_effect=lambda body, meta, **kwargs: (body, "original"))

    result = await server.breath(query="canonical", touch=False)

    assert _ids(result) == ["trust-01"]
    assert "共匹配 1" in result
    assert "trust-02" not in result


@pytest.mark.asyncio
async def test_versioned_cache_read_without_write_and_legacy_is_ignored(test_config, monkeypatch):
    from dehydrator import Dehydrator
    from dehydration_cache_identity import DEHYDRATE_PROMPT_VERSION, dehydration_content_hash

    dehydrator = Dehydrator(test_config)
    content = "long canonical text " * 100
    valid = '{"core_facts":["fact"],"keywords":[],"summary":"current summary"}'
    with sqlite3.connect(dehydrator.cache_db_path) as conn:
        conn.execute(
            "INSERT INTO dehydration_cache(content_hash, summary, model) VALUES (?, ?, ?)",
            (dehydration_content_hash(content), "legacy summary", dehydrator.model),
        )
    dehydrator.api_available = True
    dehydrator._api_dehydrate = AsyncMock(return_value=valid)

    result = await dehydrator.dehydrate(content, cache_read=True, cache_write=False)
    assert "current summary" in result and "legacy summary" not in result
    assert dehydrator._get_cached_summary(content) is None
    assert dehydrator._api_dehydrate.await_count == 1

    await dehydrator.dehydrate(content, cache_read=True, cache_write=True)
    dehydrator._api_dehydrate.assert_awaited()
    assert DEHYDRATE_PROMPT_VERSION
    assert "current summary" in dehydrator._get_cached_summary(content)
    dehydrator._api_dehydrate.reset_mock()
    before = Path(dehydrator.cache_db_path).read_bytes()
    assert "current summary" in await dehydrator.dehydrate(content, cache_read=True, cache_write=False)
    dehydrator._api_dehydrate.assert_not_awaited()
    assert Path(dehydrator.cache_db_path).read_bytes() == before
    monkeypatch.setattr("dehydrator.DEHYDRATE_PROMPT_VERSION", "future-prompt")
    assert dehydrator._get_cached_summary(content) is None


def test_dehydration_prompt_and_parser_discard_model_metadata(test_config):
    from dehydrator import DEHYDRATE_PROMPT, Dehydrator

    assert '"emotion_state"' not in DEHYDRATE_PROMPT
    assert '"todos"' not in DEHYDRATE_PROMPT
    dehydrator = Dehydrator(test_config)
    result = dehydrator._parse_dehydration(
        '{"core_facts":["fact"],"keywords":[],"summary":"fact",'
        '"emotion_state":"sad","todos":["invented"]}'
    )
    assert "emotion_state" not in result and "todos" not in result


@pytest.mark.asyncio
async def test_invalid_summary_does_not_enter_versioned_cache(test_config):
    from dehydrator import AnalysisParseError, Dehydrator

    dehydrator = Dehydrator(test_config)
    content = "long canonical body " * 100
    dehydrator.api_available = True
    dehydrator._api_dehydrate = AsyncMock(return_value="not JSON")

    with pytest.raises(AnalysisParseError):
        await dehydrator.dehydrate(content, cache_read=True, cache_write=True)
    assert dehydrator._get_cached_summary(content) is None


def test_digest_metadata_parse_fallback_uses_all_default_fields(test_config):
    from dehydrator import Dehydrator

    dehydrator = Dehydrator(test_config)
    item = dehydrator._parse_digest(
        '[{"content":"canonical diary body","name":"model title",'
        '"importance":9,"tags":["tag"],"valence":0.8,"arousal":0.7}]'
    )[0]

    assert item["_metadata_failure"] == "parse_error"
    assert item["name"] == ""
    assert item["domain"] == ["未分类"]
    assert item["importance"] == 5
    assert item["tags"] == []
    assert item["valence"] == 0.5 and item["arousal"] == 0.3


@pytest.mark.asyncio
async def test_grow_metadata_failure_and_name_fallback(tmp_path, monkeypatch):
    server = _server(tmp_path, monkeypatch)
    server._detect_conflict_warning = AsyncMock(return_value="")
    server.dehydrator.analyze = AsyncMock(side_effect=server.AnalysisParseError("bad"))
    body = "甲乙丙丁戊己庚辛壬癸子丑寅卯辰巳午未申酉戌"

    result = await server.grow(body)
    buckets = await server.bucket_mgr.list_all()

    assert len(buckets) == 1
    assert buckets[0]["metadata"]["name"] == body[:20]
    assert "自动打标失败；原因=parse_error；已使用默认 metadata" in result


def test_name_fallback_collapses_whitespace_and_counts_unicode(tmp_path, monkeypatch):
    server = _server(tmp_path, monkeypatch)
    body = "  甲\n\n乙   😀 " + "丙" * 30
    assert server._canonical_body_name(body) == ("甲 乙 😀 " + "丙" * 30)[:20]


@pytest.mark.asyncio
async def test_grow_failure_categories_are_bounded(tmp_path, monkeypatch):
    from openai import RateLimitError
    import httpx

    server = _server(tmp_path, monkeypatch)
    server._detect_conflict_warning = AsyncMock(return_value="")
    rate_limit = RateLimitError(
        "secret provider detail",
        response=httpx.Response(429, request=httpx.Request("POST", "https://test.invalid")),
        body=None,
    )
    server.dehydrator.analyze = AsyncMock(side_effect=rate_limit)
    result = await server.grow("short text")
    assert "原因=rate_limited" in result
    assert "secret provider detail" not in result

    server.dehydrator.digest = AsyncMock(side_effect=server.AnalysisParseError("secret parser detail"))
    result = await server.grow("a sufficiently long diary entry that uses digest")
    assert result == "日记整理失败。 reason=parse_error"

    server.dehydrator.analyze = AsyncMock(return_value={
        "domain": ["学习"], "valence": 0.5, "arousal": 0.3,
        "tags": [], "suggested_name": "",
    })
    server.bucket_mgr.create = AsyncMock(side_effect=OSError("private storage path"))
    result = await server.grow("short write")
    assert result == "记忆写入失败。 reason=persistence_error"
    assert "private storage path" not in result


def test_dehydrator_client_has_explicit_retry_and_timeout(test_config):
    from dehydrator import Dehydrator

    config = dict(test_config)
    config["dehydration"] = dict(test_config["dehydration"], api_key="test-key")
    dehydrator = Dehydrator(config)

    assert dehydrator.client.max_retries == 2
    assert dehydrator.client.timeout == 60.0


def test_connection_failure_has_stable_public_category(tmp_path, monkeypatch):
    import httpx
    from openai import APIConnectionError

    server = _server(tmp_path, monkeypatch)
    error = APIConnectionError(request=httpx.Request("POST", "https://test.invalid"))
    assert server._provider_failure_category(error) == "connection_error"


@pytest.mark.asyncio
async def test_hold_shared_analyze_fallback_is_explicit(tmp_path, monkeypatch):
    server = _server(tmp_path, monkeypatch)
    server._similarity_doorbell = AsyncMock(return_value="")
    server._detect_conflict_warning = AsyncMock(return_value="")
    server.dehydrator.analyze = AsyncMock(side_effect=server.AnalysisParseError("bad"))

    result = await server.hold("hold fallback body")

    assert "新建" in result
    assert "自动打标失败；原因=parse_error；已使用默认 metadata" in result


@pytest.mark.asyncio
@pytest.mark.parametrize("retry_after", ["0.01", None])
async def test_sdk_retry_after_or_bounded_backoff_has_three_requests(test_config, retry_after):
    import httpx
    from openai import AsyncOpenAI, RateLimitError
    from dehydrator import Dehydrator

    dehydrator = Dehydrator(test_config)
    attempts = []

    def handler(request):
        attempts.append(request)
        headers = {"retry-after": retry_after} if retry_after is not None else {}
        return httpx.Response(
            429, headers=headers,
            json={"error": {"message": "limited", "type": "rate_limit_error"}},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = AsyncOpenAI(
            api_key="test-key", base_url="https://test.invalid/v1",
            http_client=http_client, max_retries=2, timeout=60.0,
        )
        calculate = client._calculate_retry_timeout
        delays = []

        def record_delay(*args, **kwargs):
            delay = calculate(*args, **kwargs)
            delays.append(delay)
            return 0

        client._calculate_retry_timeout = record_delay
        with pytest.raises(RateLimitError):
            await client.chat.completions.create(
                model="test", messages=[{"role": "user", "content": "x"}],
            )

    assert len(attempts) == 3
    assert len(delays) == 2
    if retry_after is not None:
        assert delays == [0.01, 0.01]
    else:
        assert 0 < delays[0] < delays[1] < 10
    assert dehydrator.client is None or dehydrator.client.max_retries == 2

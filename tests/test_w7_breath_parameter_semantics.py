"""W-7 public parameter behavior and retrieval/side-effect invariants."""
import importlib
import inspect
import re
import sys
from datetime import datetime as RealDatetime
from unittest.mock import AsyncMock, MagicMock

import pytest


@pytest.fixture
def server(tmp_path, monkeypatch):
    monkeypatch.setenv("OMBRE_BUCKETS_DIR", str(tmp_path / "buckets"))
    monkeypatch.delenv("OMBRE_API_KEY", raising=False)
    monkeypatch.delenv("OMBRE_BREATH_MIN_SCORE", raising=False)
    monkeypatch.setenv("OMBRE_RESPONSE_SEAL", "w7-test")
    sys.modules.pop("server", None)
    module = importlib.import_module("server")
    module.decay_engine.ensure_started = AsyncMock()
    module.bucket_mgr.touch = AsyncMock(return_value=True)
    module.dehydrator.dehydrate = AsyncMock(
        side_effect=lambda content, metadata=None, **kwargs: content
    )
    module._fire_webhook = AsyncMock()
    return module


def bucket(index, *, importance=8, domain="project", kind="dynamic", date="2026-09-27", **metadata):
    return {
        "id": f"w7-{index}", "content": f"needle body {index}", "score": 0.8,
        "metadata": {
            "name": f"memory {index}", "importance": importance,
            "domain": [domain], "type": kind, "tags": ["selected"], "topics": ["topic"],
            "updated_at": date, "created_at": date, "created": f"{date}T00:00:00",
            "last_active": f"{date}T00:00:00", "activation_count": 1,
            "valence": 0.5, "arousal": 0.5, **metadata,
        },
    }


def corpus(server, buckets):
    by_id = {item["id"]: item for item in buckets}
    server.bucket_mgr.list_all = AsyncMock(return_value=buckets)
    server.bucket_mgr.get = AsyncMock(side_effect=lambda identifier: by_id.get(identifier))

    async def search(query, **kwargs):
        candidates = kwargs.get("candidate_buckets", buckets)
        return [dict(item) for item in candidates if query.casefold() in item["content"].casefold()]

    server.bucket_mgr.search = AsyncMock(side_effect=search)
    return by_id


def next_cursor(result):
    match = re.search(r"^下一页 cursor: (\S+)$", result, re.MULTILINE)
    return match.group(1) if match else ""


@pytest.mark.parametrize("arguments,selector", [
    ({}, "default_emergence"), ({"domain": "project"}, "default_emergence"),
    ({"importance_min": 7, "domain": "project"}, "importance_only"),
    ({"tags_filter": ["selected"], "importance_min": 7}, "tags_only"),
    ({"resonance": "0.5,0.5", "importance_min": 7, "tags_filter": ["selected"]}, "resonance"),
    ({"query": "needle", "importance_min": 7}, "ordinary_query"),
    ({"query": "needle", "tags_filter": ["selected"], "importance_min": 7}, "ordinary_query"),
    ({"domain": " SESSION, session ", "importance_min": 7}, "session"),
    ({"topic_filter": ["topic"], "tags_filter": ["selected"]}, "session"),
    ({"domain": " Feel, FEEL "}, "feel"), ({"feels": True}, "feel"),
    ({"as_of": "2099-01-01", "query": "needle"}, "historical_query"),
    ({"mailbox": True}, "mailbox"),
])
def test_selector_dispatch_matrix(server, arguments, selector):
    assert server._prepare_breath_request(**arguments)["selector"] == selector


@pytest.mark.asyncio
@pytest.mark.parametrize("selector", [
    {"query": "needle"}, {"query": "needle", "tags_filter": ["selected"]},
    {"tags_filter": ["selected"]}, {"domain": "project"},
    {"resonance": "0.5,0.5", "tags_filter": ["selected"], "domain": "project"},
    {"domain": "session"}, {"domain": "session", "topic_filter": ["topic"]},
    {"feels": True}, {"feels": True, "tags_filter": ["selected"]},
])
async def test_importance_intersection_matrix(server, selector):
    kind = "feel" if selector.get("feels") else "dynamic"
    domain = "session" if selector.get("domain") == "session" else "project"
    high = bucket("high", kind=kind, domain=domain)
    low = bucket("low", importance=2, kind=kind, domain=domain)
    other = bucket("other", kind=kind, domain=domain)
    other["content"] = "unrelated text"
    corpus(server, [high, low, other])
    result = await server.breath(**selector, importance_min=7, touch=False)
    assert high["id"] in result
    assert low["id"] not in result
    if selector.get("query"):
        assert other["id"] not in result
    if server.bucket_mgr.search.await_args:
        assert low not in server.bucket_mgr.search.await_args.kwargs.get("candidate_buckets", [])


@pytest.mark.asyncio
@pytest.mark.parametrize("selector", [{}, {"query": "needle"}, {"importance_min": 7},
                                      {"resonance": "0.5,0.5"}, {"tags_filter": ["selected"]}])
async def test_domain_scope_matrix(server, selector):
    inside, outside = bucket("inside"), bucket("outside", domain="other")
    corpus(server, [inside, outside])
    result = await server.breath(**selector, domain=" PROJECT, project ", touch=False)
    assert inside["id"] in result and outside["id"] not in result
    empty = await server.breath(**selector, domain="missing", touch=False)
    assert inside["id"] not in empty and outside["id"] not in empty


@pytest.mark.asyncio
async def test_query_empty_domain_scope_never_falls_back(server):
    identifier = await server.bucket_mgr.create(content="needle real search", domain=["project"])
    server.bucket_mgr.search = AsyncMock(wraps=server.bucket_mgr.search)
    result = await server.breath(query="needle", domain="missing", touch=False)
    assert identifier not in result
    server.bucket_mgr.search.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("arguments,parameter", [
    ({"domain": "session,project"}, "domain"), ({"domain": "session,feel"}, "domain"),
    ({"feels": True, "domain": "project"}, "domain"),
    ({"feels": True, "topic_filter": ["topic"]}, "topic_filter"),
    ({"feels": True, "resonance": "0.5,0.5"}, "resonance"),
    ({"feels": True, "as_of": "2099-01-01", "query": "needle"}, "feels"),
    ({"topic_filter": ["topic"], "domain": "project"}, "domain"),
    ({"topic_filter": ["topic"], "resonance": "0.5,0.5"}, "resonance"),
    ({"domain": "session", "min_score": 0.1}, "min_score"),
    ({"domain": "feel", "valence": 0.5}, "valence"),
    ({"domain": "session", "arousal": 0.5}, "arousal"),
    ({"domain": "feel", "include_dormant": True}, "include_dormant"),
    ({"domain": "session", "wake_dormant": True}, "wake_dormant"),
    ({"recent_days": -2}, "recent_days"), ({"mailbox_limit": 2}, "mailbox_limit"),
    ({"as_of": "2099-01-01", "query": "needle", "recent_days": 0}, "recent_days"),
    ({"as_of": "2099-01-01", "query": "needle", "importance_min": 7}, "importance_min"),
    ({"tags_filter": ["selected"], "query": "needle", "cursor": "fake"}, "cursor"),
    ({"domain": "session", "cursor": "fake"}, "cursor"),
    ({"importance_min": 7, "cursor": "fake"}, "cursor"),
    ({"resonance": "0.5,0.5", "cursor": "fake"}, "cursor"),
    ({"tags_filter": ["selected"], "cursor": "fake"}, "cursor"),
    ({"mailbox": True, "cursor": "fake"}, "cursor"),
])
async def test_unsupported_parameter_matrix_has_actionable_errors(server, arguments, parameter):
    server.bucket_mgr.search = AsyncMock()
    server.bucket_mgr.list_all = AsyncMock()
    result = await server.breath(**arguments)
    assert "breath mode=" in result and f"不支持参数 {parameter}" in result
    assert "该模式可使用" in result
    server.decay_engine.ensure_started.assert_not_awaited()
    server.bucket_mgr.touch.assert_not_awaited()
    server.bucket_mgr.search.assert_not_awaited()
    server.bucket_mgr.list_all.assert_not_awaited()
    server.dehydrator.dehydrate.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("name,value", [
    ("query", "needle"), ("domain", "project"), ("feels", True), ("touch", False),
    ("importance_min", 7), ("min_score", 0), ("recent_days", 0), ("max_tokens", 12),
    ("max_results", 3), ("mode", "full"), ("include_dormant", True),
    ("emotion_trend", True), ("tags_filter", ["selected"]), ("topic_filter", ["topic"]),
    ("date_from", "2026-09-01"), ("date_to", "2026-09-30"), ("resonance", "0.5,0.5"),
    ("valence", 0.5), ("arousal", 0.5), ("as_of", "2099-01-01"), ("wake_dormant", True),
])
async def test_mailbox_allowlist(server, monkeypatch, name, value):
    formatter = MagicMock(return_value="letter")
    monkeypatch.setattr(server, "_format_mailbox", formatter)
    result = await server.breath(mailbox=True, **{name: value})
    assert "breath mode=mailbox" in result and name in result
    formatter.assert_not_called()


@pytest.mark.asyncio
async def test_mailbox_supported_arguments(server, monkeypatch):
    formatter = MagicMock(return_value="letter")
    monkeypatch.setattr(server, "_format_mailbox", formatter)
    assert "letter" in await server.breath(mailbox=True, mailbox_limit=4, include_sealed=True)
    formatter.assert_called_once_with(4, include_sealed=True)
    server.decay_engine.ensure_started.assert_not_awaited()


@pytest.mark.parametrize("selector", [{"query": "needle"}, {"query": "needle", "tags_filter": ["selected"]}])
def test_emotion_parameter_applicability(server, selector):
    server._prepare_breath_request(**selector, valence=0.2, arousal=0.3)
    server._prepare_breath_request(**selector, valence=0.2)
    with pytest.raises(ValueError, match="arousal"):
        server._prepare_breath_request(**selector, arousal=0.3)
    with pytest.raises(ValueError, match="valence"):
        server._prepare_breath_request(**selector, valence=0.2, mode="full")
    for field in ("valence", "arousal"):
        with pytest.raises(ValueError):
            server._prepare_breath_request(query="needle", as_of="2099-01-01", **{field: 0.2})


@pytest.mark.asyncio
async def test_historical_fixed_body_and_cursor_mode(server):
    corpus(server, [bucket(index) for index in range(3)])
    for mode in ("summary", "full"):
        result = await server.breath(query="needle", as_of="2099-01-01", mode=mode,
                                     valence=0.2, arousal=0.3, max_results=1)
        assert "[显示=历史原文]" in result and "needle body" in result
        token = next_cursor(result)
        state = server._BREATH_CURSOR_STATES[token]
        assert state["context"] == {"selector": "historical_query", "recent_days": -1, "recent_cutoff": None}
        other = "full" if mode == "summary" else "summary"
        changed = await server.breath(query="needle", as_of="2099-01-01", mode=other,
                                      valence=0.2, arousal=0.3, cursor=token)
        assert "不支持参数 cursor" in changed
    server.dehydrator.dehydrate.assert_not_awaited()
    server.bucket_mgr.touch.assert_not_awaited()
    server.decay_engine.ensure_started.assert_not_awaited()


@pytest.mark.asyncio
async def test_min_score_is_display_threshold(server):
    strong, weak = bucket("strong"), bucket("weak")
    weak["content"], weak["score"] = "another fuzzy match", 0.1
    corpus(server, [strong, weak])
    server.bucket_mgr.search = AsyncMock(return_value=[strong, weak])
    result = await server.breath(query="needle", min_score=0.7)
    assert strong["id"] in result and weak["id"] in result
    assert "弱匹配" in result and "共匹配 2" in result
    assert [call.args[0] for call in server.bucket_mgr.touch.await_args_list] == [strong["id"]]


@pytest.mark.parametrize("selector", [{}, {"importance_min": 7}, {"resonance": "0.5,0.5"},
                                      {"tags_filter": ["selected"]}, {"domain": "session"}, {"feels": True}])
def test_scoreless_modes_reject_min_score(server, selector):
    with pytest.raises(ValueError, match="min_score"):
        server._prepare_breath_request(**selector, min_score=0)


@pytest.mark.asyncio
async def test_resonance_filters_before_sorting(server):
    low = bucket("low", importance=2)
    far = bucket("far", valence=0.9)
    close = bucket("close", valence=0.55)
    outside = bucket("outside", domain="other")
    wrong_tag = bucket("wrong-tag", tags=["excluded"])
    corpus(server, [low, far, close, outside, wrong_tag])
    result = await server.breath(resonance="0.5,0.5", importance_min=7, domain="project",
                                 tags_filter=["selected"], touch=False)
    assert result.index(close["id"]) < result.index(far["id"])
    assert all(item["id"] not in result for item in (low, outside, wrong_tag))


def freeze_day(server, monkeypatch, day):
    class FrozenDatetime(RealDatetime):
        @classmethod
        def now(cls, tz=None):
            return cls.fromisoformat(f"{day}T12:00:00")
    monkeypatch.setattr(server, "datetime", FrozenDatetime)


@pytest.mark.asyncio
async def test_recent_days_disabled_zero_positive_and_invalid(server, monkeypatch):
    freeze_day(server, monkeypatch, "2026-09-27")
    today, yesterday, future = bucket("today"), bucket("yesterday", date="2026-09-26"), bucket("future", date="2026-09-28")
    corpus(server, [today, yesterday, future])
    zero = await server.breath(query="needle", recent_days=0, touch=False)
    positive = await server.breath(query="needle", recent_days=1, touch=False)
    disabled = await server.breath(query="needle", recent_days=-1, touch=False)
    assert today["id"] in zero and yesterday["id"] not in zero and future["id"] not in zero
    assert all(item["id"] in positive and item["id"] in disabled for item in (today, yesterday, future))
    assert "不支持参数 recent_days" in await server.breath(recent_days=-2)


@pytest.mark.asyncio
async def test_emergence_preserves_core_recency_exception_and_full_dehydration(server, monkeypatch):
    freeze_day(server, monkeypatch, "2026-09-27")
    pinned = bucket("pinned", date="2020-01-01", pinned=True, dormant=True)
    protected = bucket("protected", date="2020-01-01", protected=True, dormant=True)
    old = bucket("old", date="2020-01-01")
    corpus(server, [pinned, protected, old])
    result = await server.breath(recent_days=0, mode="full", touch=False)
    assert pinned["id"] in result and protected["id"] in result and old["id"] not in result
    assert server.dehydrator.dehydrate.await_count == 2
    assert all(call.kwargs["cache_write"] is False for call in server.dehydrator.dehydrate.await_args_list)


@pytest.mark.parametrize("arguments", [
    {"importance_min": 7}, {"resonance": "0.5,0.5"}, {"tags_filter": ["selected"]},
    {"domain": "session"}, {"feels": True}, {"feels": True, "query": "needle"},
])
def test_fixed_listing_modes_reject_full(server, arguments):
    with pytest.raises(ValueError, match="mode"):
        server._prepare_breath_request(**arguments, mode="full")


@pytest.mark.asyncio
@pytest.mark.parametrize("selector", [{"domain": "session"}, {"feels": True, "tags_filter": ["selected"]}])
async def test_session_feel_query_full_and_readonly_eligibility(server, selector):
    is_feel = selector.get("feels", False)
    matching = bucket("matching", domain="session" if not is_feel else "project",
                      kind="feel" if is_feel else "archived", dormant=True)
    missing = bucket("missing", domain="session" if not is_feel else "project",
                     kind="feel" if is_feel else "archived", date="2026-09-26")
    missing["content"] = "unrelated text"
    corpus(server, [missing, matching])
    result = await server.breath(**selector, query="needle", mode="full")
    assert matching["id"] in result and missing["id"] not in result
    assert "[显示=原文]" in result
    server.bucket_mgr.search.assert_not_awaited()
    server.bucket_mgr.touch.assert_not_awaited()
    server.dehydrator.dehydrate.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("selector", [{"domain": "session"}, {"feels": True}])
@pytest.mark.parametrize("filter_args", [
    {"importance_min": 7}, {"tags_filter": ["selected"]}, {"recent_days": 0},
    {"date_from": "2026-09-27"}, {"date_to": "2026-09-27"}, {"include_sealed": True},
])
async def test_session_feel_metadata_filter_matrix(server, monkeypatch, selector, filter_args):
    freeze_day(server, monkeypatch, "2026-09-27")
    is_feel = selector.get("feels", False)
    matching = bucket("matching", domain="project" if is_feel else "session",
                      kind="feel" if is_feel else "archived", dormant=True)
    excluded = bucket("excluded", domain="project" if is_feel else "session",
                      kind="feel" if is_feel else "archived", importance=2, tags=["other"], date="2026-09-26")
    if "date_to" in filter_args:
        excluded["metadata"]["updated_at"] = "2026-09-28"
    if "include_sealed" in filter_args:
        matching["metadata"]["sealed"] = 1
    corpus(server, [matching, excluded])
    result = await server.breath(**selector, **filter_args, touch=False)
    assert matching["id"] in result
    if "include_sealed" not in filter_args:
        assert excluded["id"] not in result
    server.bucket_mgr.touch.assert_not_awaited()
    server.decay_engine.ensure_started.assert_not_awaited()


@pytest.mark.asyncio
async def test_cursor_freezes_recency_and_allows_page_budget_changes(server, monkeypatch):
    freeze_day(server, monkeypatch, "2026-09-27")
    items = [bucket(index) for index in range(4)]
    corpus(server, items)
    first = await server.breath(query="needle", recent_days=0, max_results=1, touch=False)
    token = next_cursor(first)
    assert server._BREATH_CURSOR_STATES[token]["context"]["recent_cutoff"] == "2026-09-27"
    freeze_day(server, monkeypatch, "2026-09-28")
    second = await server.breath(query="needle", recent_days=0, cursor=token, max_results=3, max_tokens=20000, touch=False)
    assert all(item["id"] in second for item in items[1:])
    assert items[0]["id"] not in second and "后续剩余 0" in second
    assert server.bucket_mgr.search.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("change", [
    {"importance_min": 9}, {"wake_dormant": True}, {"recent_days": 1},
    {"domain": "other"}, {"touch": True}, {"include_sealed": True}, {"mode": "full"},
])
async def test_cursor_binds_filter_and_side_effect_scope(server, change):
    corpus(server, [bucket(index) for index in range(3)])
    arguments = dict(query="needle", importance_min=7, include_dormant=True, touch=False)
    first = await server.breath(**arguments, max_results=1)
    result = await server.breath(**{**arguments, **change}, cursor=next_cursor(first))
    assert "不支持参数 cursor" in result
    server.bucket_mgr.touch.assert_not_awaited()
    server.decay_engine.ensure_started.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("corruption", ["expired", "version", "selector", "cutoff", "days", "position", "ttl"])
async def test_invalid_cursor_state_never_interprets_frozen_cutoff(server, monkeypatch, corruption):
    corpus(server, [bucket(index) for index in range(3)])
    token = next_cursor(await server.breath(query="needle", max_results=1, touch=False))
    state = server._BREATH_CURSOR_STATES[token]
    if corruption == "expired":
        state["expires_at"] = 0
    elif corruption == "version":
        state["version"] = 999
    elif corruption == "selector":
        state["context"]["selector"] = "session"
    elif corruption == "cutoff":
        state["context"]["recent_cutoff"] = "malformed"
    elif corruption == "days":
        state["context"]["recent_days"] = True
    elif corruption == "position":
        state["position"] = -1
    else:
        state["expires_at"] = state["created_at"] + server._BREATH_CURSOR_TTL_SECONDS * 2
    scope = MagicMock(side_effect=AssertionError("untrusted context reached scope construction"))
    monkeypatch.setattr(server, "_breath_cursor_scope", scope)
    result = await server.breath(query="needle", cursor=token, touch=False)
    assert "不支持参数 cursor" in result
    scope.assert_not_called()
    assert server.bucket_mgr.search.await_count == 1


@pytest.mark.asyncio
async def test_cursor_revalidates_filters_without_gaps(server):
    items = [bucket(index) for index in range(5)]
    corpus(server, items)
    arguments = dict(query="needle", importance_min=7, domain="project", touch=False)
    first = await server.breath(**arguments, max_results=1)
    items[1]["metadata"]["importance"] = 2
    items[2]["metadata"]["domain"] = ["other"]
    items[3]["metadata"]["sealed"] = 1
    second = await server.breath(**arguments, cursor=next_cursor(first), max_results=2)
    assert items[4]["id"] in second and all(item["id"] not in second for item in items[:4])
    assert "共匹配 2" in second and "前页已消费 1" in second and "后续剩余 0" in second


@pytest.mark.asyncio
@pytest.mark.parametrize("arguments", [
    {"importance_min": 7}, {"resonance": "0.5,0.5"}, {},
    {"tags_filter": ["selected"]}, {"query": "needle"},
    {"query": "needle", "tags_filter": ["selected"]},
])
async def test_direct_touch_only_emitted_and_failures_preserve_accounting(server, monkeypatch, arguments):
    items = [bucket(index) for index in range(3)]
    for item in items[1:]:
        item["metadata"]["updated_at"] = "2026-09-26"
    corpus(server, items)
    # Make the first rendered entry fit and the following entry exceed the budget.
    async def summary(item, **kwargs):
        return f"[bucket_id:{item['id']}] " + ("short" if item["id"] == items[0]["id"] else "长" * 500)
    monkeypatch.setattr(server, "_bucket_summary_line", summary)
    server.dehydrator.dehydrate = AsyncMock(side_effect=lambda content, *args, **kwargs: "short" if content.endswith("0") else "长" * 500)
    server.bucket_mgr.touch.side_effect = RuntimeError("atomic touch refused")
    result = await server.breath(**arguments, max_results=3, max_tokens=100)
    assert items[0]["id"] in result and items[1]["id"] not in result and items[2]["id"] not in result
    assert "本次显示 1" in result and "因 token 预算省略 2" in result and "后续剩余 2" in result
    assert "side-effect/accounting warning" in result and "未重试 touch" in result
    assert [call.args[0] for call in server.bucket_mgr.touch.await_args_list] == [items[0]["id"]]
    if "query" in arguments and "tags_filter" not in arguments:
        resumed = await server.breath(**arguments, cursor=next_cursor(result), max_tokens=2000)
        assert items[1]["id"] in resumed and items[2]["id"] in resumed


@pytest.mark.asyncio
async def test_fallback_emission_touches_once(server, monkeypatch):
    item = bucket("fallback")
    corpus(server, [item])
    server.dehydrator.dehydrate.side_effect = RuntimeError("provider unavailable")
    server.bucket_mgr.touch.side_effect = RuntimeError("touch refused")
    result = await server.breath(query="needle")
    assert item["id"] in result and "显示=" in result and "本次显示 1" in result
    assert "因组装失败省略 0" in result and "side-effect/accounting warning" in result
    server.bucket_mgr.touch.assert_awaited_once()


@pytest.mark.asyncio
async def test_query_touch_preserves_existing_ripple_arguments_and_readonly_cache(server):
    corpus(server, [bucket("one")])
    await server.breath(query="needle")
    assert "ripple_ids" not in server.bucket_mgr.touch.await_args.kwargs
    server.bucket_mgr.touch.reset_mock()
    server.decay_engine.ensure_started.reset_mock()
    server.dehydrator.dehydrate.reset_mock()
    await server.breath(query="needle", touch=False, include_dormant=True, wake_dormant=True)
    server.bucket_mgr.touch.assert_not_awaited()
    server.decay_engine.ensure_started.assert_not_awaited()
    assert server.dehydrator.dehydrate.await_args.kwargs["cache_write"] is False


def test_public_parameter_defaults_preserved(server):
    defaults = {name: field.default for name, field in inspect.signature(server.breath).parameters.items()}
    assert defaults == {
        "query": "", "max_tokens": 10000, "domain": "", "valence": -1, "arousal": -1,
        "max_results": 5, "importance_min": -1, "mode": "summary", "recent_days": -1,
        "emotion_trend": False, "include_dormant": False, "include_sealed": False,
        "date_from": "", "date_to": "", "resonance": "", "mailbox": False,
        "mailbox_limit": 1, "feels": False, "tags_filter": None, "topic_filter": None,
        "wake_dormant": False, "touch": True, "min_score": -1, "as_of": "", "cursor": "",
    }

"""Archive query contracts using isolated synthetic buckets; no formal data."""
import importlib
import re
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import frontmatter
import pytest


@pytest.fixture
def server(test_config, monkeypatch):
    import utils
    monkeypatch.setattr(utils, "load_config", lambda: test_config)
    monkeypatch.setenv("OMBRE_BUCKETS_DIR", test_config["buckets_dir"])
    monkeypatch.delenv("OMBRE_API_KEY", raising=False)
    monkeypatch.delenv("OMBRE_BREATH_MIN_SCORE", raising=False)
    sys.modules.pop("server", None)
    module = importlib.import_module("server")
    module.decay_engine.ensure_started = AsyncMock()
    module.dehydrator.dehydrate = AsyncMock(side_effect=lambda content, *a, **k: (content, "original"))
    module._fire_webhook = AsyncMock()
    return module


async def make(server, name, *, archived=False, sealed=False, dormant=False, domain="project", importance=8):
    identifier = await server.bucket_mgr.create(
        content=f"needle body {name}", name=name, tags=["selected"],
        domain=[domain], importance=importance, sealed=sealed,
    )
    if archived:
        assert await server.bucket_mgr.archive(identifier)
    if dormant:
        assert await server.bucket_mgr.set_dormant(identifier)
    return identifier


def ids(result):
    return re.findall(r"\[bucket_id:([^\]]+)\]", result)


def cursor(result):
    return re.search(r"^下一页 cursor: (\S+)$", result, re.MULTILINE).group(1)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["summary", "full"])
@pytest.mark.parametrize("tags", [None, ["selected"]])
async def test_archive_default_and_sealed_visibility(server, mode, tags):
    active = await make(server, "active")
    archived = await make(server, "archived", archived=True)
    private = await make(server, "sealed", archived=True, sealed=True)
    arguments = dict(query="needle", mode=mode, tags_filter=tags, touch=False, max_tokens=20000)
    default = await server.breath(**arguments)
    explicit_false = await server.breath(**arguments, include_archive=False)
    explicit_none = await server.breath(**arguments, include_archive=None)
    assert default == explicit_none
    assert set(ids(default)) == {active, archived}
    assert ids(explicit_false) == [active]
    assert "[归档]" not in explicit_false
    included = await server.breath(**arguments, include_archive=True)
    assert included == default
    assert set(ids(included)) == {active, archived}
    assert private not in included
    assert included.count("[归档]") == 1
    visible = await server.breath(**arguments, include_archive=True, include_sealed=True)
    assert set(ids(visible)) == {active, archived, private}
    assert visible.count("[归档]") == 2


@pytest.mark.asyncio
async def test_archive_tagged_query_intersects_existing_filters(server):
    wanted = await make(server, "wanted", archived=True)
    wrong_domain = await make(server, "other", archived=True, domain="other")
    low = await make(server, "low", archived=True, importance=2)
    sleeping = await make(server, "sleep", archived=True, dormant=True)
    result = await server.breath(query="needle", include_archive=True, tags_filter=["selected"],
                                domain="project", importance_min=7, touch=False)
    assert ids(result) == [wanted]
    assert all(identifier not in result for identifier in (wrong_domain, low, sleeping))
    assert set(ids(await server.breath(query="needle", include_archive=True,
                                      tags_filter=["selected"], domain="project",
                                      importance_min=7, include_dormant=True, touch=False))) == {wanted, sleeping}


@pytest.mark.asyncio
@pytest.mark.parametrize("arguments", [
    {}, {"query": " "}, {"domain": "session", "query": "needle"},
    {"topic_filter": ["topic"], "query": "needle"}, {"domain": "feel", "query": "needle"},
    {"feels": True, "query": "needle"}, {"mailbox": True},
    {"as_of": "2026-10-01", "query": "needle"}, {"resonance": "0.5,0.5"},
    {"importance_min": 7}, {"tags_filter": ["selected"]},
])
async def test_invalid_selector_rejected_before_runtime_initialization(server, monkeypatch, arguments):
    initialize = MagicMock(side_effect=AssertionError("must not initialize/read/write"))
    monkeypatch.setattr(server, "_get_runtime_components", initialize)
    result = await server.breath(include_archive=True, **arguments)
    assert "include_archive" in result and "ordinary query" in result
    initialize.assert_not_called()


@pytest.mark.asyncio
async def test_touch_and_wake_preserve_archive_path_and_type(server):
    identifier = await make(server, "sleep", archived=True, dormant=True)
    before = await server.bucket_mgr.get(identifier)
    result = await server.breath(query="needle", include_archive=True, include_dormant=True,
                                wake_dormant=True, touch=True)
    assert ids(result) == [identifier] and "[归档]" in result
    after = await server.bucket_mgr.get(identifier)
    assert after["path"] == before["path"]
    assert after["metadata"]["type"] == "archived"
    assert not after["metadata"]["dormant"]
    assert after["metadata"]["activation_count"] == before["metadata"]["activation_count"] + 1


@pytest.mark.asyncio
@pytest.mark.parametrize("include_archive", [False, True])
async def test_cursor_binds_archive_and_valid_pages(server, include_archive):
    identifiers = [await make(server, f"page{i}", archived=include_archive) for i in range(3)]
    arguments = dict(query="needle", touch=False, include_archive=include_archive, max_results=1)
    first = await server.breath(**arguments)
    token = cursor(first)
    invalid = await server.breath(**{**arguments, "include_archive": not include_archive}, cursor=token)
    assert "cursor" in invalid and "不匹配" in invalid
    second = await server.breath(**arguments, cursor=token)
    third = await server.breath(**arguments, cursor=cursor(second))
    assert set(ids(first) + ids(second) + ids(third)) == set(identifiers)
    assert len(ids(first) + ids(second) + ids(third)) == 3
    assert (first + second + third).count("[归档]") == (3 if include_archive else 0)


@pytest.mark.asyncio
async def test_false_cursor_drops_bucket_archived_after_first_page(server):
    for i in range(3):
        await make(server, f"moving{i}")
    arguments = dict(query="needle", touch=False, include_archive=False, max_results=1)
    first = await server.breath(**arguments)
    token = cursor(first)
    pending = server._BREATH_CURSOR_STATES[token]["matches"][1]["id"]
    assert await server.bucket_mgr.archive(pending)
    rest = await server.breath(**{**arguments, "max_results": 5}, cursor=token)
    assert pending not in rest and "[归档]" not in rest
    assert len(ids(rest)) == 1


@pytest.mark.asyncio
async def test_marker_uses_storage_not_stale_type_and_covers_weak_and_fallback(server):
    archived = await make(server, "stale", archived=True)
    active = await make(server, "active")
    bucket = await server.bucket_mgr.get(archived)
    bucket["metadata"]["type"] = "dynamic"
    # Synthetic stale metadata persists only in this fixture root.
    path = Path(bucket["path"])
    post = frontmatter.load(path)
    post["type"] = "dynamic"
    path.write_text(frontmatter.dumps(post), encoding="utf-8")
    other = await server.bucket_mgr.get(active)
    other["metadata"]["type"] = "archived"
    assert server._breath_archive_marker(other) == ""
    summary = await server.breath(query="needle", include_archive=True, touch=False)
    assert summary.count("[归档]") == 1
    weak_bucket = dict(bucket, _breath_weak=True, _breath_score=0.1)
    weak, _ = await server._compose_breath_query_matches(
        [weak_bucket], max_tokens=2000, q_valence=None, emotion_trend=False,
        hidden_count=0, touch=False,
    )
    assert archived in weak and "[归档]" in weak
    server.dehydrator.dehydrate = AsyncMock(side_effect=RuntimeError("synthetic provider failure"))
    fallback = await server.breath(query="needle", include_archive=True, touch=False, max_tokens=25)
    assert "[归档]" in fallback and "摘要服务暂不可用" in fallback


@pytest.mark.asyncio
async def test_explicit_false_keeps_original_search_path(server):
    identifiers = [await make(server, f"rank{i}", importance=9-i) for i in range(3)]
    await make(server, "excluded", archived=True)
    original = server.bucket_mgr.search
    spy = AsyncMock(wraps=original)
    server.bucket_mgr.search = spy

    result = await server.breath(query="needle", include_archive=False, touch=False, max_tokens=20000)
    assert ids(result) == identifiers
    assert "candidate_buckets" not in spy.call_args.kwargs


@pytest.mark.asyncio
async def test_synthetic_archive_classification(server, monkeypatch):
    # One session archive, one decay-eligible ordinary archive, one explicit archive.
    receipt = await server.archive_session("needle session summary")
    session = receipt.split("bucket_id:", 1)[1].strip()
    decay = await make(server, "decay")
    monkeypatch.setattr(server.decay_engine, "calculate_score", lambda metadata: 0.0)
    stats = await server.decay_engine.run_decay_cycle()
    assert stats["archived"] == 1
    other = await make(server, "manual", archived=True, domain="other")
    corpus = await server.bucket_mgr.list_all(include_archive=True)
    counts = {"session": 0, "ordinary_decay_fixture": 0, "other": 0}
    for bucket in corpus:
        if bucket["id"] == session:
            assert Path(bucket["path"]).parent.name == "session"
            counts["session"] += 1
        elif bucket["id"] == decay:
            counts["ordinary_decay_fixture"] += 1
        elif bucket["id"] == other:
            counts["other"] += 1
    assert counts == {"session": 1, "ordinary_decay_fixture": 1, "other": 1}
    result = await server.breath(query="needle", include_archive=True, touch=False)
    assert set(ids(result)) == {session, decay, other}
    assert result.count("[归档]") == 3

@pytest.mark.asyncio
async def test_archive_query_time_and_resonance_filters(server):
    near = await make(server, "near", archived=True)
    far = await make(server, "far", archived=True)
    old = await make(server, "old", archived=True)
    today = server.datetime.now().date().isoformat()
    for identifier, day, valence in [(near, today, 0.9), (far, today, 0.1),
                                     (old, "2000-01-01", 0.9)]:
        bucket = await server.bucket_mgr.get(identifier)
        path = Path(bucket["path"])
        post = frontmatter.load(path)
        post["updated_at"] = day
        post["valence"] = valence
        post["arousal"] = 0.3
        path.write_text(frontmatter.dumps(post), encoding="utf-8")
    for filters in [dict(recent_days=0), dict(date_from=today, date_to=today)]:
        result = await server.breath(query="needle", include_archive=True,
                                    resonance="0.9,0.3", touch=False, **filters)
        assert ids(result) == [near, far]
        assert old not in result
        assert result.count("[归档]") == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("arguments", [
    {}, {"query": " "}, {"domain": "session", "query": "needle"},
    {"topic_filter": ["topic"], "query": "needle"}, {"domain": "feel", "query": "needle"},
    {"feels": True, "query": "needle"}, {"mailbox": True},
    {"as_of": "2026-10-01", "query": "needle"}, {"resonance": "0.5,0.5"},
    {"importance_min": 7}, {"tags_filter": ["selected"]},
])
async def test_nonordinary_none_and_false_preserve_defaults(server, arguments):
    await make(server, "active")
    archived = await make(server, "archive", archived=True)
    for value in (None, False):
        request = server._prepare_breath_request(**arguments, include_archive=value)
        assert request["include_archive"] is False
    assert server._prepare_breath_request(**arguments)["include_archive"] is False
    # Mailbox has its own touch default restriction.
    options = arguments if arguments.get("mailbox") else dict(arguments, touch=False)
    default = await server.breath(**options)
    assert "breath mode=" not in default
    assert default == await server.breath(**options, include_archive=None)
    assert default == await server.breath(**options, include_archive=False)
    assert archived not in default


@pytest.mark.asyncio
@pytest.mark.parametrize("initial", ["omitted", None, True])
async def test_cursor_default_none_and_true_are_equivalent(server, initial):
    identifiers = [await make(server, f"equiv{i}", archived=True) for i in range(3)]
    arguments = dict(query="needle", touch=False, max_results=1)
    first_options = {} if initial == "omitted" else {"include_archive": initial}
    first = await server.breath(**arguments, **first_options)
    token = cursor(first)
    assert "不匹配" in await server.breath(**arguments, include_archive=False, cursor=token)
    second = await server.breath(**arguments, include_archive=None, cursor=token)
    third = await server.breath(**arguments, include_archive=True, cursor=cursor(second))
    assert set(ids(first) + ids(second) + ids(third)) == set(identifiers)


@pytest.mark.asyncio
async def test_default_cursor_restore_includes_new_archive_but_hides_new_seal(server):
    for i in range(4):
        await make(server, f"restore{i}")
    arguments = dict(query="needle", touch=False, max_results=1)
    first = await server.breath(**arguments)
    token = cursor(first)
    pending = server._BREATH_CURSOR_STATES[token]["matches"]
    archived, sealed = pending[1]["id"], pending[2]["id"]
    assert await server.bucket_mgr.archive(archived)
    bucket = await server.bucket_mgr.get(sealed)
    post = frontmatter.load(bucket["path"])
    post["sealed"] = True
    Path(bucket["path"]).write_text(frontmatter.dumps(post), encoding="utf-8")
    rest = await server.breath(**{**arguments, "max_results": 5}, include_archive=True, cursor=token)
    assert archived in ids(rest) and "[归档]" in rest
    assert sealed not in ids(rest)
    assert len(ids(rest)) == 2


@pytest.mark.asyncio
async def test_internal_default_uses_resolved_archive_candidates(server):
    archived = await make(server, "internal", archived=True)
    for tags in (None, ["selected"]):
        result = await server._breath_impl(query="needle", tags_filter=tags, touch=False)
        assert ids(result) == [archived]


@pytest.mark.asyncio
async def test_no_query_keeps_active_pinned_and_excludes_archive_pinned(server):
    active = await server.bucket_mgr.create(content="needle active pinned", name="active pinned",
                                            pinned=True, importance=8)
    archived = await make(server, "archive pinned", archived=True)
    bucket = await server.bucket_mgr.get(archived)
    post = frontmatter.load(bucket["path"])
    post["pinned"] = True
    Path(bucket["path"]).write_text(frontmatter.dumps(post), encoding="utf-8")
    for options in ({}, {"include_archive": None}, {"include_archive": False}):
        result = await server.breath(touch=False, **options)
        assert active in result
        assert archived not in result


@pytest.mark.asyncio
@pytest.mark.parametrize("initial", ["omitted", None, False])
async def test_historical_cursor_none_and_false_are_equivalent(server, initial):
    identifiers = [await make(server, f"historical{i}") for i in range(3)]
    arguments = dict(query="needle", as_of="2099-01-01", touch=False, max_results=1)
    first_options = {} if initial == "omitted" else {"include_archive": initial}
    first = await server.breath(**arguments, **first_options)
    token = cursor(first)
    invalid = await server.breath(**arguments, include_archive=True, cursor=token)
    assert "include_archive" in invalid and "ordinary query" in invalid
    second = await server.breath(**arguments, include_archive=None, cursor=token)
    third = await server.breath(**arguments, include_archive=False, cursor=cursor(second))
    assert set(ids(first) + ids(second) + ids(third)) == set(identifiers)

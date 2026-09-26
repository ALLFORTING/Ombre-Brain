import importlib
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from bucket_manager import BucketManager


def _bucket(bucket_id, content="memory", **metadata):
    return {
        "id": bucket_id,
        "content": content,
        "metadata": {
            "name": metadata.pop("name", bucket_id),
            "domain": metadata.pop("domain", ["test"]),
            "type": metadata.pop("type", "dynamic"),
            "importance": metadata.pop("importance", 5),
            "valence": metadata.pop("valence", 0.5),
            "arousal": metadata.pop("arousal", 0.3),
            **metadata,
        },
    }


def _fixed_scores(manager, *, topic=0.6, exact=0.0, emotion=0.6, time=0.6):
    manager._calc_topic_score = MagicMock(return_value=topic)
    manager._calc_exact_match_score = MagicMock(return_value=exact)
    manager._calc_emotion_score = MagicMock(return_value=emotion)
    manager._calc_time_score = MagicMock(return_value=time)


@pytest.mark.asyncio
async def test_trace_admits_resolved_before_ranking_penalty(test_config):
    manager = BucketManager(test_config)
    _fixed_scores(manager)
    trace = {}

    results = await manager.search(
        "resolved query",
        candidate_buckets=[_bucket("resolved", resolved=True, importance=6)],
        trace=trace,
    )

    entry = trace["candidates"][0]
    assert results[0]["id"] == "resolved"
    assert entry["pre_penalty_score"] == 60.0
    assert entry["threshold"] == 50
    assert entry["admitted"] is True
    assert entry["ranking_penalty"] == 0.3
    assert entry["final_ranking_score"] == 18.0
    assert entry["final_ranking_score"] < entry["threshold"]


@pytest.mark.asyncio
async def test_trace_reports_semantic_hit_and_hybrid_state(test_config):
    embedding = MagicMock(enabled=True, last_error="")
    embedding.search_similar = AsyncMock(return_value=[("semantic", 0.91)])
    manager = BucketManager(test_config, embedding_engine=embedding)
    _fixed_scores(manager, topic=0.05, emotion=0.1, time=0.1)
    trace = {}

    results = await manager.search(
        "weak lexical query",
        candidate_buckets=[_bucket("semantic", importance=1)],
        trace=trace,
    )

    entry = trace["candidates"][0]
    assert results[0]["id"] == "semantic"
    assert trace["semantic"]["status"] == "available"
    assert entry["scores"]["semantic"] == 0.91
    assert entry["semantic_threshold"] is True
    assert entry["admitted"] is True


@pytest.mark.asyncio
async def test_trace_reports_exact_match_tier(test_config):
    manager = BucketManager(test_config)
    _fixed_scores(manager, topic=1.0, exact=1.0)
    trace = {}

    await manager.search(
        "exact query",
        candidate_buckets=[_bucket("exact")],
        trace=trace,
    )

    entry = trace["candidates"][0]
    assert entry["scores"]["exact_match"] == 1.0
    assert entry["match_tier"] == 3


@pytest.mark.asyncio
async def test_trace_distinguishes_disabled_and_provider_error(test_config):
    disabled = BucketManager(test_config)
    disabled_trace = {}
    await disabled.search(
        "query",
        candidate_buckets=[_bucket("disabled")],
        trace=disabled_trace,
    )
    assert disabled_trace["semantic"] == {"enabled": False, "status": "disabled"}

    failing_embedding = MagicMock(enabled=True, last_error="")

    async def fail_search(*args, **kwargs):
        failing_embedding.last_error = "embedding_provider_error"
        return []

    failing_embedding.search_similar = AsyncMock(side_effect=fail_search)
    failing = BucketManager(test_config, embedding_engine=failing_embedding)
    failing_trace = {}
    await failing.search(
        "query",
        candidate_buckets=[_bucket("provider-error")],
        trace=failing_trace,
    )
    assert failing_trace["semantic"]["status"] == "provider_error"
    assert failing_trace["semantic"]["error_code"] == "embedding_provider_error"


def _load_server(tmp_path, monkeypatch):
    monkeypatch.setenv("OMBRE_BUCKETS_DIR", str(tmp_path / "buckets"))
    monkeypatch.delenv("OMBRE_API_KEY", raising=False)
    monkeypatch.setenv("OMBRE_RESPONSE_SEAL", "test-seal-filters")
    sys.modules.pop("server", None)
    server = importlib.import_module("server")
    server._require_auth = lambda request: None
    server.decay_engine.ensure_started = AsyncMock(return_value=None)
    server.dehydrator.dehydrate = AsyncMock(
        side_effect=lambda content, metadata=None, **kwargs: content[:120]
    )
    return server


def _debug_client(server):
    app = Starlette(routes=[
        Route("/api/breath-debug", server.api_breath_debug, methods=["GET"]),
    ])
    return TestClient(app)


@pytest.mark.asyncio
async def test_debug_excludes_sealed_and_dormant_metadata(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    sealed_id = await server.bucket_mgr.create(
        content="sealed-debug-sentinel",
        name="Sealed Debug Sentinel",
        domain=["sealed-domain"],
    )
    dormant_id = await server.bucket_mgr.create(
        content="dormant-debug-sentinel",
        name="Dormant Debug Sentinel",
    )
    await server.trace(sealed_id, sealed=1)
    await server.trace(dormant_id, dormant=1)

    response = _debug_client(server).get(
        "/api/breath-debug",
        params={"q": "debug-sentinel"},
    )
    payload_text = response.text

    assert response.status_code == 200
    assert "sealed-debug-sentinel" not in payload_text
    assert "Sealed Debug Sentinel" not in payload_text
    assert "sealed-domain" not in payload_text
    assert "dormant-debug-sentinel" not in payload_text
    assert "Dormant Debug Sentinel" not in payload_text


@pytest.mark.asyncio
async def test_debug_reuses_structured_tag_filter_and_preserves_response_fields(
    tmp_path, monkeypatch
):
    server = _load_server(tmp_path, monkeypatch)
    wanted_id = await server.bucket_mgr.create(
        content="wanted diagnostic memory",
        tags=["wanted"],
    )
    excluded_id = await server.bucket_mgr.create(
        content="excluded diagnostic memory",
        tags=["other"],
    )

    response = _debug_client(server).get(
        "/api/breath-debug",
        params={"q": "diagnostic memory", "tags": "wanted"},
    )
    payload = response.json()
    result_ids = {item["id"] for item in payload["results"]}

    assert response.status_code == 200
    assert payload["equivalence"] == "runtime_query_trace"
    assert payload["candidate_source"] == "structured_filtered_active_buckets"
    assert wanted_id in result_ids
    assert excluded_id not in result_ids
    assert {"weights", "threshold", "results", "passed_count"} <= payload.keys()


@pytest.mark.asyncio
async def test_debug_does_not_touch_memory_state(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    bucket_id = await server.bucket_mgr.create(
        content="side effect diagnostic memory",
        name="Side Effect Diagnostic",
    )
    before = await server.bucket_mgr.get(bucket_id)
    server.bucket_mgr.touch = AsyncMock(side_effect=AssertionError("debug touched memory"))

    response = _debug_client(server).get(
        "/api/breath-debug",
        params={"q": "side effect diagnostic"},
    )
    after = await server.bucket_mgr.get(bucket_id)

    assert response.status_code == 200
    assert server.bucket_mgr.touch.await_count == 0
    assert before["metadata"] == after["metadata"]


@pytest.mark.asyncio
async def test_normal_query_breath_touches_selected_bucket_once(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    bucket_id = await server.bucket_mgr.create(
        content="normal query activation diagnostic memory",
        name="Normal Query Activation",
    )
    before = await server.bucket_mgr.get(bucket_id)
    real_touch = server.bucket_mgr.touch
    server.bucket_mgr.touch = AsyncMock(side_effect=real_touch)

    response = await server.breath(
        query="normal query activation diagnostic",
        max_results=1,
    )
    after = await server.bucket_mgr.get(bucket_id)

    assert bucket_id in response
    assert server.bucket_mgr.touch.await_count == 1
    assert server.bucket_mgr.touch.await_args.args[0] == bucket_id
    assert after["metadata"]["activation_count"] == (
        before["metadata"]["activation_count"] + 1
    )


@pytest.mark.asyncio
async def test_debug_explains_token_budget_omission(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    bucket_id = await server.bucket_mgr.create(
        content="budget diagnostic memory",
        name="Budget Diagnostic",
    )
    server.dehydrator.dehydrate = AsyncMock(
        side_effect=lambda content, metadata=None, **kwargs: "x" * 500
    )

    response = _debug_client(server).get(
        "/api/breath-debug",
        params={"q": "budget diagnostic", "max_tokens": "1"},
    )
    payload = response.json()
    entry = next(item for item in payload["results"] if item["id"] == bucket_id)

    assert response.status_code == 200
    assert payload["final_composition"]["surfaced_count"] == 0
    assert entry["final_decision"] == "omitted_token_budget"


def test_dashboard_renders_runtime_trace_fields():
    dashboard = open("dashboard.html", encoding="utf-8").read()
    assert "runtime_query_trace" in dashboard or "Runtime Breath trace" in dashboard
    assert "final_decision" in dashboard
    assert "semantic.status" in dashboard


@pytest.mark.asyncio
@pytest.mark.parametrize("semantic", [0.0, 0.9])
@pytest.mark.parametrize(
    "superseded_by,resolved,penalty",
    [("successor", False, 0.1), ("none", False, 0.1), ("successor", True, 0.03)],
)
async def test_exact_name_priority_preserves_scores_and_penalties(
    test_config, semantic, superseded_by, resolved, penalty
):
    query = "W3 Complete Name"
    embedding = MagicMock(enabled=True, last_error="") if semantic else None
    if embedding:
        embedding.search_similar = AsyncMock(return_value=[("old", semantic)])
    manager = BucketManager(test_config, embedding_engine=embedding)
    manager._calc_time_score = MagicMock(return_value=1.0)
    target = _bucket("old", name=query, superseded_by=superseded_by, resolved=resolved)
    peers = [_bucket(f"body-{i}", content=f"prefix {query} suffix") for i in range(5)]
    peers.append(_bucket("tag", tags=[query]))
    trace = {}

    results = await manager.search(query, limit=1, candidate_buckets=[*peers, target], trace=trace)

    assert [bucket["id"] for bucket in results] == ["old"]
    entries = {entry["id"]: entry for entry in trace["candidates"]}
    entry = entries["old"]
    raw = (4 * 0.85 + 2 * 0.5 + 1.5 + 0.5) / 8.5 * 100
    hybrid = raw * 0.65 + semantic * 100 * 0.35 if semantic else raw
    assert entry["scores"] == {
        "fuzzy_lexical": 0.85, "exact_match": 0.85, "emotion": 0.5,
        "time": 1.0, "importance": 0.5, "semantic": semantic,
    }
    assert entry["pre_penalty_score"] == 75.29
    assert entry["threshold"] == 50
    assert entry["match_tier"] == results[0]["match_tier"] == 2
    assert entry["ranking_penalty"] == (0.3 if resolved else 1.0)
    assert entry["superseded_factor"] == 0.1
    assert entry["final_ranking_score"] == results[0]["score"] == round(hybrid * penalty, 2)
    assert entry["exact_name_match"] is results[0]["exact_name_match"] is True
    assert entry["admitted"] is True and entry["rank"] == 1
    assert entries["tag"]["match_tier"] == 3
    assert entries["tag"]["exact_name_match"] is False
    assert trace["ranking"] == ["old"]


@pytest.mark.asyncio
@pytest.mark.parametrize("tags", [[], ["Shared Name"]])
async def test_same_exact_names_keep_live_before_superseded(test_config, tags):
    manager = BucketManager(test_config)
    manager._calc_time_score = MagicMock(return_value=1.0)
    old = _bucket("old", name="Shared Name", tags=tags, superseded_by="live")
    live = _bucket("live", name="Shared Name", tags=tags)
    trace = {}

    results = await manager.search("Shared Name", candidate_buckets=[old, live], trace=trace)

    assert [bucket["id"] for bucket in results] == ["live", "old"]
    assert all(bucket["exact_name_match"] for bucket in results)
    assert all(bucket["match_tier"] == (3 if tags else 2) for bucket in results)
    raw_score = (4 * (1.0 if tags else 0.85) + 1.0 + 1.5 + 0.5) / 8.5 * 100
    assert results[0]["score"] == round(raw_score, 2)
    assert results[1]["score"] == round(raw_score * 0.1, 2)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "query,name",
    [(" Full Name ", "FULL\tNAME"), (" 中文 名称 ", "中文名称"),
     (" 雨 ", "雨"), ("婷易", "婷"), (" 婷 ", "婷易")],
)
async def test_exact_name_uses_existing_normalization(test_config, query, name):
    manager = BucketManager(test_config)
    manager._calc_time_score = MagicMock(return_value=1.0)
    target = _bucket("target", name=name, superseded_by="none")
    trace = {}

    results = await manager.search(query, candidate_buckets=[target], trace=trace)

    assert results[0]["exact_name_match"] is True
    assert trace["candidates"][0]["exact_name_match"] is True


@pytest.mark.asyncio
async def test_non_name_matches_do_not_gain_exact_name_priority(test_config):
    query = "Whole Name"
    manager = BucketManager(test_config)
    manager._calc_time_score = MagicMock(return_value=1.0)
    candidates = [
        _bucket("prefix", name=f"prefix {query}"),
        _bucket("suffix", name=f"{query} suffix"),
        _bucket("body", content=f"prefix {query} suffix"),
        _bucket("summary", summary=query),
        _bucket("tag", tags=[query]),
        _bucket("keyword", keywords=[query]),
    ]
    trace = {}

    results = await manager.search(query, candidate_buckets=candidates, trace=trace)

    assert len(results) == len(candidates)
    assert all(bucket["exact_name_match"] is False for bucket in results)
    entries = {entry["id"]: entry for entry in trace["candidates"]}
    assert all(entry["exact_name_match"] is False for entry in entries.values())
    assert [bucket["id"] for bucket in results[:2]] == ["tag", "keyword"]
    assert entries["tag"]["scores"]["exact_match"] == 1.0
    assert entries["tag"]["match_tier"] == 3
    assert all(entries[bid]["match_tier"] == 2 for bid in ("prefix", "suffix", "body", "summary"))


@pytest.mark.asyncio
@pytest.mark.parametrize("field,include_arg", [("sealed", "include_sealed"), ("dormant", "include_dormant")])
async def test_exact_name_does_not_bypass_visibility(test_config, field, include_arg):
    manager = BucketManager(test_config)
    manager._calc_time_score = MagicMock(return_value=1.0)
    target = _bucket("target", name="Hidden Name", superseded_by="none", **{field: True})
    peer = _bucket("peer", content="Hidden Name")
    manager.list_all = AsyncMock(return_value=[target, peer])
    trace = {}

    hidden = await manager.search("Hidden Name", limit=1, trace=trace)

    assert [bucket["id"] for bucket in hidden] == ["peer"]
    entry = next(entry for entry in trace["candidates"] if entry["id"] == "target")
    assert entry["eligible"] is False and entry["exclusion_reasons"] == [field]
    visible = await manager.search("Hidden Name", limit=1, **{include_arg: True})
    assert [bucket["id"] for bucket in visible] == ["target"]


@pytest.mark.asyncio
async def test_exact_name_does_not_bypass_domain_or_threshold(test_config):
    manager = BucketManager(test_config)
    manager._calc_time_score = MagicMock(return_value=1.0)
    target = _bucket("target", name="Scoped Name", domain=["outside"], superseded_by="none")
    peer = _bucket("peer", content="Scoped Name", domain=["inside"])
    manager.list_all = AsyncMock(return_value=[target, peer])
    trace = {}

    results = await manager.search("Scoped Name", domain_filter=["inside"], trace=trace)

    assert [bucket["id"] for bucket in results] == ["peer"]
    entry = next(entry for entry in trace["candidates"] if entry["id"] == "target")
    assert entry["eligible"] is False and entry["exclusion_reasons"] == ["domain_filter"]
    manager.fuzzy_threshold = 95
    rejected = await manager.search("Scoped Name", candidate_buckets=[target], trace=trace)
    assert rejected == []
    assert trace["candidates"][0]["exact_name_match"] is True
    assert trace["candidates"][0]["admitted"] is False
    assert trace["candidates"][0]["exclusion_reasons"] == ["threshold"]


@pytest.mark.asyncio
async def test_debug_exposes_exact_name_priority_with_original_penalty(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    server.bucket_mgr.embedding_engine = None
    server.bucket_mgr._calc_time_score = MagicMock(return_value=1.0)
    target_id = await server.bucket_mgr.create(content="obsolete body", name="Diagnostic Name")
    peer_id = await server.bucket_mgr.create(content="Diagnostic Name", name="Body Peer")
    await server.bucket_mgr.update(target_id, superseded_by="none")
    server.bucket_mgr.touch = AsyncMock(side_effect=AssertionError("debug touched memory"))

    response = _debug_client(server).get("/api/breath-debug", params={"q": "Diagnostic Name", "max_results": 1})

    assert response.status_code == 200
    payload = response.json()
    entries = {entry["id"]: entry for entry in payload["results"]}
    target = entries[target_id]
    assert target["exact_name_match"] is True
    assert entries[peer_id]["exact_name_match"] is False
    assert target["rank"] == 1 and target["final_decision"] == "surfaced"
    assert target["pre_penalty_score"] == 75.29
    assert target["match_tier"] == 2 and target["superseded_factor"] == 0.1
    assert target["final_ranking_score"] == 7.53
    server.bucket_mgr.touch.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("superseded_by,resolved", [("none", False), ("successor", True)])
async def test_ordinary_superseded_matches_keep_ranking_penalty(test_config, superseded_by, resolved):
    manager = BucketManager(test_config)
    manager._calc_time_score = MagicMock(return_value=1.0)
    old = _bucket("old", content="ordinary query", superseded_by=superseded_by, resolved=resolved)
    live = _bucket("live", content="ordinary query")
    trace = {}

    results = await manager.search("ordinary query", candidate_buckets=[old, live], trace=trace)

    assert [bucket["id"] for bucket in results] == ["live", "old"]
    assert all(bucket["exact_name_match"] is False for bucket in results)
    entries = {entry["id"]: entry for entry in trace["candidates"]}
    assert entries["old"]["superseded_factor"] == 0.1
    assert entries["old"]["ranking_penalty"] == (0.3 if resolved else 1.0)
    assert entries["old"]["pre_penalty_score"] == entries["live"]["pre_penalty_score"] == 75.29
    assert results[1]["score"] == (2.26 if resolved else 7.53)

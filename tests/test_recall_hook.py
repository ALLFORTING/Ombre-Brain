import hashlib
import importlib
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import frontmatter
import pytest
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

pytestmark = pytest.mark.security

HOOK_TOKEN = "R" * 32
AUTH = {"Authorization": f"Bearer {HOOK_TOKEN}"}
QUERY = "上次我们聊过的那件事"


def _load_server(tmp_path, monkeypatch, *, hook_token=HOOK_TOKEN):
    monkeypatch.setenv("OMBRE_BUCKETS_DIR", str(tmp_path / "buckets"))
    for name in (
        "OMBRE_DASHBOARD_PASSWORD", "OMBRE_HOOK_TOKEN", "OMBRE_AUTH_TOKEN",
        "OMBRE_RECALL_MIN_SCORE",
    ):
        monkeypatch.delenv(name, raising=False)
    if hook_token is not None:
        monkeypatch.setenv("OMBRE_HOOK_TOKEN", hook_token)
    sys.modules.pop("server", None)
    return importlib.import_module("server")


def _client(server):
    app = Starlette(routes=[Route("/recall-hook", server.recall_hook, methods=["POST"])])
    return TestClient(app)


def _bucket(bucket_id, content="正文 [[链接]] 内容", **metadata):
    return {"id": bucket_id, "content": content, "metadata": metadata}


def _install(server, buckets, scores, *, enabled=True, last_error=""):
    """Fake bucket list and embedding search; any write path fails the test."""
    forbidden = AsyncMock(side_effect=AssertionError("recall-hook must be read-only"))
    server.bucket_mgr = SimpleNamespace(
        list_all=AsyncMock(return_value=buckets),
        touch=forbidden,
        update=forbidden,
        create=forbidden,
    )
    engine = SimpleNamespace(enabled=enabled, last_error=last_error)

    async def search_similar(query, top_k=10, candidate_ids=None):
        pairs = [(bid, s) for bid, s in scores.items() if candidate_ids is None or bid in candidate_ids]
        pairs.sort(key=lambda item: item[1], reverse=True)
        return pairs[:top_k]

    engine.search_similar = AsyncMock(side_effect=search_similar)
    server.embedding_engine = engine
    server._fire_webhook = AsyncMock()
    server.decay_engine = SimpleNamespace(
        ensure_started=AsyncMock(side_effect=AssertionError("decay must not start")),
        calculate_score=lambda metadata: 1.0,
    )
    return engine


def _post(server, body, headers=AUTH):
    return _client(server).post("/recall-hook", json=body, headers=headers)


def test_unconfigured_returns_503_without_reading_memory(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch, hook_token=None)
    server.bucket_mgr = SimpleNamespace(list_all=AsyncMock(side_effect=AssertionError("no read")))
    response = _post(server, {"query": QUERY})
    assert response.status_code == 503
    assert response.json() == {"error": "hook_not_configured"}


@pytest.mark.parametrize(
    "headers",
    [{}, {"Authorization": "Bearer wrong"}, {"Authorization": "Basic abc"}],
)
def test_bad_auth_returns_401_without_reading_memory(tmp_path, monkeypatch, headers):
    server = _load_server(tmp_path, monkeypatch)
    server.bucket_mgr = SimpleNamespace(list_all=AsyncMock(side_effect=AssertionError("no read")))
    response = _post(server, {"query": QUERY}, headers)
    assert response.status_code == 401
    assert response.json() == {"error": "hook_unauthorized"}


@pytest.mark.parametrize("query", ["", "好的", "哈 哈！", "。。。。。。", "a b c", None, 123])
def test_short_query_returns_empty_without_searching(tmp_path, monkeypatch, query):
    server = _load_server(tmp_path, monkeypatch)
    engine = _install(server, [_bucket("a")], {"a": 0.9})
    response = _post(server, {"query": query})
    assert response.status_code == 200
    assert response.json() == {"items": [], "reason": "query_too_short"}
    engine.search_similar.assert_not_awaited()


def test_four_chars_after_stripping_punctuation_is_searched(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    _install(server, [_bucket("a", created="2026-10-01T08:00:00")], {"a": 0.9})
    body = _post(server, {"query": "你，好！吗。啊"}).json()
    assert [item["bucket_id"] for item in body["items"]] == ["a"]


@pytest.mark.parametrize(
    "metadata",
    [
        {"sealed": 1},
        {"superseded_by": "other"},
        {"superseded_by": "none"},
        {"resolved": True},
        {"dormant": True},
        {"type": "feel"},
        {"pinned": True},
        {"protected": True},
        {"tags": ["test"]},
    ],
)
def test_excluded_buckets_never_returned_even_with_top_score(tmp_path, monkeypatch, metadata):
    server = _load_server(tmp_path, monkeypatch)
    engine = _install(
        server,
        [_bucket("bad", **metadata), _bucket("good", created="2026-10-02T00:00:00")],
        {"bad": 0.99, "good": 0.8},
    )
    body = _post(server, {"query": QUERY, "debug": True}).json()
    assert [item["bucket_id"] for item in body["items"]] == ["good"]
    assert "bad" not in {c["bucket_id"] for c in body["candidates"]}
    assert "bad" not in engine.search_similar.await_args.kwargs["candidate_ids"]


def test_similar_but_not_exact_test_tag_is_kept(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    _install(server, [_bucket("a", tags=["testing", "latest"])], {"a": 0.9})
    assert [i["bucket_id"] for i in _post(server, {"query": QUERY}).json()["items"]] == ["a"]


def test_archive_is_not_listed(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    _install(server, [_bucket("a")], {"a": 0.9})
    _post(server, {"query": QUERY})
    server.bucket_mgr.list_all.assert_awaited_once_with(include_archive=False)


def test_all_excluded_returns_no_candidates(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    engine = _install(server, [_bucket("a", sealed=1)], {"a": 0.9})
    assert _post(server, {"query": QUERY}).json() == {"items": [], "reason": "no_candidates"}
    engine.search_similar.assert_not_awaited()


def _ids(server, scores, **body):
    buckets = [_bucket(bid) for bid in scores]
    _install(server, buckets, scores)
    return _post(server, {"query": QUERY, **body}).json()


def test_zero_pass_returns_below_threshold(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    body = _ids(server, {"a": 0.64, "b": 0.3})
    assert body == {"items": [], "reason": "below_threshold"}


def test_one_pass_returns_one(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    body = _ids(server, {"a": 0.90, "b": 0.60})
    assert [i["bucket_id"] for i in body["items"]] == ["a"]


def test_two_pass_returns_two(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    body = _ids(server, {"a": 0.90, "b": 0.70, "c": 0.2})
    assert [i["bucket_id"] for i in body["items"]] == ["a", "b"]


def test_three_or_more_pass_takes_highest_two(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    body = _ids(server, {"c": 0.70, "a": 0.95, "d": 0.66, "b": 0.80})
    assert [i["bucket_id"] for i in body["items"]] == ["a", "b"]


def test_close_scores_both_returned(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    body = _ids(server, {"a": 0.80, "b": 0.78})
    assert [i["bucket_id"] for i in body["items"]] == ["a", "b"]
    assert body["reason"] == ""


def test_score_exactly_at_threshold_passes(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    body = _ids(server, {"a": 0.65})
    assert [i["bucket_id"] for i in body["items"]] == ["a"]


def test_max_items_one_returns_one_and_cap_is_two(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    scores = {"a": 0.95, "b": 0.70, "c": 0.68}
    assert len(_ids(server, scores, max_items=1)["items"]) == 1
    assert len(_ids(server, scores, max_items=9)["items"]) == 2
    assert len(_ids(server, scores, max_items="x")["items"]) == 2


def test_env_overrides_threshold(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    monkeypatch.setenv("OMBRE_RECALL_MIN_SCORE", "0.5")
    assert [i["bucket_id"] for i in _ids(server, {"a": 0.55, "b": 0.54})["items"]] == ["a", "b"]
    monkeypatch.setenv("OMBRE_RECALL_MIN_SCORE", "bogus")
    assert _ids(server, {"a": 0.55})["reason"] == "below_threshold"


def test_item_shape_snippet_and_date(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    long_body = "[[婷]]" + "字" * 500
    _install(
        server,
        [_bucket("a", long_body, name="那天的事", created="2026-09-11T00:30:00"),
         _bucket("b", "x", updated_at="2026-10-01")],
        {"a": 0.9, "b": 0.1},
    )
    body = _post(server, {"query": QUERY}).json()
    assert body["reason"] == ""
    assert "candidates" not in body
    item = body["items"][0]
    assert set(item) == {"bucket_id", "name", "date", "score", "snippet"}
    assert item["name"] == "那天的事"
    assert item["date"] == "2026-09-11"
    assert item["score"] == 0.9
    assert item["snippet"].startswith("婷") and "[[" not in item["snippet"]
    assert len(item["snippet"]) == 300


def test_debug_returns_top5_names_and_scores_only(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    ids = [f"b{i}" for i in range(7)]
    _install(
        server,
        [_bucket(i, f"秘密正文{i}", name=f"名{i}") for i in ids],
        {bid: 0.5 - n * 0.01 for n, bid in enumerate(ids)},
    )
    response = _post(server, {"query": QUERY, "debug": True})
    body = response.json()
    assert body["reason"] == "below_threshold"
    assert [c["bucket_id"] for c in body["candidates"]] == ids[:5]
    assert all(set(c) == {"bucket_id", "name", "score"} for c in body["candidates"])
    assert "秘密正文" not in response.text


def test_embedding_unavailable_cases(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    _install(server, [_bucket("a")], {"a": 0.9}, enabled=False)
    assert _post(server, {"query": QUERY}).json() == {"items": [], "reason": "embedding_unavailable"}

    engine = _install(server, [_bucket("a")], {"a": 0.9})

    async def failing(query, top_k=10, candidate_ids=None):
        engine.last_error = "embedding_timeout"
        return []

    engine.search_similar = AsyncMock(side_effect=failing)
    assert _post(server, {"query": QUERY}).json() == {"items": [], "reason": "embedding_unavailable"}


def test_errors_return_200_empty_without_leaking_details(tmp_path, monkeypatch, caplog):
    server = _load_server(tmp_path, monkeypatch)
    engine = _install(server, [_bucket("a")], {"a": 0.9})
    engine.search_similar = AsyncMock(side_effect=RuntimeError("secret-detail"))
    with caplog.at_level("WARNING"):
        response = _post(server, {"query": QUERY, "debug": True})
    assert response.status_code == 200
    assert response.json() == {"items": [], "reason": "error", "candidates": []}
    assert "secret-detail" not in response.text
    assert any("Recall hook failed" in r.message for r in caplog.records)

    server.bucket_mgr.list_all = AsyncMock(side_effect=OSError("disk"))
    assert _post(server, {"query": QUERY}).json() == {"items": [], "reason": "error"}
    bad = _client(server).post("/recall-hook", content=b"not json", headers=AUTH)
    assert bad.status_code == 200 and bad.json() == {"items": [], "reason": "error"}


def _snapshot(root: Path):
    return {
        str(path.relative_to(root)): (hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns)
        for path in sorted(root.rglob("*")) if path.is_file()
    }


def test_real_bucket_files_and_metadata_unchanged(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    root = Path(tmp_path / "buckets")
    folder = root / "dynamic" / "日常"
    folder.mkdir(parents=True)
    for bid, extra in (("aaa", {}), ("bbb", {"resolved": True})):
        post = frontmatter.Post(
            "真实正文 [[链接]]",
            id=bid, name=f"桶{bid}", type="dynamic", created="2026-10-01T10:00:00",
            activation_count=3, last_active="2026-10-01T10:00:00", **extra,
        )
        (folder / f"{bid}.md").write_text(frontmatter.dumps(post), encoding="utf-8")

    engine = SimpleNamespace(enabled=True, last_error="")

    async def search_similar(query, top_k=10, candidate_ids=None):
        return [(b, 0.9 - n * 0.2) for n, b in enumerate(sorted(candidate_ids))][:top_k]

    engine.search_similar = AsyncMock(side_effect=search_similar)
    server.embedding_engine = engine
    server._fire_webhook = AsyncMock()
    forbidden = AsyncMock(side_effect=AssertionError("write path used"))
    real = server.bucket_mgr
    for name in ("touch", "update", "create"):
        monkeypatch.setattr(real, name, forbidden, raising=False)
    monkeypatch.setattr(
        server.decay_engine, "ensure_started", forbidden, raising=False
    )

    before = _snapshot(root)
    body = _post(server, {"query": QUERY, "debug": True}).json()
    after = _snapshot(root)

    assert [i["bucket_id"] for i in body["items"]] == ["aaa"]
    assert before == after
    meta = frontmatter.load(folder / "aaa.md").metadata
    assert meta["activation_count"] == 3 and meta["last_active"] == "2026-10-01T10:00:00"
    forbidden.assert_not_awaited()
    server._fire_webhook.assert_not_awaited()

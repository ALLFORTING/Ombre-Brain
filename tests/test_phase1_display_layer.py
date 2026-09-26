import importlib
import re
import sys
from unittest.mock import AsyncMock

import pytest


def _load_server(tmp_path, monkeypatch):
    monkeypatch.setenv("OMBRE_BUCKETS_DIR", str(tmp_path / "buckets"))
    monkeypatch.delenv("OMBRE_API_KEY", raising=False)
    monkeypatch.setenv("OMBRE_RESPONSE_SEAL", "phase1-display-seal")
    monkeypatch.delenv("OMBRE_BREATH_MIN_SCORE", raising=False)
    sys.modules.pop("server", None)
    server = importlib.import_module("server")
    server.decay_engine.ensure_started = AsyncMock(return_value=None)
    server.bucket_mgr.touch = AsyncMock(return_value=True)
    server.dehydrator.dehydrate = AsyncMock(
        side_effect=lambda content, metadata=None, **kwargs: content
    )
    return server


def _bucket(bucket_id, content, *, name=None, pinned=False, protected=False, score=42.0):
    return {
        "id": bucket_id,
        "content": content,
        "score": score,
        "metadata": {
            "name": name or bucket_id,
            "pinned": pinned,
            "protected": protected,
            "created_at": "2026-09-15T00:00:00",
            "updated_at": "2026-09-15T00:00:00",
            "tags": [],
        },
    }


def _search_with_scores(buckets, scores):
    async def search(*args, trace=None, **kwargs):
        if trace is not None:
            trace["candidates"] = [
                {"id": bucket["id"], "scores": scores[bucket["id"]]}
                for bucket in buckets
            ]
        return buckets

    return search


def _next_cursor(result):
    match = re.search(r"^下一页 cursor: (\S+)$", result, re.MULTILINE)
    return match.group(1) if match else ""


@pytest.mark.asyncio
async def test_breath_body_substring_is_not_exact_and_shows_retrieval_score(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    bucket = _bucket("anchor", "only 琥珀风筝2309 appears here", name="anchor memory", score=37.25)
    server.bucket_mgr.search = AsyncMock(
        side_effect=_search_with_scores(
            [bucket], {"anchor": {"fuzzy_lexical": 0.31, "semantic": 0.72}}
        )
    )

    result = await server.breath(query=" 琥珀 风筝2309 ", min_score=0.90)

    assert "[检索分=37.25]" in result
    assert "[通道:双]" in result
    assert "[通道:精确]" not in result
    assert "--- 弱匹配（仅列名） ---" not in result


@pytest.mark.asyncio
async def test_breath_shows_the_search_result_score(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    bucket_id = await server.bucket_mgr.create("unique retrieval score marker")
    matches = await server.bucket_mgr.search("unique retrieval score marker")
    expected = next(bucket["score"] for bucket in matches if bucket["id"] == bucket_id)

    result = await server.breath(query="unique retrieval score marker", touch=False)

    assert f"[检索分={expected:.2f}]" in result


@pytest.mark.asyncio
async def test_breath_exact_channel_requires_whole_name_or_tag(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    name = _bucket("name", "unrelated body", name="琥珀 风筝", score=34.5)
    tag = _bucket("tag", "unrelated body", score=29.25)
    tag["metadata"]["tags"] = ["琥珀风筝"]
    server.bucket_mgr.search = AsyncMock(
        side_effect=_search_with_scores(
            [name, tag],
            {
                "name": {"fuzzy_lexical": 0.31, "semantic": 0.0},
                "tag": {"fuzzy_lexical": 0.31, "semantic": 0.0},
            },
        )
    )

    result = await server.breath(query=" 琥珀 风筝 ")

    assert result.count("[通道:精确]") == 2
    assert "[检索分=34.50]" in result
    assert "[检索分=29.25]" in result


@pytest.mark.asyncio
async def test_breath_weak_matches_have_no_body_and_do_not_consume_budget(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    strong = _bucket("strong", "strong body", name="Strong")
    weak = _bucket("weak", "weak body must stay hidden", name="Weak", score=17.25)
    server.bucket_mgr.search = AsyncMock(
        side_effect=_search_with_scores(
            [strong, weak],
            {
                "strong": {"fuzzy_lexical": 0.80, "semantic": 0.0},
                "weak": {"fuzzy_lexical": 0.30, "semantic": 0.0},
            },
        )
    )

    result = await server.breath(query="not an exact anchor", min_score=0.45)

    assert "strong body" in result
    assert "--- 弱匹配（仅列名） ---" in result
    assert "[bucket_id:weak] 💭 Weak 检索分=17.25" in result
    assert "weak body must stay hidden" not in result
    assert "因低于阈值降级 1" in result
    assert server.dehydrator.dehydrate.await_count == 1


@pytest.mark.asyncio
async def test_breath_cursor_freezes_scores_and_weak_group(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    strong = _bucket("strong", "strong body", name="Strong")
    weak = _bucket("weak", "weak body", name="Weak", score=17.25)
    by_id = {"strong": strong, "weak": weak}
    server.bucket_mgr.search = AsyncMock(
        side_effect=_search_with_scores(
            [strong, weak],
            {
                "strong": {"fuzzy_lexical": 0.80, "semantic": 0.0},
                "weak": {"fuzzy_lexical": 0.20, "semantic": 0.0},
            },
        )
    )
    server.bucket_mgr.get = AsyncMock(side_effect=lambda bucket_id: by_id.get(bucket_id))

    first = await server.breath(query="not an exact anchor", max_results=1, min_score=0.45)
    cursor = _next_cursor(first)
    by_id["weak"] = _bucket("weak", "not an exact anchor now appears here", name="Weak", score=90.0)
    second = await server.breath(
        query="not an exact anchor", max_results=1, min_score=0.45, cursor=cursor
    )

    assert cursor
    assert "[bucket_id:weak] 💭 Weak 检索分=17.25" in second
    assert "not an exact anchor now appears here" not in second
    assert server.bucket_mgr.search.await_count == 1


@pytest.mark.asyncio
async def test_legacy_cursor_keeps_page_but_does_not_relabel_old_sim(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    first_bucket = _bucket("first", "first body", score=40.0)
    second_bucket = _bucket("second", "anchor in second body", score=19.5)
    by_id = {"first": first_bucket, "second": second_bucket}
    server.bucket_mgr.search = AsyncMock(
        side_effect=_search_with_scores(
            [first_bucket, second_bucket],
            {
                "first": {"fuzzy_lexical": 0.7, "semantic": 0.0},
                "second": {"fuzzy_lexical": 0.6, "semantic": 0.0},
            },
        )
    )
    server.bucket_mgr.get = AsyncMock(side_effect=lambda bucket_id: by_id.get(bucket_id))

    first = await server.breath(query="anchor", max_results=1)
    cursor = _next_cursor(first)
    for record in server._BREATH_CURSOR_STATES[cursor]["matches"]:
        record.pop("retrieval_score")
        record.pop("vector_match")
        if record["id"] == "second":
            record["channel"] = "精确"  # legacy body-substring label
    second = await server.breath(query="anchor", max_results=1, cursor=cursor)

    assert "[bucket_id:second]" in second
    assert "[检索分=未记录]" in second
    assert "[检索分=0.60]" not in second
    assert "[通道:精确]" not in second
    assert "[通道:关键词]" in second
    assert server.bucket_mgr.search.await_count == 1


@pytest.mark.asyncio
async def test_cursor_reencoding_preserves_retrieval_display_and_weak_group(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    buckets = [_bucket(str(index), f"body {index}", score=40.0 - index) for index in range(4)]
    buckets[2]["vector_match"] = True
    by_id = {bucket["id"]: bucket for bucket in buckets}
    server.bucket_mgr.search = AsyncMock(
        side_effect=_search_with_scores(
            buckets,
            {
                "0": {"fuzzy_lexical": 0.9, "semantic": 0.0},
                "1": {"fuzzy_lexical": 0.8, "semantic": 0.0},
                "2": {"fuzzy_lexical": 0.7, "semantic": 0.8},
                "3": {"fuzzy_lexical": 0.2, "semantic": 0.0},
            },
        )
    )
    server.bucket_mgr.get = AsyncMock(side_effect=lambda bucket_id: by_id.get(bucket_id))

    first = await server.breath(query="anchor", max_results=1, min_score=0.45)
    second = await server.breath(query="anchor", max_results=1, min_score=0.45, cursor=_next_cursor(first))
    by_id["2"] = _bucket("2", "changed body", score=99.0)
    third = await server.breath(query="anchor", max_results=1, min_score=0.45, cursor=_next_cursor(second))
    fourth = await server.breath(query="anchor", max_results=1, min_score=0.45, cursor=_next_cursor(third))

    assert "[检索分=38.00]" in third
    assert "[通道:双]" in third and "[语义关联]" in third
    assert "弱匹配（仅列名）" in fourth
    assert "检索分=37.00" in fourth and "body 3" not in fourth
    assert server.bucket_mgr.search.await_count == 1


@pytest.mark.asyncio
async def test_breath_dual_channel_keeps_channel_but_shows_retrieval_score(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    bucket = _bucket("dual", "ordinary body")
    server.bucket_mgr.search = AsyncMock(
        side_effect=_search_with_scores(
            [bucket], {"dual": {"fuzzy_lexical": 0.41, "semantic": 0.83}}
        )
    )

    result = await server.breath(query="not an exact anchor")

    assert "[检索分=42.00]" in result
    assert "[通道:双]" in result


@pytest.mark.asyncio
async def test_breath_default_threshold_is_zero_and_explicit_value_overrides_env(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    bucket = _bucket("candidate", "ordinary body")
    server.bucket_mgr.search = AsyncMock(
        side_effect=_search_with_scores(
            [bucket], {"candidate": {"fuzzy_lexical": 0.20, "semantic": 0.0}}
        )
    )

    default_result = await server.breath(query="not an exact anchor")
    monkeypatch.setenv("OMBRE_BREATH_MIN_SCORE", "0.90")
    explicit_result = await server.breath(query="not an exact anchor", min_score=0.10)

    assert server._resolve_breath_min_score(-1) == 0.90
    assert "ordinary body" in default_result
    assert "弱匹配" not in default_result
    assert "ordinary body" in explicit_result
    assert "弱匹配" not in explicit_result


@pytest.mark.asyncio
async def test_breath_pin_marker_requires_pinned_true(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    protected = _bucket("protected", "protected body", protected=True)
    pinned = _bucket("pinned", "pinned body", pinned=True)
    server.bucket_mgr.search = AsyncMock(
        side_effect=_search_with_scores(
            [protected, pinned],
            {
                "protected": {"fuzzy_lexical": 0.70, "semantic": 0.0},
                "pinned": {"fuzzy_lexical": 0.70, "semantic": 0.0},
            },
        )
    )

    result = await server.breath(query="not an exact anchor")
    protected_line = next(line for line in result.splitlines() if "bucket_id:protected" in line)
    pinned_line = next(line for line in result.splitlines() if "bucket_id:pinned" in line)

    assert "📌" not in protected_line
    assert "📌" in pinned_line


@pytest.mark.asyncio
async def test_query_icons_and_dormant_marker_reuse_pulse_types(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    cases = [
        ({"type": "permanent"}, "📦"),
        ({"type": "archived"}, "🗄️"),
        ({"type": "feel"}, "🫧"),
        ({"resolved": True}, "✅"),
        ({"sealed": 1}, "🔒"),
        ({"dormant": True}, "💭"),
    ]
    for meta, icon in cases:
        bucket = _bucket("icon", "body")
        bucket["metadata"].update(meta)
        bucket["_breath_channel"] = "关键词"
        line = await server._format_breath_query_summary(bucket, "summary")
        assert f"[bucket_id:icon] {icon}" in line
        assert ("[休眠]" in line) == bool(meta.get("dormant"))


@pytest.mark.asyncio
async def test_importance_mode_shows_type_and_real_importance(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    permanent_id = await server.bucket_mgr.create(
        "permanent importance body", importance=9, bucket_type="permanent"
    )
    dynamic_id = await server.bucket_mgr.create(
        "dynamic importance body", importance=8, bucket_type="dynamic"
    )
    pinned_id = await server.bucket_mgr.create(
        "pinned importance body", importance=8, pinned=True
    )

    result = await server.breath(importance_min=8, touch=False)

    assert f"📦 [bucket_id:{permanent_id}]" in result
    assert f"💭 [bucket_id:{dynamic_id}]" in result
    assert f"📌 [bucket_id:{pinned_id}]" in result
    assert result.count("重要:9") == 1
    assert result.count("重要:8") == 1
    assert result.count("重要:10") == 1  # create() promotes pinned importance
    assert "权重:0.00" not in result


@pytest.mark.asyncio
async def test_filtered_breath_uses_same_score_and_weak_match_presentation(
    tmp_path, monkeypatch
):
    server = _load_server(tmp_path, monkeypatch)
    strong = _bucket("strong", "strong body", name="Strong")
    weak = _bucket("weak", "weak filtered body", name="Weak", pinned=True)
    server.bucket_mgr.list_all = AsyncMock(return_value=[strong, weak])
    server.bucket_mgr.search = AsyncMock(
        side_effect=_search_with_scores(
            [strong, weak],
            {
                "strong": {"fuzzy_lexical": 0.80, "semantic": 0.0},
                "weak": {"fuzzy_lexical": 0.20, "semantic": 0.0},
            },
        )
    )

    result = await server.breath(
        query="not an exact anchor", tags_filter=["tag"], min_score=0.45
    )

    assert "[检索分=42.00]" in result
    assert "[bucket_id:weak] 📌 Weak 检索分=42.00" in result
    assert "weak filtered body" not in result


@pytest.mark.asyncio
async def test_pulse_default_mode_honors_limit_and_show_all_remains_bounded(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    for index in range(8):
        await server.bucket_mgr.create(content=f"pulse limit marker {index}")

    default_result = await server.pulse(show_all=False, limit=5)
    show_all_result = await server.pulse(show_all=True, limit=3)

    default_lines = [line for line in default_result.splitlines() if "bucket_id:" in line]
    all_lines = [line for line in show_all_result.splitlines() if "bucket_id:" in line]
    assert len(default_lines) <= 5
    assert len(all_lines) == 3
    assert "limit=5" in default_result


@pytest.mark.asyncio
async def test_pulse_limit_boundary_values(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)

    assert "limit 必须大于等于 1" in await server.pulse(limit=0)
    assert "limit 和 offset 必须是整数" in await server.pulse(limit="invalid")

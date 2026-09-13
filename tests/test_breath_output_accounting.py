import importlib
import sys
from unittest.mock import AsyncMock

import pytest


def _load_server(tmp_path, monkeypatch):
    monkeypatch.setenv("OMBRE_BUCKETS_DIR", str(tmp_path / "buckets"))
    monkeypatch.delenv("OMBRE_API_KEY", raising=False)
    monkeypatch.setenv("OMBRE_RESPONSE_SEAL", "breath-accounting-seal")
    sys.modules.pop("server", None)
    server = importlib.import_module("server")
    server.decay_engine.ensure_started = AsyncMock(return_value=None)
    server.bucket_mgr.touch = AsyncMock(return_value=True)
    return server


def _bucket(index, *, vector_match=False):
    return {
        "id": f"breath-accounting-{index:02d}",
        "content": f"memory content {index}",
        "vector_match": vector_match,
        "metadata": {
            "name": f"memory-{index}",
            "created_at": "2026-09-12T00:00:00",
            "updated_at": "2026-09-12T00:00:00",
            "tags": [],
        },
    }


@pytest.mark.asyncio
async def test_breath_vector_result_uses_utf8_semantic_label(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    server.bucket_mgr.search = AsyncMock(return_value=[_bucket(1, vector_match=True)])
    server.dehydrator.dehydrate = AsyncMock(return_value="ordinary query summary")

    result = await server.breath(query="unique anchor")

    assert "[语义关联]" in result
    assert "璇箟鍏宠仈" not in result
    assert "杩樻湁" not in result


@pytest.mark.asyncio
async def test_breath_31_matches_displays_8_and_reports_23(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    server.bucket_mgr.search = AsyncMock(
        return_value=[_bucket(index) for index in range(31)]
    )
    server.dehydrator.dehydrate = AsyncMock(
        side_effect=lambda content, metadata=None: content
    )

    result = await server.breath(query="accounting anchor", max_results=8)

    assert "还有23个相关记忆未显示" in result
    assert (
        "共匹配 31 / 本次显示 8 / 因结果上限省略 23 / "
        "因 token 预算省略 0"
    ) in result
    assert "杩樻湁" not in result


@pytest.mark.asyncio
async def test_breath_counts_selected_items_omitted_by_token_budget(
    tmp_path,
    monkeypatch,
):
    server = _load_server(tmp_path, monkeypatch)
    server.bucket_mgr.search = AsyncMock(
        return_value=[_bucket(index) for index in range(4)]
    )
    summaries = iter(["short", "长" * 100, "unused third summary"])
    server.dehydrator.dehydrate = AsyncMock(
        side_effect=lambda content, metadata=None: next(summaries)
    )

    result = await server.breath(
        query="token budget anchor",
        max_results=3,
        max_tokens=5,
    )

    assert "还有3个相关记忆未显示" in result
    assert (
        "共匹配 4 / 本次显示 1 / 因结果上限省略 1 / "
        "因 token 预算省略 2"
    ) in result
    assert server.dehydrator.dehydrate.await_count == 2

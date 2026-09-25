import importlib
import sys
from unittest.mock import AsyncMock

import pytest


def _load_server(tmp_path, monkeypatch):
    monkeypatch.setenv("OMBRE_BUCKETS_DIR", str(tmp_path / "buckets"))
    monkeypatch.setenv("OMBRE_RESPONSE_SEAL", "test-seal")
    monkeypatch.delenv("OMBRE_API_KEY", raising=False)
    sys.modules.pop("server", None)
    server = importlib.import_module("server")
    server.decay_engine.ensure_started = AsyncMock(return_value=None)
    server.dehydrator.dehydrate = AsyncMock(
        side_effect=lambda content, metadata=None: content[:120]
    )
    return server


def test_resonance_distance_preserves_zero_and_defaults_only_none_or_missing(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    assert server._resonance_distance(
        {"metadata": {"valence": 0, "arousal": 0}}, (0, 0)
    ) == 0.0
    assert server._resonance_distance(
        {"metadata": {"valence": None, "arousal": None}}, (0.5, 0.3)
    ) == 0.0
    assert server._resonance_distance({"metadata": {}}, (0.5, 0.3)) == 0.0


@pytest.mark.asyncio
async def test_breath_resonance_zero_coordinates_rank_first(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    zero_id = await server.bucket_mgr.create(
        content="zero mood", valence=0, arousal=0
    )
    default_id = await server.bucket_mgr.create(
        content="default mood", valence=0.5, arousal=0.3
    )

    result = await server.breath(resonance="0,0", max_results=2)
    assert zero_id in result and default_id in result
    assert result.index(zero_id) < result.index(default_id)


@pytest.mark.asyncio
async def test_breath_resonance_sorts_by_emotion_distance(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    close_id = await server.bucket_mgr.create(
        content="close low valence high arousal",
        valence=0.2,
        arousal=0.7,
        importance=5,
    )
    far_id = await server.bucket_mgr.create(
        content="far happy calm",
        valence=0.9,
        arousal=0.1,
        importance=10,
    )

    result = await server.breath(resonance="0.2,0.7", max_results=2)

    assert close_id in result
    assert far_id in result
    assert result.index(close_id) < result.index(far_id)


@pytest.mark.asyncio
async def test_breath_query_then_resonance_reranks_matches(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    close_id = await server.bucket_mgr.create(
        content="needle conflict close mood",
        valence=0.21,
        arousal=0.69,
        importance=5,
    )
    far_id = await server.bucket_mgr.create(
        content="needle conflict far mood",
        valence=0.9,
        arousal=0.1,
        importance=9,
    )

    result = await server.breath(query="needle conflict", resonance="0.2,0.7", max_results=2)

    assert close_id in result
    assert far_id in result
    assert result.index(close_id) < result.index(far_id)


@pytest.mark.asyncio
async def test_invalid_resonance_rejected(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)

    result = await server.breath(resonance="bad")

    assert "resonance must use" in result

import importlib
import json
import sys
from unittest.mock import AsyncMock

import pytest


def _server(tmp_path, monkeypatch):
    monkeypatch.setenv("OMBRE_BUCKETS_DIR", str(tmp_path / "buckets"))
    monkeypatch.delenv("OMBRE_API_KEY", raising=False)
    sys.modules.pop("server", None)
    server = importlib.import_module("server")
    server.decay_engine.ensure_started = AsyncMock(return_value=None)
    return server


@pytest.mark.asyncio
async def test_sealed_archive_never_writes_snapshot_and_open_archive_has_source(
    tmp_path, monkeypatch
):
    server = _server(tmp_path, monkeypatch)
    sealed = await server.archive_session(
        "private session", valence=0.1, arousal=0.9, sealed=True
    )
    assert not server._load_emotion_timeline()
    visible = await server.archive_session(
        "ordinary session", valence=0.7, arousal=0.2
    )
    visible_id = visible.split("bucket_id:", 1)[1]
    entry = server._load_emotion_timeline()[0]
    assert entry["bucket_id"] == visible_id
    assert entry["source"] == "archive"
    assert sealed.split("bucket_id:", 1)[1] != visible_id


@pytest.mark.asyncio
async def test_known_sealed_entries_are_filtered_before_budget_and_legacy_is_readable(
    tmp_path, monkeypatch
):
    server = _server(tmp_path, monkeypatch)
    visible_id = await server.bucket_mgr.create("visible")
    sealed_id = await server.bucket_mgr.create("secret", sealed=True)
    records = [
        {"timestamp": "2026-01-01T00:00:00", "valence": 0.7, "arousal": 0.2,
         "source": "hold", "bucket_id": visible_id},
        {"timestamp": "2026-01-02T00:00:00", "valence": 0.123, "arousal": 0.987,
         "source": "archive", "bucket_id": sealed_id},
        {"timestamp": "2026-01-03T00:00:00", "valence": 0.5, "arousal": 0.5,
         "source": "hold"},
    ]
    path = server._emotion_timeline_path()
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(records, handle)
    result = server._with_emotion_timeline("body", True, 1000)
    assert visible_id in result
    assert sealed_id not in result
    assert "0.123" not in result
    assert '"timestamp":"2026-01-03T00:00:00"' in result
    constrained = server._with_emotion_timeline("body", True, 35)
    assert server.count_tokens_approx(constrained) <= 35
    assert sealed_id not in constrained
    assert "0.123" not in constrained
    with open(path, "w", encoding="utf-8") as handle:
        json.dump([records[0], records[2]], handle)
    assert server._with_emotion_timeline("body", True, 1000) == result
    assert server._with_emotion_timeline("body", True, 35) == constrained


@pytest.mark.asyncio
async def test_breath_emotion_timeline_uses_public_budget(tmp_path, monkeypatch):
    server = _server(tmp_path, monkeypatch)
    bucket_id = await server.bucket_mgr.create("visible")
    for _ in range(30):
        server._record_emotion_snapshot(0.6, 0.4, "hold", bucket_id)
    response = await server.breath(
        query="no such memory", touch=False, emotion_trend=True, max_tokens=120
    )
    assert server.count_tokens_approx(response.split("\n\nseal:", 1)[0]) <= 120
    assert "emotion_history_truncated: true" in response

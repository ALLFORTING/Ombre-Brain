import importlib
import sys
from unittest.mock import AsyncMock

import pytest


def _load_server(tmp_path, monkeypatch):
    monkeypatch.setenv("OMBRE_BUCKETS_DIR", str(tmp_path / "buckets"))
    monkeypatch.delenv("OMBRE_API_KEY", raising=False)
    sys.modules.pop("server", None)
    server = importlib.import_module("server")
    server.decay_engine.ensure_started = AsyncMock(return_value=None)
    server.dehydrator.analyze = AsyncMock(
        return_value={
            "domain": ["test"],
            "valence": 0.5,
            "arousal": 0.3,
            "tags": ["conflict-test"],
            "suggested_name": "conflict-test",
        }
    )
    server.dehydrator.digest = AsyncMock(
        return_value=[
            {
                "name": "digest-item",
                "content": "new item content",
                "domain": ["test"],
                "valence": 0.5,
                "arousal": 0.3,
                "tags": ["conflict-test"],
                "importance": 5,
            }
        ]
    )
    return server


@pytest.mark.asyncio
async def test_hold_appends_conflict_warning(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    server._detect_conflict_warning = AsyncMock(return_value="bucket abc has conflicting date")

    result = await server.hold("new conflicting content")

    assert "conflict: bucket abc has conflicting date" in result


@pytest.mark.asyncio
async def test_hold_preserves_return_when_no_conflict(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    server._detect_conflict_warning = AsyncMock(return_value="")

    result = await server.hold("ordinary content")

    assert "conflict:" not in result


@pytest.mark.asyncio
async def test_grow_short_path_appends_conflict_warning(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    server._detect_conflict_warning = AsyncMock(return_value="bucket xyz has conflicting number")

    result = await server.grow("short conflict")

    assert "conflict: bucket xyz has conflicting number" in result


@pytest.mark.asyncio
async def test_grow_digest_path_appends_conflict_warning(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    server._detect_conflict_warning = AsyncMock(return_value="bucket old has conflicting fact")

    result = await server.grow("this is a longer diary entry that should use the digest path")

    assert "conflict: digest-item: bucket old has conflicting fact" in result


@pytest.mark.asyncio
async def test_conflict_detection_uses_lexical_fallback_candidates(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    old_id = await server.bucket_mgr.create(
        content="codex_conflict_marker Alpha invoice due date is 2026-07-06.",
        pinned=True,
    )
    server.bucket_mgr.search = AsyncMock(return_value=[])
    captured = {}

    async def fake_call(new_content, old_buckets):
        captured["old_ids"] = [bucket["id"] for bucket in old_buckets]
        return "bucket has conflicting date"

    server._call_conflict_api = fake_call

    warning = await server._detect_conflict_warning(
        "codex_conflict_marker Alpha invoice due date is 2026-07-01."
    )

    assert warning == "bucket has conflicting date"
    assert old_id in captured["old_ids"]

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
async def test_hold_explicit_supersession_replaces_bucket_in_place(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    old_content = "fact_evolution_old_only_token: invoice due on July 6."
    replacement = "fact_evolution_new_only_token: invoice due on July 9."
    target_id = await server.bucket_mgr.create(content=old_content)
    before = await server.bucket_mgr.list_all(include_archive=False)
    server._detect_conflict_warning = AsyncMock(return_value="")

    result = await server.hold(replacement, supersedes_id=f"  {target_id}  ")

    after = await server.bucket_mgr.list_all(include_archive=False)
    target = await server.bucket_mgr.get(target_id)
    history = server.bucket_mgr.get_history(target_id)
    replacement_matches = await server.bucket_mgr.search(
        "fact_evolution_new_only_token", limit=10, include_sealed=False
    )
    old_matches = await server.bucket_mgr.search(
        "fact_evolution_old_only_token", limit=10, include_sealed=False
    )

    assert target_id in result
    assert len(after) == len(before)
    assert target["id"] == target_id
    assert target["content"] == replacement
    assert history[0]["old_content"] == old_content
    assert any(bucket["id"] == target_id for bucket in replacement_matches)
    assert not any(bucket["id"] == target_id for bucket in old_matches)
    server._detect_conflict_warning.assert_not_awaited()
    server.dehydrator.analyze.assert_not_awaited()


@pytest.mark.asyncio
async def test_hold_invalid_supersession_target_does_not_create_bucket(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    before = await server.bucket_mgr.list_all(include_archive=False)

    result = await server.hold(
        "fact_evolution_should_not_be_written", supersedes_id="missing-target"
    )

    after = await server.bucket_mgr.list_all(include_archive=False)
    assert len(after) == len(before)
    assert "not found or invalid" in result
    assert "missing-target" in result


@pytest.mark.asyncio
async def test_hold_without_supersedes_uses_normal_merge_path(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    server._detect_conflict_warning = AsyncMock(return_value="")
    server._merge_or_create = AsyncMock(return_value=("normal-path-id", False))

    result = await server.hold("fact_evolution_normal_path")

    server._merge_or_create.assert_awaited_once()
    assert "normal-path-id" in result


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

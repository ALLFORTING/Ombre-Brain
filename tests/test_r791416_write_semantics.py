import importlib
import sys
from datetime import datetime
from unittest.mock import AsyncMock

import frontmatter
import pytest


def _load_server(tmp_path, monkeypatch):
    monkeypatch.setenv("OMBRE_BUCKETS_DIR", str(tmp_path / "buckets"))
    monkeypatch.delenv("OMBRE_API_KEY", raising=False)
    monkeypatch.delenv("OMBRE_DIGEST_API_KEY", raising=False)
    sys.modules.pop("server", None)
    server = importlib.import_module("server")
    server.decay_engine.ensure_started = AsyncMock(return_value=None)
    return server


def _token(text):
    return text.split("confirm_token:", 1)[1].strip()


@pytest.mark.asyncio
async def test_merge_preview_binds_both_bodies_and_requires_one_shot_token(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    target = await server.bucket_mgr.create("target body")
    source = await server.bucket_mgr.create("source body")
    preview = await server.trace(target, merge=source)
    assert "merge preview" in preview
    assert (await server.bucket_mgr.get(target))["content"] == "target body"
    assert await server.bucket_mgr.get(source)
    assert "confirmation invalid" in await server.trace(target, merge=source, confirm_token="wrong")
    assert await server.bucket_mgr.update(source, content="changed source")
    assert "confirmation invalid" in await server.trace(target, merge=source,
                                                        confirm_token=_token(preview))
    fresh = await server.trace(target, merge=source)
    result = await server.trace(target, merge=source, confirm_token=_token(fresh))
    assert "已合并" in result and "operation_id:" in result
    assert (await server.bucket_mgr.get(target))["content"].count("changed source") == 1
    assert await server.bucket_mgr.get(source) is None


@pytest.mark.asyncio
async def test_merge_resumes_after_restart_without_reappending(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    target = await server.bucket_mgr.create("target body")
    source = await server.bucket_mgr.create("source body")
    preview = await server.trace(target, merge=source)
    server.bucket_mgr.delete = AsyncMock(return_value=False)
    failed = await server.trace(target, merge=source, confirm_token=_token(preview))
    assert "merge partial failure" in failed
    operation_id = failed.split("operation_id:", 1)[1].splitlines()[0].strip()
    assert (await server.bucket_mgr.get(target))["content"].count("source body") == 1

    restarted = _load_server(tmp_path, monkeypatch)
    resumed = await restarted.trace(target, merge=source)
    assert f"operation_id: {operation_id}" in resumed
    assert "已合并" in resumed
    assert (await restarted.bucket_mgr.get(target))["content"].count("source body") == 1
    assert await restarted.bucket_mgr.get(source) is None
    record = next(row for row in restarted.bucket_mgr.read_merge_operations()
                  if row["operation_id"] == operation_id)
    assert record["status"] == "complete"


@pytest.mark.asyncio
async def test_merge_recovers_when_progress_write_fails_after_target_marker(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    target = await server.bucket_mgr.create("before")
    source = await server.bucket_mgr.create("after")
    preview = await server.trace(target, merge=source)
    original_write = server.bucket_mgr.write_merge_operation
    failed_once = False

    def fail_after_target(*args, **kwargs):
        nonlocal failed_once
        if (not failed_once and kwargs.get("status") == "running"
                and kwargs.get("completed") == ["target"]):
            failed_once = True
            raise OSError("injected progress failure")
        return original_write(*args, **kwargs)

    server.bucket_mgr.write_merge_operation = fail_after_target
    failed = await server.trace(target, merge=source, confirm_token=_token(preview))
    assert "merge partial failure" in failed
    assert (await server.bucket_mgr.get(target))["content"].count("after") == 1
    restarted = _load_server(tmp_path, monkeypatch)
    assert "已合并" in await restarted.trace(target, merge=source)
    assert (await restarted.bucket_mgr.get(target))["content"].count("after") == 1


@pytest.mark.asyncio
async def test_combined_unpin_demotion_has_one_plan_and_unpin_alone_keeps_type(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    first = await server.bucket_mgr.create("first pinned", pinned=True)
    assert (await server.bucket_mgr.get(first))["metadata"]["type"] == "permanent"
    assert "cancel pinned first" in await server.trace(first, permanent=0)
    assert (await server.bucket_mgr.get(first))["metadata"]["pinned"] is True
    preview = await server.trace(first, pinned=0)
    result = await server.trace(first, pinned=0, confirm_token=_token(preview))
    assert "type=permanent" in result
    assert (await server.bucket_mgr.get(first))["metadata"]["type"] == "permanent"

    second = await server.bucket_mgr.create("second pinned", pinned=True)
    combined = await server.trace(second, pinned=0, permanent=0)
    assert "type=permanent->dynamic" in combined
    assert (await server.bucket_mgr.get(second))["metadata"]["pinned"] is True
    lowered = await server.trace(second, pinned=0, permanent=0,
                                 confirm_token=_token(combined))
    assert "pinned=False, type=dynamic" in lowered
    assert (await server.bucket_mgr.get(second))["metadata"]["type"] == "dynamic"
    assert "dynamic" in (await server.bucket_mgr.get(second))["path"]

    ordinary = await server.bucket_mgr.create("ordinary")
    assert "type=permanent" in await server.trace(ordinary, permanent=1)
    preview = await server.trace(ordinary, permanent=0)
    assert "confirmation required" in preview
    assert "type=dynamic" in await server.trace(
        ordinary, permanent=0, confirm_token=_token(preview))


@pytest.mark.asyncio
async def test_demotion_move_failure_restores_original_file(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    bucket_id = await server.bucket_mgr.create("pinned", pinned=True)
    old_path = server.bucket_mgr._find_bucket_file(bucket_id)
    with open(old_path, "rb") as handle:
        original = handle.read()
    preview = await server.trace(bucket_id, pinned=0, permanent=0)
    monkeypatch.setattr(server.bucket_mgr, "_move_bucket",
                        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("move failed")))
    assert "修改失败" in await server.trace(
        bucket_id, pinned=0, permanent=0, confirm_token=_token(preview))
    with open(old_path, "rb") as handle:
        assert handle.read() == original
    metadata = (await server.bucket_mgr.get(bucket_id))["metadata"]
    assert metadata["pinned"] is True and metadata["type"] == "permanent"


@pytest.mark.asyncio
async def test_permanent_parameter_rejects_feel_and_archived_types(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    feel_id = await server.bucket_mgr.create("feeling", bucket_type="feel")
    archived_id = await server.bucket_mgr.create("old memory")
    assert await server.bucket_mgr.archive(archived_id)
    assert "only dynamic and permanent" in await server.trace(feel_id, permanent=1)
    assert "only dynamic and permanent" in await server.trace(archived_id, permanent=0)
    assert (await server.bucket_mgr.get(feel_id))["metadata"]["type"] == "feel"
    assert (await server.bucket_mgr.get(archived_id))["metadata"]["type"] == "archived"


@pytest.mark.asyncio
async def test_unpin_of_legacy_pinned_dynamic_bucket_reports_actual_type(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    bucket_id = await server.bucket_mgr.create("legacy pinned", pinned=True)
    path = server.bucket_mgr._find_bucket_file(bucket_id)
    post = frontmatter.load(path)
    post["type"] = "dynamic"
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(frontmatter.dumps(post))
    preview = await server.trace(bucket_id, pinned=0)
    assert "type=dynamic->permanent" in preview
    result = await server.trace(bucket_id, pinned=0, confirm_token=_token(preview))
    assert "pinned=False, type=permanent" in result


@pytest.mark.asyncio
async def test_hold_reuse_reports_fields_and_preserves_trigger(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    bucket_id = await server.bucket_mgr.create("same body", tags=["old"], importance=7)
    server._similarity_doorbell = AsyncMock(return_value="")
    server._detect_conflict_warning = AsyncMock(return_value="")
    server.dehydrator.analyze = AsyncMock(return_value={
        "domain": ["work"], "valence": 0.6, "arousal": 0.4,
        "tags": ["new"], "suggested_name": "new name", "todos": ["task"],
    })
    server.bucket_mgr.search = AsyncMock(return_value=[await server.bucket_mgr.get(bucket_id)])
    result = await server.hold("same body", tags="manual", importance=9,
                               trigger_date="2026-10-01")
    assert f"bucket_id={bucket_id} reused=true" in result
    assert "trigger_date" in result and "ignored_fields" in result
    bucket = await server.bucket_mgr.get(bucket_id)
    assert bucket["metadata"]["trigger_date"] == "2026-10-01"
    assert bucket["metadata"]["importance"] == 7
    assert bucket["metadata"]["tags"] == ["old"]
    server.bucket_mgr.search = AsyncMock(return_value=[bucket])
    rejected = await server.hold("same body", trigger_date="2026-10-02")
    assert "rejected without writing" in rejected
    assert (await server.bucket_mgr.get(bucket_id))["metadata"]["trigger_date"] == "2026-10-01"


@pytest.mark.asyncio
async def test_trace_trigger_none_clears_without_changing_boot_seen_policy(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    bucket_id = await server.bucket_mgr.create("dated body")
    today = datetime.now().date().isoformat()
    assert await server.bucket_mgr.update(bucket_id, trigger_date=today,
                                          trigger_last_seen=today)
    result = await server.trace(bucket_id, trigger_date="none")
    assert "trigger_date=" in result
    bucket = await server.bucket_mgr.get(bucket_id)
    assert bucket["metadata"]["trigger_date"] == ""
    assert bucket["metadata"]["trigger_last_seen"] == ""


@pytest.mark.asyncio
async def test_invalid_public_ranges_are_rejected_without_writing(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    bucket_id = await server.bucket_mgr.create("original")
    cases = [
        (server.trace(bucket_id, importance=11), "importance"),
        (server.trace(bucket_id, valence=-2), "valence"),
        (server.trace(bucket_id, arousal=1.2), "arousal"),
        (server.trace(bucket_id, pinned=2), "pinned"),
        (server.hold("new", importance=0), "importance"),
        (server.hold("new", valence=2), "valence"),
        (server.breath(mode="typo", touch=False), "mode"),
        (server.breath(importance_min=11, touch=False), "importance_min"),
    ]
    for pending, field in cases:
        assert field in await pending
    assert (await server.bucket_mgr.get(bucket_id))["content"] == "original"
    assert len(await server.bucket_mgr.list_all()) == 1

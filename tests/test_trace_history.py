import importlib
import sys
from unittest.mock import AsyncMock, Mock

import pytest


def _load_server(tmp_path, monkeypatch):
    monkeypatch.setenv("OMBRE_BUCKETS_DIR", str(tmp_path / "buckets"))
    monkeypatch.delenv("OMBRE_API_KEY", raising=False)
    sys.modules.pop("server", None)
    server = importlib.import_module("server")
    server.decay_engine.ensure_started = AsyncMock(return_value=None)
    return server


@pytest.mark.asyncio
async def test_trace_append_preserves_content_and_records_history(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    bucket_id = await server.bucket_mgr.create(
        content="original body",
        name="Append history control",
    )

    result = await server.trace(bucket_id, content="appended body", append=True)
    bucket = await server.bucket_mgr.get(bucket_id)
    history = server.bucket_mgr.get_history(bucket_id)

    assert "content=已追加" in result
    assert bucket["content"] == "original body\n\nappended body"
    assert history[0]["change_type"] == "append"
    assert history[0]["old_content"] == "original body"


@pytest.mark.asyncio
async def test_trace_replace_and_delete_record_history(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    bucket_id = await server.bucket_mgr.create(
        content="before replace",
        name="Replace history control",
    )

    await server.trace(bucket_id, content="after replace")
    await server.trace(bucket_id, delete=True)
    history = server.bucket_mgr.get_history(bucket_id)

    assert [row["change_type"] for row in history[:2]] == ["delete", "replace"]
    assert history[0]["old_content"] == "after replace"
    assert history[1]["old_content"] == "before replace"


@pytest.mark.asyncio
async def test_protected_trace_destructive_targets_are_rejected(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    cases = (
        {"sealed": True},
        {"pinned": True},
        {"protected": True},
    )

    for index, options in enumerate(cases):
        bucket_id = await server.bucket_mgr.create(
            content=f"protected content {index}",
            **options,
        )

        replace = await server.trace(bucket_id, content="replacement")
        append = await server.trace(bucket_id, content="append", append=True)
        delete = await server.trace(bucket_id, delete=True)

        assert "受到保护" in replace
        assert "受到保护" in append
        assert "受到保护" in delete
        bucket = await server.bucket_mgr.get(bucket_id)
        assert bucket["content"] == f"protected content {index}"


@pytest.mark.asyncio
async def test_destructive_content_updates_fail_closed_when_history_capture_fails(
    tmp_path, monkeypatch
):
    server = _load_server(tmp_path, monkeypatch)
    bucket_id = await server.bucket_mgr.create(content="original trace content")
    superseded_id = await server.bucket_mgr.create(content="original superseded content")
    server.bucket_mgr.record_history = Mock(side_effect=RuntimeError("history unavailable"))

    replaced = await server.trace(bucket_id, content="replacement")
    superseded = await server.hold("new superseded content", supersedes_id=superseded_id)

    assert replaced == f"修改失败: {bucket_id}"
    assert "supersedes update failed" in superseded
    assert (await server.bucket_mgr.get(bucket_id))["content"] == "original trace content"
    assert (await server.bucket_mgr.get(superseded_id))["content"] == "original superseded content"

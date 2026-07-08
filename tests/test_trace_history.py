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

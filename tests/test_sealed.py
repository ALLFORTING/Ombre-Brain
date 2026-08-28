import importlib
import sys
from datetime import datetime, timedelta
from unittest.mock import AsyncMock

import frontmatter
import pytest

from decay_engine import DecayEngine


def _load_server(tmp_path, monkeypatch):
    monkeypatch.setenv("OMBRE_BUCKETS_DIR", str(tmp_path / "buckets"))
    monkeypatch.delenv("OMBRE_API_KEY", raising=False)
    sys.modules.pop("server", None)
    server = importlib.import_module("server")
    server.decay_engine.ensure_started = AsyncMock(return_value=None)
    return server


@pytest.mark.asyncio
async def test_trace_sets_and_clears_sealed(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    bucket_id = await server.bucket_mgr.create(content="sealed trace test")

    await server.trace(bucket_id, sealed=1)
    sealed = await server.bucket_mgr.get(bucket_id)
    await server.trace(bucket_id, sealed=0)
    unsealed = await server.bucket_mgr.get(bucket_id)

    assert sealed["metadata"]["sealed"] == 1
    assert unsealed["metadata"]["sealed"] == 0


@pytest.mark.asyncio
async def test_sealed_is_hidden_unless_explicitly_included(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    server.dehydrator.dehydrate = AsyncMock(
        side_effect=lambda content, metadata=None: content
    )
    bucket_id = await server.bucket_mgr.create(
        content="unique sealed query needle",
        name="Sealed control",
        pinned=True,
    )
    await server.trace(bucket_id, sealed=1)

    surfaced = await server.breath(mode="summary")
    queried = await server.breath(query="unique sealed query needle")
    queried_included = await server.breath(
        query="unique sealed query needle",
        include_sealed=True,
    )
    included = await server.breath(mode="summary", include_sealed=True)

    assert bucket_id not in surfaced
    assert bucket_id not in queried
    assert "Sealed control" not in queried
    assert bucket_id in queried_included
    assert bucket_id in included


@pytest.mark.asyncio
async def test_decay_never_sets_or_clears_sealed(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    plain_id = await server.bucket_mgr.create(
        content="plain decay control",
        importance=10,
    )
    sealed_id = await server.bucket_mgr.create(
        content="sealed decay control",
        importance=10,
    )
    await server.trace(sealed_id, sealed=1)
    for bucket_id in (plain_id, sealed_id):
        path = server.bucket_mgr._find_bucket_file(bucket_id)
        post = frontmatter.load(path)
        old = datetime.now() - timedelta(days=31)
        post["last_active"] = old.isoformat()
        post["updated_at"] = old.date().isoformat()
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(frontmatter.dumps(post))

    engine = DecayEngine(
        {"decay": {"threshold": -1, "check_interval_hours": 24}},
        server.bucket_mgr,
    )
    await engine.run_decay_cycle()

    plain = await server.bucket_mgr.get(plain_id)
    sealed = await server.bucket_mgr.get(sealed_id)
    assert plain["metadata"]["sealed"] == 0
    assert sealed["metadata"]["sealed"] == 1


@pytest.mark.asyncio
async def test_decay_skips_sealed_content_mutation(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    plain_id = await server.bucket_mgr.create(
        content="plain content eligible for decay compression",
        importance=1,
    )
    sealed_id = await server.bucket_mgr.create(
        content="sealed content must remain unchanged",
        importance=1,
    )
    await server.trace(sealed_id, sealed=1)
    old = datetime.now() - timedelta(days=31)
    for bucket_id in (plain_id, sealed_id):
        path = server.bucket_mgr._find_bucket_file(bucket_id)
        post = frontmatter.load(path)
        post["last_active"] = old.isoformat()
        post["updated_at"] = old.date().isoformat()
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(frontmatter.dumps(post))

    engine = DecayEngine(
        {"decay": {"threshold": -1, "check_interval_hours": 24}},
        server.bucket_mgr,
    )
    result = await engine.run_decay_cycle()

    assert result["compressed"] == 1
    assert (await server.bucket_mgr.get(plain_id))["content"].endswith("...")
    assert (await server.bucket_mgr.get(sealed_id))["content"] == (
        "sealed content must remain unchanged"
    )


@pytest.mark.asyncio
async def test_pulse_hides_sealed_by_default_and_marks_when_included(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    sealed_id = await server.bucket_mgr.create(content="sealed pulse control")
    dormant_id = await server.bucket_mgr.create(content="dormant pulse control")
    await server.trace(sealed_id, sealed=1)
    await server.trace(dormant_id, dormant=1)

    hidden = await server.pulse(show_all=True)
    result = await server.pulse(show_all=True, include_sealed=True)

    assert sealed_id not in hidden
    assert "sealed pulse control" not in hidden
    sealed_line = next(line for line in result.splitlines() if sealed_id in line)
    dormant_line = next(line for line in result.splitlines() if dormant_id in line)
    assert "[封存]" in sealed_line
    assert "[休眠]" in dormant_line and "[封存]" not in dormant_line


@pytest.mark.asyncio
async def test_dream_and_todos_hide_sealed_by_default(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    sealed_id = await server.bucket_mgr.create(
        content="sealed dream todo control",
        name="Sealed dream todo",
    )
    await server.trace(sealed_id, sealed=1)
    path = server.bucket_mgr._find_bucket_file(sealed_id)
    post = frontmatter.load(path)
    post["todos"] = ["hidden sealed todo"]
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(frontmatter.dumps(post))

    dream_recent = await server.dream()
    dream_detail = await server.dream(detail_ids=sealed_id)
    todos = await server.todos()

    assert sealed_id not in dream_recent
    assert "Sealed dream todo" not in dream_recent
    assert sealed_id not in dream_detail
    assert "Sealed dream todo" not in dream_detail
    assert "sealed dream todo control" not in dream_detail
    assert sealed_id not in todos
    assert "hidden sealed todo" not in todos

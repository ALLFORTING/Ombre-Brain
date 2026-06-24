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
async def test_sealed_is_hidden_from_surfacing_but_queryable(tmp_path, monkeypatch):
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
    included = await server.breath(mode="summary", include_sealed=True)

    assert bucket_id not in surfaced
    assert bucket_id in queried
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
async def test_pulse_marks_sealed_separately_from_dormant(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    sealed_id = await server.bucket_mgr.create(content="sealed pulse control")
    dormant_id = await server.bucket_mgr.create(content="dormant pulse control")
    await server.trace(sealed_id, sealed=1)
    await server.trace(dormant_id, dormant=1)

    result = await server.pulse(show_all=True)

    sealed_line = next(line for line in result.splitlines() if sealed_id in line)
    dormant_line = next(line for line in result.splitlines() if dormant_id in line)
    assert "🔒" in sealed_line and "[封存]" in sealed_line
    assert "[休眠]" in dormant_line and "[封存]" not in dormant_line

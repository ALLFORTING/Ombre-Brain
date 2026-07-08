import importlib
import sys
from datetime import datetime, timedelta
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


def _age_bucket(server, bucket_id, days=40):
    path = server.bucket_mgr._find_bucket_file(bucket_id)
    post = frontmatter.load(path)
    old = datetime.now() - timedelta(days=days)
    post["last_active"] = old.isoformat()
    post["updated_at"] = old.date().isoformat()
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(frontmatter.dumps(post))


@pytest.mark.asyncio
async def test_digest_dry_run_lists_only_safe_candidates(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    candidate_id = await server.bucket_mgr.create(
        content="old low importance candidate",
        importance=2,
        domain=["digest-test"],
    )
    pinned_id = await server.bucket_mgr.create(
        content="old pinned must not digest",
        importance=2,
        domain=["digest-test"],
        pinned=True,
    )
    sealed_id = await server.bucket_mgr.create(
        content="old sealed must not digest",
        importance=2,
        domain=["digest-test"],
    )
    await server.trace(sealed_id, sealed=1)
    for bucket_id in (candidate_id, pinned_id, sealed_id):
        _age_bucket(server, bucket_id)

    result = await server.digest(dry_run=True)
    candidate = await server.bucket_mgr.get(candidate_id)

    assert candidate_id in result
    assert pinned_id not in result
    assert sealed_id not in result
    assert candidate["metadata"].get("digested") is not True


@pytest.mark.asyncio
async def test_digest_live_creates_digest_and_marks_sources(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    server._call_digest_api = AsyncMock(return_value="condensed digest body")
    source_id = await server.bucket_mgr.create(
        content="old source to digest",
        importance=2,
        domain=["digest-test"],
    )
    _age_bucket(server, source_id)

    result = await server.digest(dry_run=False)
    source = await server.bucket_mgr.get(source_id)
    all_buckets = await server.bucket_mgr.list_all(include_archive=False)
    digest_buckets = [
        bucket for bucket in all_buckets
        if "auto-digested" in bucket["metadata"].get("tags", [])
    ]

    assert "已消化: 1 个桶" in result
    assert source["metadata"]["digested"] is True
    assert source["metadata"]["source_bucket"] == digest_buckets[0]["id"]
    assert digest_buckets[0]["content"] == "condensed digest body"

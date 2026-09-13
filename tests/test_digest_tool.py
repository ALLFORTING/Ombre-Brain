import importlib
import json
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path
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
    post["created"] = old.isoformat()
    post["last_active"] = old.isoformat()
    post["created_at"] = old.date().isoformat()
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


def _confirm_token(result):
    for line in result.splitlines():
        if line.startswith("confirm_token:"):
            return line.split(":", 1)[1].strip()
    raise AssertionError("confirm_token not found")


@pytest.mark.asyncio
async def test_digest_rebalance_dry_run_lists_metadata_without_content(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    candidate_id = await server.bucket_mgr.create(
        content="rebalance body must stay hidden",
        importance=9,
        domain=["rebalance-test"],
    )
    fresh_id = await server.bucket_mgr.create(
        content="fresh high importance exclusion",
        importance=9,
        domain=["rebalance-test"],
    )
    _age_bucket(server, candidate_id, days=40)

    dry_run = await server.digest(dry_run=True)
    candidate = await server.bucket_mgr.get(candidate_id)

    assert "importance rebalance dry-run" in dry_run
    assert "confirm_token:" in dry_run
    assert candidate_id in dry_run
    assert fresh_id not in dry_run
    assert "rebalance body must stay hidden" not in dry_run
    assert candidate["metadata"]["importance"] == 9


@pytest.mark.asyncio
async def test_digest_rebalance_requires_confirmation_then_lowers_once(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    candidate_id = await server.bucket_mgr.create(
        content="old high importance rebalance candidate",
        importance=9,
        domain=["rebalance-test"],
    )
    _age_bucket(server, candidate_id, days=40)

    blocked = await server.digest(dry_run=False)
    before = await server.bucket_mgr.get(candidate_id)
    token = _confirm_token(await server.digest(dry_run=True))
    result = await server.digest(dry_run=False, confirm_token=token)
    candidate = await server.bucket_mgr.get(candidate_id)

    assert "confirmation required" in blocked
    assert before["metadata"]["importance"] == 9
    assert "importance rebalanced: 1" in result
    assert candidate["metadata"]["importance"] == 8


@pytest.mark.asyncio
async def test_digest_rebalance_pinned_is_never_candidate_or_lowered(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    pinned_id = await server.bucket_mgr.create(
        content="old pinned rebalance exclusion",
        importance=9,
        domain=["rebalance-test"],
        pinned=True,
    )
    _age_bucket(server, pinned_id, days=40)

    dry_run = await server.digest(dry_run=True)
    result = await server.digest(dry_run=False, confirm_token="anything")
    pinned = await server.bucket_mgr.get(pinned_id)

    assert pinned_id not in dry_run
    assert "=== importance rebalance" not in dry_run
    assert "importance rebalanced:" not in result
    assert pinned["metadata"]["importance"] == 10


@pytest.mark.asyncio
async def test_digest_rebalance_empty_candidates_reports_none(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    fresh_id = await server.bucket_mgr.create(
        content="fresh high importance exclusion",
        importance=9,
        domain=["rebalance-test"],
    )

    result = await server.digest(dry_run=True)

    assert fresh_id not in result
    assert "confirm_token:" not in result
    assert "No digest or importance rebalance candidates." in result


@pytest.mark.asyncio
async def test_digest_rebalance_repeated_confirmation_does_not_lower_again(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    candidate_id = await server.bucket_mgr.create(
        content="old high importance rebalance candidate",
        importance=9,
        domain=["rebalance-test"],
    )
    _age_bucket(server, candidate_id, days=40)
    token = _confirm_token(await server.digest(dry_run=True))

    first = await server.digest(dry_run=False, confirm_token=token)
    after_first = await server.bucket_mgr.get(candidate_id)
    second = await server.digest(dry_run=False, confirm_token=token)
    after_second = await server.bucket_mgr.get(candidate_id)

    assert "importance rebalanced: 1" in first
    assert after_first["metadata"]["importance"] == 8
    assert "confirmation required" in second
    assert after_second["metadata"]["importance"] == 8


@pytest.mark.asyncio
async def test_digest_dedupe_is_readonly_skips_sealed_and_reports_orphans(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    first_id = await server.bucket_mgr.create(
        content="first duplicate body",
        importance=5,
        domain=["dedupe-test"],
        name="first duplicate",
    )
    second_id = await server.bucket_mgr.create(
        content="second duplicate body",
        importance=5,
        domain=["dedupe-test"],
        name="second duplicate",
    )
    dormant_id = await server.bucket_mgr.create(
        content="dormant nearby body",
        importance=5,
        domain=["dedupe-test"],
        name="dormant nearby",
    )
    sealed_id = await server.bucket_mgr.create(
        content="SEALED_BODY_MUST_NOT_APPEAR",
        importance=5,
        domain=["dedupe-test"],
        name="SEALED_NAME_MUST_NOT_APPEAR",
    )
    assert await server.bucket_mgr.set_dormant(dormant_id, True)
    await server.trace(sealed_id, sealed=1)

    server.embedding_engine._store_embedding(first_id, [1.0, 0.0])
    server.embedding_engine._store_embedding(second_id, [1.0, 0.0])
    server.embedding_engine._store_embedding(dormant_id, [0.9, 0.435889894])
    # Simulate a stale derived vector left behind after a bucket was sealed.
    server.embedding_engine._store_embedding(sealed_id, [1.0, 0.0])
    server.embedding_engine._store_embedding("orphan-vector-row", [1.0, 0.0])
    with sqlite3.connect(server.embedding_engine.db_path) as conn:
        conn.execute(
            "INSERT INTO embeddings (bucket_id, embedding, model, updated_at) VALUES (?, ?, ?, ?)",
            ("other-model-row", json.dumps([1.0, 0.0]), "other-model", "2026-09-13T00:00:00"),
        )

    tracked_ids = (first_id, second_id, dormant_id, sealed_id)
    bucket_paths = {
        bucket_id: server.bucket_mgr._find_bucket_file(bucket_id)
        for bucket_id in tracked_ids
    }
    before_bytes = {
        bucket_id: Path(path).read_bytes()
        for bucket_id, path in bucket_paths.items()
    }
    before_metadata = {
        bucket_id: {
            key: frontmatter.load(path).get(key)
            for key in ("dormant", "last_active", "activation_count", "digested")
        }
        for bucket_id, path in bucket_paths.items()
    }
    embedding_before = Path(server.embedding_engine.db_path).read_bytes()
    server.decay_engine.ensure_started.reset_mock()
    server.bucket_mgr.list_all = AsyncMock(side_effect=AssertionError("dedupe must not load buckets"))
    server.embedding_engine._generate_embedding = AsyncMock(
        side_effect=AssertionError("dedupe must not call embedding API")
    )

    result = await server.digest(mode="dedupe")

    assert server.decay_engine.ensure_started.await_count == 0
    assert server.bucket_mgr.list_all.await_count == 0
    assert server.embedding_engine._generate_embedding.await_count == 0
    assert "向量: N=3（当前模型行=5，sealed 跳过=1，无效跳过=0）" in result
    assert "桶: M=4（sealed=1，元数据不可读=0）" in result
    assert "差额: K=M-N=1" in result
    assert "孤儿向量行: 1" in result
    assert f"- {server.embedding_engine.model}: 5" in result
    assert "- other-model: 1" in result
    assert "- 0.95+: 1" in result
    assert "- 0.90-0.95: 2" in result
    assert f"{first_id} name='first duplicate' dormant=False" in result
    assert f"{dormant_id} name='dormant nearby' dormant=True" in result
    assert sealed_id not in result
    assert "SEALED_NAME_MUST_NOT_APPEAR" not in result
    assert "SEALED_BODY_MUST_NOT_APPEAR" not in result
    assert "orphan-vector-row" not in result
    assert embedding_before == Path(server.embedding_engine.db_path).read_bytes()
    assert before_bytes == {
        bucket_id: Path(path).read_bytes()
        for bucket_id, path in bucket_paths.items()
    }
    assert before_metadata == {
        bucket_id: {
            key: frontmatter.load(path).get(key)
            for key in ("dormant", "last_active", "activation_count", "digested")
        }
        for bucket_id, path in bucket_paths.items()
    }

"""Synthetic interrupted restore replay, storage guards and truthful receipts."""
import importlib
import json
import sqlite3
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import frontmatter
import pytest

from bucket_manager import BucketIdempotencyError


@pytest.fixture
def server(test_config, monkeypatch):
    import utils
    monkeypatch.setattr(utils, "load_config", lambda: test_config)
    monkeypatch.setenv("OMBRE_BUCKETS_DIR", test_config["buckets_dir"])
    monkeypatch.delenv("OMBRE_API_KEY", raising=False)
    sys.modules.pop("server", None)
    module = importlib.import_module("server")
    module.decay_engine.ensure_started = AsyncMock()
    module._fire_webhook = AsyncMock()
    return module


async def published(server, *, relations=False, raw_alias=False):
    manager = server.bucket_mgr
    first = await manager.create("first endpoint")
    second = await manager.create("second endpoint")
    bucket = await manager.create("restore original")
    if raw_alias:
        path = Path(manager._find_bucket_file(bucket))
        post = frontmatter.load(path)
        post.content = "婷易 raw original"
        path.write_text(frontmatter.dumps(post), encoding="utf-8")
    if relations:
        manager.mutate_related(bucket, add=[first])
    await manager.update(bucket, content="newer body")
    ref = manager.list_bucket_revisions(bucket)[0]["ref"]
    if relations:
        manager.mutate_related(bucket, replace=second)
    plan = manager.plan_restore(bucket, ref)
    restore_id = manager._publish_restore(plan, "synthetic")
    return bucket, restore_id, plan, first, second


def row(manager, restore_id):
    return next(item for item in manager.restoration_rows() if item["restore_id"] == restore_id)


def reasons(manager, restore_id):
    return {(item["kind"], item["reason"]) for item in row(manager, restore_id)["report"]["skipped"]}


@pytest.mark.asyncio
async def test_changed_related_not_overwritten_after_publication(server):
    m = server.bucket_mgr
    bucket, rid, plan, first, second = await published(server, relations=True)
    third = await m.create("later endpoint")
    m.mutate_related(bucket, replace=third)
    await m.resume_restorations(bucket)
    assert (await m.get(bucket))["metadata"]["related_buckets"] == third
    assert ("related", "current_related_changed") in reasons(m, rid)
    assert row(m, rid)["status"] == "completed"


@pytest.mark.asyncio
@pytest.mark.parametrize("change,reason", [("sealed", "current_sealed"), ("content", "current_content_changed")])
async def test_changed_embedding_input_never_sent(server, monkeypatch, change, reason):
    m = server.bucket_mgr
    bucket, rid, plan, *_ = await published(server)
    if change == "sealed":
        await m.update(bucket, sealed=1)
    else:
        await m.update(bucket, content="latest body must survive")
    refresh = AsyncMock()
    monkeypatch.setattr(m, "_refresh_ordinary_embedding_best_effort", refresh)
    await m.resume_restorations(bucket)
    refresh.assert_not_called()
    assert ("embedding", reason) in reasons(m, rid)
    if change == "content":
        assert (await m.get(bucket))["content"] == "latest body must survive"


@pytest.mark.asyncio
async def test_touch_only_allows_normal_related_and_idempotent_replay(server):
    m = server.bucket_mgr
    bucket, rid, plan, first, second = await published(server, relations=True)
    await m.touch(bucket)
    assert await m.resume_restorations(bucket) == [rid]
    assert (await m.get(bucket))["metadata"]["related_buckets"] == first
    assert not reasons(m, rid)
    assert row(m, rid)["report"]["steps"] == ["embedding", "related", "supersession", "boot_delta"]
    before = Path(m._find_bucket_file(bucket)).read_bytes()
    assert await m.resume_restorations(bucket) == []
    assert Path(m._find_bucket_file(bucket)).read_bytes() == before


@pytest.mark.asyncio
async def test_legacy_published_guard_skips_related_without_guessing(server):
    m = server.bucket_mgr
    bucket, rid, plan, first, second = await published(server, relations=True)
    guard = row(m, rid)["published_guard"]
    guard.pop("restore_related_fingerprint")
    with sqlite3.connect(m.history_db_path) as conn:
        conn.execute("UPDATE ob_bucket_restorations SET published_guard_json=? WHERE restore_id=?",
                     (json.dumps(guard), rid))
    await m.resume_restorations(bucket)
    assert (await m.get(bucket))["metadata"]["related_buckets"] == second
    assert ("related", "published_related_guard_missing") in reasons(m, rid)


@pytest.mark.asyncio
async def test_recover_new_publication_captures_fingerprints(server):
    m = server.bucket_mgr
    bucket, rid, plan, *_ = await published(server)
    with sqlite3.connect(m.history_db_path) as conn:
        conn.execute("UPDATE ob_bucket_restorations SET status='pending',published_guard_json=NULL WHERE restore_id=?",
                     (rid,))
    m.recover_restorations()
    guard = row(m, rid)["published_guard"]
    assert guard["restore_related_fingerprint"] and guard["restore_forward_fingerprint"]
    assert await m.resume_restorations(bucket) == [rid]


@pytest.mark.asyncio
async def test_raw_display_alias_body_does_not_false_skip_embedding(server, monkeypatch):
    m = server.bucket_mgr
    bucket, rid, plan, *_ = await published(server, raw_alias=True)
    assert plan["content"] == "婷易 raw original"
    assert (await m.get(bucket))["content"] != plan["content"]
    refresh = AsyncMock()
    monkeypatch.setattr(m, "_refresh_ordinary_embedding_best_effort", refresh)
    await m.resume_restorations(bucket)
    refresh.assert_awaited_once_with(bucket, plan["content"])
    assert ("embedding", "current_content_changed") not in reasons(m, rid)


@pytest.mark.asyncio
async def test_missing_bucket_skips_every_pending_step(server, monkeypatch):
    m = server.bucket_mgr
    bucket, rid, plan, *_ = await published(server)
    Path(m._find_bucket_file(bucket)).unlink()  # synthetic tmp_path only
    refresh = AsyncMock()
    monkeypatch.setattr(m, "_refresh_ordinary_embedding_best_effort", refresh)
    await m.resume_restorations(bucket)
    refresh.assert_not_called()
    assert reasons(m, rid) == {(kind, "current_missing") for kind in
                               ("embedding", "related", "superseded_by", "boot_delta")}
    assert row(m, rid)["status"] == "completed"


@pytest.mark.asyncio
async def test_changed_supersession_is_not_replayed(server):
    m = server.bucket_mgr
    bucket, rid, plan, first, second = await published(server)
    await m.update(bucket, superseded_by=second)
    await m.resume_restorations(bucket)
    assert (await m.get(bucket))["metadata"]["superseded_by"] == second
    assert ("superseded_by", "current_supersession_changed") in reasons(m, rid)


@pytest.mark.asyncio
@pytest.mark.parametrize("valid", [True, False])
async def test_preview_or_rejection_after_resume_reports_real_steps(server, valid):
    m = server.bucket_mgr
    bucket, rid, plan, *_ = await published(server)
    result = await server.restore_revision(bucket, plan["ref"] if valid else "r999999")
    assert rid in result and "补做步骤=embedding,related,supersession,boot_delta" in result
    assert "no changes made" not in result
    assert ("restore preview" if valid else "restore rejected") in result


@pytest.mark.asyncio
async def test_partial_replay_error_receipt_is_redacted_and_truthful(server, monkeypatch):
    m = server.bucket_mgr
    bucket, rid, plan, *_ = await published(server)
    original = m._restore_step

    def fail_after_embedding(restore_id, step, **kwargs):
        if step == "related":
            raise RuntimeError("PRIVATE_PATH PRIVATE_BODY")
        return original(restore_id, step, **kwargs)

    monkeypatch.setattr(m, "_restore_step", fail_after_embedding)
    result = await server.restore_revision(bucket, plan["ref"])
    assert rid in result and "补做步骤=embedding" in result and "RuntimeError" in result
    assert "PRIVATE" not in result and "no changes made" not in result
    assert row(m, rid)["status"] == "published"


@pytest.mark.asyncio
async def test_unstarted_preview_or_rejection_keeps_original_no_changes_claim(server):
    m = server.bucket_mgr
    bucket = await m.create("old")
    await m.update(bucket, content="new")
    ref = m.list_bucket_revisions(bucket)[0]["ref"]
    assert "no changes made" in await server.restore_revision(bucket, ref)
    assert "no changes made" in await server.restore_revision(bucket, "r999999")


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["ordinary", "trace", "archive"])
async def test_bottom_store_sealed_gate_preserves_vector_cleanup(server, route):
    m, engine = server.bucket_mgr, server.embedding_engine
    bucket = await m.create("open")
    engine._store_embedding(bucket, [1.0, 0.0])
    await m.update(bucket, sealed=1)
    with pytest.raises(BucketIdempotencyError, match="sealed_embedding_source"):
        if route == "ordinary":
            engine._store_embedding(bucket, [1.0, 0.0])
        elif route == "trace":
            engine.store_trace_embedding("effect", bucket, [1.0, 0.0], "digest", engine.model, "2026-10-12")
        else:
            engine.store_archive_embedding(bucket, [1.0, 0.0], "digest", engine.model, "2026-10-12")
    # Simulate a stale vector in the isolated database, bypassing production admission.
    with sqlite3.connect(engine.db_path) as conn:
        conn.execute("INSERT OR REPLACE INTO embeddings VALUES(?,?,?,?)",
                     (bucket, "[1,0]", engine.model, "2026-10-12"))
    engine.delete_embedding(bucket)
    assert await engine.get_embedding(bucket) is None


@pytest.mark.asyncio
async def test_sealed_generate_does_not_call_provider(server, monkeypatch):
    m, engine = server.bucket_mgr, server.embedding_engine
    bucket = await m.create("private", sealed=True)
    engine.enabled = True
    provider = AsyncMock(return_value=[1.0, 0.0])
    monkeypatch.setattr(engine, "_generate_embedding", provider)
    assert not await engine.generate_and_store(bucket, "private")
    provider.assert_not_called()


@pytest.mark.asyncio
async def test_sealing_during_provider_prevents_vector_storage(server, monkeypatch):
    m, engine = server.bucket_mgr, server.embedding_engine
    bucket = await m.create("open body")
    engine.enabled = True

    async def provider(content):
        await m.update(bucket, sealed=1)
        return [1.0, 0.0]

    monkeypatch.setattr(engine, "_generate_embedding", AsyncMock(side_effect=provider))
    assert not await engine.generate_and_store(bucket, "open body")
    assert (await m.get(bucket))["metadata"]["sealed"] == 1
    assert await engine.get_embedding(bucket) is None

@pytest.mark.asyncio
async def test_resumed_preview_reports_skipped_embedding_reason(server):
    m = server.bucket_mgr
    bucket, rid, plan, *_ = await published(server)
    await m.update(bucket, sealed=1)
    result = await server.restore_revision(bucket, plan["ref"])
    assert rid in result and "跳过=embedding(current_sealed)" in result
    assert "no changes made" not in result
    assert "婷易 raw original" not in result

@pytest.mark.asyncio
@pytest.mark.parametrize("later_change", [False, True])
async def test_planned_supersession_replay_respects_publication_baseline(server, later_change):
    m = server.bucket_mgr
    endpoints = [await m.create(f"forward endpoint {index}") for index in range(3)]
    bucket = await m.create("old forward body")
    await m.update(bucket, superseded_by=endpoints[0])
    await m.update(bucket, content="new forward body")
    ref = m.list_bucket_revisions(bucket)[0]["ref"]
    await m.update(bucket, superseded_by=endpoints[1])
    plan = m.plan_restore(bucket, ref)
    assert plan["forward"]["apply"] and plan["forward"]["superseded_by"] == endpoints[0]
    rid = m._publish_restore(plan, "synthetic")
    if later_change:
        await m.update(bucket, superseded_by=endpoints[2])
    await m.resume_restorations(bucket)
    expected = endpoints[2] if later_change else endpoints[0]
    assert (await m.get(bucket))["metadata"]["superseded_by"] == expected
    assert (("superseded_by", "current_supersession_changed") in reasons(m, rid)) == later_change

@pytest.mark.asyncio
async def test_resumed_preview_preserves_literal_status_phrase_in_body_diff(server):
    m = server.bucket_mgr
    bucket = await m.create("original v1")
    await m.update(bucket, content="no changes made exact phrase")
    original_ref = m.list_bucket_revisions(bucket)[0]["ref"]
    await m.update(bucket, content="newest v3")
    phrase_ref = m.list_bucket_revisions(bucket)[0]["ref"]
    plan = m.plan_restore(bucket, original_ref)
    rid = m._publish_restore(plan, "synthetic")
    result = await server.restore_revision(bucket, phrase_ref)
    assert rid in result.splitlines()[0] and "补做步骤=" in result.splitlines()[0]
    assert "restore preview: 旧收尾已尝试" in result
    assert "+no changes made exact phrase" in result
    assert result.count("no changes made") == 1

@pytest.mark.asyncio
async def test_target_disappearing_after_publication_reports_planned_target(server):
    from related_integrity import parse_related
    m = server.bucket_mgr
    bucket, rid, plan, first, second = await published(server, relations=True)
    assert plan["related"] == [first]
    current = frontmatter.load(m._find_bucket_file(bucket))
    assert parse_related(current.metadata).fingerprint == row(m, rid)["published_guard"]["restore_related_fingerprint"]
    Path(m._find_bucket_file(first)).unlink()  # isolated synthetic endpoint only
    await m.resume_restorations(bucket)
    assert (await m.get(bucket))["metadata"]["related_buckets"] == second
    omissions = [item for item in row(m, rid)["report"]["skipped"] if item["kind"] == "related"]
    assert len(omissions) == 1
    assert omissions[0]["target"] == first and omissions[0]["target"] != bucket
    assert "missing" in omissions[0]["reason"]
    assert row(m, rid)["status"] == "completed"
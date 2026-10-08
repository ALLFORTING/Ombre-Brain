"""Full revisions, two-stage restore and resurrect-by-original-id (undo phase 2)."""
import hashlib
import importlib
import json
import sqlite3
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import frontmatter
import pytest

from backup_export import build_backup_payload
from confirmed_delete_admission import DeleteAdmissionError


@pytest.fixture
def server(tmp_path, monkeypatch):
    monkeypatch.setenv("OMBRE_BUCKETS_DIR", str(tmp_path / "buckets"))
    monkeypatch.delenv("OMBRE_API_KEY", raising=False)
    sys.modules.pop("server", None)
    module = importlib.import_module("server")
    module.decay_engine.ensure_started = AsyncMock(return_value=None)
    return module


def token(preview):
    for line in preview.splitlines():
        if line.startswith("confirm_token:"):
            return line.split(":", 1)[1].strip()
    raise AssertionError(preview)


def kinds(server, bucket_id):
    return [item["op_kind"] for item in server.bucket_mgr.list_bucket_revisions(bucket_id)]


async def delete(server, bucket_id):
    preview = await server.trace(bucket_id, delete=True)
    result = await server.trace(bucket_id, delete=True, confirm_token=token(preview))
    assert await server.bucket_mgr.get(bucket_id) is None, result


async def restore(server, bucket_id, ref, **kwargs):
    preview = await server.restore_revision(bucket_id, ref, **kwargs)
    result = await server.restore_revision(bucket_id, ref, confirm_token=token(preview), **kwargs)
    assert "已还原" in result, result
    return preview, result


def ref_of(server, bucket_id, op_kind):
    return next(item["ref"] for item in server.bucket_mgr.list_bucket_revisions(bucket_id)
                if item["op_kind"] == op_kind)


# ---------------------------------------------------------------- A1 hooks
@pytest.mark.asyncio
async def test_trace_writes_full_revisions_for_content_metadata_and_relations(server):
    manager = server.bucket_mgr
    a = await manager.create(content="alpha body", name="Alpha", tags=["x"], importance=4)
    b = await manager.create(content="beta body", name="Beta")
    await server.trace(a, content="alpha new")
    await server.trace(a, importance=7)
    await server.trace(a, related=b)
    await server.trace(a, superseded_by=b)
    revisions = manager.list_bucket_revisions(a)
    assert [item["op_kind"] for item in revisions][::-1] == ["replace", "update", "related", "update"]
    first = revisions[-1]
    assert first["content"] == "alpha body"
    assert first["metadata"]["importance"] == 4 and first["metadata"]["tags"] == ["x"]
    assert revisions[-2]["metadata"]["importance"] == 4 and revisions[-2]["content"] == "alpha new"
    assert "supersession_reverse" in kinds(server, b)


@pytest.mark.asyncio
async def test_todo_tg_feel_hold_reuse_and_deletes_capture_revisions(server):
    manager = server.bucket_mgr
    bucket = await manager.create(content="todo body", todos=["one", "two"])
    identity = (await manager.get(bucket))["metadata"]["todo_provenance"][0]["id"]
    preview = await server.trace(bucket, todo_done=identity)
    await server.trace(bucket, todo_done=identity, confirm_token=token(preview))
    second = (await manager.get(bucket))["metadata"]["todo_provenance"][1]["id"]
    preview = await server.trace(bucket, todo_drop=second)
    await server.trace(bucket, todo_drop=second, confirm_token=token(preview))
    body_hash = hashlib.sha256("todo body".encode()).hexdigest()
    assert (await manager.refresh_tg_summary(bucket, "summary", body_hash))[0] == "updated"
    assert kinds(server, bucket)[::-1] == ["todo_done", "todo_drop", "tg_summary"]

    source = await manager.create(content="feel source")
    assert manager.mark_feel_source(source)["status"] == "marked"
    assert kinds(server, source) == ["feel_source"]

    reused = await manager.create(content="same words")
    await server._merge_or_create("same words", [], 5, [], 0.5, 0.3, trigger_date="2026-12-01")
    assert kinds(server, reused) == ["hold_reuse"]

    await delete(server, reused)
    deleted = manager.list_bucket_revisions(reused)[0]
    assert deleted["op_kind"] == "delete" and deleted["op_id"]
    assert deleted["metadata"]["trigger_date"] == "2026-12-01"
    assert deleted["relative_path"].endswith(f"{reused}.md")


@pytest.mark.asyncio
async def test_merge_records_target_source_and_persists_relation_restore_values(server):
    manager = server.bucket_mgr
    target = await manager.create(content="target body", name="T")
    source = await manager.create(content="source body", name="S")
    inbound = await manager.create(content="inbound body", name="I")
    await server.trace(inbound, superseded_by=source)
    preview = await server.trace(target, merge=source)
    result = await server.trace(target, merge=source, confirm_token=token(preview))
    assert "已合并" in result, result
    assert "merge_target" in kinds(server, target)
    assert manager.list_bucket_revisions(source)[0]["op_kind"] == "delete"
    plan = manager.read_merge_operations()[0]["plan"]
    restores = {item["bucket_id"]: item["restore"] for item in plan["relations"]}
    assert restores[inbound] == {"superseded_by": source, "superseded_at": restores[inbound]["superseded_at"]}
    assert restores[inbound]["superseded_at"]


@pytest.mark.asyncio
async def test_digest_operation_keys_record_digest_revisions_and_state_keeps_source_bucket(server):
    manager = server.bucket_mgr
    bucket = await manager.create(content="digest me", importance=5)
    await manager.update(bucket, source_bucket="older")
    await manager.apply_import_operation("digest:op1:rebalance:" + bucket, operation_kind="update",
                                         target_bucket_id=bucket, payload={"kwargs": {"importance": 4}})
    assert manager.list_bucket_revisions(bucket)[0]["op_kind"] == "digest"
    state = server._digest_bucket_state(await manager.get(bucket))
    assert state["source_bucket"] == "older"
    legacy_state = {key: value for key, value in state.items() if key != "source_bucket"}
    assert server._digest_state_matches(await manager.get(bucket), legacy_state)
    assert not server._digest_state_matches(await manager.get(bucket), {**state, "source_bucket": "x"})


@pytest.mark.asyncio
async def test_background_writers_do_not_capture_revisions(server):
    manager = server.bucket_mgr
    bucket = await manager.create(content="quiet body")
    await manager.update(bucket, tags=["compressed"], _skip_revision=True)
    await manager.update(bucket, resolved=True, _skip_revision=True)
    await manager.touch(bucket)
    assert kinds(server, bucket) == []


@pytest.mark.asyncio
async def test_revision_capture_failure_refuses_writes(server, monkeypatch):
    manager = server.bucket_mgr
    bucket = await manager.create(content="guarded body", importance=3)
    def broken(*args, **kwargs):
        raise sqlite3.OperationalError("disk full")
    from bucket_manager import BucketManager
    monkeypatch.setattr(BucketManager, "_insert_revision", staticmethod(broken))
    assert await manager.update(bucket, content="changed") is False
    assert await manager.update(bucket, importance=9) is False
    body_hash = hashlib.sha256("guarded body".encode()).hexdigest()
    assert (await manager.refresh_tg_summary(bucket, "s", body_hash))[0] == "write_failed"
    assert await manager.delete(bucket) is False
    assert manager.list_bucket_revisions(bucket) == []
    current = await manager.get(bucket)
    assert current["content"] == "guarded body" and current["metadata"]["importance"] == 3


@pytest.mark.asyncio
async def test_confirmed_delete_refuses_when_revision_insert_fails(server, monkeypatch):
    manager = server.bucket_mgr
    bucket = await manager.create(content="keep me")
    def broken(*args, **kwargs):
        raise sqlite3.OperationalError("disk full")
    from bucket_manager import BucketManager
    monkeypatch.setattr(BucketManager, "_insert_revision", staticmethod(broken))
    preview = await server.trace(bucket, delete=True)
    await server.trace(bucket, delete=True, confirm_token=token(preview))
    assert (await manager.get(bucket))["content"] == "keep me"
    assert manager.list_bucket_revisions(bucket) == []


# --------------------------------------------------------------- A3 restore
@pytest.mark.asyncio
async def test_restore_existing_bucket_keeps_runtime_fields_and_is_undoable(server):
    manager = server.bucket_mgr
    bucket = await manager.create(content="v1 body", name="Versioned", tags=["keep"], importance=4)
    await server.trace(bucket, content="v2 body", importance=8, tags="new")
    await manager.update(bucket, tags=["new", "compressed"], _skip_revision=True)
    await manager.touch(bucket)
    before = (await manager.get(bucket))["metadata"]
    preview, result = await restore(server, bucket, ref_of(server, bucket, "replace"))
    assert "v2 body" in preview and "v1 body" in preview and "importance: 8 → 4" in preview
    restored = await manager.get(bucket)
    assert restored["content"] == "v1 body"
    assert restored["metadata"]["importance"] == 4
    assert restored["metadata"]["tags"] == ["keep", "compressed"]
    assert restored["metadata"]["last_active"] == before["last_active"]
    assert restored["metadata"]["activation_count"] == before["activation_count"]
    undo = ref_of(server, bucket, "pre_restore")
    assert f"已存为 {undo}" in result
    await restore(server, bucket, undo)
    again = await manager.get(bucket)
    assert again["content"] == "v2 body" and again["metadata"]["importance"] == 8
    row = manager.restoration_rows(bucket)[0]
    assert row["status"] == "completed" and row["actor"] == "mcp"
    assert row["report"]["steps"] == ["embedding", "related", "supersession", "boot_delta"]
    events = manager.get_boot_delta_events(0, 10_000)
    assert any(event["event_type"] == "restored" and event["bucket_id"] == bucket for event in events)


@pytest.mark.asyncio
async def test_legacy_history_rows_restore_body_only(server):
    manager = server.bucket_mgr
    bucket = await manager.create(content="old words", importance=3)
    await server.trace(bucket, content="new words")
    await server.trace(bucket, importance=6)
    history_ref = "h" + str(manager.list_history_rows(bucket)[0]["id"])
    await restore(server, bucket, history_ref)
    restored = await manager.get(bucket)
    assert restored["content"] == "old words" and restored["metadata"]["importance"] == 6


@pytest.mark.asyncio
async def test_sealed_rule_either_side_sealed_restores_sealed(server):
    manager = server.bucket_mgr
    open_first = await manager.create(content="open text")
    await server.trace(open_first, importance=6)
    await server.trace(open_first, sealed=1)
    ref = manager.list_bucket_revisions(open_first)[-1]["ref"]
    preview = await server.restore_revision(open_first, ref)
    assert "结果 sealed：是" in preview and "importance: [sealed，已隐藏]" in preview
    assert "importance: 6 → 5" in await server.restore_revision(open_first, ref, include_sealed=True)
    await restore(server, open_first, ref)
    assert (await manager.get(open_first))["metadata"]["sealed"] == 1

    sealed_first = await manager.create(content="secret text", sealed=True)
    await server.trace(sealed_first, sealed=0)
    sealed_ref = manager.list_bucket_revisions(sealed_first)[0]["ref"]
    listing = await server.list_revisions(sealed_first)
    assert "secret text" not in listing and "[sealed 正文已隐藏]" in listing
    assert "secret text" in await server.list_revisions(sealed_first, include_sealed=True)
    await restore(server, sealed_first, sealed_ref)
    assert (await manager.get(sealed_first))["metadata"]["sealed"] == 1


@pytest.mark.asyncio
async def test_legacy_history_of_currently_sealed_bucket_is_hidden_by_default(server):
    manager = server.bucket_mgr
    bucket = await manager.create(content="private draft")
    await server.trace(bucket, content="private final")
    await server.trace(bucket, sealed=1)
    listing = await server.list_revisions(bucket)
    assert "private draft" not in listing and "private final" not in listing
    assert "正文已隐藏" in listing
    assert "private draft" in await server.list_revisions(bucket, include_sealed=True)


@pytest.mark.asyncio
async def test_missing_counterparts_are_skipped_and_reported(server):
    manager = server.bucket_mgr
    a = await manager.create(content="a body", name="A")
    b = await manager.create(content="b body", name="B")
    c = await manager.create(content="c body", name="C")
    await server.trace(a, related=b)
    await server.trace(a, superseded_by=c)
    await server.trace(a, content="a later")
    await delete(server, b)
    await server.trace(a, superseded_by="")
    await delete(server, c)
    ref = ref_of(server, a, "replace")
    preview = await server.restore_revision(a, ref)
    assert f"related → {b}（target_missing）" in preview
    assert f"superseded_by → {c}（target_missing）" in preview
    _, result = await restore(server, a, ref)
    assert "已跳过" in result
    restored = await manager.get(a)
    assert restored["content"] == "a body"
    assert not restored["metadata"].get("superseded_by")
    report = manager.restoration_rows(a)[-1]["report"]
    assert {item["target"] for item in report["skipped"]} == {b, c}


@pytest.mark.asyncio
async def test_resurrect_keeps_original_id_and_replays_relations(server):
    manager = server.bucket_mgr
    a = await manager.create(content="resurrect me", name="Phoenix", tags=["t"])
    b = await manager.create(content="partner")
    successor = await manager.create(content="newer version")
    await server.trace(a, related=b)
    await server.trace(a, superseded_by=successor)
    await delete(server, a)
    assert a not in ((await manager.get(successor))["metadata"].get("supersedes") or [])
    assert a not in ((await manager.get(b))["metadata"].get("related_buckets") or "")
    listing = await server.list_revisions(a)
    assert "当前状态：已删除" in listing
    preview, _ = await restore(server, a, ref_of(server, a, "delete"))
    assert "复活已删除的桶" in preview
    restored = await manager.get(a)
    assert restored["id"] == a and restored["content"] == "resurrect me"
    assert restored["metadata"]["name"] == "Phoenix" and restored["metadata"]["tags"] == ["t"]
    assert b in restored["metadata"]["related_buckets"]
    assert a in (await manager.get(b))["metadata"]["related_buckets"]
    assert restored["metadata"]["superseded_by"] == successor
    assert a in (await manager.get(successor))["metadata"]["supersedes"]
    # A resurrected identity is an ordinary live bucket again.
    assert "已修改" in await server.trace(a, content="alive again")
    manager.admit_delayed_effect(a, None, "stable_update")
    manager._confirmed_embedding_admission(a)


@pytest.mark.asyncio
async def test_other_deleted_ids_stay_refused_and_old_incarnation_stays_dead(server):
    manager = server.bucket_mgr
    a = await manager.create(content="comes back")
    other = await manager.create(content="stays gone")
    old_guard = manager.delete_admission.capture(a)
    await delete(server, a)
    await delete(server, other)
    await restore(server, a, ref_of(server, a, "delete"))
    for kind in ("publication", "stable_update"):
        with pytest.raises(DeleteAdmissionError):
            manager.delete_admission.admit(other, kind=kind, allow_missing=True)
    with pytest.raises(Exception):
        manager._confirmed_embedding_admission(other)
    with pytest.raises(DeleteAdmissionError):
        manager.delete_admission.admit(a, expected_source=old_guard, kind="stale_effect")
    with pytest.raises(Exception, match="history_row_has_no_metadata|revision_not_found"):
        manager.plan_restore(other, "h1")


@pytest.mark.asyncio
async def test_redelete_after_resurrect_is_dead_again_and_can_be_restored_again(server):
    manager = server.bucket_mgr
    a = await manager.create(content="twice")
    await delete(server, a)
    await restore(server, a, ref_of(server, a, "delete"))
    await delete(server, a)
    with pytest.raises(DeleteAdmissionError):
        manager.delete_admission.admit(a, kind="publication", allow_missing=True)
    await restore(server, a, ref_of(server, a, "delete"))
    assert (await manager.get(a))["content"] == "twice"
    assert len(manager.restoration_rows(a)) == 2


# ----------------------------------------------------------------- A4 tokens
@pytest.mark.asyncio
async def test_restore_tokens_are_one_shot_expire_and_bind_the_plan(server, monkeypatch):
    manager = server.bucket_mgr
    bucket = await manager.create(content="token v1")
    await server.trace(bucket, content="token v2")
    ref = ref_of(server, bucket, "replace")
    preview = await server.restore_revision(bucket, ref)
    assert "no changes made" in preview and (await manager.get(bucket))["content"] == "token v2"
    first = token(preview)
    assert "已还原" in await server.restore_revision(bucket, ref, confirm_token=first)
    await server.trace(bucket, content="token v3")
    assert "invalid, expired, used, or stale" in await server.restore_revision(bucket, ref, confirm_token=first)

    stale = token(await server.restore_revision(bucket, ref))
    await server.trace(bucket, content="token v4")
    assert "invalid, expired, used, or stale" in await server.restore_revision(bucket, ref, confirm_token=stale)

    expiring = token(await server.restore_revision(bucket, ref))
    real = server.time.monotonic
    monkeypatch.setattr(server.time, "monotonic", lambda: real() + server._MUTATION_CONFIRM_TTL_SECONDS + 1)
    assert "invalid, expired, used, or stale" in await server.restore_revision(bucket, ref, confirm_token=expiring)
    assert (await manager.get(bucket))["content"] == "token v4"


@pytest.mark.asyncio
async def test_restore_rejects_foreign_or_unknown_revisions(server):
    manager = server.bucket_mgr
    a = await manager.create(content="a")
    b = await manager.create(content="b")
    await server.trace(b, content="b2")
    foreign = manager.list_bucket_revisions(b)[0]["ref"]
    assert "revision_not_found" in await server.restore_revision(a, foreign)
    assert "revision_ref_invalid" in await server.restore_revision(a, "x9")


# -------------------------------------------------------------- A3 recovery
@pytest.mark.asyncio
async def test_interrupted_publication_is_settled_and_followups_resume(server, monkeypatch):
    manager = server.bucket_mgr
    bucket = await manager.create(content="crash v1")
    await server.trace(bucket, content="crash v2")
    plan = manager.plan_restore(bucket, ref_of(server, bucket, "replace"))
    restore_id = manager._publish_restore(plan, "test")
    assert manager.restoration_rows(bucket)[0]["status"] == "published"
    assert "restore_in_progress" in str(pytest.raises(Exception, manager.plan_restore, bucket, "r1").value)
    resumed = await manager.resume_restorations(bucket)
    assert resumed == [restore_id]
    assert manager.restoration_rows(bucket)[0]["status"] == "completed"

    with sqlite3.connect(manager.history_db_path) as conn:
        conn.execute("UPDATE ob_bucket_restorations SET status='pending', restore_id='stuck' WHERE restore_id=?",
                     (restore_id,))
        conn.execute("UPDATE ob_bucket_restorations SET plan_json=json_set(plan_json, '$.file_sha256', 'nope') "
                     "WHERE restore_id='stuck'")
    with pytest.raises(DeleteAdmissionError, match="bucket_restore_pending"):
        manager.delete_admission.active(bucket)
    manager.recover_restorations()
    assert manager.restoration_rows(bucket)[0]["status"] == "failed"
    manager.delete_admission.active(bucket)


# ---------------------------------------------------------- A6 baseline backfill
def test_baseline_backfill_is_dry_run_by_default_and_idempotent(server, tmp_path):
    import asyncio
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    backfill = importlib.import_module("backfill_revision_baseline")
    manager = server.bucket_mgr
    first = asyncio.run(manager.create(content="baseline one", name="One", importance=6))
    second = asyncio.run(manager.create(content="baseline two", sealed=True))
    backup = tmp_path / "2026-10-08.json"
    backup.write_text(json.dumps(build_backup_payload(manager.base_dir), ensure_ascii=False), encoding="utf-8")

    dry = backfill.migrate(manager.base_dir, backup)
    assert dry["mode"] == "dry_run" and dry["to_insert"] == 2 and dry["inserted"] == 0
    assert manager.list_bucket_revisions(first) == []
    applied = backfill.migrate(manager.base_dir, backup, apply=True)
    assert applied["inserted"] == 2
    again = backfill.migrate(manager.base_dir, backup, apply=True)
    assert again["inserted"] == 0 and again["already_present"] == 2
    baseline = manager.list_bucket_revisions(first)
    assert len(baseline) == 1 and baseline[0]["op_kind"] == "baseline"
    assert baseline[0]["metadata"]["importance"] == 6
    assert manager.list_bucket_revisions(second)[0]["sealed_at_capture"] == 1
    assert backfill.main(["--buckets-dir", manager.base_dir, "--backup", str(backup)]) == 0


@pytest.mark.asyncio
async def test_sealed_result_cleans_vector_first_and_skips_refresh(server, monkeypatch):
    manager = server.bucket_mgr
    bucket = await manager.create(content="vector v1")
    await server.trace(bucket, content="vector v2")
    ref = ref_of(server, bucket, "replace")
    cleanup, refresh = AsyncMock(), AsyncMock()
    monkeypatch.setattr(manager, "_delete_ordinary_embedding", cleanup)
    monkeypatch.setattr(manager, "_refresh_ordinary_embedding_best_effort", refresh)
    await restore(server, bucket, ref)
    refresh.assert_awaited_once_with(bucket, "vector v1")
    cleanup.assert_not_awaited()
    refresh.reset_mock()
    await server.trace(bucket, sealed=1)
    cleanup.reset_mock()
    await restore(server, bucket, ref_of(server, bucket, "pre_restore"))
    cleanup.assert_awaited_with(bucket)
    refresh.assert_not_awaited()
    assert (await manager.get(bucket))["metadata"]["sealed"] == 1


# ------------------------------------------------- pending effects on resurrect
def _import_status(manager, key):
    with sqlite3.connect(manager.history_db_path) as conn:
        return conn.execute("SELECT status FROM ob_import_operations WHERE operation_key=?", (key,)).fetchone()[0]


@pytest.mark.asyncio
async def test_resurrect_is_refused_while_pending_effects_reference_the_id(server):
    manager = server.bucket_mgr
    a = await manager.create(content="stale target")
    key = "o5b:stale-update"
    manager._ensure_import_operation(key, operation_kind="update", target_bucket_id=a,
                                     payload={"kwargs": {"importance": 3}})
    await delete(server, a)
    ref = ref_of(server, a, "delete")
    preview = await server.restore_revision(a, ref)
    assert "no confirm_token issued" in preview and "confirm_token:" not in preview
    assert "import_operation: 1 条" in preview and key in preview
    plan = manager.plan_restore(a, ref)
    assert plan["blocked"] and plan["pending_effects"] == [{"kind": "import_operation", "id": key}]
    with pytest.raises(server.RestoreError, match="resurrect_blocked_by_pending_effects"):
        await manager.execute_restore(plan)
    assert await manager.get(a) is None
    assert _import_status(manager, key) == "planned"  # never voided or deleted

    manager._mark_import_operation_applied(key)
    await restore(server, a, ref)
    assert (await manager.get(a))["content"] == "stale target"


@pytest.mark.asyncio
async def test_unfinished_digest_and_merge_records_block_resurrect(server):
    manager = server.bucket_mgr
    a = await manager.create(content="digest source")
    state = server._digest_bucket_state(await manager.get(a))
    manager.write_digest_operation("d1", "rebalance", {"importance_rebalance": [state]},
                                   owner="gone", status="failed")
    await delete(server, a)
    preview = await server.restore_revision(a, ref_of(server, a, "delete"))
    assert "digest_operation: 1 条" in preview and "confirm_token:" not in preview

    b = await manager.create(content="merge source")
    await delete(server, b)
    manager.write_merge_operation("m1", {"target_id": "t0", "source_id": b, "relations": []},
                                  status="failed", completed=[], create=True)
    assert "merge_operation: 1 条" in await server.restore_revision(b, ref_of(server, b, "delete"))


@pytest.mark.asyncio
async def test_pending_effect_appearing_after_preview_invalidates_token_and_publish(server):
    manager = server.bucket_mgr
    a = await manager.create(content="race")
    await delete(server, a)
    ref = ref_of(server, a, "delete")
    preview = await server.restore_revision(a, ref)
    plan = manager.plan_restore(a, ref)
    manager._ensure_import_operation("o5b:late", operation_kind="update", target_bucket_id=a,
                                     payload={"kwargs": {"importance": 2}})
    late = await server.restore_revision(a, ref, confirm_token=token(preview))
    assert "no confirm_token issued" in late and "o5b:late" in late
    with pytest.raises(server.RestoreError, match="resurrect_blocked_by_pending_effects"):
        manager._publish_restore(plan, "test")
    assert await manager.get(a) is None and manager.restoration_rows(a) == []


@pytest.mark.asyncio
async def test_pending_effects_do_not_block_restoring_a_live_bucket(server):
    manager = server.bucket_mgr
    a = await manager.create(content="live v1")
    await server.trace(a, content="live v2")
    manager._ensure_import_operation("o5b:live", operation_kind="update", target_bucket_id=a,
                                     payload={"kwargs": {"importance": 2}})
    plan = manager.plan_restore(a, ref_of(server, a, "replace"))
    assert plan["mode"] == "existing" and not plan["blocked"] and plan["pending_effects"] == []

"""W-4B2 completion, projection, confirmation and durable replay invariants."""
import asyncio
import importlib
import sys
from pathlib import Path
from unittest.mock import AsyncMock
from uuid import uuid4

import frontmatter
import pytest

from bucket_manager import (
    BucketManager, active_todo_projection, automatic_todo_provenance,
    merge_todo_provenance, prepare_todo_provenance, reconcile_todo_provenance,
)

AT = "2026-09-26T12:34:56"
LATER = "2026-09-27T12:34:56"


def rec(text="same", identity=None, **fields):
    return {**automatic_todo_provenance([text])[0], "id": identity or "todo_" + str(uuid4()), **fields}


def snapshot(root):
    return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()}


@pytest.fixture
def server(tmp_path, monkeypatch):
    monkeypatch.setenv("OMBRE_BUCKETS_DIR", str(tmp_path / "buckets"))
    monkeypatch.delenv("OMBRE_API_KEY", raising=False)
    sys.modules.pop("server", None)
    module = importlib.import_module("server")
    module.decay_engine.ensure_started = AsyncMock(return_value=None)
    return module


def token(preview):
    return preview.split("confirm_token:", 1)[1].strip()


async def setup(server, records=None):
    records = records or [rec("one"), rec("two")]
    bucket = await server.bucket_mgr.create("body", todos=list(dict.fromkeys(r["text"] for r in records)), todo_provenance=records)
    return bucket, records


async def finish(server, bucket, identity):
    preview = await server.trace(bucket, todo_done=identity)
    return await server.trace(bucket, todo_done=identity, confirm_token=token(preview))


@pytest.mark.asyncio
async def test_preview_commit_and_response_loss_are_stable(server, tmp_path, monkeypatch):
    bucket, records = await setup(server)
    before_bucket = await server.bucket_mgr.get(bucket)
    before = snapshot(tmp_path)
    preview = await server.trace(bucket, todo_done=records[0]["id"])
    assert all(value in preview for value in (bucket, records[0]["id"], "one", "pending", "mark completed", "resolved"))
    assert snapshot(tmp_path) == before
    monkeypatch.setattr("bucket_manager.now_iso", lambda: AT)
    confirmed = await server.trace(bucket, todo_done=records[0]["id"], confirm_token=token(preview))
    assert "todo completed" in confirmed and AT in confirmed
    current = await server.bucket_mgr.get(bucket)
    expected = {**before_bucket, "metadata": {**before_bucket["metadata"], "todo_provenance": [{**records[0], "done_at": AT}, records[1]]}}
    assert current == expected
    assert current["metadata"].get("resolved", False) is False
    monkeypatch.setattr("bucket_manager.now_iso", lambda: pytest.fail("retry must not request a new time"))
    committed = snapshot(tmp_path)
    for supplied in (None, token(preview), "wrong"):
        result = await server.trace(bucket, todo_done=records[0]["id"], **({"confirm_token": supplied} if supplied else {}))
        assert "already completed" in result and AT in result
        assert snapshot(tmp_path) == committed
    restarted = BucketManager(server.config)
    assert restarted.preview_todo_completion(bucket, records[0]["id"])["target"]["done_at"] == AT


@pytest.mark.asyncio
@pytest.mark.parametrize("value", ["one", "0", "todo_bad", "", 0])
async def test_non_id_and_legacy_are_zero_write(server, tmp_path, value):
    bucket, records = await setup(server)
    path = Path((await server.bucket_mgr.get(bucket))["path"])
    post = frontmatter.load(path)
    post.metadata.pop("todo_provenance")
    path.write_text(frontmatter.dumps(post))
    before = snapshot(tmp_path)
    result = await server.trace(bucket, todo_done=value)
    assert "stable" in result and "legacy" in result
    assert snapshot(tmp_path) == before
    result = await server.trace(bucket, todo_done=records[0]["id"])
    assert "unknown todo ID" in result
    assert snapshot(tmp_path) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("extra", [
    {"name": "changed"}, {"domain": "x"}, {"valence": 0}, {"arousal": 0},
    {"importance": 5}, {"tags": "tag"}, {"todos": []}, {"todo_items": []},
    {"resolved": 1}, {"pinned": 1}, {"permanent": 0}, {"digested": 1},
    {"dormant": 0}, {"sealed": 1}, {"content": "changed"},
    {"provenance_kind": "summary"}, {"related": "other"}, {"unrelate": "other"},
    {"superseded_by": ""}, {"merge": "other"}, {"append": True},
    {"trigger_date": "none"}, {"delete": True},
])
async def test_mixed_mutations_rejected_before_any_write(server, tmp_path, extra):
    bucket, records = await setup(server)
    before = snapshot(tmp_path)
    assert "alone" in await server.trace(bucket, todo_done=records[0]["id"], **extra)
    assert snapshot(tmp_path) == before


@pytest.mark.asyncio
async def test_same_text_ids_and_legacy_projection(server, tmp_path):
    bucket, records = await setup(server, [rec(), rec()])
    await finish(server, bucket, records[0]["id"])
    assert "same" in await server.todos()
    detail = await server.todos(include_provenance=True)
    assert records[0]["id"] not in detail and records[1]["id"] in detail
    await finish(server, bucket, records[1]["id"])
    assert "same" not in await server.todos()
    post = frontmatter.load((await server.bucket_mgr.get(bucket))["path"])
    post["todo_provenance"].append(automatic_todo_provenance(["same"])[0])
    path = Path((await server.bucket_mgr.get(bucket))["path"])
    path.write_text(frontmatter.dumps(post))
    before = snapshot(tmp_path)
    assert "same" in await server.todos()
    assert "todo_id:null" in await server.todos(include_provenance=True)
    assert snapshot(tmp_path) == before
    assert (await server.bucket_mgr.get(bucket))["metadata"]["todos"] == ["same"]


@pytest.mark.asyncio
async def test_tokens_bind_bucket_identity_and_state_not_unrelated_metadata(server, tmp_path):
    bucket, records = await setup(server)
    other, _ = await setup(server, records)
    preview = await server.trace(bucket, todo_done=records[0]["id"])
    for target, identity in [(bucket, records[1]["id"]), (other, records[0]["id"])]:
        before = snapshot(tmp_path)
        assert "confirmation invalid" in await server.trace(target, todo_done=identity, confirm_token=token(preview))
        assert snapshot(tmp_path) == before
    await server.trace(bucket, tags="unrelated", importance=8, content="different body")
    assert "todo completed" in await server.trace(bucket, todo_done=records[0]["id"], confirm_token=token(preview))
    pending = await server.trace(bucket, todo_done=records[1]["id"])
    await server.trace(bucket, todo_items=[{**records[0]}, {**records[1], "text": "renamed"}])
    before = snapshot(tmp_path)
    assert "stale" in await server.trace(bucket, todo_done=records[1]["id"], confirm_token=token(pending))
    assert snapshot(tmp_path) == before
    assert "batch" in await server.trace(bucket + "," + other, todo_done=records[1]["id"])
    assert snapshot(tmp_path) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["serialize", "fsync", "replace"])
async def test_write_failure_consumes_token_without_false_completion(server, tmp_path, monkeypatch, stage):
    bucket, records = await setup(server)
    preview = await server.trace(bucket, todo_done=records[0]["id"])
    before = snapshot(tmp_path)
    with monkeypatch.context() as scoped:
        target = {"serialize": "bucket_manager.frontmatter.dumps", "fsync": "bucket_manager.os.fsync",
                  "replace": "bucket_manager.os.replace"}[stage]
        scoped.setattr(target, lambda *a: (_ for _ in ()).throw(OSError("injected")))
        assert "write failed" in await server.trace(bucket, todo_done=records[0]["id"], confirm_token=token(preview))
    assert snapshot(tmp_path) == before
    assert "confirmation invalid" in await server.trace(bucket, todo_done=records[0]["id"], confirm_token=token(preview))
    assert "todo completed" in await finish(server, bucket, records[0]["id"])


@pytest.mark.parametrize("incoming", [{}, {"done_at": None}, {"done_at": AT}])
@pytest.mark.parametrize("speaker", ["unknown", "ting", "model"])
def test_completion_sticky_in_every_shared_helper(incoming, speaker):
    original = rec(done_at=AT, said_by="ting")
    source = {**original, "said_by": speaker}
    source.pop("done_at")
    source.update(incoming)
    for result in (
        reconcile_todo_provenance(["same"], [original, source], strict=True),
        prepare_todo_provenance(["same"], [source], previous_todos=["same"], previous_provenance=[original]),
        merge_todo_provenance(["same"], [original], ["same"], [source])[1],
    ):
        assert len(result) == 1 and result[0]["done_at"] == AT
    assert prepare_todo_provenance([], [], previous_todos=["same"], previous_provenance=[original]) == [original]


def test_conflicting_completion_never_arbitrated_and_legacy_remains_active():
    original = rec(done_at=AT)
    conflicting = {**original, "done_at": LATER}
    for strict in (False, True):
        with pytest.raises(ValueError, match="completion conflict"):
            reconcile_todo_provenance(["same"], [original, conflicting], strict=strict)
    with pytest.raises(ValueError, match="completion conflict"):
        prepare_todo_provenance(["same"], [conflicting], previous_todos=["same"], previous_provenance=[original])
    with pytest.raises(ValueError, match="completion conflict"):
        merge_todo_provenance(["same"], [original], ["same"], [conflicting])
    texts, records = merge_todo_provenance(["same"], [original], ["same"], None, source_is_persisted=True)
    assert active_todo_projection(texts, records)[0] == ["same"]
    prepared = prepare_todo_provenance(texts, records, previous_todos=["same"], previous_provenance=[original])
    assert any("id" not in r for r in prepared)
    assert active_todo_projection(["same"], [original])[0] == []
    assert active_todo_projection(["legacy"], None)[0] == ["legacy"]


@pytest.mark.asyncio
async def test_pending_durable_apply_and_restart_preserve_completion(server, tmp_path, monkeypatch):
    bucket, records = await setup(server)
    manager = server.bucket_mgr
    key = "o5b:" + "a" * 64
    plan = manager.plan_import_operation(key, operation_kind="update", target_bucket_id=bucket,
        payload={"kwargs": {"todos": ["one", "two"], "todo_provenance": records, "content": "planned body"}})
    monkeypatch.setattr("bucket_manager.now_iso", lambda: AT)
    await finish(server, bucket, records[0]["id"])
    restarted = BucketManager(server.config)
    await restarted.apply_import_operation(key)
    assert restarted.inspect_import_operation(key)["payload"] == plan["payload"]
    assert restarted.preview_todo_completion(bucket, records[0]["id"])["target"]["done_at"] == AT
    before = snapshot(tmp_path)
    await restarted.apply_import_operation(key)
    assert snapshot(tmp_path) == before
    assert "one" not in await server.todos()
    before = snapshot(tmp_path)
    assert "server-generated" in await server.trace(bucket, todo_items=[{**records[0], "done_at": LATER}])
    assert snapshot(tmp_path) == before


@pytest.mark.asyncio
async def test_sealed_async_update_reloads_after_completion(server):
    bucket, records = await setup(server)
    entered, release = asyncio.Event(), asyncio.Event()
    async def cleanup(_):
        entered.set()
        await release.wait()
    server.bucket_mgr._delete_ordinary_embedding = cleanup
    task = asyncio.create_task(server.bucket_mgr.update(bucket, sealed=1, content="changed"))
    await entered.wait()
    await finish(server, bucket, records[0]["id"])
    done_at = server.bucket_mgr.preview_todo_completion(bucket, records[0]["id"])["target"]["done_at"]
    release.set()
    assert await task
    assert server.bucket_mgr.preview_todo_completion(bucket, records[0]["id"])["target"]["done_at"] == done_at


@pytest.mark.asyncio
async def test_merge_resume_after_target_completion(server):
    target, records = await setup(server, [rec("task")])
    source = await server.bucket_mgr.create("source", todos=["new"])
    preview = await server.trace(target, merge=source)
    execute = server._execute_merge_operation
    server._execute_merge_operation = AsyncMock(return_value="deferred")
    await server.trace(target, merge=source, confirm_token=token(preview))
    server._execute_merge_operation = execute
    operation = server.bucket_mgr.read_merge_operations()[-1]
    # Apply the target atomically, then simulate response loss before checkpoint.
    await server.bucket_mgr.apply_import_operation("merge:" + operation["operation_id"] + ":target",
        operation_kind="update", target_bucket_id=target, payload={"kwargs": operation["plan"]["target_update"]})
    await finish(server, target, records[0]["id"])
    done_at = server.bucket_mgr.preview_todo_completion(target, records[0]["id"])["target"]["done_at"]
    result = await execute(operation)
    assert "已合并" in result
    assert await server.bucket_mgr.get(source) is None
    assert server.bucket_mgr.preview_todo_completion(target, records[0]["id"])["target"]["done_at"] == done_at


def test_orphaned_completion_cannot_change_timestamp_or_identity():
    original = rec(done_at=AT)
    assert prepare_todo_provenance([], [original], previous_todos=[], previous_provenance=[original]) == [original]
    for incoming in [{**original, "done_at": LATER}, {**original, "text": "changed"}]:
        with pytest.raises(ValueError, match="conflict"):
            prepare_todo_provenance([], [incoming], previous_todos=[], previous_provenance=[original])
    texts, records = merge_todo_provenance(["same"], None, ["same"], [original], source_is_persisted=True)
    assert active_todo_projection(texts, records)[0] == ["same"]
    assert any("id" not in r for r in prepare_todo_provenance(texts, records,
        previous_todos=["same"], previous_provenance=None))


@pytest.mark.asyncio
async def test_expired_and_provenance_stale_confirmations_are_zero_write(server, tmp_path):
    bucket, records = await setup(server)
    pending = token(await server.trace(bucket, todo_done=records[0]["id"]))
    server._mutation_confirm_tokens[pending]["expires_at"] = 0
    before = snapshot(tmp_path)
    assert "expired" in await server.trace(bucket, todo_done=records[0]["id"], confirm_token=pending)
    assert snapshot(tmp_path) == before
    pending = token(await server.trace(bucket, todo_done=records[0]["id"]))
    await server.trace(bucket, todo_items=[{**records[0], "said_by": "ting"}, records[1]])
    before = snapshot(tmp_path)
    assert "stale" in await server.trace(bucket, todo_done=records[0]["id"], confirm_token=pending)
    assert snapshot(tmp_path) == before
    unknown = "todo_" + str(uuid4())
    assert "unknown todo ID" in await server.trace(bucket, todo_done=unknown)
    assert snapshot(tmp_path) == before


@pytest.mark.asyncio
async def test_conflicting_direct_update_is_zero_write(server, tmp_path):
    bucket, records = await setup(server)
    await finish(server, bucket, records[0]["id"])
    completed = server.bucket_mgr.preview_todo_completion(bucket, records[0]["id"])["target"]
    before = snapshot(tmp_path)
    conflicting = [{**completed, "done_at": LATER}, records[1]]
    assert not await server.bucket_mgr.update(bucket, todo_provenance=conflicting)
    assert snapshot(tmp_path) == before
    with pytest.raises(ValueError, match="completion conflict"):
        server.bucket_mgr.plan_import_operation("o5b:" + "b" * 64,
            operation_kind="update", target_bucket_id=bucket,
            payload={"kwargs": {"todos": ["one", "two"], "todo_provenance": conflicting}})
    assert snapshot(tmp_path) == before


@pytest.mark.asyncio
async def test_merge_source_completion_between_validation_and_delete_is_retained(server):
    target, _ = await setup(server, [rec("target task")])
    source, records = await setup(server, [rec("source task")])
    preview = await server.trace(target, merge=source)
    delete = server.bucket_mgr.delete
    completed = {}
    async def interleave(bucket, **kwargs):
        completed.update(server.bucket_mgr.complete_todo(bucket, records[0]["id"], lambda _: True))
        return await delete(bucket, **kwargs)
    server.bucket_mgr.delete = interleave
    result = await server.trace(target, merge=source, confirm_token=token(preview))
    assert "source deletion failed" in result
    assert (await server.bucket_mgr.get(source)) is not None
    assert server.bucket_mgr.preview_todo_completion(source, records[0]["id"])["target"]["done_at"] == completed["done_at"]


@pytest.mark.asyncio
async def test_mcp_client_completion_and_injected_done_at_contract(server, tmp_path):
    from mcp.shared.memory import create_connected_server_and_client_session
    bucket, records = await setup(server)
    before = snapshot(tmp_path)
    async with create_connected_server_and_client_session(server.mcp) as client:
        preview = await client.call_tool("trace", {"bucket_id": bucket, "todo_done": records[0]["id"]})
        assert not preview.isError
        preview_text = "\n".join(item.text for item in preview.content if hasattr(item, "text"))
        assert snapshot(tmp_path) == before
        confirmed = await client.call_tool("trace", {"bucket_id": bucket, "todo_done": records[0]["id"],
                                                     "confirm_token": token(preview_text)})
        assert not confirmed.isError
        assert "todo completed" in "\n".join(item.text for item in confirmed.content if hasattr(item, "text"))
        committed = snapshot(tmp_path)
        invalid = await client.call_tool("trace", {"bucket_id": bucket, "todo_items": [{**records[0], "done_at": LATER}]})
        assert "server-generated" in "\n".join(item.text for item in invalid.content if hasattr(item, "text"))
        assert snapshot(tmp_path) == committed

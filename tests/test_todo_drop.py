"""W-4D explicit abandonment, sticky terminal history and confirmation."""
from copy import deepcopy
from unittest.mock import AsyncMock
from uuid import uuid4

import frontmatter
import pytest

from bucket_manager import (
    BucketManager, _merge_todo_terminal_state, _todo_provenance_record,
    active_todo_projection, automatic_todo_provenance, merge_todo_provenance,
    prepare_todo_provenance, reconcile_todo_provenance,
)
from tests.test_todo_completion import AT, LATER, finish, rec, server, setup, snapshot, token


async def abandon(server, bucket, identity):
    preview = await server.trace(bucket, todo_drop=identity)
    return await server.trace(bucket, todo_drop=identity, confirm_token=token(preview))


@pytest.mark.asyncio
async def test_preview_confirm_preserves_history_and_lifecycle(server, tmp_path, monkeypatch):
    bucket, records = await setup(server)
    original = await server.bucket_mgr.get(bucket)
    before = snapshot(tmp_path)
    preview = await server.trace(bucket, todo_drop=records[0]["id"])
    for fragment in (bucket, records[0]["id"], "one", "current=active", "放弃", "不等于已完成", "不删除历史", "不自动 resolved"):
        assert fragment in preview
    assert snapshot(tmp_path) == before
    monkeypatch.setattr("bucket_manager.now_iso", lambda: AT)
    receipt = await server.trace(bucket, todo_drop=records[0]["id"], confirm_token=token(preview))
    assert "todo dropped" in receipt and AT in receipt
    assert token(preview) not in server._mutation_confirm_tokens
    expected = deepcopy(original)
    expected["metadata"]["todo_provenance"][0]["dropped_at"] = AT
    assert await server.bucket_mgr.get(bucket) == expected
    before = snapshot(tmp_path)
    monkeypatch.setattr("bucket_manager.now_iso", lambda: (_ for _ in ()).throw(AssertionError("retry generated time")))
    # Discard the receipt, restart the storage manager, then replay old/bogus/no token.
    server.bucket_mgr = BucketManager(server.config)
    for supplied in (None, token(preview), "wrong-token"):
        result = await server.trace(bucket, todo_drop=records[0]["id"], **({"confirm_token": supplied} if supplied else {}))
        assert "already dropped" in result and AT in result
        assert snapshot(tmp_path) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["serialize", "fsync", "replace"])
async def test_atomic_failure_consumes_token_and_requires_new_preview(server, tmp_path, monkeypatch, stage):
    bucket, records = await setup(server)
    preview = await server.trace(bucket, todo_drop=records[0]["id"])
    before = snapshot(tmp_path)
    with monkeypatch.context() as scoped:
        target = {"serialize": "bucket_manager.frontmatter.dumps", "fsync": "bucket_manager.os.fsync", "replace": "bucket_manager.os.replace"}[stage]
        scoped.setattr(target, lambda *a: (_ for _ in ()).throw(OSError("injected")))
        result = await server.trace(bucket, todo_drop=records[0]["id"], confirm_token=token(preview))
        assert "write failed" in result and "token consumed" in result
    assert snapshot(tmp_path) == before
    assert not server.bucket_mgr.preview_todo_drop(bucket, records[0]["id"])["target"].get("dropped_at")
    assert "confirmation invalid" in await server.trace(bucket, todo_drop=records[0]["id"], confirm_token=token(preview))
    assert "todo dropped" in await abandon(server, bucket, records[0]["id"])


@pytest.mark.asyncio
async def test_token_bucket_todo_operation_and_metadata_binding(server, tmp_path):
    bucket, records = await setup(server)
    other, _ = await setup(server, records)
    preview = await server.trace(bucket, todo_drop=records[0]["id"])
    for target, identity in ((other, records[0]["id"]), (bucket, records[1]["id"])):
        before = snapshot(tmp_path)
        assert "confirmation invalid" in await server.trace(target, todo_drop=identity, confirm_token=token(preview))
        assert snapshot(tmp_path) == before
    before = snapshot(tmp_path)
    assert "confirmation invalid" in await server.trace(bucket, todo_done=records[0]["id"], confirm_token=token(preview))
    assert snapshot(tmp_path) == before
    done_preview = await server.trace(bucket, todo_done=records[0]["id"])
    assert "confirmation invalid" in await server.trace(bucket, todo_drop=records[0]["id"], confirm_token=token(done_preview))
    await server.trace(bucket, name="renamed bucket", content="new body", tags="tag", importance=8)
    assert "todo dropped" in await server.trace(bucket, todo_drop=records[0]["id"], confirm_token=token(preview))


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["text", "attribution", "identity", "other_todo", "expiry"])
async def test_stale_and_expired_drop_tokens_are_zero_write(server, tmp_path, change):
    bucket, records = await setup(server)
    pending = token(await server.trace(bucket, todo_drop=records[0]["id"]))
    if change == "expiry":
        server._mutation_confirm_tokens[pending]["expires_at"] = 0
    elif change == "other_todo":
        await finish(server, bucket, records[1]["id"])
    elif change == "identity":
        replacement = rec("one")
        assert await server.bucket_mgr.update(bucket, todo_provenance=[replacement, records[1]])
    else:
        revised = {**records[0], **({"text": "renamed"} if change == "text" else {"said_by": "ting"})}
        await server.trace(bucket, todo_items=[revised, records[1]])
    before = snapshot(tmp_path)
    result = await server.trace(bucket, todo_drop=records[0]["id"], confirm_token=pending)
    assert ("unknown todo ID" if change == "identity" else "confirmation invalid") in result
    assert snapshot(tmp_path) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("value", ["task text", "0", "todo_bad", None, [], ["todo_x", "todo_y"], "todo_x,todo_y"])
async def test_invalid_drop_targets_are_zero_write(server, tmp_path, value):
    bucket, _ = await setup(server)
    before = snapshot(tmp_path)
    assert "stable" in await server.trace(bucket, todo_drop=value)
    assert snapshot(tmp_path) == before


@pytest.mark.asyncio
async def test_unknown_legacy_and_multi_bucket_targets(server, tmp_path):
    bucket, records = await setup(server)
    other, _ = await setup(server)
    path = server.bucket_mgr._find_bucket_file(other)
    post = frontmatter.load(path)
    post.metadata.pop("todo_provenance")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(frontmatter.dumps(post))
    before = snapshot(tmp_path)
    assert "unknown todo ID" in await server.trace(bucket, todo_drop="todo_" + str(uuid4()))
    assert "该 todo 为旧格式，无稳定 ID，当前不能单条放弃。" in await server.trace(other, todo_drop=records[0]["id"])
    assert "batch" in await server.trace(bucket + "," + other, todo_drop=records[0]["id"])
    assert snapshot(tmp_path) == before


EXTRAS = [
    {"name": "changed"}, {"domain": "x"}, {"valence": 0}, {"arousal": 0}, {"importance": 5},
    {"tags": "tag"}, {"todos": []}, {"todo_items": []}, {"resolved": 1}, {"pinned": 1},
    {"permanent": 0}, {"digested": 1}, {"dormant": 0}, {"sealed": 1}, {"content": "x"},
    {"provenance_kind": "summary"}, {"related": "x"}, {"unrelate": "x"}, {"superseded_by": ""},
    {"merge": "x"}, {"append": True}, {"trigger_date": "none"}, {"delete": True}, {"todo_done": "todo_x"},
    {"name": ""}, {"importance": -1}, {"resolved": -1}, {"delete": False}, {"todos": None}, {"todo_done": None},
]


@pytest.mark.asyncio
@pytest.mark.parametrize("extra", EXTRAS)
@pytest.mark.parametrize("transport", ["direct", "mcp"])
async def test_raw_presence_guard_rejects_mutations_and_explicit_defaults(server, tmp_path, extra, transport):
    bucket, records = await setup(server)
    before = snapshot(tmp_path)
    arguments = {"bucket_id": bucket, "todo_drop": records[0]["id"], **extra}
    if transport == "direct":
        result = await server.trace(**arguments)
    else:
        from mcp.shared.memory import create_connected_server_and_client_session
        async with create_connected_server_and_client_session(server.mcp) as client:
            response = await client.call_tool("trace", arguments)
            assert response.isError
            result = "\n".join(item.text for item in response.content if hasattr(item, "text"))
    assert "alone" in result
    assert snapshot(tmp_path) == before


@pytest.mark.asyncio
async def test_mcp_drop_contract_and_other_parsing_unchanged(server, tmp_path):
    from mcp.shared.memory import create_connected_server_and_client_session
    bucket, records = await setup(server)
    before = snapshot(tmp_path)
    async with create_connected_server_and_client_session(server.mcp) as client:
        schemas = {tool.name: tool.inputSchema for tool in (await client.list_tools()).tools}
        trace_schema = schemas["trace"]
        assert "todo_drop" in trace_schema["properties"] and "todo_drop" not in trace_schema["required"]
        item = trace_schema["properties"]["todo_items"]["anyOf"][0]["items"]
        assert "done_at" not in item["properties"] and "dropped_at" not in item["properties"]
        assert item["additionalProperties"] is False
        invalid = await client.call_tool("trace", {"bucket_id": bucket, "todo_drop": None})
        assert invalid.isError
        assert "stable" in invalid.content[0].text
        preview = await client.call_tool("trace", {"bucket_id": bucket, "todo_drop": records[0]["id"]})
        text = "\n".join(item.text for item in preview.content if hasattr(item, "text"))
        assert snapshot(tmp_path) == before
        result = await client.call_tool("trace", {"bucket_id": bucket, "todo_drop": records[0]["id"], "confirm_token": token(text)})
        assert "todo dropped" in result.content[0].text
        # Existing JSON pre-parsing and explicit defaults remain accepted without drop.
        update = await client.call_tool("trace", {"bucket_id": bucket, "todos": '["one", "two"]', "name": ""})
        assert not update.isError
        assert server.bucket_mgr.preview_todo_drop(bucket, records[0]["id"])["target"].get("dropped_at")
        assert not (await client.call_tool("todos", {"include_provenance": False})).isError


@pytest.mark.parametrize("current,incoming,field,conflict", [
    ({}, {}, None, False), ({}, {"done_at": AT}, "done_at", False),
    ({"done_at": AT}, {}, "done_at", False), ({}, {"dropped_at": AT}, "dropped_at", False),
    ({"dropped_at": AT}, {}, "dropped_at", False),
    ({"done_at": AT}, {"done_at": AT}, "done_at", False),
    ({"done_at": AT}, {"done_at": LATER}, None, True),
    ({"dropped_at": AT}, {"dropped_at": AT}, "dropped_at", False),
    ({"dropped_at": AT}, {"dropped_at": LATER}, None, True),
    ({"done_at": AT}, {"dropped_at": AT}, None, True),
    ({"dropped_at": AT}, {"done_at": AT}, None, True),
])
def test_terminal_merge_matrix_in_every_shared_helper(current, incoming, field, conflict):
    first = rec(**current)
    second = {**rec(identity=first["id"]), **incoming}
    calls = [
        lambda: _merge_todo_terminal_state(first, second),
        lambda: reconcile_todo_provenance(["same"], [first, second], strict=True)[0],
        lambda: reconcile_todo_provenance(["same"], [first, second], strict=False)[0],
        lambda: prepare_todo_provenance(["same"], [second], previous_todos=["same"], previous_provenance=[first])[0],
        lambda: merge_todo_provenance(["same"], [first], ["same"], [second])[1][0],
    ]
    for call in calls:
        if conflict:
            with pytest.raises(ValueError, match="conflict"):
                call()
        else:
            result = call()
            assert result.get(field) == AT if field else not (result.get("done_at") or result.get("dropped_at"))


@pytest.mark.parametrize("incoming", [{}, {"dropped_at": None}, {"dropped_at": AT}])
@pytest.mark.parametrize("speaker", ["unknown", "ting", "model"])
def test_dropped_at_sticky_attribution_and_orphan_history(incoming, speaker):
    original = rec(dropped_at=AT, said_by="ting")
    source = {**original, "said_by": speaker}
    source.pop("dropped_at")
    source.update(incoming)
    for records in (
        reconcile_todo_provenance(["same"], [original, source]),
        prepare_todo_provenance(["same"], [source], previous_todos=["same"], previous_provenance=[original]),
        merge_todo_provenance(["same"], [original], ["same"], [source])[1],
    ):
        assert len(records) == 1 and records[0]["dropped_at"] == AT
    assert prepare_todo_provenance([], [], previous_todos=["same"], previous_provenance=[original]) == [original]
    assert reconcile_todo_provenance([], [original], strict=True) == [original]
    assert prepare_todo_provenance([], [original], previous_todos=[], previous_provenance=[original]) == [original]
    for incoming in ({**original, "dropped_at": LATER}, {**original, "text": "changed"}):
        with pytest.raises(ValueError, match="conflict"):
            prepare_todo_provenance([], [incoming], previous_todos=[], previous_provenance=[original])


@pytest.mark.parametrize("strict", [False, True])
@pytest.mark.parametrize("malformed", [{}, {"extra": True}, {"said_by": "invalid"}, {"text": "different"}, {"id": None}])
def test_dual_terminal_precedes_malformed_fallback(strict, malformed):
    dual = {**rec(done_at=AT, dropped_at=LATER), **malformed}
    with pytest.raises(ValueError, match="terminal conflict"):
        _todo_provenance_record(dual, strict=strict)
    with pytest.raises(ValueError, match="terminal conflict"):
        active_todo_projection(["same"], [dual])
    first = rec(done_at=AT)
    if "id" not in malformed:
        second = {**rec(identity=first["id"], dropped_at=AT), **malformed}
        with pytest.raises(ValueError, match="terminal conflict"):
            reconcile_todo_provenance(["same"], [first, second], strict=strict)


@pytest.mark.parametrize("field", ["done_at", "dropped_at"])
@pytest.mark.parametrize("value", [None, AT])
@pytest.mark.asyncio
async def test_external_timestamps_including_null_rejected(server, tmp_path, field, value):
    bucket, records = await setup(server)
    before = snapshot(tmp_path)
    submitted = [{**records[0], field: value}, records[1]]
    assert "server-generated" in await server.trace(bucket, todo_items=submitted)
    from mcp.shared.memory import create_connected_server_and_client_session
    async with create_connected_server_and_client_session(server.mcp) as client:
        result = await client.call_tool("trace", {"bucket_id": bucket, "todo_items": submitted})
        assert "server-generated" in result.content[0].text
    assert snapshot(tmp_path) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [None, AT])
async def test_external_dual_state_fields_rejected(server, tmp_path, value):
    bucket, records = await setup(server)
    before = snapshot(tmp_path)
    submitted = [{**records[0], "done_at": value, "dropped_at": value}, records[1]]
    assert "server-generated" in await server.trace(bucket, todo_items=submitted)
    assert snapshot(tmp_path) == before


def test_persisted_state_validation_and_legacy_same_text_projection():
    for raw in (rec(), rec(done_at=None, dropped_at=None), rec(dropped_at=AT)):
        assert _todo_provenance_record(raw, strict=True) == raw
    for timestamp in ("", "not-a-date", 123):
        with pytest.raises(ValueError, match="dropped_at"):
            _todo_provenance_record(rec(dropped_at=timestamp), strict=True)
    with pytest.raises(ValueError, match="dropped_at"):
        _todo_provenance_record({**automatic_todo_provenance(["same"])[0], "dropped_at": AT}, strict=True)
    dropped, active = rec(dropped_at=AT), rec()
    assert active_todo_projection(["same"], [dropped])[0] == []
    assert active_todo_projection(["same"], [dropped, active]) == (["same"], [active])
    texts, records = merge_todo_provenance(["same"], [dropped], ["same"], None, source_is_persisted=True)
    prepared = prepare_todo_provenance(texts, records, previous_todos=["same"], previous_provenance=[dropped])
    assert any("id" not in r for r in prepared)
    assert active_todo_projection(texts, prepared)[0] == ["same"]


@pytest.mark.asyncio
async def test_todos_same_text_active_identity_and_legacy(server, tmp_path):
    bucket, records = await setup(server, [rec(), rec()])
    await abandon(server, bucket, records[0]["id"])
    detail = await server.todos(include_provenance=True)
    assert records[0]["id"] not in detail and records[1]["id"] in detail
    assert "same" in await server.todos()
    await abandon(server, bucket, records[1]["id"])
    assert "same" not in await server.todos()
    path = server.bucket_mgr._find_bucket_file(bucket)
    post = frontmatter.load(path)
    post["todo_provenance"].append(automatic_todo_provenance(["same"])[0])
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(frontmatter.dumps(post))
    before = snapshot(tmp_path)
    assert "todo_id:null" in await server.todos(include_provenance=True)
    assert snapshot(tmp_path) == before


@pytest.mark.asyncio
async def test_pending_import_restart_replay_and_replacement_preserve_drop(server, tmp_path, monkeypatch):
    bucket, records = await setup(server)
    manager = server.bucket_mgr
    key = "o5b:" + "d" * 64
    plan = manager.plan_import_operation(key, operation_kind="update", target_bucket_id=bucket,
        payload={"kwargs": {"todos": ["one", "two"], "todo_provenance": records, "content": "planned"}})
    monkeypatch.setattr("bucket_manager.now_iso", lambda: AT)
    await abandon(server, bucket, records[0]["id"])
    restarted = BucketManager(server.config)
    await restarted.apply_import_operation(key)
    assert restarted.inspect_import_operation(key)["payload"] == plan["payload"]
    assert restarted.preview_todo_drop(bucket, records[0]["id"])["target"]["dropped_at"] == AT
    before = snapshot(tmp_path)
    await restarted.apply_import_operation(key)
    assert snapshot(tmp_path) == before
    assert await restarted.update(bucket, todos=[], content="replacement")
    assert restarted.preview_todo_drop(bucket, records[0]["id"])["target"]["dropped_at"] == AT
    assert "one" not in await server.todos()


@pytest.mark.asyncio
async def test_import_and_merge_duplicate_proposals_do_not_reopen(server):
    bucket, records = await setup(server, [rec("one")])
    await abandon(server, bucket, records[0]["id"])
    importer = server.import_engine
    item = {"content": "body", "todos": ["one"], "importance": 5}
    assert await importer._merge_or_create_item(item)
    target = server.bucket_mgr.preview_todo_drop(bucket, records[0]["id"])["target"]
    assert target["dropped_at"] and "one" not in await server.todos()
    source = await server.bucket_mgr.create("source", todos=["new"])
    preview = await server.trace(bucket, merge=source)
    assert "已合并" in await server.trace(bucket, merge=source, confirm_token=token(preview))
    assert server.bucket_mgr.preview_todo_drop(bucket, records[0]["id"])["target"]["dropped_at"] == target["dropped_at"]


@pytest.mark.asyncio
async def test_merge_resume_after_drop_keeps_original_plan(server):
    bucket, records = await setup(server)
    source = await server.bucket_mgr.create("source", todos=["new"])
    preview = await server.trace(bucket, merge=source)
    execute = server._execute_merge_operation
    server._execute_merge_operation = AsyncMock(return_value="deferred")
    await server.trace(bucket, merge=source, confirm_token=token(preview))
    server._execute_merge_operation = execute
    operation = server.bucket_mgr.read_merge_operations()[-1]
    original_plan = deepcopy(operation["plan"])
    await server.bucket_mgr.apply_import_operation("merge:" + operation["operation_id"] + ":target",
        operation_kind="update", target_bucket_id=bucket, payload={"kwargs": original_plan["target_update"]})
    await abandon(server, bucket, records[0]["id"])
    at = server.bucket_mgr.preview_todo_drop(bucket, records[0]["id"])["target"]["dropped_at"]
    assert "已合并" in await execute(operation)
    assert server.bucket_mgr.read_merge_operations()[-1]["plan"] == original_plan
    assert server.bucket_mgr.preview_todo_drop(bucket, records[0]["id"])["target"]["dropped_at"] == at


@pytest.mark.asyncio
async def test_resume_directional_state_matrix_and_other_drift(server):
    identity = rec()
    for field in ("done_at", "dropped_at"):
        terminal = {**identity, field: AT}
        assert server._todo_resume_matches([identity], [terminal])
        assert server._todo_resume_matches([terminal], [terminal])
        assert not server._todo_resume_matches([terminal], [identity])
        for altered in ({**terminal, field: LATER}, {**identity, ("dropped_at" if field == "done_at" else "done_at"): AT}):
            with pytest.raises(ValueError, match="conflict"):
                server._todo_resume_matches([terminal], [altered])
        assert server._todo_resume_matches([identity], [identity, rec("history", **{field: AT})])
    for change in ({"text": "changed"}, {"id": "todo_" + str(uuid4())}, {"said_by": "ting"}):
        assert not server._todo_resume_matches([identity], [{**identity, **change}])
    assert not server._todo_resume_matches([identity], [identity, rec("extra")])
    assert server._todo_resume_matches([], [{**identity, "dropped_at": AT}])
    assert not server._todo_resume_matches([], [identity])
    legacy = automatic_todo_provenance(["same"])[0]
    assert server._todo_resume_matches([legacy], [identity], allow_legacy_assigned_ids=True)
    assert not server._todo_resume_matches([legacy], [identity])
    with pytest.raises(ValueError, match="terminal conflict"):
        server._todo_resume_matches([identity], [{**identity, "done_at": AT, "dropped_at": AT}])
    with pytest.raises(ValueError, match="terminal conflict"):
        server._todo_resume_matches([], [{**identity, "done_at": AT, "dropped_at": AT}])


@pytest.mark.asyncio
async def test_conflicting_update_and_import_plan_are_zero_write(server, tmp_path):
    bucket, records = await setup(server)
    await abandon(server, bucket, records[0]["id"])
    dropped = server.bucket_mgr.preview_todo_drop(bucket, records[0]["id"])["target"]
    for change in ({"dropped_at": LATER}, {"done_at": AT}):
        incoming = [{**dropped, **change}, records[1]]
        before = snapshot(tmp_path)
        assert not await server.bucket_mgr.update(bucket, todo_provenance=incoming)
        with pytest.raises(ValueError, match="conflict"):
            server.bucket_mgr.plan_import_operation("o5b:" + "e" * 64,
                operation_kind="update", target_bucket_id=bucket,
                payload={"kwargs": {"todos": ["one", "two"], "todo_provenance": incoming}})
        assert snapshot(tmp_path) == before


@pytest.mark.asyncio
async def test_merge_source_drop_between_validation_and_delete_is_retained(server):
    target, _ = await setup(server)
    source, records = await setup(server)
    preview = await server.trace(target, merge=source)
    delete = server.bucket_mgr.delete
    async def interleave(bucket, **kwargs):
        server.bucket_mgr.drop_todo(bucket, records[0]["id"], lambda _: True)
        return await delete(bucket, **kwargs)
    server.bucket_mgr.delete = interleave
    result = await server.trace(target, merge=source, confirm_token=token(preview))
    assert "source deletion failed" in result
    assert await server.bucket_mgr.get(source) is not None
    assert server.bucket_mgr.preview_todo_drop(source, records[0]["id"])["target"].get("dropped_at")

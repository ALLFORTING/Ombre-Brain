"""W-4B1 identity, explicit legacy writes, and durable replay invariants."""

import importlib
import json
import sqlite3
import sys
from pathlib import Path
from unittest.mock import AsyncMock
from uuid import uuid4

import frontmatter
import pytest

from bucket_manager import (
    BucketIdempotencyError, BucketManager, automatic_todo_provenance,
    merge_todo_provenance, reconcile_todo_provenance, valid_todo_id,
)


def _id():
    return "todo_" + str(uuid4())


def _record(text, identity, speaker="unknown"):
    return {**automatic_todo_provenance([text])[0], "id": identity, "said_by": speaker}


def _load_server(tmp_path, monkeypatch):
    monkeypatch.setenv("OMBRE_BUCKETS_DIR", str(tmp_path / "buckets"))
    monkeypatch.delenv("OMBRE_API_KEY", raising=False)
    sys.modules.pop("server", None)
    server = importlib.import_module("server")
    server.decay_engine.ensure_started = AsyncMock(return_value=None)
    return server


def _create_payload(values):
    return {"tags": [], "importance": 5, "domain": None, "valence": 0.5,
            "arousal": 0.3, "name": None, **values}


def _snapshot(root):
    return {str(path.relative_to(root)): path.read_bytes() for path in root.rglob("*") if path.is_file()}


async def _records(manager, bucket_id):
    return (await manager.get(bucket_id))["metadata"]["todo_provenance"]


@pytest.mark.asyncio
async def test_explicit_writes_keep_order_identity_and_return_ids(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    bucket_id = await server.bucket_mgr.create("context", todos=["first", "second"])
    original = await _records(server.bucket_mgr, bucket_id)
    assert all(valid_todo_id(record["id"]) for record in original)
    await server.trace(bucket_id, todos=["second", "first"])
    assert await _records(server.bucket_mgr, bucket_id) == list(reversed(original))
    result = await server.trace(bucket_id, todo_items=[
        {"text": "rewritten", "id": original[0]["id"], "said_by": "ting"},
        {"text": "second"},
        {"text": "new"},
    ])
    records = await _records(server.bucket_mgr, bucket_id)
    assert records[0] == _record("rewritten", original[0]["id"], "ting")
    assert records[1]["id"] == original[1]["id"]
    assert records[2]["id"] not in {r["id"] for r in original}
    assert all(r["id"] in result for r in records)
    assert "_todo_references_only" not in result
    detailed = await server.todos(include_provenance=True)
    assert all("todo_id:" + r["id"] in detailed for r in records)
    assert "todo_id:" not in await server.todos()


@pytest.mark.asyncio
@pytest.mark.parametrize("sidecar", [False, True])
async def test_legacy_reads_and_unrelated_updates_do_not_add_ids(tmp_path, monkeypatch, sidecar):
    server = _load_server(tmp_path, monkeypatch)
    bucket_id = await server.bucket_mgr.create("legacy", todos=["old", "untouched"])
    bucket = await server.bucket_mgr.get(bucket_id)
    path = Path(bucket["path"])
    post = frontmatter.load(path)
    if sidecar:
        post["todo_provenance"] = automatic_todo_provenance(["old", "untouched"])
    else:
        post.metadata.pop("todo_provenance")
    path.write_text(frontmatter.dumps(post), encoding="utf-8")
    before = _snapshot(tmp_path)
    assert "todo_id:null" in await server.todos(include_provenance=True)
    await server.bucket_mgr.get(bucket_id)
    await server.bucket_mgr.list_all(include_archive=True)
    assert _snapshot(tmp_path) == before
    await server.trace(bucket_id, tags="tag")
    metadata = (await server.bucket_mgr.get(bucket_id))["metadata"]
    assert all("id" not in r for r in metadata.get("todo_provenance", []))
    assert ("todo_provenance" in metadata) is sidecar
    await server.trace(bucket_id, todo_items=[{"text": "old"}])
    records = await _records(server.bucket_mgr, bucket_id)
    assert len(records) == 1 and valid_todo_id(records[0]["id"])


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["foreign", "format", "conflict", "extra"])
async def test_invalid_identity_inputs_are_zero_write(tmp_path, monkeypatch, failure):
    server = _load_server(tmp_path, monkeypatch)
    bucket_id = await server.bucket_mgr.create("before", todos=["keep"])
    other_id = await server.bucket_mgr.create("other", todos=["other"])
    identity = (await _records(server.bucket_mgr, bucket_id))[0]["id"]
    foreign = (await _records(server.bucket_mgr, other_id))[0]["id"]
    items = {
        "foreign": [{"text": "keep", "id": foreign}],
        "format": [{"text": "keep", "id": "todo_invalid"}],
        "conflict": [{"text": "keep", "id": identity}, {"text": "different", "id": identity}],
        "extra": [{"text": "keep", "unknown_field": 1}],
    }[failure]
    before = _snapshot(tmp_path)
    result = await server.trace(bucket_id, todo_items=items, content="must not write", tags="changed")
    assert "无效" in result
    assert _snapshot(tmp_path) == before


def test_identity_reconciliation_and_attribution_conflicts_are_independent():
    first, second = _id(), _id()
    records = reconcile_todo_provenance(["same"], [
        _record("same", first, "ting"), _record("same", first, "model"),
        _record("same", second, "system"),
    ], strict=True)
    assert records == [_record("same", first), _record("same", second, "system")]
    texts, merged = merge_todo_provenance(["same"], records, ["same"], [_record("same", first)])
    assert texts == ["same"] and merged == records
    with pytest.raises(ValueError, match="identity conflict"):
        merge_todo_provenance(["same"], [_record("same", first)], ["other"], [_record("other", first)])


@pytest.mark.asyncio
async def test_merge_preserves_both_same_text_ids_and_source_deletion(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    target = await server.bucket_mgr.create("target", todos=["same"], todo_provenance=automatic_todo_provenance(["same"]))
    source = await server.bucket_mgr.create("source", todos=["same"], todo_provenance=automatic_todo_provenance(["same"]))
    expected = await _records(server.bucket_mgr, target) + await _records(server.bucket_mgr, source)
    before = _snapshot(tmp_path)
    preview = await server.trace(target, merge=source)
    assert _snapshot(tmp_path) == before
    result = await server.trace(target, merge=source, confirm_token=preview.split("confirm_token:", 1)[1].strip())
    assert await server.bucket_mgr.get(source) is None, result
    assert await _records(server.bucket_mgr, target) == expected
    assert (await server.bucket_mgr.get(target))["metadata"]["todos"] == ["same"]
    detailed = await server.todos(include_provenance=True)
    assert all(record["id"] in detailed for record in expected)
    # Text-only reorder preserves both identities, but an unqualified structured
    # reference is ambiguous and must not silently choose an identity.
    await server.trace(target, todos=["same"])
    assert await _records(server.bucket_mgr, target) == expected
    before = _snapshot(tmp_path)
    assert "ambiguous" in await server.trace(target, todo_items=[{"text": "same"}])
    assert _snapshot(tmp_path) == before


@pytest.mark.asyncio
async def test_duplicate_unknown_extraction_keeps_multiple_existing_identities(test_config):
    manager = BucketManager(test_config)
    identities = [_record("same", _id(), "ting"), _record("same", _id(), "model")]
    target = await manager.create("body", todos=["same"], todo_provenance=identities)
    from import_memory import ImportEngine
    manager.search = AsyncMock(return_value=[await manager.get(target)])
    engine = ImportEngine(test_config, manager, object())
    assert await engine._merge_or_create_item({"content": "body", "todos": ["same"]}) is True
    assert await _records(manager, target) == identities


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["create", "update"])
async def test_durable_plan_retry_restart_and_changed_input(test_config, kind):
    manager = BucketManager(test_config)
    target = await manager.create("old", todos=["keep"]) if kind == "update" else None
    previous = await _records(manager, target) if target else []
    values = {"content": "planned", "todos": ["keep", "new"]}
    payload = _create_payload(values) if kind == "create" else {"kwargs": values}
    key = "o5b:" + ("a" if kind == "create" else "b") * 64
    plan = manager.plan_import_operation(key, operation_kind=kind, target_bucket_id=target, payload=payload)
    planned_values = plan["payload"] if kind == "create" else plan["payload"]["kwargs"]
    records = planned_values["todo_provenance"]
    assert all(valid_todo_id(r["id"]) for r in records)
    if previous:
        assert records[0]["id"] == previous[0]["id"]
    assert manager.plan_import_operation(key, operation_kind=kind, target_bucket_id=target, payload=payload)["payload_digest"] == plan["payload_digest"]
    with pytest.raises(BucketIdempotencyError):
        manager.plan_import_operation(key, operation_kind=kind, target_bucket_id=target, payload={**payload, "unexpected": "changed"})
    result = await manager.apply_import_operation(key)
    bucket_id = result["result_id"]
    assert await _records(manager, bucket_id) == records
    restarted = BucketManager(test_config)
    await restarted.apply_import_operation(key)
    assert await _records(restarted, bucket_id) == records
    assert restarted.inspect_import_operation(key)["payload_digest"] == plan["payload_digest"]
    # A marker makes retry a no-op even after an explicit identity-preserving rewrite.
    await restarted.update(bucket_id, todos=["renamed", "new"], todo_provenance=[
        {**records[0], "text": "renamed"}, records[1],
    ], _todo_references_only=True)
    before = await restarted.get(bucket_id)
    await restarted.apply_import_operation(key)
    assert await restarted.get(bucket_id) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["create", "update"])
async def test_old_durable_payload_is_not_rewritten(test_config, kind, monkeypatch):
    manager = BucketManager(test_config)
    target = await manager.create("old", todos=["old"]) if kind == "update" else None
    values = {"content": "old plan", "todos": ["old", "new"], "todo_provenance": automatic_todo_provenance(["old", "new"])}
    payload = _create_payload(values) if kind == "create" else {"kwargs": values}
    key = "o5b:" + ("c" if kind == "create" else "d") * 64
    plan = manager.plan_import_operation(key, operation_kind=kind, target_bucket_id=target, payload=payload)
    # Reconstruct an actual pre-W-4B1 persisted operation in the isolated fixture.
    serialized, digest = manager._canonical_import_payload(payload)
    with sqlite3.connect(manager.history_db_path) as connection:
        connection.execute("UPDATE ob_import_operations SET payload_json=?, payload_digest=? WHERE operation_key=?", (serialized, digest, key))
    assert manager.plan_import_operation(key, operation_kind=kind, target_bucket_id=target, payload=payload)["payload_digest"] == digest
    result = await manager.apply_import_operation(key)
    records = await _records(manager, result["result_id"])
    assert all(valid_todo_id(r["id"]) for r in records)
    restarted = BucketManager(test_config)
    with monkeypatch.context() as scoped:
        scoped.setattr("bucket_manager.uuid4", lambda: pytest.fail("replay must not generate IDs"))
        await restarted.apply_import_operation(key)
    assert await _records(restarted, result["result_id"]) == records
    persisted = restarted.inspect_import_operation(key)
    assert persisted["payload"] == payload and persisted["payload_digest"] == digest


@pytest.mark.asyncio
async def test_late_target_identity_and_attribution_survive_pending_import(test_config):
    manager = BucketManager(test_config)
    target = await manager.create("old")
    key = "o5b:" + "e" * 64
    plan = manager.plan_import_operation(key, operation_kind="update", target_bucket_id=target, payload={"kwargs": {
        "todos": ["task"], "todo_provenance": automatic_todo_provenance(["task"]),
    }})
    await manager.update(target, todos=["task"], todo_provenance=[{"text": "task", "said_by": "ting"}])
    current = await _records(manager, target)
    assert current[0]["id"] != plan["payload"]["kwargs"]["todo_provenance"][0]["id"]
    await manager.apply_import_operation(key)
    assert await _records(manager, target) == current
    assert manager.inspect_import_operation(key)["payload_digest"] == plan["payload_digest"]


@pytest.mark.asyncio
async def test_runtime_schema_exposes_optional_id_and_no_other_phase_fields(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    from mcp.shared.memory import create_connected_server_and_client_session
    async with create_connected_server_and_client_session(server.mcp) as client:
        tools = {tool.name: tool for tool in (await client.list_tools()).tools}
        schema = tools["trace"].inputSchema
        item = schema["properties"]["todo_items"]["anyOf"][0]["items"]
        assert "id" in item["properties"] and "id" not in item["required"]
        assert "todo_done" in schema["properties"]
        assert "todo_done" not in schema["required"]
        assert "done_at" not in item["properties"]
        bucket = await server.bucket_mgr.create("MCP", todos=["old"])
        identity = (await _records(server.bucket_mgr, bucket))[0]["id"]
        result = await client.call_tool("trace", {"bucket_id": bucket, "todo_items": [{"text": "new", "id": identity}]})
        assert not result.isError
        assert (await _records(server.bucket_mgr, bucket))[0]["id"] == identity


@pytest.mark.asyncio
async def test_confirmed_merge_keeps_id_when_attribution_conflicts(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    identity = _id()
    target = await server.bucket_mgr.create("target", todos=["same"], todo_provenance=[_record("same", identity, "ting")])
    source = await server.bucket_mgr.create("source", todos=["same"], todo_provenance=[_record("same", identity, "model")])
    preview = await server.trace(target, merge=source)
    result = await server.trace(target, merge=source, confirm_token=preview.split("confirm_token:", 1)[1].strip())
    assert await server.bucket_mgr.get(source) is None, result
    assert await _records(server.bucket_mgr, target) == [_record("same", identity)]


@pytest.mark.asyncio
async def test_legacy_merge_materializes_ids_only_after_confirmation(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    target = await server.bucket_mgr.create("target", todos=["target task"])
    source = await server.bucket_mgr.create("source", todos=["source task"])
    for bucket_id in (target, source):
        path = Path((await server.bucket_mgr.get(bucket_id))["path"])
        post = frontmatter.load(path)
        post.metadata.pop("todo_provenance")
        path.write_text(frontmatter.dumps(post), encoding="utf-8")
    before = _snapshot(tmp_path)
    preview = await server.trace(target, merge=source)
    assert _snapshot(tmp_path) == before
    result = await server.trace(target, merge=source, confirm_token=preview.split("confirm_token:", 1)[1].strip())
    assert await server.bucket_mgr.get(source) is None, result
    records = await _records(server.bucket_mgr, target)
    assert all(valid_todo_id(record["id"]) for record in records)
    operation = server.bucket_mgr.read_merge_operations()[-1]
    assert operation["plan"]["target_update"]["todo_provenance"] == records


@pytest.mark.asyncio
async def test_persisted_identity_conflict_is_not_silently_repaired(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    bucket = await server.bucket_mgr.create("body", todos=["one", "two"])
    path = Path((await server.bucket_mgr.get(bucket))["path"])
    post = frontmatter.load(path)
    identity = post["todo_provenance"][0]["id"]
    post["todo_provenance"][1]["id"] = identity
    path.write_text(frontmatter.dumps(post), encoding="utf-8")
    before = _snapshot(tmp_path)
    assert "identity conflict" in await server.trace(bucket, todo_items=[{"text": "one"}])
    assert _snapshot(tmp_path) == before
    assert not await server.bucket_mgr.update(bucket, todos=["one", "two"], content="changed")
    assert _snapshot(tmp_path) == before


@pytest.mark.asyncio
async def test_import_restart_between_plan_and_item_checkpoint_reuses_plan(test_config, tmp_path):
    from import_memory import ImportEngine
    from raw_evidence_import import RawEvidenceImportCoordinator
    config = dict(test_config, raw_evidence_root=str(tmp_path / "evidence"))
    manager = BucketManager(config)
    target = await manager.create("old", todos=["keep"])
    candidate = await manager.get(target)
    candidate["score"] = 100
    manager.search = AsyncMock(return_value=[candidate])
    dehydrator = type("D", (), {"api_available": True})()
    dehydrator.merge = AsyncMock(return_value="planned merge")
    engine = ImportEngine(config, manager, dehydrator)
    coordinator = RawEvidenceImportCoordinator(config)
    source = b"source"
    prepared = coordinator.prepare_run(source, filename="source.txt", media_type="text/plain", preserve_raw=False, resume=False)
    coordinator.capture(prepared, source, filename="source.txt", media_type="text/plain")
    item = {"content": "new context", "todos": ["keep", "new"], "name": "item"}
    original_upsert = coordinator.upsert_item

    def crash_before_checkpoint(*args, **kwargs):
        if kwargs.get("status") == "memory_planned":
            raise RuntimeError("interrupted before item checkpoint")
        return original_upsert(*args, **kwargs)

    coordinator.upsert_item = crash_before_checkpoint
    with pytest.raises(RuntimeError, match="checkpoint"):
        await engine._o5b_plan_item(coordinator, prepared.run_id, 0, 0, item, False)
    key = engine._o5b_operation_key(prepared.run_id, 0, 0)
    original_plan = manager.inspect_import_operation(key)
    restarted_manager = BucketManager(config)
    restarted_manager.search = AsyncMock(side_effect=AssertionError("must reuse existing plan"))
    dehydrator.merge = AsyncMock(side_effect=AssertionError("must not merge twice"))
    restarted = ImportEngine(config, restarted_manager, dehydrator)
    restarted_coordinator = RawEvidenceImportCoordinator(config)
    record = await restarted._o5b_plan_item(restarted_coordinator, prepared.run_id, 0, 0, item, False)
    assert record["payload_digest"] == original_plan["payload_digest"]
    await restarted._o5b_apply_item(restarted_coordinator, prepared.run_id, record)
    assert await _records(restarted_manager, target) == original_plan["payload"]["kwargs"]["todo_provenance"]


@pytest.mark.asyncio
async def test_old_merge_journal_replays_without_rewriting_plan(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    target = await server.bucket_mgr.create("target", todos=["target task"])
    source = await server.bucket_mgr.create("source", todos=["source task"])
    for bucket_id in (target, source):
        path = Path((await server.bucket_mgr.get(bucket_id))["path"])
        post = frontmatter.load(path)
        post["todo_provenance"] = automatic_todo_provenance(post["todos"])
        path.write_text(frontmatter.dumps(post), encoding="utf-8")
    execute = server._execute_merge_operation
    write = server.bucket_mgr.write_merge_operation

    def old_plan(operation_id, plan, **kwargs):
        plan = json.loads(json.dumps(plan))
        for record in plan["target_update"]["todo_provenance"]:
            record.pop("id", None)
        return write(operation_id, plan, **kwargs)

    with monkeypatch.context() as scoped:
        scoped.setattr(server.bucket_mgr, "write_merge_operation", old_plan)
        scoped.setattr(server, "_execute_merge_operation", AsyncMock(return_value="deferred"))
        preview = await server.trace(target, merge=source)
        await server.trace(target, merge=source, confirm_token=preview.split("confirm_token:", 1)[1].strip())
    operation = server.bucket_mgr.read_merge_operations()[-1]
    original = operation["plan"]
    result = await execute(operation)
    assert await server.bucket_mgr.get(source) is None, result
    assert all(valid_todo_id(r["id"]) for r in await _records(server.bucket_mgr, target))
    assert server.bucket_mgr.read_merge_operations()[-1]["plan"] == original

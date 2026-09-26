"""W-4A automatic provenance, feel exclusion, and import replay boundaries."""

import importlib
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from bucket_manager import BucketManager, automatic_todo_provenance
from dehydrator import ANALYZE_PROMPT, DIGEST_PROMPT
from import_memory import ImportEngine
from raw_evidence_import import RawEvidenceImportCoordinator
from utils import apply_display_aliases


def _record(text, said_by="unknown", said_at=None, source_bucket=None):
    return dict(text=text, said_by=said_by, said_at=said_at, source_bucket=source_bucket)


def _analysis(todos):
    return dict(domain=["事务"], tags=["tag"], valence=0.6, arousal=0.4,
                suggested_name="action", importance=5, todos=todos)


def _load_server(tmp_path, monkeypatch):
    monkeypatch.setenv("OMBRE_BUCKETS_DIR", str(tmp_path / "buckets"))
    monkeypatch.delenv("OMBRE_API_KEY", raising=False)
    sys.modules.pop("server", None)
    server = importlib.import_module("server")
    server.decay_engine.ensure_started = AsyncMock(return_value=None)
    server._similarity_doorbell = AsyncMock(return_value="")
    server._detect_conflict_warning = AsyncMock(return_value="")
    return server


@pytest.mark.asyncio
@pytest.mark.parametrize("pinned", [False, True])
async def test_hold_extraction_cannot_assert_speaker_or_source_time(tmp_path, monkeypatch, pinned):
    server = _load_server(tmp_path, monkeypatch)
    analysis = _analysis(["send file", "send file", ""])
    analysis["said_at"] = "2020-01-01T12:00:00+08:00"
    analysis["todo_provenance"] = [_record("send file", "ting", analysis["said_at"])]
    server.dehydrator.analyze = AsyncMock(return_value=analysis)
    await server.hold("Please send the file", pinned=pinned)
    buckets = await server.bucket_mgr.list_all()
    assert len(buckets) == 1
    assert buckets[0]["metadata"]["todos"] == ["send file"]
    assert buckets[0]["metadata"]["todo_provenance"] == [_record("send file")]


@pytest.mark.asyncio
async def test_feel_ignores_extracted_todos_and_provenance_but_keeps_tags(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    analysis = _analysis(["must not persist"])
    analysis["todo_provenance"] = [_record("must not persist", "ting")]
    server.dehydrator.analyze = AsyncMock(return_value=analysis)
    await server.hold("a reflection", feel=True, valence=0.8, arousal=0.2)
    buckets = await server.bucket_mgr.list_all()
    assert len(buckets) == 1
    metadata = buckets[0]["metadata"]
    assert metadata["type"] == "feel"
    assert metadata["tags"] == ["tag"]
    assert metadata["valence"] == 0.8
    assert metadata["arousal"] == 0.2
    assert metadata["todos"] == []
    assert "todo_provenance" not in metadata


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["hold", "fast", "long", "import"])
@pytest.mark.parametrize("source_task", ["send file", "向婷易回信"])
async def test_automatic_reuse_preserves_known_and_only_attributes_incoming_todos(
    tmp_path, monkeypatch, route, source_task,
):
    server = _load_server(tmp_path, monkeypatch)
    stored_task = apply_display_aliases(source_task)
    known = _record(stored_task, "ting", "2020-01-01T12:00:00+08:00", "source")
    bucket_id = await server.bucket_mgr.create(
        "same content", domain=["事务"], todos=[stored_task, "legacy untouched"],
        todo_provenance=[known],
    )
    server.dehydrator.analyze = AsyncMock(return_value=_analysis([source_task, "next", "next"]))
    item = {**_analysis([source_task, "next", "next"]),
            "content": "same content", "name": "action", "preserve_raw": False}
    server.dehydrator.digest = AsyncMock(return_value=[item])
    engine = ImportEngine(server.config, server.bucket_mgr, server.dehydrator)
    for _ in range(2):
        server.bucket_mgr.search = AsyncMock(return_value=[await server.bucket_mgr.get(bucket_id)])
        if route == "hold":
            await server.hold("same content")
        elif route == "fast":
            await server.grow("same content")
        elif route == "long":
            await server.grow("a sufficiently long source for the diary digest route")
        else:
            assert await engine._merge_or_create_item(item) is True
    buckets = await server.bucket_mgr.list_all()
    assert len(buckets) == 1
    assert buckets[0]["metadata"]["todos"] == [stored_task, "legacy untouched", "next"]
    assert buckets[0]["metadata"]["todo_provenance"] == [known, _record("next")]


@pytest.mark.asyncio
@pytest.mark.parametrize("preserve_raw", [False, True])
async def test_plain_import_persists_unknown_without_using_conversation_range(
    test_config, preserve_raw,
):
    manager = BucketManager(test_config)
    engine = ImportEngine(test_config, manager, type("D", (), {"api_available": True})())
    engine._extract_memories = AsyncMock(return_value=[{
        **_analysis(["send file", "send file"]), "content": "action", "name": "action",
    }])
    source = '{"messages":[{"role":"user","content":"send file","timestamp":"2020-01-01T00:00:00"}]}'
    assert (await engine.start(source, filename="source.json", preserve_raw=preserve_raw))["status"] == "completed"
    bucket = (await manager.list_all())[0]
    assert bucket["metadata"]["todos"] == ["send file"]
    assert bucket["metadata"]["todo_provenance"] == [_record("send file")]


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["create", "raw", "merge"])
async def test_capture_restarts_after_applied_write_without_todo_or_provenance_drift(
    test_config, tmp_path, route,
):
    config = dict(test_config, raw_evidence_root=str(tmp_path / "raw-evidence"))
    manager = BucketManager(config)
    known = _record("send file", "ting", "2020-01-01T12:00:00+08:00", "source")
    if route == "merge":
        target_id = await manager.create(
            "existing context", todos=["send file"], todo_provenance=[known],
        )
        candidate = await manager.get(target_id)
        candidate["score"] = 100
        manager.search = AsyncMock(return_value=[candidate])
    else:
        manager.search = AsyncMock(return_value=[])
    dehydrator = type("D", (), {"api_available": True})()
    dehydrator.merge = AsyncMock(return_value="merged context")
    engine = ImportEngine(config, manager, dehydrator)
    engine._extract_memories = AsyncMock(return_value=[{
        **_analysis(["send file", "next", "next"]), "content": "action", "name": "action",
    }])
    original_apply = manager.apply_import_operation
    operation_keys = []

    async def crash_after_write(operation_key, **kwargs):
        operation_keys.append(operation_key)
        await original_apply(operation_key, **kwargs)
        raise RuntimeError("simulated interruption after durable write")

    manager.apply_import_operation = crash_after_write
    source = b"User: send file and take the next action"
    result = await engine.start_raw_evidence(source, filename="source.txt", preserve_raw=route == "raw")
    assert result["status"] == "error"
    assert len(operation_keys) == 1
    operation_before = manager.inspect_import_operation(operation_keys[0])
    bucket_before = await manager.get(operation_before["result_id"])
    expected = [known if route == "merge" else _record("send file"), _record("next")]
    assert bucket_before["metadata"]["todos"] == ["send file", "next"]
    assert bucket_before["metadata"]["todo_provenance"] == expected

    restarted_manager = BucketManager(config)
    restarted = ImportEngine(config, restarted_manager, dehydrator)
    restarted._extract_memories = AsyncMock(side_effect=AssertionError("must reuse persisted plan"))
    dehydrator.merge = AsyncMock(side_effect=AssertionError("must not merge again"))
    result = await restarted.start_raw_evidence(
        source, filename="source.txt", preserve_raw=route == "raw", resume=True,
    )
    assert result["status"] == "completed"
    assert len(await restarted_manager.list_all()) == 1
    operation_after = restarted_manager.inspect_import_operation(operation_keys[0])
    assert operation_after["payload"] == operation_before["payload"]
    assert operation_after["payload_digest"] == operation_before["payload_digest"]
    bucket_after = await restarted_manager.get(operation_before["result_id"])
    assert bucket_after == bucket_before


@pytest.mark.asyncio
async def test_durable_update_preserves_known_provenance_added_after_planning(test_config, tmp_path):
    config = dict(test_config, raw_evidence_root=str(tmp_path / "raw-evidence"))
    manager = BucketManager(config)
    target_id = await manager.create("existing", todos=["send file", "legacy task"])
    candidate = await manager.get(target_id)
    candidate["score"] = 100
    manager.search = AsyncMock(return_value=[candidate])
    dehydrator = type("D", (), {"api_available": True})()
    dehydrator.merge = AsyncMock(return_value="merged")
    engine = ImportEngine(config, manager, dehydrator)
    coordinator = RawEvidenceImportCoordinator(config)
    source = b"source"
    prepared = coordinator.prepare_run(
        source, filename="source.txt", media_type="text/plain", preserve_raw=False, resume=False,
    )
    coordinator.capture(prepared, source, filename="source.txt", media_type="text/plain")
    item = {**_analysis(["send file", "next"]), "content": "action", "name": "action"}
    record = await engine._o5b_plan_item(coordinator, prepared.run_id, 0, 0, item, False)
    planned = manager.inspect_import_operation(record["operation_key"])
    known = _record("send file", "ting", "2020-01-01T12:00:00+08:00")
    legacy_known = _record("legacy task", "model")
    assert await manager.update(target_id, todo_provenance=[known, legacy_known])
    for _ in range(2):
        await manager.apply_import_operation(record["operation_key"])
    bucket = await manager.get(target_id)
    assert bucket["metadata"]["todos"] == ["send file", "legacy task", "next"]
    assert bucket["metadata"]["todo_provenance"] == [known, legacy_known, _record("next")]
    assert manager.inspect_import_operation(record["operation_key"])["payload_digest"] == planned["payload_digest"]


@pytest.mark.asyncio
async def test_legacy_read_does_not_add_sidecar(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    bucket_id = await server.bucket_mgr.create("legacy", todos=["legacy task"])
    bucket = await server.bucket_mgr.get(bucket_id)
    original_bytes = Path(bucket["path"]).read_bytes()
    assert "todo_provenance" not in bucket["metadata"]
    assert "legacy task" in await server.todos(include_provenance=True)
    assert (await server.bucket_mgr.get(bucket_id))["metadata"] == bucket["metadata"]
    assert Path(bucket["path"]).read_bytes() == original_bytes


def test_automatic_sidecar_does_not_generate_source_facts():
    assert automatic_todo_provenance([" send file ", "send file", ""]) == [_record("send file")]
    assert automatic_todo_provenance([]) == []


@pytest.mark.parametrize("prompt", [ANALYZE_PROMPT, DIGEST_PROMPT])
def test_extraction_prompts_require_concrete_explicit_unfinished_actions(prompt):
    assert "明确表达、尚未完成的具体行动事项" in prompt
    for exclusion in ("原则/规则", "偏好", "建议", "推测", "假设性行动", "模型自行推断出的任务"):
        assert exclusion in prompt

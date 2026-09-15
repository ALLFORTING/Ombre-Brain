import importlib
import json
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


def _confirm_token(result: str) -> str:
    for line in result.splitlines():
        if line.startswith("confirm_token:"):
            return line.split(":", 1)[1].strip()
    raise AssertionError(f"confirm_token not found in: {result}")


def _age_bucket(server, bucket_id: str, days: int = 40) -> None:
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
async def test_hold_new_bucket_echoes_persisted_values(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    server.dehydrator.analyze = AsyncMock(
        return_value={
            "domain": ["work"],
            "valence": 0.5,
            "arousal": 0.3,
            "tags": ["auto"],
            "suggested_name": "Persisted title",
            "todos": [],
        }
    )

    result = await server.hold(
        "persisted hold body", tags="manual", importance=2, pinned=True
    )

    assert "新建 " in result
    assert "Persisted title" in result
    assert "importance=10" in result
    assert "tags=[auto, manual]" in result
    assert "domain=[work]" in result


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kwargs",
    ({"content": "forbidden batch body"}, {"name": "forbidden batch name"}, {"content": "body", "name": "name"}),
)
async def test_batch_trace_rejects_content_or_name_without_writes(tmp_path, monkeypatch, kwargs):
    server = _load_server(tmp_path, monkeypatch)
    first_id = await server.bucket_mgr.create(content="first body", name="First")
    second_id = await server.bucket_mgr.create(content="second body", name="Second")

    result = await server.trace(f"{first_id},{second_id}", **kwargs)

    assert result == "批量 trace 不支持修改 content/name，请逐桶操作。"
    first = await server.bucket_mgr.get(first_id)
    second = await server.bucket_mgr.get(second_id)
    assert first["content"] == "first body"
    assert second["content"] == "second body"
    assert first["metadata"]["name"] == "First"
    assert second["metadata"]["name"] == "Second"


@pytest.mark.asyncio
async def test_delete_requires_matching_unexpired_confirmation(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    target_id = await server.bucket_mgr.create(
        content="delete preview body that is deliberately long enough to display",
        name="Delete target",
        importance=7,
    )
    other_id = await server.bucket_mgr.create(content="other delete target")

    preview = await server.trace(target_id, delete=True)
    token = _confirm_token(preview)
    mismatch = await server.trace(other_id, delete=True, confirm_token=token)

    assert "删除确认：本次不会删除。" in preview
    assert f"bucket_id:{target_id}" in preview
    assert "name:Delete target" in preview
    assert "importance:7" in preview
    assert "delete preview body" in preview
    assert await server.bucket_mgr.get(target_id) is not None
    assert "删除确认无效" in mismatch
    assert await server.bucket_mgr.get(target_id) is not None
    assert await server.bucket_mgr.get(other_id) is not None

    confirmed = await server.trace(target_id, delete=True, confirm_token=token)
    assert "已遗忘" in confirmed
    assert await server.bucket_mgr.get(target_id) is None


@pytest.mark.asyncio
async def test_delete_expired_confirmation_and_inbound_protection_fail_closed(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    expiring_id = await server.bucket_mgr.create(content="expire delete")
    preview = await server.trace(expiring_id, delete=True)
    token = _confirm_token(preview)
    server._mutation_confirm_tokens[token]["expires_at"] = 0

    expired = await server.trace(expiring_id, delete=True, confirm_token=token)
    assert "删除确认无效" in expired
    assert await server.bucket_mgr.get(expiring_id) is not None

    target_id = await server.bucket_mgr.create(content="inbound target")
    source_id = await server.bucket_mgr.create(content="inbound source")
    await server.trace(source_id, superseded_by=target_id)
    blocked = await server.trace(target_id, delete=True)

    assert "仍被以下作废关系引用" in blocked
    assert "confirm_token:" not in blocked
    assert await server.bucket_mgr.get(target_id) is not None


@pytest.mark.asyncio
async def test_batch_delete_preview_is_zero_write_and_token_binds_full_target_set(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    first_id = await server.bucket_mgr.create(content="batch delete first")
    second_id = await server.bucket_mgr.create(content="batch delete second")

    preview = await server.trace(f"{first_id},{second_id}", delete=True)
    token = _confirm_token(preview)
    mismatch = await server.trace(first_id, delete=True, confirm_token=token)

    assert f"bucket_id:{first_id}" in preview
    assert f"bucket_id:{second_id}" in preview
    assert await server.bucket_mgr.get(first_id) is not None
    assert await server.bucket_mgr.get(second_id) is not None
    assert "删除确认无效" in mismatch

    deleted = await server.trace(
        f"{first_id},{second_id}", delete=True, confirm_token=token
    )
    assert deleted.count("已遗忘") == 2
    assert await server.bucket_mgr.get(first_id) is None
    assert await server.bucket_mgr.get(second_id) is None


@pytest.mark.asyncio
async def test_digest_writes_only_after_matching_plan_confirmation(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    server._call_digest_api = AsyncMock(return_value="condensed digest body")
    source_id = await server.bucket_mgr.create(
        content="old digest source", importance=2, domain=["digest-test"]
    )
    _age_bucket(server, source_id)

    preview = await server.digest(dry_run=False)
    token = _confirm_token(preview)
    before = await server.bucket_mgr.get(source_id)
    executed = await server.digest(dry_run=False, confirm_token=token)
    after = await server.bucket_mgr.get(source_id)

    assert "confirmation required" in preview
    assert before["metadata"].get("digested") is not True
    assert "已消化: 1 个桶" in executed
    assert after["metadata"]["digested"] is True


@pytest.mark.asyncio
async def test_digest_token_rejects_changed_plan_and_dry_run_stays_read_only(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    server._call_digest_api = AsyncMock(return_value="must not be used")
    first_id = await server.bucket_mgr.create(
        content="first digest source", importance=2, domain=["one"]
    )
    second_id = await server.bucket_mgr.create(
        content="second digest source", importance=2, domain=["two"]
    )
    _age_bucket(server, first_id)
    _age_bucket(server, second_id)

    dry_run = await server.digest(dry_run=True, max_groups=1)
    token = _confirm_token(dry_run)
    changed = await server.digest(dry_run=False, max_groups=2, confirm_token=token)

    assert "confirm_token:" in dry_run
    assert "confirmation required" in changed
    assert (await server.bucket_mgr.get(first_id))["metadata"].get("digested") is not True
    assert (await server.bucket_mgr.get(second_id))["metadata"].get("digested") is not True
    server._call_digest_api.assert_not_awaited()


@pytest.mark.asyncio
async def test_unrelate_clears_only_requested_bidirectional_relation(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    source_id = await server.bucket_mgr.create(content="source")
    remove_id = await server.bucket_mgr.create(content="remove")
    retain_id = await server.bucket_mgr.create(content="retain")
    await server.trace(source_id, related=f"{remove_id},{retain_id}")

    result = await server.trace(source_id, unrelate=remove_id)
    source = await server.bucket_mgr.get(source_id)
    removed = await server.bucket_mgr.get(remove_id)
    retained = await server.bucket_mgr.get(retain_id)

    assert f"unrelate={remove_id}" in result
    assert remove_id not in source["metadata"]["related_buckets"]
    assert retain_id in source["metadata"]["related_buckets"]
    assert source_id not in removed["metadata"].get("related_buckets", "")
    assert source_id in retained["metadata"].get("related_buckets", "")

    await server.trace(source_id, unrelate="missing-id")
    source_after = await server.bucket_mgr.get(source_id)
    retained_after = await server.bucket_mgr.get(retain_id)
    assert retain_id in source_after["metadata"]["related_buckets"]
    assert source_id in retained_after["metadata"].get("related_buckets", "")


@pytest.mark.asyncio
async def test_trace_schema_describes_sentinels_and_unrelate_contract(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    from mcp.shared.memory import create_connected_server_and_client_session

    async with create_connected_server_and_client_session(server.mcp) as client:
        tools = (await client.list_tools()).tools
    schema = next(tool.inputSchema for tool in tools if tool.name == "trace")

    assert "-1 means unchanged" in schema["properties"]["dormant"]["description"]
    assert "unpinning" in schema["properties"]["pinned"]["description"]
    assert "0.0-1.0" in schema["properties"]["valence"]["description"]
    assert "bidirectionally" in schema["properties"]["unrelate"]["description"]
    assert schema["properties"]["name"]["default"] == ""
    manifest = json.loads(
        (Path(__file__).resolve().parent.parent / "docs" / "mcp-public-contract.json").read_text(
            encoding="utf-8"
        )
    )
    trace_contract = next(item for item in manifest["tools"] if item["name"] == "trace")
    assert "unrelate" in trace_contract["input_schema_contract"]["properties"]

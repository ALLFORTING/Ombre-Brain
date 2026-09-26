"""S-2: real OB HTTP app, MCP 1.29.1, isolated storage and loopback sockets."""
import asyncio
import ast
import importlib
import importlib.metadata
import json
import logging
import re
import socket
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import frontmatter
import httpx
import pytest
import uvicorn
from sse_starlette.sse import AppStatus


@pytest.fixture
def ob(tmp_path, monkeypatch):
    assert importlib.metadata.version("mcp") == "1.29.1"
    for name in ("OMBRE_API_KEY", "OMBRE_DIGEST_API_KEY", "OMBRE_EMBEDDING_API_KEY",
                 "OMBRE_HOOK_URL", "OMBRE_AUTH_TOKEN", "OMBRE_MCP_QUERY_TOKEN",
                 "OMBRE_MCP_ALLOW_QUERY_TOKEN", "OMBRE_MCP_ALLOW_ANONYMOUS_HTTP",
                 "OMBRE_MCP_STATELESS_HTTP", "OMBRE_RM_RUNTIME_ENABLED"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("OMBRE_BUCKETS_DIR", str(tmp_path / "buckets"))
    monkeypatch.setenv("OMBRE_AUTH_TOKEN", "s2-private-bearer")
    sys.modules.pop("server", None)
    module = importlib.import_module("server")
    module.decay_engine.ensure_started = AsyncMock(return_value=None)
    # All persistent business writes stay in tmp_path; no provider/network calls.
    module.bucket_mgr.embedding_engine = None
    names = ("mcp.server.streamable_http_manager", "mcp.server.streamable_http", "mcp.server.sse")
    filters = {name: list(logging.getLogger(name).filters) for name in names}
    yield module
    for name, original in filters.items():
        logging.getLogger(name).filters[:] = original


def build(ob):
    app = ob.build_streamable_http_app()
    ob.add_mcp_auth_middleware(app)
    ob.add_http_cors_middleware(app)
    ob.add_mcp_diagnostic_middleware(app)
    return app


@asynccontextmanager
async def live(ob):
    """Real HTTP, including TCP disconnect; no manual task cancellation injection."""
    # sse-starlette has a process-global shutdown flag. Each test runs a new
    # ephemeral server; restore it after shutdown so later apps are not drained.
    previous_exit = AppStatus.should_exit
    AppStatus.should_exit = False
    app = build(ob)
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(128)
    config = uvicorn.Config(app, log_level="warning", access_log=False, lifespan="on")
    runner = uvicorn.Server(config)
    task = asyncio.create_task(runner.serve(sockets=[sock]))
    try:
        async with asyncio.timeout(10):
            while not runner.started:
                if task.done():
                    await task
                    raise AssertionError("uvicorn did not start")
                await asyncio.sleep(0.01)
        url = f"http://127.0.0.1:{sock.getsockname()[1]}"
        async with httpx.AsyncClient(base_url=url, timeout=10, headers={
            "Authorization": "Bearer s2-private-bearer",
            "Accept": "application/json, text/event-stream",
        }) as client:
            yield client
    finally:
        runner.should_exit = True
        try:
            await asyncio.wait_for(task, 10)
        finally:
            sock.close()
            AppStatus.should_exit = previous_exit


async def rpc(client, method, params=None, *, headers=None, path="/mcp"):
    response = await client.post(path, headers=headers, json={
        "jsonrpc": "2.0", "id": 1, "method": method, "params": params or {},
    })
    return response


def result(response):
    assert response.status_code == 200, response.text
    assert "text/event-stream" in response.headers["content-type"]
    messages = [json.loads(line[6:]) for line in response.text.splitlines()
                if line.startswith("data: ")]
    return next(message["result"] for message in messages if message.get("id") == 1)


def text(response):
    payload = result(response)
    assert not payload.get("isError"), payload
    return "\n".join(item["text"] for item in payload["content"] if item["type"] == "text")


async def call(client, name, arguments=None, **kwargs):
    return await rpc(client, "tools/call", {"name": name, "arguments": arguments or {}}, **kwargs)


async def initialize(client):
    response = await rpc(client, "initialize", {
        "protocolVersion": "2025-03-26", "capabilities": {},
        "clientInfo": {"name": "s2-test", "version": "1"},
    })
    result(response)
    session = response.headers.get("mcp-session-id")
    if session:
        client.headers["Mcp-Session-Id"] = session
    notification = await client.post("/mcp", json={
        "jsonrpc": "2.0", "method": "notifications/initialized",
    })
    assert notification.status_code == 202
    return session


@pytest.mark.parametrize("value,enabled", [
    (None, False), ("", False), ("false", False), ("0", False), ("junk", False),
    ("true", True), ("  TRUE ", True), ("1", True), ("yes", True), ("on", True),
])
def test_flag_and_shared_entrypoints(ob, monkeypatch, value, enabled):
    if value is not None:
        monkeypatch.setenv("OMBRE_MCP_STATELESS_HTTP", value)
    ob.build_streamable_http_app()
    assert ob.mcp.settings.stateless_http is enabled
    assert ob.mcp.session_manager.stateless is enabled
    assert ob.mcp.settings.json_response is False
    root = Path(__file__).parents[1]
    for filename in ("server.py", "backup_entry.py"):
        tree = ast.parse((root / filename).read_text(encoding="utf-8-sig"))
        calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)]
        assert sum((isinstance(node.func, ast.Name) and node.func.id == "build_streamable_http_app")
                   or (isinstance(node.func, ast.Attribute) and node.func.attr == "build_streamable_http_app")
                   for node in calls) == 1
    assert ob.mcp.sse_app() is not None  # SSE builds independently of HTTP setting.


@pytest.mark.asyncio
@pytest.mark.parametrize("stateless", [False, True])
async def test_protocol_and_diagnostic_privacy(ob, monkeypatch, caplog, stateless):
    monkeypatch.setenv("OMBRE_MCP_STATELESS_HTTP", str(stateless))
    with caplog.at_level(logging.INFO):
        async with live(ob) as client:
            session = await initialize(client)
            assert bool(session) is not stateless
            tools = result(await rpc(client, "tools/list"))
            assert "archive_session" in {tool["name"] for tool in tools["tools"]}
            assert "未找到记忆桶" in text(await call(client, "trace", {"bucket_id": "s2-missing"}))
            old = {"Mcp-Session-Id": "s2-private-old-session"}
            old_post = await rpc(client, "tools/list", headers=old)
            if stateless:
                async with client.stream("GET", "/mcp", headers={**old, "Accept": "text/event-stream"}) as stream:
                    old_get_status = stream.status_code
                    assert stream.headers["content-type"].startswith("text/event-stream")
            else:
                old_get_status = (await client.get("/mcp", headers={**old, "Accept": "text/event-stream"})).status_code
            if stateless:
                async with client.stream("GET", "/mcp", headers={"Accept": "text/event-stream"}) as stream:
                    no_session_get_status, no_session_get_text = stream.status_code, "SSE stream"
                    assert stream.headers["content-type"].startswith("text/event-stream")
            else:
                async with client.stream("GET", "/mcp", headers={"Accept": "text/event-stream"}) as stream:
                    assert stream.status_code == 200
                    assert stream.headers["content-type"].startswith("text/event-stream")
                client.headers.pop("Mcp-Session-Id")
                no_session_get = await client.get("/mcp", headers={"Accept": "text/event-stream"})
                no_session_get_status, no_session_get_text = no_session_get.status_code, no_session_get.text
                client.headers["Mcp-Session-Id"] = session
            deleted = await client.delete("/mcp")
            print("S2 protocol", stateless, "old POST", old_post.status_code,
                  "GET", no_session_get_status, no_session_get_text,
                  "old GET", old_get_status, "DELETE", deleted.status_code, deleted.text)
            assert old_post.status_code == (200 if stateless else 404)
            assert old_get_status == (200 if stateless else 404)
            assert no_session_get_status == (200 if stateless else 400)
            assert deleted.status_code == (405 if stateless else 200)
            if stateless:
                assert "mcp-session-id" not in old_post.headers
                assert not ob.mcp.session_manager._server_instances
            else:
                assert (await rpc(client, "tools/list")).status_code == 404
    records = [record.getMessage() for record in caplog.records if record.name == "ombre_brain.mcp"]
    assert any("has_session=false" in record for record in records)
    assert any("has_session=true" in record for record in records)
    secrets = ["s2-private-bearer", "s2-private-old-session"] + ([session] if session else [])
    assert not any(secret in record.getMessage() for record in caplog.records for secret in secrets)


@pytest.mark.asyncio
@pytest.mark.parametrize("stateless", [False, True])
@pytest.mark.parametrize("mode,status", [
    ("bearer", 200), ("bad-bearer", 401), ("query", 200), ("bad-query", 401),
    ("query-disabled", 401), ("anonymous", 200), ("anonymous-disabled", 401),
    ("anonymous-with-bearer-config", 401),
])
async def test_auth_equivalence(ob, monkeypatch, stateless, mode, status):
    monkeypatch.setenv("OMBRE_MCP_STATELESS_HTTP", str(stateless))
    if mode.startswith("query") or mode == "bad-query":
        monkeypatch.setenv("OMBRE_MCP_QUERY_TOKEN", "s2-private-query")
        monkeypatch.setenv("OMBRE_MCP_ALLOW_QUERY_TOKEN", "false" if mode == "query-disabled" else "true")
    if mode.startswith("anonymous"):
        if mode != "anonymous-with-bearer-config":
            monkeypatch.delenv("OMBRE_AUTH_TOKEN")
        monkeypatch.setenv("OMBRE_MCP_ALLOW_ANONYMOUS_HTTP", "true" if mode != "anonymous-disabled" else "false")
    async with live(ob) as client:
        client.headers.pop("Authorization")
        path = "/mcp"
        if "query" in mode:
            path += "?token=" + ("wrong" if mode == "bad-query" else "s2-private-query")
        if "bearer" in mode and not mode.startswith("anonymous"):
            client.headers["Authorization"] = "Bearer " + ("wrong" if mode == "bad-bearer" else "s2-private-bearer")
        response = await rpc(client, "initialize", {
            "protocolVersion": "2025-03-26", "capabilities": {},
            "clientInfo": {"name": "s2-auth", "version": "1"},
        }, path=path)
        assert response.status_code == status


@pytest.mark.asyncio
async def test_confirmation_and_cursor_across_independent_http_requests(ob, monkeypatch):
    monkeypatch.setenv("OMBRE_MCP_STATELESS_HTTP", "true")
    scope = ob._breath_cursor_scope(
        query="needle", domain="", valence=-1, arousal=-1, recent_cutoff=None,
        include_dormant=False, include_sealed=False, date_from="", date_to="",
        resonance="", min_score=0, touch=False,
    )
    @ob.mcp.tool()
    async def s2_helper(action: str, token: str = "", binding: str = "needle") -> dict:
        if action == "issue-confirm":
            return {"token": ob._issue_mutation_confirmation("s2.test", {"query": binding})}
        if action == "consume-confirm":
            return {"ok": ob._consume_mutation_confirmation("s2.test", {"query": binding}, token)}
        if action == "issue-cursor":
            return {"token": ob._encode_breath_cursor([{"id": "a", "score": 0.8}, {"id": "b", "score": 0.7}], 1, scope)}
        try:
            matches, position = ob._decode_breath_cursor(token, scope if binding == "needle" else "changed-query")
            return {"ids": [match["id"] for match in matches[position:]]}
        except ValueError:
            return {"invalid": True}
    async with live(ob) as client:
        async def helper(action, **kwargs):
            return json.loads(text(await call(client, "s2_helper", {"action": action, **kwargs})))
        token = (await helper("issue-confirm"))["token"]
        assert not (await helper("consume-confirm", token=token, binding="different"))["ok"]
        assert (await helper("consume-confirm", token=token))["ok"]
        assert not (await helper("consume-confirm", token=token))["ok"]
        expired = (await helper("issue-confirm"))["token"]
        cursor = (await helper("issue-cursor"))["token"]
        assert (await helper("read-cursor", token=cursor))["ids"] == ["b"]
        assert (await helper("read-cursor", token=cursor, binding="different"))["invalid"]
        confirmation = ob._mutation_confirm_tokens[expired]
        cursor_state = ob._BREATH_CURSOR_STATES[cursor]
        assert 0 < confirmation["expires_at"] - ob.time.monotonic() <= ob._MUTATION_CONFIRM_TTL_SECONDS
        assert cursor_state["expires_at"] - cursor_state["created_at"] == pytest.approx(ob._BREATH_CURSOR_TTL_SECONDS, abs=1e-7, rel=0)
        # Expire only these OB states; transport clocks remain real.
        confirmation["expires_at"] = cursor_state["expires_at"] = 0
        assert not (await helper("consume-confirm", token=expired))["ok"]
        assert (await helper("read-cursor", token=cursor))["invalid"]
        assert not ob.mcp.session_manager._server_instances


async def wait(event):
    await asyncio.wait_for(event.wait(), 5)


async def disconnect_call(client, name, arguments, started):
    async with client.stream("POST", "/mcp", json={
        "jsonrpc": "2.0", "id": 2, "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    }) as response:
        assert response.status_code == 200
        await wait(started)
        # Closing an unread SSE response closes the real TCP connection.
    return response.is_closed


@pytest.mark.asyncio
@pytest.mark.parametrize("stateless", [False, True])
async def test_tcp_disconnect_cancels_only_stateless(ob, monkeypatch, stateless):
    monkeypatch.setenv("OMBRE_MCP_STATELESS_HTTP", str(stateless))
    started, release, cancelled, continued, finished = [asyncio.Event() for _ in range(5)]
    @ob.mcp.tool()
    async def s2_slow() -> str:
        started.set()
        try:
            await release.wait()
            continued.set()
            return "completed"
        except asyncio.CancelledError:
            cancelled.set()
            raise
        finally:
            finished.set()
    async with live(ob) as client:
        session = await initialize(client)
        assert await disconnect_call(client, "s2_slow", {}, started)
        if stateless:
            await wait(cancelled)
            await wait(finished)
            assert not continued.is_set()
        else:
            # A second independent request proves the manager remains usable.
            result(await rpc(client, "tools/list"))
            assert not cancelled.is_set() and not finished.is_set()
            release.set()
            await wait(continued)
            await wait(finished)
        print("S2 disconnect", stateless, "cancelled", cancelled.is_set(),
              "continued", continued.is_set(), "closed", True)
        assert bool(session) is not stateless
        result(await rpc(client, "tools/list"))
        assert len(ob.mcp.session_manager._server_instances) == (0 if stateless else 1)


@pytest.mark.asyncio
@pytest.mark.parametrize("stateless", [False, True])
async def test_archive_response_loss_retry_duplicate(ob, monkeypatch, stateless):
    monkeypatch.setenv("OMBRE_MCP_STATELESS_HTTP", str(stateless))
    started, release, cancelled, finished = [asyncio.Event() for _ in range(4)]
    first = True
    async def archive_then_pause(**kwargs):
        nonlocal first
        # The real handler completes ALL writes. A test adapter then delays
        # returning its result to the SDK, modeling response loss after success.
        outcome = await ob.archive_session(**kwargs)
        if first:
            first = False
            started.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise
            finally:
                finished.set()
        return outcome
    tool = ob.mcp._tool_manager.get_tool("archive_session")
    monkeypatch.setattr(tool, "fn", archive_then_pause)
    async with live(ob) as client:
        await initialize(client)
        arguments = {"summary": "identical isolated summary", "letter": "isolated letter"}
        assert await disconnect_call(client, "archive_session", arguments, started)
        if stateless:
            await wait(cancelled)
        else:
            release.set()
        await wait(finished)
        initial = await ob.bucket_mgr.list_all(include_archive=True)
        assert len(initial) == 1 and initial[0]["metadata"]["type"] == "archived"
        assert len(ob.bucket_mgr.get_letters(limit=10)) == 1
        assert "已归档" in text(await call(client, "archive_session", arguments))
        buckets = await ob.bucket_mgr.list_all(include_archive=True)
        assert len(buckets) == 2 and len({bucket["id"] for bucket in buckets}) == 2
        assert all(bucket["metadata"]["type"] == "archived" for bucket in buckets)
        names = sorted(bucket["metadata"]["name"] for bucket in buckets)
        assert names[0].endswith("_01") and names[1].endswith("_02")
        assert all("identical isolated summary" in bucket["content"] for bucket in buckets)
        assert "idempotency" not in str(__import__("inspect").signature(ob.archive_session))
        print("S2 archive duplicate", stateless, len(buckets), "cancelled", cancelled.is_set())


@pytest.mark.asyncio
async def test_archive_cancel_after_create_leaves_unarchived_bucket(ob, monkeypatch):
    monkeypatch.setenv("OMBRE_MCP_STATELESS_HTTP", "true")
    started, cancelled = asyncio.Event(), asyncio.Event()
    original = ob.bucket_mgr.embedding_engine
    async def pause_after_body(*args):
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise
    monkeypatch.setattr(ob.bucket_mgr, "embedding_engine", SimpleNamespace(
        enabled=True, generate_and_store=pause_after_body,
    ))
    async with live(ob) as client:
        assert await disconnect_call(client, "archive_session", {"summary": "partial isolated summary"}, started)
        await wait(cancelled)
        buckets = await ob.bucket_mgr.list_all(include_archive=True)
        assert len(buckets) == 1
        assert buckets[0]["metadata"]["type"] == "dynamic"
        monkeypatch.setattr(ob.bucket_mgr, "embedding_engine", original)
        text(await call(client, "archive_session", {"summary": "partial isolated summary"}))
        buckets = await ob.bucket_mgr.list_all(include_archive=True)
        assert sorted(bucket["metadata"]["type"] for bucket in buckets) == ["archived", "dynamic"]


@pytest.mark.asyncio
async def test_stateless_merge_cancel_then_http_resume_uses_disk_markers(ob, monkeypatch):
    monkeypatch.setenv("OMBRE_MCP_STATELESS_HTTP", "true")
    target = await ob.bucket_mgr.create("target body")
    source = await ob.bucket_mgr.create("source body")
    started, cancelled = asyncio.Event(), asyncio.Event()
    original = ob.bucket_mgr.delete
    async def pause_delete(*args, **kwargs):
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise
    async with live(ob) as client:
        preview = text(await call(client, "trace", {"bucket_id": target, "merge": source}))
        token = preview.split("confirm_token:", 1)[1].strip()
        monkeypatch.setattr(ob.bucket_mgr, "delete", pause_delete)
        await disconnect_call(client, "trace", {"bucket_id": target, "merge": source, "confirm_token": token}, started)
        await wait(cancelled)
        record = ob.bucket_mgr.read_merge_operations()[0]
        assert record["status"] == "running" and "target" in record["completed"]
        assert (await ob.bucket_mgr.get(target))["content"].count("source body") == 1
        monkeypatch.setattr(ob.bucket_mgr, "delete", original)
        resumed = text(await call(client, "trace", {"bucket_id": target, "merge": source}))
        assert "已合并" in resumed and record["operation_id"] in resumed
        assert (await ob.bucket_mgr.get(target))["content"].count("source body") == 1
        assert await ob.bucket_mgr.get(source) is None
        assert ob.bucket_mgr.read_merge_operations()[0]["status"] == "complete"


@pytest.mark.asyncio
async def test_stateless_digest_cancel_then_http_resume_uses_disk_markers(ob, monkeypatch):
    monkeypatch.setenv("OMBRE_MCP_STATELESS_HTTP", "true")
    bucket = await ob.bucket_mgr.create("old high", importance=9, domain=["high"])
    path = ob.bucket_mgr._find_bucket_file(bucket)
    post = frontmatter.load(path)
    post["created"] = post["last_active"] = "2000-01-01T00:00:00"
    Path(path).write_text(frontmatter.dumps(post), encoding="utf-8")
    started, cancelled = asyncio.Event(), asyncio.Event()
    original = ob.bucket_mgr.apply_import_operation
    async def apply_then_pause(*args, **kwargs):
        outcome = await original(*args, **kwargs)
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise
        return outcome
    async with live(ob) as client:
        preview = text(await call(client, "digest", {"dry_run": True}))
        token = re.search(r"confirm_token:\s*(\S+)", preview).group(1)
        monkeypatch.setattr(ob.bucket_mgr, "apply_import_operation", apply_then_pause)
        await disconnect_call(client, "digest", {"dry_run": False, "confirm_token": token}, started)
        await wait(cancelled)
        assert (await ob.bucket_mgr.get(bucket))["metadata"]["importance"] == 8
        record = ob.bucket_mgr.read_digest_operations()[0]
        assert record["status"] == "running"
        reopened = ob.BucketManager({"buckets_dir": ob.config["buckets_dir"]})
        assert reopened.read_digest_operations()[0]["operation_id"] == record["operation_id"]
        monkeypatch.setattr(ob.bucket_mgr, "apply_import_operation", original)
        preview = text(await call(client, "digest", {"dry_run": True}))
        token = re.search(r"resume_confirm_token:\s*(\S+)", preview).group(1)
        resumed = text(await call(client, "digest", {"dry_run": False, "confirm_token": token}))
        assert "importance rebalanced: 1" in resumed
        assert (await ob.bucket_mgr.get(bucket))["metadata"]["importance"] == 8
        assert ob.bucket_mgr.read_digest_operations()[0]["status"] == "complete"

@pytest.mark.asyncio
@pytest.mark.parametrize("stateless", [False, True])
async def test_trace_cancel_at_embedding_boundary_can_leave_one_way_relation(ob, monkeypatch, stateless):
    monkeypatch.setenv("OMBRE_MCP_STATELESS_HTTP", str(stateless))
    left = await ob.bucket_mgr.create("original left")
    right = await ob.bucket_mgr.create("original right")
    started, release, cancelled, finished = [asyncio.Event() for _ in range(4)]
    original = ob.bucket_mgr.embedding_engine
    async def pause_refresh(bucket_id, content):
        if bucket_id == left:
            started.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise
            finally:
                finished.set()
        return None
    monkeypatch.setattr(ob.bucket_mgr, "embedding_engine", SimpleNamespace(
        enabled=True, generate_and_store=pause_refresh,
    ))
    async with live(ob) as client:
        await initialize(client)
        await disconnect_call(client, "trace", {
            "bucket_id": left, "content": "appended fragment", "append": True, "related": right,
        }, started)
        if stateless:
            await wait(cancelled)
        else:
            release.set()
        await wait(finished)
        # Stateful continues the reciprocal write after the embedding await returns.
        result(await rpc(client, "tools/list"))
        left_bucket, right_bucket = await ob.bucket_mgr.get(left), await ob.bucket_mgr.get(right)
        assert right in left_bucket["metadata"]["related_buckets"]
        reverse = left in right_bucket["metadata"].get("related_buckets", "")
        assert reverse is not stateless
        print("S2 trace relation", stateless, "reciprocal", reverse)
        if stateless:
            monkeypatch.setattr(ob.bucket_mgr, "embedding_engine", original)
            text(await call(client, "trace", {
                "bucket_id": left, "content": "appended fragment", "append": True, "related": right,
            }))
            assert (await ob.bucket_mgr.get(left))["content"].count("appended fragment") == 2


@pytest.mark.asyncio
async def test_stateless_real_breath_cursor_pages_and_binding(ob, monkeypatch):
    monkeypatch.setenv("OMBRE_MCP_STATELESS_HTTP", "true")
    ids = [await ob.bucket_mgr.create(f"s2 cursor needle {index}") for index in range(3)]
    monkeypatch.setattr(ob.dehydrator, "dehydrate", AsyncMock(
        side_effect=lambda content, metadata=None, **kwargs: content,
    ))
    async with live(ob) as client:
        arguments = {"query": "s2 cursor needle", "max_results": 1, "touch": False}
        first = text(await call(client, "breath", arguments))
        cursor = re.search(r"^下一页 cursor: (\S+)$", first, re.MULTILINE).group(1)
        second = text(await call(client, "breath", {**arguments, "cursor": cursor}))
        first_ids = {bucket_id for bucket_id in ids if bucket_id in first}
        second_ids = {bucket_id for bucket_id in ids if bucket_id in second}
        assert len(first_ids) == len(second_ids) == 1 and first_ids.isdisjoint(second_ids)
        changed = text(await call(client, "breath", {**arguments, "query": "changed query", "cursor": cursor}))
        assert "无效、已过期或与当前检索条件不匹配" in changed
        ob._BREATH_CURSOR_STATES[cursor]["expires_at"] = 0
        expired = text(await call(client, "breath", {**arguments, "cursor": cursor}))
        assert "无效、已过期或与当前检索条件不匹配" in expired
        assert not ob.mcp.session_manager._server_instances

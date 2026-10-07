"""S-2: real OB HTTP app, MCP 1.29.1, isolated storage and loopback sockets."""
import asyncio
import ast
import importlib
import importlib.metadata
import json
import logging
import os
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
    # These tests drive real disconnect cancellation into the durable write
    # paths; the production shield is covered by the tests that re-enable it.
    module._SHIELD_STATELESS_TOOL_CALLS = False
    names = ("mcp.server.streamable_http_manager", "mcp.server.streamable_http", "mcp.server.sse")
    filters = {name: list(logging.getLogger(name).filters) for name in names}
    yield module
    for name, original in filters.items():
        logging.getLogger(name).filters[:] = original


def reload_server(monkeypatch):
    """A fresh server module stands in for a restarted process: no SDK session survives."""
    sys.modules.pop("server", None)
    module = importlib.import_module("server")
    module.decay_engine.ensure_started = AsyncMock(return_value=None)
    module.bucket_mgr.embedding_engine = None
    return module


async def settle_sse_shutdown_watcher():
    """Let the previous server's sse-starlette shutdown watcher finish.

    The watcher is per thread and polls every 0.5 s. A second in-process server
    started before it notices the first shutdown would have its streams drained.
    """
    from sse_starlette import sse
    state = sse._get_shutdown_state()
    async with asyncio.timeout(5):
        while state.watcher_started:
            await asyncio.sleep(0.05)
    AppStatus.should_exit = False


def build(ob):
    app = ob.build_streamable_http_app()
    ob.add_mcp_auth_middleware(app)
    ob.add_http_cors_middleware(app)
    ob.add_mcp_diagnostic_middleware(app)
    return app


@asynccontextmanager
async def live(ob, *, access_log=False, scope_snapshots=None):
    """Real HTTP, including TCP disconnect; no manual task cancellation injection."""
    # sse-starlette has a process-global shutdown flag. Each test runs a new
    # ephemeral server; restore it after shutdown so later apps are not drained.
    previous_exit = AppStatus.should_exit
    AppStatus.should_exit = False
    app = build(ob)
    if access_log:
        ob.install_uvicorn_access_log_redaction()
    if scope_snapshots is not None:
        from copy import deepcopy
        original_app = app
        async def observed_app(scope, receive, send):
            if scope["type"] != "http":
                return await original_app(scope, receive, send)
            fields = ("path", "raw_path", "query_string", "headers", "method", "http_version")
            before = deepcopy({key: scope.get(key) for key in fields})
            try:
                await original_app(scope, receive, send)
            finally:
                scope_snapshots.append((before, deepcopy({key: scope.get(key) for key in fields})))
        app = observed_app
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(128)
    log_options = {"log_config": None} if access_log else {}
    config = uvicorn.Config(app, log_level="info" if access_log else "warning",
                            access_log=access_log, lifespan="on", **log_options)
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
    (None, True), ("", True), ("junk", True), ("true", True), ("  TRUE ", True),
    ("1", True), ("yes", True), ("on", True),
    ("false", False), (" FALSE ", False), ("0", False), ("no", False), ("off", False),
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
async def test_s4_keyed_trace_disconnect_resume_and_schema_validation(ob, monkeypatch):
    from embedding_engine import EmbeddingEngine
    monkeypatch.setenv("OMBRE_MCP_STATELESS_HTTP", "true")
    left = await ob.bucket_mgr.create("original left")
    right = await ob.bucket_mgr.create("original right")
    engine = EmbeddingEngine({"buckets_dir": ob.config["buckets_dir"], "embedding": {"enabled": False}})
    engine.enabled = True
    started, cancelled = asyncio.Event(), asyncio.Event()
    async def provider(*args, **kwargs):
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise
    engine._generate_embedding = provider
    ob.bucket_mgr.embedding_engine = engine
    payload = {"bucket_id": left, "content": "keyed fragment", "append": True,
               "related": right, "operation_id": "http-retry"}
    async with live(ob) as client:
        await initialize(client)
        for invalid in ("", "x" * 129, 123):
            rejected = await call(client, "trace", {**payload, "operation_id": invalid})
            assert result(rejected)["isError"]
        assert (await ob.bucket_mgr.get(left))["content"] == "original left"
        await disconnect_call(client, "trace", payload, started)
        await wait(cancelled)
        assert ob.bucket_mgr.inspect_trace_request("http-retry")["phase"] == "memory_applied"
        engine._generate_embedding = AsyncMock(return_value=[.1, .2])
        resumed = text(await call(client, "trace", payload))
        assert text(await call(client, "trace", payload)) == resumed
        assert (await ob.bucket_mgr.get(left))["content"].count("keyed fragment") == 1
        assert right in (await ob.bucket_mgr.get(left))["metadata"]["related_buckets"]
        assert left in (await ob.bucket_mgr.get(right))["metadata"]["related_buckets"]


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
async def test_legacy_archive_cancel_after_publish_leaves_archived_bucket(ob, monkeypatch):
    monkeypatch.setenv("OMBRE_MCP_STATELESS_HTTP", "true")
    started, release, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()
    original = ob.bucket_mgr.embedding_engine
    async def pause_after_body(*args):
        started.set()
        await release.wait()
        finished.set()
    monkeypatch.setattr(ob.bucket_mgr, "embedding_engine", SimpleNamespace(
        enabled=True, generate_and_store=pause_after_body,
    ))
    async with live(ob) as client:
        assert await disconnect_call(client, "archive_session", {"summary": "partial isolated summary"}, started)
        release.set()
        await wait(finished)
        from tests.test_legacy_post_effect_cancellation import drain
        await drain(ob)
        buckets = await ob.bucket_mgr.list_all(include_archive=True)
        assert len(buckets) == 1
        assert buckets[0]["metadata"]["type"] == "archived"
        monkeypatch.setattr(ob.bucket_mgr, "embedding_engine", original)
        text(await call(client, "archive_session", {"summary": "partial isolated summary"}))
        buckets = await ob.bucket_mgr.list_all(include_archive=True)
        assert sorted(bucket["metadata"]["type"] for bucket in buckets) == ["archived", "archived"]


@pytest.mark.asyncio
@pytest.mark.parametrize('stateless', [False, True])
@pytest.mark.parametrize('family', ['archive', 'hold'])
@pytest.mark.parametrize('boundary', ['provider', 'response'])
async def test_legacy_post_effects_real_tcp(ob, monkeypatch, stateless, family, boundary, caplog):
    from tests.test_legacy_post_effect_cancellation import prepare, snapshot, complete, drain
    prepare(ob, monkeypatch)
    runtime = ob._get_runtime_components()
    engine = runtime['embedding_engine']
    ob.bucket_mgr.embedding_engine = engine
    engine.enabled = False
    neighbor = await ob.bucket_mgr.create('synthetic neighbor', domain=['synthetic'])
    engine._store_embedding(neighbor, [.1, .2])
    engine.enabled = True
    started, release = asyncio.Event(), asyncio.Event()
    provider_calls = []
    async def provider(*args, **kwargs):
        provider_calls.append(args)
        if boundary == 'provider' and len(provider_calls) == 1:
            started.set(); await release.wait()
        return [.1, .2]
    monkeypatch.setattr(engine, '_generate_embedding', provider)
    monkeypatch.setenv('OMBRE_MCP_STATELESS_HTTP', str(stateless))
    name = 'archive_session' if family == 'archive' else 'hold'
    arguments = (dict(summary='synthetic session', letter='frozen letter', valence=.7, arousal=.4)
                 if family == 'archive' else
                 dict(content='synthetic pinned memory', pinned=True, trigger_date='2030-01-01',
                      valence=.7, arousal=.4))
    if boundary == 'response':
        tool = ob.mcp._tool_manager.get_tool(name)
        original = tool.fn
        first = True
        async def delivery(**kwargs):
            nonlocal first
            result = await original(**kwargs)
            if first:
                first = False
                started.set(); await release.wait()
            return result
        monkeypatch.setattr(tool, 'fn', delivery)
    errors = []
    loop = asyncio.get_running_loop()
    previous = loop.get_exception_handler()
    loop.set_exception_handler(lambda loop, context: errors.append(context))
    try:
        async with live(ob) as client:
            session = await initialize(client)
            assert bool(session) == (not stateless)
            assert await disconnect_call(client, name, arguments, started)
            buckets = [b for b in await ob.bucket_mgr.list_all(include_archive=True) if b['id'] != neighbor]
            original_id = buckets[0]['id']
            at_disconnect = snapshot(ob, original_id)
            release.set()
            await drain(ob)
            first_state = snapshot(ob, original_id)
            complete(first_state, family)
            if family == 'hold':
                assert neighbor in first_state['metadata']['related_buckets']
                assert original_id in (await ob.bucket_mgr.get(neighbor))['metadata']['related_buckets']
            # No retry preceded the original object's completion checks.
            response = text(await call(client, name, arguments))
            buckets = [b for b in await ob.bucket_mgr.list_all(include_archive=True) if b['id'] != neighbor]
            assert len(buckets) == 2
            after_retry = [snapshot(ob, b['id']) for b in buckets]
            for state in after_retry:
                complete(state, family)
                if family == 'hold':
                    assert neighbor in state['metadata']['related_buckets']
                    assert state['identity'] in (await ob.bucket_mgr.get(neighbor))['metadata']['related_buckets']
            await drain(ob)
            evidence = dict(stateless=stateless, family=family, boundary=boundary,
                            root=ob.config['buckets_dir'], loopback_url=str(client.base_url),
                            at_disconnect=at_disconnect, original_completed_without_retry=first_state,
                            after_retry=after_retry, provider_calls=len(provider_calls),
                            remaining_tasks=len(ob._LEGACY_POST_EFFECT_TASKS), omitted_exceptions=errors,
                            response=response)
        assert not errors and not ob._LEGACY_POST_EFFECT_TASKS
        assert 'legacy post-effects failed' not in caplog.text
        target = os.environ.get('OB_LEGACY_TCP_EVIDENCE')
        if target:
            output = Path(target); output.mkdir(parents=True, exist_ok=True)
            (output / f'{family}-{boundary}-{stateless}.json').write_text(
                json.dumps(evidence, ensure_ascii=False, indent=2), encoding='utf-8')
    finally:
        release.set()
        await drain(ob)
        loop.set_exception_handler(previous)


@pytest.mark.asyncio
@pytest.mark.parametrize('stateless', [False, True])
async def test_legacy_executor_drains_on_http_shutdown(ob, monkeypatch, stateless):
    from tests.test_legacy_post_effect_cancellation import prepare, snapshot, complete, drain
    prepare(ob, monkeypatch)
    engine = ob._get_runtime_components()['embedding_engine']
    ob.bucket_mgr.embedding_engine = engine
    engine.enabled = True
    started, release = asyncio.Event(), asyncio.Event()
    async def provider(*a, **k):
        started.set(); await release.wait(); return [.1, .2]
    monkeypatch.setattr(engine, '_generate_embedding', provider)
    monkeypatch.setenv('OMBRE_MCP_STATELESS_HTTP', str(stateless))
    async with live(ob) as client:
        await initialize(client)
        await disconnect_call(client, 'archive_session', dict(
            summary='shutdown synthetic', letter='frozen letter', valence=.7, arousal=.4), started)
        identity = (await ob.bucket_mgr.list_all(include_archive=True))[0]['id']
        worker, = ob._LEGACY_POST_EFFECT_TASKS
        async def finish_during_shutdown():
            await asyncio.sleep(.4)
            assert not worker.done()
            release.set()
        releasing = asyncio.create_task(finish_during_shutdown())
    await releasing
    await drain(ob)
    complete(snapshot(ob, identity), 'archive')


@pytest.mark.asyncio
@pytest.mark.parametrize("stateless", [False, True])
async def test_archive_operation_http_response_loss_replays_receipt(ob, monkeypatch, stateless):
    monkeypatch.setenv("OMBRE_MCP_STATELESS_HTTP", str(stateless))
    started, release, finished = [asyncio.Event() for _ in range(3)]
    first_result = []
    async def completed_then_pause(**kwargs):
        outcome = await ob.archive_session(**kwargs)
        if not first_result:
            first_result.append(outcome)
            started.set()
            try:
                await release.wait()
            finally:
                finished.set()
        return outcome
    monkeypatch.setattr(ob.mcp._tool_manager.get_tool("archive_session"), "fn", completed_then_pause)
    args = {"summary": "response lost", "letter": "handoff", "valence": .7,
            "arousal": .4, "operation_id": "http-stable"}
    async with live(ob) as client:
        await initialize(client)
        assert await disconnect_call(client, "archive_session", args, started)
        release.set()
        await wait(finished)
        root = Path(ob.config["buckets_dir"])
        before = {str(p): p.read_bytes() for p in root.rglob("*") if p.is_file()}
        assert text(await call(client, "archive_session", args)) == first_result[0]
        assert before == {str(p): p.read_bytes() for p in root.rglob("*") if p.is_file()}
        assert len(await ob.bucket_mgr.list_all(include_archive=True)) == 1
        assert len(ob.bucket_mgr.get_letters(limit=10)) == len(ob._load_emotion_timeline()) == 1


@pytest.mark.asyncio
async def test_archive_operation_real_tcp_cancel_and_reconnect_without_detached_worker(ob, monkeypatch):
    from embedding_engine import EmbeddingEngine
    from archive_session_operations import ArchiveSessionOperations
    monkeypatch.setenv("OMBRE_MCP_STATELESS_HTTP", "true")
    engine = EmbeddingEngine(ob.config)
    engine.enabled = True
    monkeypatch.setattr(ob.bucket_mgr, "embedding_engine", engine)
    started, cancelled, finished = [asyncio.Event() for _ in range(3)]
    async def provider(*args, **kwargs):
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise
        finally:
            finished.set()
    monkeypatch.setattr(engine, "_generate_embedding", provider)
    args = {"summary": "interrupted", "letter": "handoff", "valence": .7,
            "arousal": .4, "operation_id": "reconnect-stable"}
    journal = ArchiveSessionOperations(ob.config["buckets_dir"])
    async with live(ob) as client:
        assert await disconnect_call(client, "archive_session", args, started)
        await wait(cancelled)
        await wait(finished)
        op = journal.lookup(args["operation_id"])
        assert op["status"] == "pending" and op["embedding_resolution"] is None
        assert op["boot_event_id"] is not None and op["letter_id"] is None
        assert len(await ob.bucket_mgr.list_all(include_archive=True)) == 1
        assert not ob.bucket_mgr.get_letters(limit=10) and not ob._load_emotion_timeline()
        result(await rpc(client, "tools/list"))  # Allow server work to run; no detached continuation.
        assert journal.lookup(args["operation_id"])["status"] == "pending"
        assert not ob.bucket_mgr.get_letters(limit=10)
        monkeypatch.setattr(engine, "_generate_embedding", AsyncMock(return_value=[.1, .2]))
        assert text(await call(client, "archive_session", args)) == op["result_text"]
        assert len(await ob.bucket_mgr.list_all(include_archive=True)) == 1
        assert len(ob.bucket_mgr.get_letters(limit=10)) == len(ob._load_emotion_timeline()) == 1
        assert journal.lookup(args["operation_id"])["status"] == "completed"


@pytest.mark.asyncio
@pytest.mark.parametrize("stateless", [False, True])
async def test_archive_operation_concurrent_http_calls(ob, monkeypatch, stateless):
    from embedding_engine import EmbeddingEngine
    from archive_session_operations import ArchiveSessionOperations
    monkeypatch.setenv("OMBRE_MCP_STATELESS_HTTP", str(stateless))
    engine = EmbeddingEngine(ob.config)
    engine.enabled = True
    monkeypatch.setattr(ob.bucket_mgr, "embedding_engine", engine)
    both = asyncio.Event()
    entered = 0
    async def provider(*args, **kwargs):
        nonlocal entered
        entered += 1
        if entered == 2:
            both.set()
        await wait(both)
        return [.1, .2]
    monkeypatch.setattr(engine, "_generate_embedding", provider)
    args = {"summary": "concurrent", "letter": "handoff", "valence": .7,
            "arousal": .4, "operation_id": "http-concurrent"}
    async with live(ob) as client:
        await initialize(client)
        # Separate JSON-RPC IDs within a stateful session.
        async def request(identity):
            response = await client.post("/mcp", json={"jsonrpc": "2.0", "id": identity,
                "method": "tools/call", "params": {"name": "archive_session", "arguments": args}})
            assert response.status_code == 200
            message = next(json.loads(line[6:]) for line in response.text.splitlines()
                           if line.startswith("data: "))
            assert not message["result"].get("isError")
            return message["result"]["content"][0]["text"]
        first, second = await asyncio.gather(request(21), request(22))
        assert entered == 2 and first == second and "已归档" in first
        assert len(await ob.bucket_mgr.list_all(include_archive=True)) == 1
        assert len(ob.bucket_mgr.get_letters(limit=10)) == len(ob._load_emotion_timeline()) == 1
        assert ArchiveSessionOperations(ob.config["buckets_dir"]).lookup(args["operation_id"])["status"] == "completed"


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
async def test_trace_cancel_at_embedding_boundary_never_leaves_one_way_relation(ob, monkeypatch, stateless):
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
        forward = right in left_bucket["metadata"].get("related_buckets", "")
        reverse = left in right_bucket["metadata"].get("related_buckets", "")
        assert forward == reverse == (not stateless)
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


@pytest.mark.asyncio
@pytest.mark.parametrize('stateless',[False,True])
@pytest.mark.parametrize('loss',['accepted','completed'])
async def test_s4e_confirmed_delete_tcp_disconnect_and_stable_replay(ob,monkeypatch,stateless,loss):
    monkeypatch.setenv('OMBRE_MCP_STATELESS_HTTP',str(stateless))
    identity=await ob.bucket_mgr.create('S4E isolated HTTP delete')
    preview=await ob._delete_with_confirmation([identity],'')
    arguments={'bucket_id':identity,'delete':True,'confirm_token':preview['confirm_token']}
    started,release,cancelled,finished=[asyncio.Event() for _ in range(4)]
    first=True
    observed=[]
    original_pause=ob.bucket_mgr.confirmed_delete_pause
    async def pause(point,context):
        nonlocal first
        if loss=='accepted' and first and point=='child_accepted':
            first=False;started.set()
            try:await release.wait()
            except asyncio.CancelledError:cancelled.set();raise
            finally:finished.set()
        return await original_pause(point,context)
    monkeypatch.setattr(ob.bucket_mgr,'confirmed_delete_pause',pause)
    original_trace=ob.mcp._tool_manager.get_tool("trace").fn
    async def wrapped(**kwargs):
        nonlocal first
        output=await original_trace(**kwargs)
        observed.append(output)
        if loss=='completed' and first:
            first=False;started.set()
            try:await release.wait()
            except asyncio.CancelledError:cancelled.set();raise
            finally:finished.set()
        return output
    monkeypatch.setattr(ob.mcp._tool_manager.get_tool('trace'),'fn',wrapped)
    async with live(ob) as client:
        session=await initialize(client)
        assert bool(session) is not stateless
        assert await disconnect_call(client,'trace',arguments,started)
        if stateless:await wait(cancelled)
        else:release.set()
        await wait(finished)
        rows=ob.bucket_mgr.confirmed_delete_rows()
        assert len(rows)==1,observed
        if stateless and loss=='accepted':assert rows[0]['phase']=='accepted'
        output=text(await call(client,'trace',arguments))
        assert output=='已遗忘记忆桶: '+identity
        assert text(await call(client,'trace',arguments))==output
        assert len(ob.bucket_mgr.get_history(identity))==1
        assert len(ob.bucket_mgr.relation_store.operations())==1
        assert ob.bucket_mgr.confirmed_delete_rows()[0]['status']=='completed'


@pytest.mark.asyncio
async def test_stateless_is_default_and_false_restores_sessions(ob, monkeypatch):
    monkeypatch.delenv('OMBRE_MCP_STATELESS_HTTP', raising=False)
    async with live(ob) as client:
        assert await initialize(client) is None
    await settle_sse_shutdown_watcher()
    monkeypatch.setenv('OMBRE_MCP_STATELESS_HTTP', 'false')
    restarted = reload_server(monkeypatch)
    async with live(restarted) as client:
        assert await initialize(client)


@pytest.mark.asyncio
@pytest.mark.parametrize("stateless", [False, True])
@pytest.mark.parametrize("entry", ["metadata", "reindex"])
@pytest.mark.parametrize("boundary", ["provider", "response"])
async def test_legacy_asset_index_tcp_cancellation_safety(ob, monkeypatch, tmp_path, stateless, entry, boundary):
    import hashlib
    monkeypatch.setenv("OMBRE_MCP_STATELESS_HTTP", str(stateless))
    store, index, engine = ob.asset_store, ob.asset_embedding_index, ob.embedding_engine
    engine.enabled, engine.model = True, "tcp-synthetic-v1"
    engine._generate_embedding = AsyncMock(return_value=[1., 0.])
    source = store.create_temp_path()
    source.write_bytes(b"synthetic TCP asset")
    asset = store.persist_upload(source, hashlib.sha256(source.read_bytes()).hexdigest(),
                                 source.stat().st_size, "synthetic.bin", "application/octet-stream")
    identity = asset["asset_id"]
    asset = store.update_metadata(identity, title="old keyword")
    assert await index.index_asset(asset) == "indexed"
    old = index._existing(identity)
    started, release, cancelled, finished = [asyncio.Event() for _ in range(4)]
    calls = 0
    async def provider(*args, **kwargs):
        nonlocal calls
        calls += 1
        if boundary == "provider" and calls == 1:
            started.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise
            finally:
                finished.set()
        return [0., 1.]
    engine._generate_embedding = provider
    if entry == "metadata":
        name = "rm_asset_update_metadata"
        arguments = {"asset_id": identity, "title": "new keyword"}
    else:
        store.update_metadata(identity, title="new keyword")
        name = "rm_asset_reindex_embeddings"
        arguments = {"asset_id": identity}
    tool = ob.mcp._tool_manager.get_tool(name)
    original = tool.fn
    outcomes = []
    async def response_loss(**kwargs):
        output = await original(**kwargs)
        outcomes.append(output)
        if len(outcomes) == 1:
            started.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise
            finally:
                finished.set()
        return output
    if boundary == "response":
        monkeypatch.setattr(tool, "fn", response_loss)
    async with live(ob) as client:
        assert bool(await initialize(client)) is not stateless
        assert await disconnect_call(client, name, arguments, started)
        if stateless:
            await wait(cancelled)
        else:
            release.set()
        await wait(finished)
        # Round trip lets the stateful handler finish its synchronous commit.
        result(await rpc(client, "tools/list"))
        assert store.get(identity)["title"] == "new keyword"
        before_retry = index._existing(identity)
        interrupted = stateless and boundary == "provider"
        assert index.is_current(store.get(identity)) is not interrupted
        if interrupted:
            assert before_retry == old
            engine._generate_embedding = AsyncMock(return_value=[0., 1.])
            assert await index.search("semantic-only") == {}
            merged = store.search(query="new keyword", semantic_scores=await index.search("semantic-only"))
            assert merged["total"] == 1
            assert "semantic" not in merged["results"][0]["match_reasons"]
        retry = json.loads(text(await call(client, name, arguments)))
        assert retry["ok"]
        assert index.is_current(store.get(identity))
        stable = index._existing(identity)
        count = calls
        recovery_calls = engine._generate_embedding.await_count if interrupted else None
        again = json.loads(text(await call(client, name, arguments)))
        assert again["ok"] and index._existing(identity) == stable
        if interrupted:
            assert engine._generate_embedding.await_count == recovery_calls
        if entry == "reindex":
            assert retry["indexed"] == int(interrupted)
            assert retry["skipped"] == int(not interrupted)
            assert again == {"ok": True, "scanned": 1, "indexed": 0, "skipped": 1, "failed": 0}
        if not interrupted:
            assert calls == count == 1
        evidence = {"entry": entry, "stateless": stateless, "boundary": boundary,
                    "cancelled": cancelled.is_set(), "old_preserved": before_retry == old,
                    "current_before_retry": not interrupted, "current_after_retry": True,
                    "retry": retry, "repeat": again, "provider_calls_before_recovery": count}
        print("ASSET_TCP_EVIDENCE " + json.dumps(evidence, sort_keys=True))
        import os
        if os.environ.get("S5_RUN"):
            (Path(os.environ["S5_RUN"]) / ("asset-tcp-" + entry + "-" + str(stateless) + "-" + boundary + ".json")).write_text(
                json.dumps(evidence, indent=2), encoding="utf-8")



@pytest.mark.asyncio
@pytest.mark.parametrize("stateless", [False, True])
@pytest.mark.parametrize("authority", ["legacy", "rm"])
async def test_access_log_tickets_real_http(ob, monkeypatch, caplog, stateless, authority):
    """Real custom routes, real L/R persistence, and formatted server logs."""
    import hashlib
    import io
    from PIL import Image

    monkeypatch.setenv("OMBRE_MCP_STATELESS_HTTP", str(stateless))
    monkeypatch.setenv("OMBRE_MCP_ALLOW_QUERY_TOKEN", "true")
    monkeypatch.setenv("OMBRE_MCP_QUERY_TOKEN", "private-query-log-test")
    runtime = ob._get_runtime_components()
    if authority == "rm":
        from asset_backend import RuntimeAssetBackendRegistry
        from tests.test_rm_cutover_routing import _open_rm_state
        pytest.importorskip("remember_me")
        root = Path(ob.config["buckets_dir"])
        monkeypatch.setenv("OMBRE_RM_RUNTIME_ENABLED", "true")
        monkeypatch.setenv("OMBRE_RM_DATA_ROOT", str(root / "remember-me"))
        bundle = ob._bootstrap_remember_me_host(runtime["asset_store"], runtime["embedding_engine"])
        _open_rm_state(root)
        monkeypatch.setenv("OMBRE_ASSET_AUTHORITY", "rm")
        registry = RuntimeAssetBackendRegistry.from_runtime(
            legacy_store=runtime["asset_store"], bundle_provider=lambda: bundle,
            embedding_index=runtime["asset_embedding_index"],
        )
        runtime.update(remember_me_host_bundle=bundle, asset_backend_registry=registry)
    assert ob._selected_asset_backend().name == authority
    for name in ("httpx", "httpcore"):
        monkeypatch.setattr(logging.getLogger(name), "level", logging.WARNING)
    access = logging.getLogger("uvicorn.access")
    monkeypatch.setattr(access, "handlers", [caplog.handler])
    monkeypatch.setattr(access, "propagate", False)
    monkeypatch.setattr(access, "filters", list(access.filters))
    image = io.BytesIO()
    Image.new("RGB", (7, 5), "purple").save(image, format="PNG")
    raw = image.getvalue()
    scopes, secrets_seen = [], ["s2-private-bearer", "private-query-log-test", "private-old-session"]
    expected_rows = []
    with caplog.at_level(logging.INFO):
        async with live(ob, access_log=True, scope_snapshots=scopes) as client:
            session = await initialize(client)
            assert bool(session) == (not stateless)
            if session:
                secrets_seen.append(session)
            browser = json.loads(await ob.asset_browser_upload_link(
                len(raw), hashlib.sha256(raw).hexdigest(), "synthetic.png", "image/png"))
            upload = json.loads(text(await call(client, "rm_asset_upload_link", {
                "expected_bytes": len(raw), "filename": "synthetic.png", "mime_type": "image/png"})))
            assert browser["ok"] and upload["ok"]
            # Seed a real asset for the download route; a separate ticket is used for upload.
            backend = ob._selected_asset_backend()
            if authority == "rm":
                asset = backend.ingest_public_metadata(raw, len(raw), "synthetic.png", "image/png",
                                                       title="", description="", tags=())
            else:
                temp = backend.create_temp_path()
                temp.write_bytes(raw)
                asset = backend.persist_upload(temp, hashlib.sha256(raw).hexdigest(), len(raw),
                                               "synthetic.png", "image/png", require_image=True)
            download = json.loads(text(await call(client, "rm_asset_download_link", {"asset_id": asset["asset_id"]})))
            trial = ob._asset_new_vision_trial()
            assert ob._asset_store_vision_trial(trial)[0]
            vision = json.loads(ob._asset_create_vision_download_link(trial["trial_id"]))
            assert download["ok"] and vision["ok"]
            paths = [browser["upload_path"], upload["upload_path"], download["download_path"], vision["download_path"]]
            for path in paths:
                secrets_seen.append(path.rsplit("/", 1)[1])
                prefix = path.rsplit("/", 1)[0]
                # Percent-encoded prefix, separators and token characters must behave identically.
                encoded = "/" + "".join(f"%{ord(c):02X}" for c in path[1:])
                before = await client.get(path + "?mode=kept")
                encoded_response = await client.get(encoded + "?mode=kept")
                assert before.status_code == encoded_response.status_code == 200
                assert before.content == encoded_response.content
                redirected = await client.get(path + "/?mode=kept", follow_redirects=False)
                assert redirected.status_code == 307
                assert redirected.headers["location"].endswith(path + "?mode=kept")
                uploading = prefix in ("/rm/upload", "/rm/asset-upload")
                head = await client.head(path)
                assert head.status_code == (400 if uploading else 200)
                assert not head.content
                assert (await client.put(path)).status_code == 405
                if not uploading:
                    assert int(head.headers["content-length"]) == len(before.content)
                post = await client.post(path, files={"file": ("synthetic.png", raw, "image/png")})
                assert post.status_code == (200 if uploading else 405)
                if uploading:
                    assert (await client.post(path, files={"file": ("synthetic.png", raw, "image/png")})).status_code == 404
                assert (await client.get(prefix + "/invalid-ticket")).status_code == 404
                expected_rows.extend([(prefix, "GET", 200), (prefix, "GET", 307),
                                      (prefix, "HEAD", head.status_code), (prefix, "PUT", 405), (prefix, "POST", post.status_code),
                                      (prefix, "GET", 404)])
            status = json.loads(text(await call(client, "rm_asset_upload_status", {"upload_id": upload["upload_id"]})))
            assert status["state"] == "completed" and status["source_sha256"] == hashlib.sha256(raw).hexdigest()
            assert json.loads(await ob.asset_browser_upload_status(browser["upload_id"]))["state"] == "completed"
            assert (await rpc(client, "tools/list", path="/mcp?mode=kept&token=private-query-log-test",
                              headers={"Authorization": ""})).status_code == 200
            assert (await rpc(client, "tools/list", headers={"Authorization": "Bearer private-wrong-bearer"})).status_code == 401
            assert (await rpc(client, "tools/list", headers={"Mcp-Session-Id": "private-old-session"})).status_code == (200 if stateless else 404)
            secrets_seen.append("private-wrong-bearer")
            assert (await client.get("/health?mode=kept")).status_code == 200
            def fail_download(*args, **kwargs):
                raise RuntimeError("controlled download failure")
            monkeypatch.setattr(ob, "_asset_read_vision_download", fail_download)
            assert (await client.get(vision["download_path"])).status_code == 500
            expected_rows.append(("/rm/vision-download", "GET", 500))
    rows = [record.getMessage() for record in caplog.records if record.name == "uvicorn.access"]
    for prefix, method, code in expected_rows:
        assert any(f'"{method} {prefix}/[redacted]' in row and row.endswith(f'" {code}') for row in rows)
    assert any("/mcp?mode=kept&token=[redacted] HTTP/1.1" in row for row in rows)
    assert any("/health?mode=kept HTTP/1.1" in row for row in rows)
    assert "controlled download failure" in caplog.text
    assert all(secret not in caplog.text for secret in secrets_seen)
    assert any("?mode=kept HTTP/1.1" in row for row in rows)
    assert scopes and all(before == after for before, after in scopes)
    observed_paths = {before["path"] for before, after in scopes}
    assert set(paths) <= observed_paths


@pytest.mark.asyncio
@pytest.mark.parametrize("stateless", [False, True])
@pytest.mark.parametrize("opt_in", [False, True])
async def test_encoded_query_access_log_real_http(ob, monkeypatch, caplog, stateless, opt_in):
    """Assert production output before evidence serialization or redaction."""
    import uuid
    from starlette.datastructures import QueryParams
    sentinel = "synthetic-" + uuid.uuid4().hex
    credential = sentinel + "=tail"
    monkeypatch.setenv("OMBRE_MCP_STATELESS_HTTP", str(stateless))
    monkeypatch.setenv("OMBRE_MCP_ALLOW_QUERY_TOKEN", str(opt_in))
    monkeypatch.setenv("OMBRE_MCP_QUERY_TOKEN", credential)
    cases = [
        ("literal", "token=" + credential, "token=[redacted]"),
        ("encoded-middle", "to%6ben=" + credential, "to%6ben=[redacted]"),
        ("encoded-first", "%74oken=" + credential, "%74oken=[redacted]"),
        ("duplicate-last-valid", "token=wrong&to%6ben=" + credential, "token=[redacted]&to%6ben=[redacted]"),
        ("duplicate-last-invalid", "%74oken=" + credential + "&token=wrong", "%74oken=[redacted]&token=[redacted]"),
        ("blank", "token=&to%6ben=", "token=[redacted]&to%6ben=[redacted]"),
        ("literal-uppercase", "TOKEN=" + credential, "TOKEN=[redacted]"),
        ("encoded-uppercase", "To%6Ben=" + credential, "To%6Ben=[redacted]"),
        ("double-middle", "to%256ben=public", "to%256ben=public"),
        ("double-first", "%2574oken=public", "%2574oken=public"),
        ("plus", "to+ken=public&to%2Bken=public", "to+ken=public&to%2Bken=public"),
        ("unchanged", "token&empty=&keep=a=b%26c+%20&bad%=public", "token&empty=&keep=a=b%26c+%20&bad%=public"),
        ("mixed", "keep=%74oken%3Dpublic&&to%6ben=" + credential + "&after=%2f+%20", "keep=%74oken%3Dpublic&&to%6ben=[redacted]&after=%2f+%20"),
    ]
    access = logging.getLogger("uvicorn.access")
    monkeypatch.setattr(access, "handlers", [caplog.handler])
    monkeypatch.setattr(access, "propagate", False)
    monkeypatch.setattr(access, "filters", [])
    for name in ("httpx", "httpcore"):
        monkeypatch.setattr(logging.getLogger(name), "level", logging.WARNING)
    scopes, evidence = [], []
    with caplog.at_level(logging.INFO):
        async with live(ob, access_log=True, scope_snapshots=scopes) as client:
            port = client.base_url.port
            client.headers.pop("Authorization")
            for label, query, expected_query in cases:
                start = len(caplog.records)
                response = await rpc(client, "initialize", {
                    "protocolVersion": "2025-03-26", "capabilities": {},
                    "clientInfo": {"name": "synthetic-query-regression", "version": "1"},
                }, path="/mcp?" + query)
                parsed_match = QueryParams(query).get("token") == credential
                assert response.status_code == (200 if opt_in and parsed_match else 401)
                assert bool(response.headers.get("mcp-session-id")) is (response.status_code == 200 and not stateless)
                rows = [r.getMessage() for r in caplog.records[start:] if r.name == "uvicorn.access"]
                assert len(rows) == 1
                # This is the production filter output; no archive substitution.
                assert sentinel not in rows[0]
                assert f"/mcp?{expected_query} HTTP/1.1" in rows[0]
                evidence.append(dict(case=label, status=response.status_code,
                                     parsed_match=parsed_match, sentinel_absent=True,
                                     access_line=rows[0]))
    assert len(scopes) == len(cases) and all(before == after for before, after in scopes)
    for (before, _after), (_label, query, _expected) in zip(scopes, cases):
        assert before["query_string"] == query.encode()
    with socket.socket() as check:
        check.settimeout(1)
        assert check.connect_ex(("127.0.0.1", port)) != 0
    if os.environ.get("QUERY_FIX_EVIDENCE"):
        (Path(os.environ["QUERY_FIX_EVIDENCE"]) / f"http-{stateless}-{opt_in}.json").write_text(
            json.dumps(dict(stateless=stateless, opt_in=opt_in, worker_count=1,
                            root=ob.config["buckets_dir"], port=port,
                            server_task_stopped=True, port_closed=True, scope_unchanged=True,
                            requests=evidence), indent=2), encoding="utf-8")


@pytest.mark.asyncio
@pytest.mark.parametrize("before", ["false", "true"])
async def test_old_session_id_after_restart_keeps_working(ob, monkeypatch, before):
    # A client keeps the Mcp-Session-Id it was handed before a redeploy (by a
    # stateful build) or one it still carries from an even earlier deploy.
    monkeypatch.setenv("OMBRE_MCP_STATELESS_HTTP", before)
    async with live(ob) as client:
        old = await initialize(client) or "id-from-an-earlier-deploy"
        assert "未找到记忆桶" in text(await call(client, "trace", {"bucket_id": "restart-missing"}))
    await settle_sse_shutdown_watcher()
    monkeypatch.delenv("OMBRE_MCP_STATELESS_HTTP")
    restarted = reload_server(monkeypatch)
    bucket = await restarted.bucket_mgr.create("survives the restart")
    async with live(restarted) as client:
        headers = {"Mcp-Session-Id": old}
        tools = result(await rpc(client, "tools/list", headers=headers))
        assert "breath" in {tool["name"] for tool in tools["tools"]}
        response = await call(client, "trace", {"bucket_id": bucket, "importance": 7}, headers=headers)
        assert "mcp-session-id" not in response.headers
        text(response)
    assert (await restarted.bucket_mgr.get(bucket))["metadata"]["importance"] == 7


@pytest.mark.asyncio
async def test_stateless_disconnect_finishes_tool_and_shutdown_waits(ob, monkeypatch):
    monkeypatch.setenv("OMBRE_MCP_STATELESS_HTTP", "true")
    ob._SHIELD_STATELESS_TOOL_CALLS = True
    started, release, cancelled, continued = [asyncio.Event() for _ in range(4)]
    @ob.mcp.tool()
    async def s5_slow() -> str:
        started.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise
        continued.set()
        return "completed"
    async with live(ob) as client:
        await initialize(client)
        assert await disconnect_call(client, "s5_slow", {}, started)
        result(await rpc(client, "tools/list"))
        assert not cancelled.is_set() and not continued.is_set()
        assert len(ob._STATELESS_TOOL_CALL_TASKS) == 1
        # Shutdown starts while the call still waits; the drain lets it finish.
        asyncio.get_running_loop().call_later(0.2, release.set)
    assert continued.is_set() and not cancelled.is_set()
    assert not ob._STATELESS_TOOL_CALL_TASKS


@pytest.mark.asyncio
async def test_stateless_unkeyed_trace_disconnect_still_links_both_ways(ob, monkeypatch):
    monkeypatch.setenv("OMBRE_MCP_STATELESS_HTTP", "true")
    ob._SHIELD_STATELESS_TOOL_CALLS = True
    left = await ob.bucket_mgr.create("original left")
    right = await ob.bucket_mgr.create("original right")
    started, release = asyncio.Event(), asyncio.Event()
    async def pause_refresh(bucket_id, content):
        if bucket_id == left:
            started.set()
            await release.wait()
    monkeypatch.setattr(ob.bucket_mgr, "embedding_engine", SimpleNamespace(
        enabled=True, generate_and_store=pause_refresh,
    ))
    async with live(ob) as client:
        await initialize(client)
        await disconnect_call(client, "trace", {
            "bucket_id": left, "content": "appended fragment", "append": True, "related": right,
        }, started)
        result(await rpc(client, "tools/list"))
        release.set()
        await asyncio.wait_for(asyncio.gather(*ob._STATELESS_TOOL_CALL_TASKS), 5)
    left_bucket, right_bucket = await ob.bucket_mgr.get(left), await ob.bucket_mgr.get(right)
    assert right in left_bucket["metadata"].get("related_buckets", "")
    assert left in right_bucket["metadata"].get("related_buckets", "")
    assert left_bucket["content"].count("appended fragment") == 1

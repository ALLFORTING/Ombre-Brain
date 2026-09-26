import ast
import copy
import hashlib
import importlib
import importlib.metadata
import inspect
import logging
import sys
from pathlib import Path

import pytest
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient


@pytest.fixture
def server(tmp_path, monkeypatch):
    monkeypatch.setenv("OMBRE_BUCKETS_DIR", str(tmp_path / "buckets"))
    monkeypatch.delenv("OMBRE_API_KEY", raising=False)
    sys.modules.pop("server", None)
    module = importlib.import_module("server")
    loggers = [logging.getLogger(name) for name in (
        "mcp.server.streamable_http_manager", "mcp.server.streamable_http", "mcp.server.sse",
    )]
    original_filters = [list(logger.filters) for logger in loggers]
    yield module
    for logger, filters in zip(loggers, original_filters):
        logger.filters[:] = filters


def _diagnostics(caplog):
    return [record for record in caplog.records if record.name == "ombre_brain.mcp"]


@pytest.mark.parametrize("method,status,session", [
    ("POST", 200, b"private-session-200"),
    ("POST", 404, b"private-session-expired"),
    ("GET", 200, b"private-session-get"),
    ("POST", 200, None),
    ("POST", 400, b""),
])
@pytest.mark.asyncio
async def test_diagnostic_fields_and_unchanged_request_response(
    server, caplog, method, status, session,
):
    scope = {
        "type": "http", "method": method, "path": "/mcp",
        "query_string": b"token=secret-query&private=value",
        "headers": [(b"authorization", b"Bearer secret-bearer")],
    }
    if session is not None:
        scope["headers"].append((b"mcp-session-id", session))
    original_scope = copy.deepcopy(scope)
    request = {"type": "http.request", "body": b"private request body"}
    responses = [
        {"type": "http.response.start", "status": status,
         "headers": [(b"content-type", b"text/event-stream"), (b"x-test", b"kept")]},
        {"type": "http.response.body", "body": b"unchanged response", "more_body": False},
    ]
    sent = []

    async def receive():
        return request

    async def send(message):
        sent.append(message)

    async def app(actual_scope, actual_receive, actual_send):
        assert actual_scope is scope
        assert actual_receive is receive
        assert await actual_receive() is request
        for response in responses:
            await actual_send(response)

    with caplog.at_level(logging.INFO, logger="ombre_brain.mcp"):
        await server._MCPRequestDiagnosticMiddleware(app)(scope, receive, send)
    assert scope == original_scope
    assert sent == responses
    assert all(actual is original for actual, original in zip(sent, responses))
    records = _diagnostics(caplog)
    assert len(records) == 1
    expected = f"mcp_request method={method} status={status} has_session="
    expected += "false" if session is None else "true"
    if session is not None:
        expected += " session_hash=" + hashlib.sha256(session).hexdigest()[:12]
    assert records[0].getMessage() == expected
    for secret in ("secret-query", "secret-bearer", "private=value", "private request body"):
        assert secret not in records[0].getMessage()
        assert secret not in repr(records[0].args)
    if session:
        assert session.decode() not in records[0].getMessage()


def test_session_hash_uses_full_id_and_is_stable(server):
    session = "same-private-prefix-" * 8
    digest = hashlib.sha256(session.encode()).hexdigest()[:12]
    assert server._mcp_session_hash(session) == digest
    assert server._mcp_session_hash(session.encode()) == digest
    assert server._mcp_session_hash(session + "different-tail") != digest
    assert session[:12] not in digest


@pytest.mark.asyncio
async def test_sse_is_logged_at_start_before_body_or_disconnect(server, caplog):
    scope = {"type": "http", "method": "GET", "path": "/mcp", "headers": []}
    sent = []

    async def receive():
        pytest.fail("diagnostic middleware must not receive or buffer the stream")

    async def send(message):
        # The record must already exist when downstream receives response.start.
        assert len(_diagnostics(caplog)) == 1
        sent.append(message)

    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        assert _diagnostics(caplog)[0].getMessage().endswith("has_session=false")
        await send({"type": "http.response.body", "body": b"data: first\n\n", "more_body": True})
        await send({"type": "http.response.body", "body": b"", "more_body": False})

    with caplog.at_level(logging.INFO, logger="ombre_brain.mcp"):
        await server._MCPRequestDiagnosticMiddleware(app)(scope, receive, send)
    assert len(sent) == 3
    assert len(_diagnostics(caplog)) == 1


@pytest.mark.parametrize("path", ["/health", "/dashboard", "/mcp-other", "/sse", "/messages"])
@pytest.mark.asyncio
async def test_other_endpoints_are_not_logged(server, caplog, path):
    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})

    async def noop(*args):
        pass

    with caplog.at_level(logging.INFO, logger="ombre_brain.mcp"):
        await server._MCPRequestDiagnosticMiddleware(app)(
            {"type": "http", "path": path}, noop, noop,
        )
    assert not _diagnostics(caplog)


@pytest.mark.parametrize("path", ["/mcp", "/mcp/", "/mcp/sub"])
def test_shared_install_is_idempotent_and_observes_auth_response(
    server, monkeypatch, caplog, path,
):
    monkeypatch.setenv("OMBRE_AUTH_TOKEN", "private-bearer")

    async def ok(request):
        return PlainTextResponse("kept")

    app = Starlette(routes=[Route(path, ok, methods=["POST"])])
    server.add_mcp_auth_middleware(app)
    server.add_http_cors_middleware(app)
    assert server.add_mcp_diagnostic_middleware(app) is app
    server.add_mcp_diagnostic_middleware(app)
    assert sum(item.cls is server._MCPRequestDiagnosticMiddleware for item in app.user_middleware) == 1
    for name in server._MCPSDKSessionRedactionFilter._FULL_ID_MESSAGES:
        assert sum(isinstance(item, server._MCPSDKSessionRedactionFilter)
                   for item in logging.getLogger(name).filters) == 1
    with caplog.at_level(logging.INFO, logger="ombre_brain.mcp"), TestClient(app) as client:
        denied = client.post(path + "?token=private-query", headers={"Mcp-Session-Id": "private-session"})
        allowed = client.post(path, headers={"Authorization": "Bearer private-bearer"})
    assert (denied.status_code, denied.text) == (401, "Unauthorized")
    assert (allowed.status_code, allowed.text) == (200, "kept")
    records = _diagnostics(caplog)
    assert len(records) == 2
    assert "status=401 has_session=true" in records[0].getMessage()
    assert "status=200 has_session=false" in records[1].getMessage()
    assert not any(secret in record.getMessage() for record in records
                   for secret in ("private-bearer", "private-query", "private-session"))
    assert server.mcp.settings.stateless_http is False


@pytest.mark.parametrize("name,level,prefix,suffix", [
    ("mcp.server.streamable_http_manager", logging.INFO, "Created new transport with session ID: ", ""),
    ("mcp.server.streamable_http_manager", logging.INFO, "Session ", " idle timeout"),
    ("mcp.server.streamable_http_manager", logging.ERROR, "Session ", " crashed"),
    ("mcp.server.streamable_http_manager", logging.INFO, "Cleaning up crashed session ", " from active instances."),
    ("mcp.server.streamable_http", logging.INFO, "Terminating session: ", ""),
    ("mcp.server.sse", logging.WARNING, "Received invalid session ID: ", ""),
    ("mcp.server.sse", logging.WARNING, "Could not find session for ID: ", ""),
])
def test_sdk_full_session_messages_are_hashed(server, caplog, name, level, prefix, suffix):
    server.add_mcp_diagnostic_middleware(Starlette())
    session = "private-session-prefix-" * 5
    with caplog.at_level(logging.INFO, logger=name):
        logging.getLogger(name).log(level, prefix + session + suffix)
    record = next(record for record in caplog.records if record.name == name)
    assert record.getMessage() == prefix + "session_hash=" + hashlib.sha256(session.encode()).hexdigest()[:12] + suffix
    assert session[:12] not in record.getMessage()
    assert session not in record.msg


@pytest.mark.parametrize("name,template", [
    ("mcp.server.streamable_http_manager", "Rejecting request for session %s: credential does not match the one that created the session"),
    ("mcp.server.sse", "Rejecting message for session %s: credential does not match"),
])
def test_sdk_credential_warning_redacts_truncated_id(server, caplog, name, template):
    server.add_mcp_diagnostic_middleware(Starlette())
    prefix = "private-session-prefix-" * 3
    with caplog.at_level(logging.WARNING, logger=name):
        logging.getLogger(name).warning(template, prefix[:64])
    record = next(record for record in caplog.records if record.name == name)
    assert record.getMessage() == template.replace("%s", "[redacted]")
    assert not record.args


def test_sdk_filter_preserves_exception_and_unrelated_logs(server, caplog):
    server.add_mcp_diagnostic_middleware(Starlette())
    sdk_logger = logging.getLogger("mcp.server.streamable_http_manager")
    with caplog.at_level(logging.INFO):
        try:
            raise RuntimeError("important failure detail")
        except RuntimeError:
            sdk_logger.exception("Session private-session crashed")
        sdk_logger.error("Other SDK error: %s", "detail retained")
        logging.getLogger("unrelated").info("Session private-session crashed")
    assert caplog.records[0].exc_info[0] is RuntimeError
    assert str(caplog.records[0].exc_info[1]) == "important failure detail"
    assert "private-session" not in caplog.records[0].getMessage()
    assert caplog.records[1].getMessage() == "Other SDK error: detail retained"
    assert caplog.records[2].getMessage() == "Session private-session crashed"
    assert "important failure detail" in caplog.text


def test_sdk_1291_info_session_logs_are_not_debug_only():
    from mcp.server.streamable_http import StreamableHTTPServerTransport
    from mcp.server.streamable_http_manager import StreamableHTTPSessionManager

    assert importlib.metadata.version("mcp") == "1.29.1"
    manager = inspect.getsource(StreamableHTTPSessionManager._handle_stateful_request)
    transport = inspect.getsource(StreamableHTTPServerTransport.terminate)
    assert 'logger.info(f"Created new transport with session ID: {new_session_id}")' in manager
    assert 'request_mcp_session_id[:64]' in manager
    assert 'logger.info(f"Terminating session: {self.mcp_session_id}")' in transport


def test_real_stateful_sdk_creation_termination_and_expired_request_are_private(
    server, monkeypatch, caplog,
):
    monkeypatch.setenv("OMBRE_AUTH_TOKEN", "private-bearer")
    app = server.mcp.streamable_http_app()
    server.add_mcp_auth_middleware(app)
    server.add_mcp_diagnostic_middleware(app)
    headers = {
        "Authorization": "Bearer private-bearer",
        "Accept": "application/json, text/event-stream",
    }
    with caplog.at_level(logging.INFO), TestClient(app) as client:
        initialized = client.post("/mcp", headers=headers, json={
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                       "clientInfo": {"name": "diagnostic-test", "version": "1"}},
        })
        assert initialized.status_code == 200
        session = initialized.headers["mcp-session-id"]
        expired = client.post("/mcp", headers={**headers, "Mcp-Session-Id": "private-expired-session"},
                              json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        terminated = client.delete("/mcp", headers={**headers, "Mcp-Session-Id": session})
    assert expired.status_code == 404
    assert terminated.status_code == 200
    messages = [record.getMessage() for record in caplog.records]
    session_hash = hashlib.sha256(session.encode()).hexdigest()[:12]
    assert "Created new transport with session ID: session_hash=" + session_hash in messages
    assert "Terminating session: session_hash=" + session_hash in messages
    assert any("status=404 has_session=true" in record.getMessage() for record in _diagnostics(caplog))
    assert not any(secret in message for message in messages
                   for secret in (session, session[:12], "private-expired-session", "private-bearer"))
    assert server.mcp.settings.stateless_http is False


def test_both_http_entrypoints_install_shared_helper_before_uvicorn():
    root = Path(__file__).parents[1]
    for filename in ("server.py", "backup_entry.py"):
        tree = ast.parse((root / filename).read_text(encoding="utf-8-sig"))
        calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)]
        installs = [node for node in calls if (
            isinstance(node.func, ast.Name) and node.func.id == "add_mcp_diagnostic_middleware"
        ) or (
            isinstance(node.func, ast.Attribute) and node.func.attr == "add_mcp_diagnostic_middleware"
        )]
        runs = [node for node in calls if isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "uvicorn" and node.func.attr == "run"]
        assert len(installs) == len(runs) == 1
        assert installs[0].lineno < runs[0].lineno
        assert ast.dump(installs[0].args[0]) == ast.dump(runs[0].args[0])

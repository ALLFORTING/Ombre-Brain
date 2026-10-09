import ast
import importlib
import logging
import sys

import pytest
from pathlib import Path

from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient
from tests._server_source import effective_server_source


def _load_server(monkeypatch):
    monkeypatch.delenv("OMBRE_API_KEY", raising=False)
    sys.modules.pop("server", None)
    return importlib.import_module("server")


def _app_with_auth(server):
    async def ok(request):
        return PlainTextResponse("ok")

    app = Starlette(
        routes=[
            Route("/mcp", ok, methods=["GET", "POST"]),
            Route("/mcp/sub", ok, methods=["GET", "POST"]),
            Route("/sse", ok, methods=["GET"]),
            Route("/messages", ok, methods=["POST"]),
            Route("/health", ok),
            Route("/dashboard", ok),
            Route("/auth/login", ok, methods=["POST"]),
        ]
    )
    server.add_mcp_auth_middleware(app)
    return app


def _sse_app_with_auth(server):
    app = server.mcp.sse_app()
    server.add_mcp_auth_middleware(app)
    return app


def test_mcp_fails_closed_when_token_unset_by_default(tmp_path, monkeypatch):
    monkeypatch.setenv("OMBRE_BUCKETS_DIR", str(tmp_path / "buckets"))
    monkeypatch.delenv("OMBRE_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("OMBRE_MCP_ALLOW_ANONYMOUS_HTTP", raising=False)
    server = _load_server(monkeypatch)

    client = TestClient(_app_with_auth(server))

    assert client.get("/mcp").status_code == 401
    assert client.post("/mcp/sub").status_code == 401
    assert client.get("/health").status_code == 200


def test_mcp_anonymous_requires_explicit_opt_in_and_warns(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("OMBRE_BUCKETS_DIR", str(tmp_path / "buckets"))
    monkeypatch.delenv("OMBRE_AUTH_TOKEN", raising=False)
    monkeypatch.setenv("OMBRE_MCP_ALLOW_ANONYMOUS_HTTP", "true")
    server = _load_server(monkeypatch)

    with caplog.at_level("WARNING"):
        client = TestClient(_app_with_auth(server))

    assert client.get("/mcp").status_code == 200
    assert any(
        "SECURITY WARNING" in record.getMessage()
        and "OMBRE_MCP_ALLOW_ANONYMOUS_HTTP" in record.getMessage()
        for record in caplog.records
    )


def test_mcp_requires_token_when_configured(tmp_path, monkeypatch):
    monkeypatch.setenv("OMBRE_BUCKETS_DIR", str(tmp_path / "buckets"))
    monkeypatch.setenv("OMBRE_AUTH_TOKEN", "test-token")
    server = _load_server(monkeypatch)

    client = TestClient(_app_with_auth(server))

    assert client.get("/mcp").status_code == 401
    assert client.get("/mcp", headers={"Authorization": "Bearer bad"}).status_code == 401
    assert client.get("/mcp", headers={"Authorization": "Bearer test-token"}).status_code == 200
    assert client.post("/mcp/sub?token=test-token").status_code == 401


def test_query_token_is_rejected_by_default(tmp_path, monkeypatch):
    monkeypatch.setenv("OMBRE_BUCKETS_DIR", str(tmp_path / "buckets"))
    monkeypatch.delenv("OMBRE_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("OMBRE_MCP_ALLOW_ANONYMOUS_HTTP", raising=False)
    monkeypatch.delenv("OMBRE_MCP_ALLOW_QUERY_TOKEN", raising=False)
    monkeypatch.setenv("OMBRE_MCP_QUERY_TOKEN", "query-secret")
    server = _load_server(monkeypatch)

    client = TestClient(_app_with_auth(server))

    assert client.get("/mcp?token=query-secret").status_code == 401


def test_query_token_requires_dedicated_token_when_enabled(tmp_path, monkeypatch):
    monkeypatch.setenv("OMBRE_BUCKETS_DIR", str(tmp_path / "buckets"))
    monkeypatch.delenv("OMBRE_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("OMBRE_MCP_ALLOW_ANONYMOUS_HTTP", raising=False)
    monkeypatch.setenv("OMBRE_MCP_ALLOW_QUERY_TOKEN", "true")
    monkeypatch.delenv("OMBRE_MCP_QUERY_TOKEN", raising=False)
    server = _load_server(monkeypatch)

    client = TestClient(_app_with_auth(server))

    assert client.get("/mcp?token=query-secret").status_code == 401


def test_query_token_rejects_wrong_dedicated_token(tmp_path, monkeypatch):
    monkeypatch.setenv("OMBRE_BUCKETS_DIR", str(tmp_path / "buckets"))
    monkeypatch.delenv("OMBRE_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("OMBRE_MCP_ALLOW_ANONYMOUS_HTTP", raising=False)
    monkeypatch.setenv("OMBRE_MCP_ALLOW_QUERY_TOKEN", "true")
    monkeypatch.setenv("OMBRE_MCP_QUERY_TOKEN", "query-secret")
    server = _load_server(monkeypatch)

    client = TestClient(_app_with_auth(server))

    assert client.get("/mcp?token=wrong").status_code == 401


def test_query_token_accepts_dedicated_token_when_explicitly_enabled(tmp_path, monkeypatch):
    monkeypatch.setenv("OMBRE_BUCKETS_DIR", str(tmp_path / "buckets"))
    monkeypatch.delenv("OMBRE_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("OMBRE_MCP_ALLOW_ANONYMOUS_HTTP", raising=False)
    monkeypatch.setenv("OMBRE_MCP_ALLOW_QUERY_TOKEN", "true")
    monkeypatch.setenv("OMBRE_MCP_QUERY_TOKEN", "query-secret")
    server = _load_server(monkeypatch)

    client = TestClient(_app_with_auth(server))

    assert client.get("/mcp?token=query-secret").status_code == 200


def test_bearer_token_is_not_reused_as_query_token(tmp_path, monkeypatch):
    monkeypatch.setenv("OMBRE_BUCKETS_DIR", str(tmp_path / "buckets"))
    monkeypatch.setenv("OMBRE_AUTH_TOKEN", "bearer-secret")
    monkeypatch.setenv("OMBRE_MCP_ALLOW_QUERY_TOKEN", "true")
    monkeypatch.delenv("OMBRE_MCP_QUERY_TOKEN", raising=False)
    server = _load_server(monkeypatch)

    client = TestClient(_app_with_auth(server))

    assert client.get("/mcp?token=bearer-secret").status_code == 401

    monkeypatch.setenv("OMBRE_MCP_QUERY_TOKEN", "bearer-secret")
    server = _load_server(monkeypatch)
    assert TestClient(_app_with_auth(server)).get("/mcp?token=bearer-secret").status_code == 200


def test_non_mcp_paths_remain_exempt_with_token(tmp_path, monkeypatch):
    monkeypatch.setenv("OMBRE_BUCKETS_DIR", str(tmp_path / "buckets"))
    monkeypatch.setenv("OMBRE_AUTH_TOKEN", "test-token")
    server = _load_server(monkeypatch)

    client = TestClient(_app_with_auth(server))

    assert client.get("/health").status_code == 200
    assert client.get("/dashboard").status_code == 200
    assert client.post("/auth/login").status_code == 200


def test_sse_mcp_routes_fail_closed_without_token(tmp_path, monkeypatch):
    monkeypatch.setenv("OMBRE_BUCKETS_DIR", str(tmp_path / "buckets"))
    monkeypatch.delenv("OMBRE_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("OMBRE_MCP_ALLOW_ANONYMOUS_HTTP", raising=False)
    server = _load_server(monkeypatch)

    client = TestClient(_sse_app_with_auth(server))

    assert client.get("/sse").status_code == 401
    assert client.post("/messages").status_code == 401
    assert client.get("/health").status_code == 200


def test_sse_mcp_message_route_accepts_bearer(tmp_path, monkeypatch):
    monkeypatch.setenv("OMBRE_BUCKETS_DIR", str(tmp_path / "buckets"))
    monkeypatch.setenv("OMBRE_AUTH_TOKEN", "test-token")
    server = _load_server(monkeypatch)

    client = TestClient(_sse_app_with_auth(server))

    assert client.post(
        "/messages",
        headers={"Authorization": "Bearer test-token"},
    ).status_code != 401


def test_sse_and_message_mcp_paths_accept_dedicated_query_token(tmp_path, monkeypatch):
    monkeypatch.setenv("OMBRE_BUCKETS_DIR", str(tmp_path / "buckets"))
    monkeypatch.delenv("OMBRE_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("OMBRE_MCP_ALLOW_ANONYMOUS_HTTP", raising=False)
    monkeypatch.setenv("OMBRE_MCP_ALLOW_QUERY_TOKEN", "true")
    monkeypatch.setenv("OMBRE_MCP_QUERY_TOKEN", "query-secret")
    server = _load_server(monkeypatch)

    client = TestClient(_app_with_auth(server))

    assert client.get("/sse?token=query-secret").status_code == 200
    assert client.post("/messages?token=query-secret").status_code == 200


def test_sse_mcp_message_route_allows_explicit_anonymous_opt_in(tmp_path, monkeypatch):
    monkeypatch.setenv("OMBRE_BUCKETS_DIR", str(tmp_path / "buckets"))
    monkeypatch.delenv("OMBRE_AUTH_TOKEN", raising=False)
    monkeypatch.setenv("OMBRE_MCP_ALLOW_ANONYMOUS_HTTP", "true")
    server = _load_server(monkeypatch)

    client = TestClient(_sse_app_with_auth(server))

    assert client.post("/messages").status_code != 401


def test_query_token_warning_does_not_include_secret(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("OMBRE_BUCKETS_DIR", str(tmp_path / "buckets"))
    monkeypatch.delenv("OMBRE_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("OMBRE_MCP_ALLOW_ANONYMOUS_HTTP", raising=False)
    monkeypatch.setenv("OMBRE_MCP_ALLOW_QUERY_TOKEN", "true")
    monkeypatch.setenv("OMBRE_MCP_QUERY_TOKEN", "query-secret")

    with caplog.at_level("WARNING"):
        server = _load_server(monkeypatch)
        _app_with_auth(server)

    assert "query-token compatibility enabled" in caplog.text
    assert "query-secret" not in caplog.text


def test_uvicorn_access_log_filter_redacts_only_query_token(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("OMBRE_BUCKETS_DIR", str(tmp_path / "buckets"))
    server = _load_server(monkeypatch)
    record = logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        __file__,
        1,
        '%s - "%s %s HTTP/%s" %d',
        (
            "127.0.0.1:1234",
            "GET",
            "/mcp?mode=stream&token=plain-secret&after=kept",
            "1.1",
            200,
        ),
        None,
    )

    access_logger = logging.getLogger("uvicorn.access")
    access_logger.filters.clear()
    server.install_uvicorn_access_log_redaction()
    server.install_uvicorn_access_log_redaction()
    assert len(access_logger.filters) == 1
    assert access_logger.filters[0].filter(record) is True

    rendered = record.getMessage()
    assert "plain-secret" not in rendered
    assert "/mcp?mode=stream&token=[redacted]&after=kept" in rendered


def test_http_cors_origins_are_explicit(tmp_path, monkeypatch):
    monkeypatch.setenv("OMBRE_BUCKETS_DIR", str(tmp_path / "buckets"))
    monkeypatch.delenv("OMBRE_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("OMBRE_MCP_ALLOW_ANONYMOUS_HTTP", raising=False)
    monkeypatch.delenv("OMBRE_HTTP_ALLOWED_ORIGINS", raising=False)
    server = _load_server(monkeypatch)

    app = _app_with_auth(server)
    server.add_http_cors_middleware(app)
    client = TestClient(app)
    response = client.get("/health", headers={"Origin": "https://evil.example"})
    assert response.headers.get("access-control-allow-origin") is None

    monkeypatch.setenv(
        "OMBRE_HTTP_ALLOWED_ORIGINS",
        "https://one.example, https://two.example",
    )
    app = _app_with_auth(server)
    server.add_http_cors_middleware(app)
    client = TestClient(app)

    allowed = client.get("/health", headers={"Origin": "https://two.example"})
    denied = client.get("/health", headers={"Origin": "https://other.example"})
    assert allowed.headers.get("access-control-allow-origin") == "https://two.example"
    assert denied.headers.get("access-control-allow-origin") is None


def test_both_http_entrypoints_use_shared_cors_policy():
    root = Path(__file__).parents[1]
    server_source = effective_server_source()
    backup_source = (root / "backup_entry.py").read_text(encoding="utf-8")

    assert "add_http_transport_middleware(_app)" in server_source
    helper = next(
        node for node in ast.parse(server_source).body
        if isinstance(node, ast.FunctionDef) and node.name == "add_http_transport_middleware"
    )
    helper_app = helper.args.args[0].arg
    assert any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "add_http_cors_middleware"
        and len(node.args) == 1
        and isinstance(node.args[0], ast.Name)
        and node.args[0].id == helper_app
        for node in ast.walk(helper)
    )
    assert "server.add_http_cors_middleware(app)" in backup_source
    assert "install_uvicorn_access_log_redaction()" in server_source
    assert "server.install_uvicorn_access_log_redaction()" in backup_source
    assert 'allow_origins=["*"]' not in server_source
    assert 'allow_origins=["*"]' not in backup_source



@pytest.fixture
def access_redaction_server(tmp_path, monkeypatch):
    monkeypatch.setenv("OMBRE_BUCKETS_DIR", str(tmp_path / "buckets"))
    return _load_server(monkeypatch)


@pytest.mark.parametrize("route", ["upload", "asset-upload", "asset-download", "vision-download"])
@pytest.mark.parametrize("method", ["GET", "POST", "HEAD"])
@pytest.mark.parametrize("status", [200, 404, 405, 307, 500])
def test_access_ticket_redaction_preserves_request_and_diagnostics(
    access_redaction_server, route, method, status,
):
    from copy import deepcopy
    from uvicorn.protocols.utils import get_path_with_query_string

    scope = {"path": f"/rm/{route}/private-path-ticket",
             "query_string": b"mode=kept&token=private-query&after=also-kept",
             "method": method, "http_version": "1.1",
             "headers": [(b"authorization", b"Bearer private-bearer")],
             "raw_path": f"/rm/{route}/private-path-ticket".encode()}
    original_scope = deepcopy(scope)
    original_args = ("127.0.0.1:1234", method, get_path_with_query_string(scope), "1.1", status)
    record = logging.LogRecord("uvicorn.access", logging.INFO, __file__, 1,
                               '%s - "%s %s HTTP/%s" %d', original_args, None)
    assert access_redaction_server._UvicornAccessTokenRedactionFilter().filter(record)
    formatted = logging.Formatter("%(name)s %(message)s").format(record)
    assert formatted == (f'uvicorn.access 127.0.0.1:1234 - "{method} /rm/{route}/[redacted]'
                         f'?mode=kept&token=[redacted]&after=also-kept HTTP/1.1" {status}')
    assert scope == original_scope
    assert original_args[2] == get_path_with_query_string(original_scope)
    assert record.args[:2] == original_args[:2] and record.args[3:] == original_args[3:]


@pytest.mark.parametrize("route", ["upload", "asset-upload", "asset-download", "vision-download"])
@pytest.mark.parametrize("suffix", ["private-ticket/", "invalid%25ticket", "ticket%2fextra",
                                    "%70rivate%2Dticket", "%2570rivate", "ticket%3fextra"])
def test_access_ticket_redaction_handles_uvicorn_encoded_paths(access_redaction_server, route, suffix):
    from urllib.parse import unquote
    from uvicorn.protocols.utils import get_path_with_query_string

    raw_path = f"/%72m%2f{route}/{suffix}"
    scope = {"path": unquote(raw_path), "raw_path": raw_path.encode(), "query_string": b"keep=yes"}
    logged = get_path_with_query_string(scope)
    expected = f"/rm/{route}/[redacted]" + ("/" if suffix.endswith("/") else "") + "?keep=yes"
    assert access_redaction_server._redact_uvicorn_access_path(logged) == expected
    assert scope["raw_path"] == raw_path.encode() and scope["path"] == unquote(raw_path)


@pytest.mark.parametrize("path", ["/health?mode=kept", "/rm/upload-status/public-id",
                                  "/rm/asset-upload-status/public-id", "/api/assets/public-id/image",
                                  "/rm/asset-upload-other/not-a-ticket", "/rm/upload/"])
def test_access_ticket_redaction_keeps_non_ticket_paths(access_redaction_server, path):
    assert access_redaction_server._redact_uvicorn_access_path(path) == path


@pytest.mark.parametrize("query,expected,authenticated", [
    ("token=synthetic-sentinel", "token=[redacted]", True),
    ("to%6ben=synthetic-sentinel", "to%6ben=[redacted]", True),
    ("%74oken=synthetic-sentinel", "%74oken=[redacted]", True),
    ("TOKEN=synthetic-sentinel", "TOKEN=[redacted]", False),
    ("To%6Ben=synthetic-sentinel", "To%6Ben=[redacted]", False),
    ("token=wrong&to%6ben=synthetic-sentinel", "token=[redacted]&to%6ben=[redacted]", True),
    ("%74oken=synthetic-sentinel&token=wrong", "%74oken=[redacted]&token=[redacted]", False),
    ("token=&to%6ben=", "token=[redacted]&to%6ben=[redacted]", False),
    ("token=synthetic-sentinel=tail", "token=[redacted]", False),
    ("to%256ben=public", "to%256ben=public", False),
    ("%2574oken=public", "%2574oken=public", False),
    ("to+ken=public&to%2Bken=public", "to+ken=public&to%2Bken=public", False),
    ("token&empty=&keep=a=b%26c+%20&bad%=public", "token&empty=&keep=a=b%26c+%20&bad%=public", False),
    ("keep=%74oken%3Dpublic&&to%6ben=synthetic-sentinel&after=%2f+%20", "keep=%74oken%3Dpublic&&to%6ben=[redacted]&after=%2f+%20", True),
])
@pytest.mark.parametrize("opt_in", [False, True])
def test_encoded_query_log_copy_matches_auth_parsing(access_redaction_server, monkeypatch, query, expected, authenticated, opt_in):
    from copy import deepcopy
    from starlette.datastructures import QueryParams
    from uvicorn.protocols.utils import get_path_with_query_string

    ob = access_redaction_server
    monkeypatch.setenv("OMBRE_AUTH_TOKEN", "synthetic-bearer")
    monkeypatch.setenv("OMBRE_MCP_QUERY_TOKEN", "synthetic-sentinel")
    monkeypatch.setenv("OMBRE_MCP_ALLOW_QUERY_TOKEN", str(opt_in))
    monkeypatch.delenv("OMBRE_MCP_ALLOW_ANONYMOUS_HTTP", raising=False)
    assert (QueryParams(query).get("token") == "synthetic-sentinel") is authenticated
    with TestClient(_app_with_auth(ob)) as client:
        assert client.get("/mcp?" + query).status_code == (200 if opt_in and authenticated else 401)
    scope = {"path": "/mcp", "raw_path": b"/mcp", "query_string": query.encode()}
    original = deepcopy(scope)
    args = ("127.0.0.1:1234", "GET", get_path_with_query_string(scope), "1.1", 200)
    record = logging.LogRecord("uvicorn.access", logging.INFO, __file__, 1,
                               '%s - "%s %s HTTP/%s" %d', args, None)
    assert ob._UvicornAccessTokenRedactionFilter().filter(record)
    assert record.args[2] == "/mcp?" + expected
    assert "synthetic-sentinel" not in record.getMessage()
    assert scope == original and args[2] == "/mcp?" + query

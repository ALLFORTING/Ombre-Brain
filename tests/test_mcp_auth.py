import importlib
import sys
from pathlib import Path

from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient


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


def test_sse_mcp_message_route_allows_explicit_anonymous_opt_in(tmp_path, monkeypatch):
    monkeypatch.setenv("OMBRE_BUCKETS_DIR", str(tmp_path / "buckets"))
    monkeypatch.delenv("OMBRE_AUTH_TOKEN", raising=False)
    monkeypatch.setenv("OMBRE_MCP_ALLOW_ANONYMOUS_HTTP", "true")
    server = _load_server(monkeypatch)

    client = TestClient(_sse_app_with_auth(server))

    assert client.post("/messages").status_code != 401


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
    server_source = (root / "server.py").read_text(encoding="utf-8")
    backup_source = (root / "backup_entry.py").read_text(encoding="utf-8")

    assert "add_http_cors_middleware(_app)" in server_source
    assert "server.add_http_cors_middleware(app)" in backup_source
    assert 'allow_origins=["*"]' not in server_source
    assert 'allow_origins=["*"]' not in backup_source

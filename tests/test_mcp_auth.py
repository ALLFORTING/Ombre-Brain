import importlib
import sys

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


def test_mcp_anonymous_passes_when_token_unset(tmp_path, monkeypatch):
    monkeypatch.setenv("OMBRE_BUCKETS_DIR", str(tmp_path / "buckets"))
    monkeypatch.delenv("OMBRE_AUTH_TOKEN", raising=False)
    server = _load_server(monkeypatch)

    client = TestClient(_app_with_auth(server))

    assert client.get("/mcp").status_code == 200
    assert client.post("/mcp/sub").status_code == 200
    assert client.get("/health").status_code == 200


def test_mcp_requires_token_when_configured(tmp_path, monkeypatch):
    monkeypatch.setenv("OMBRE_BUCKETS_DIR", str(tmp_path / "buckets"))
    monkeypatch.setenv("OMBRE_AUTH_TOKEN", "test-token")
    server = _load_server(monkeypatch)

    client = TestClient(_app_with_auth(server))

    assert client.get("/mcp").status_code == 401
    assert client.get("/mcp", headers={"Authorization": "Bearer bad"}).status_code == 401
    assert client.get("/mcp", headers={"Authorization": "Bearer test-token"}).status_code == 200
    assert client.post("/mcp/sub?token=test-token").status_code == 200


def test_non_mcp_paths_remain_exempt_with_token(tmp_path, monkeypatch):
    monkeypatch.setenv("OMBRE_BUCKETS_DIR", str(tmp_path / "buckets"))
    monkeypatch.setenv("OMBRE_AUTH_TOKEN", "test-token")
    server = _load_server(monkeypatch)

    client = TestClient(_app_with_auth(server))

    assert client.get("/health").status_code == 200
    assert client.get("/dashboard").status_code == 200
    assert client.post("/auth/login").status_code == 200


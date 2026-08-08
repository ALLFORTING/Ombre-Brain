import importlib
import sys

import pytest
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient


def _load_server(tmp_path, monkeypatch):
    monkeypatch.setenv("OMBRE_BUCKETS_DIR", str(tmp_path / "buckets"))
    monkeypatch.delenv("OMBRE_DASHBOARD_PASSWORD", raising=False)
    monkeypatch.delenv("OMBRE_HOOK_TOKEN", raising=False)
    monkeypatch.delenv("OMBRE_AUTH_TOKEN", raising=False)
    sys.modules.pop("server", None)
    return importlib.import_module("server")


def _client(server):
    app = Starlette(routes=[
        Route("/auth/login", server.auth_login, methods=["POST"]),
        Route("/api/config", server.api_config_update, methods=["POST"]),
        Route("/api/host-vault", server.api_host_vault_set, methods=["POST"]),
        Route("/api/import/upload", server.api_import_upload, methods=["POST"]),
        Route("/api/import/pause", server.api_import_pause, methods=["POST"]),
        Route("/api/import/review", server.api_import_review, methods=["POST"]),
    ])
    return TestClient(app, base_url="http://testserver")


def _login(server, client):
    response = client.post(
        "/auth/login",
        headers={"Origin": "http://testserver"},
        json={"password": "dashboard-password"},
    )
    assert response.status_code == 200
    session = client.cookies.get("ombre_session")
    return {
        "Origin": "http://testserver",
        "X-Ombre-CSRF": server._sessions[session]["csrf_token"],
    }


@pytest.mark.security
def test_login_requires_same_origin_and_handles_non_string_passwords(
    tmp_path, monkeypatch
):
    server = _load_server(tmp_path, monkeypatch)
    server._save_password_hash("dashboard-password")
    client = _client(server)

    assert client.post(
        "/auth/login", json={"password": "dashboard-password"}
    ).status_code == 403
    assert client.post(
        "/auth/login",
        headers={"Origin": "https://evil.example"},
        json={"password": "dashboard-password"},
    ).status_code == 403
    assert client.post(
        "/auth/login",
        headers={"Origin": "http://testserver"},
        json={"password": None},
    ).status_code == 400
    assert client.post(
        "/auth/login",
        headers={"Origin": "http://testserver"},
        json={"password": 123456},
    ).status_code == 400

    monkeypatch.setenv("OMBRE_DASHBOARD_PASSWORD", "环境密码")
    assert client.post(
        "/auth/login",
        headers={"Origin": "http://testserver"},
        json={"password": "环境密码"},
    ).status_code == 200


@pytest.mark.security
def test_all_cookie_session_write_routes_require_csrf_and_origin(
    tmp_path, monkeypatch
):
    server = _load_server(tmp_path, monkeypatch)
    server._save_password_hash("dashboard-password")
    server.import_engine._running = False
    client = _client(server)
    headers = _login(server, client)

    cases = [
        ("/api/config", {"json": {}}),
        ("/api/host-vault", {"json": {"value": 123}}),
        ("/api/import/upload", {"content": b""}),
        ("/api/import/pause", {}),
        ("/api/import/review", {"json": {"decisions": []}}),
    ]
    for path, kwargs in cases:
        missing_csrf = client.post(
            path, headers={"Origin": "http://testserver"}, **kwargs
        )
        assert missing_csrf.status_code == 403
        assert missing_csrf.json() == {"error": "csrf_required"}

        wrong_origin = client.post(
            path,
            headers={**headers, "Origin": "https://evil.example"},
            **kwargs,
        )
        assert wrong_origin.status_code == 403
        assert wrong_origin.json() == {"error": "same_origin_required"}

        allowed = client.post(path, headers=headers, **kwargs)
        assert allowed.status_code not in {401, 403}


@pytest.mark.security
def test_dashboard_write_calls_use_auth_fetch_without_multipart_override():
    dashboard = open("dashboard.html", encoding="utf-8").read()
    for path in (
        "/api/config",
        "/api/import/upload",
        "/api/import/pause",
        "/api/import/review",
    ):
        assert f"authFetch('{path}" in dashboard
    assert "fetch(BASE + '/api/config'" not in dashboard
    assert "fetch(BASE + '/api/import/upload'" not in dashboard
    assert "fetch(BASE + '/api/import/pause'" not in dashboard
    assert "fetch(BASE + '/api/import/review'" not in dashboard
    assert "multipart/form-data" not in dashboard

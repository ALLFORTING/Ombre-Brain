import importlib
import json
import os
import stat
import sys
from types import SimpleNamespace

import pytest
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient


def _load_server(tmp_path, monkeypatch, *, env_password=None):
    monkeypatch.setenv("OMBRE_BUCKETS_DIR", str(tmp_path / "buckets"))
    monkeypatch.delenv("OMBRE_HOOK_TOKEN", raising=False)
    monkeypatch.delenv("OMBRE_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("OMBRE_DASHBOARD_PASSWORD", raising=False)
    if env_password is not None:
        monkeypatch.setenv("OMBRE_DASHBOARD_PASSWORD", env_password)
    sys.modules.pop("server", None)
    return importlib.import_module("server")


def _auth_client(server):
    app = Starlette(routes=[
        Route("/auth/status", server.auth_status, methods=["GET"]),
        Route("/auth/setup", server.auth_setup_endpoint, methods=["POST"]),
        Route("/auth/login", server.auth_login, methods=["POST"]),
        Route("/auth/change-password", server.auth_change_password, methods=["POST"]),
    ])
    return TestClient(app, base_url="http://testserver")


def _auth_file(server):
    return os.path.join(server.config["buckets_dir"], ".dashboard_auth.json")


def _login_headers(server, client):
    session_token = client.cookies.get("ombre_session")
    csrf_token = server._sessions[session_token]["csrf_token"]
    return {
        "Origin": "http://testserver",
        "X-Ombre-CSRF": csrf_token,
    }, session_token


@pytest.mark.security
def test_auth_store_distinguishes_missing_valid_corrupt_and_abnormal_nodes(
    tmp_path, monkeypatch, caplog
):
    server = _load_server(tmp_path, monkeypatch)
    auth_file = _auth_file(server)

    assert server._auth_store_state() == (server._AUTH_STORE_MISSING, None)
    assert server._is_setup_needed() is True

    server._save_password_hash("file-password")
    state, stored = server._auth_store_state()
    assert state == server._AUTH_STORE_VALID
    assert isinstance(stored, str)
    assert server._is_setup_needed() is False
    if os.name != "nt":
        assert stat.S_IMODE(os.stat(auth_file).st_mode) == 0o600

    with open(auth_file, "w", encoding="utf-8") as handle:
        handle.write("{broken")
    with caplog.at_level("ERROR", logger="ombre_brain"):
        state, stored = server._auth_store_state()
    assert (state, stored) == (server._AUTH_STORE_CORRUPT, None)
    assert server._is_setup_needed() is False
    assert "auth_store_corrupt" in caplog.text
    assert "file-password" not in caplog.text

    os.remove(auth_file)
    os.mkdir(auth_file)
    with caplog.at_level("ERROR", logger="ombre_brain"):
        state, stored = server._auth_store_state()
    assert (state, stored) == (server._AUTH_STORE_UNREADABLE, None)
    assert server._is_setup_needed() is False
    assert "auth_store_unreadable" in caplog.text


@pytest.mark.security
def test_corrupt_auth_store_closes_setup_even_with_env_recovery_password(
    tmp_path, monkeypatch
):
    server = _load_server(tmp_path, monkeypatch, env_password="env-password")
    auth_file = _auth_file(server)
    os.makedirs(os.path.dirname(auth_file), exist_ok=True)
    with open(auth_file, "w", encoding="utf-8") as handle:
        handle.write("{broken")
    client = _auth_client(server)

    response = client.post("/auth/setup", json={"password": "new-password"})

    assert response.status_code == 503
    assert response.json() == {"error": "auth_store_unreadable"}
    assert "password_hash" not in response.text
    assert "env-password" not in response.text


@pytest.mark.security
def test_env_password_recovers_corrupt_store_through_change_password(
    tmp_path, monkeypatch, caplog
):
    server = _load_server(tmp_path, monkeypatch, env_password="env-password")
    auth_file = _auth_file(server)
    os.makedirs(os.path.dirname(auth_file), exist_ok=True)
    with open(auth_file, "w", encoding="utf-8") as handle:
        handle.write("{broken")
    client = _auth_client(server)

    login = client.post("/auth/login", json={"password": "env-password"})
    assert login.status_code == 200
    headers, old_session = _login_headers(server, client)

    with caplog.at_level("INFO", logger="ombre_brain"):
        response = client.post(
            "/auth/change-password",
            headers=headers,
            json={"current": "env-password", "new": "new-file-password"},
        )

    assert response.status_code == 200
    assert json.loads(open(auth_file, encoding="utf-8").read())["password_hash"]
    if os.name != "nt":
        assert stat.S_IMODE(os.stat(auth_file).st_mode) == 0o600
    assert old_session not in server._sessions
    assert "auth_store_recovered state=corrupt" in caplog.text
    assert "env-password" not in caplog.text
    assert "new-file-password" not in caplog.text

    assert server._verify_any_password("env-password") is True
    assert server._verify_any_password("new-file-password") is False


@pytest.mark.security
def test_env_password_with_valid_store_keeps_change_password_disabled(
    tmp_path, monkeypatch
):
    server = _load_server(tmp_path, monkeypatch)
    server._save_password_hash("file-password")
    monkeypatch.setenv("OMBRE_DASHBOARD_PASSWORD", "env-password")
    client = _auth_client(server)

    assert client.post("/auth/login", json={"password": "env-password"}).status_code == 200
    headers, _old_session = _login_headers(server, client)
    response = client.post(
        "/auth/change-password",
        headers=headers,
        json={"current": "env-password", "new": "new-password"},
    )

    assert response.status_code == 400
    assert "直接修改" in response.text


@pytest.mark.security
def test_corrupt_store_without_env_password_fails_closed(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    auth_file = _auth_file(server)
    os.makedirs(os.path.dirname(auth_file), exist_ok=True)
    with open(auth_file, "w", encoding="utf-8") as handle:
        handle.write("{broken")
    client = _auth_client(server)

    assert client.post("/auth/login", json={"password": "anything"}).status_code == 401
    setup = client.post("/auth/setup", json={"password": "new-password"})
    assert setup.status_code == 503
    assert setup.json() == {"error": "auth_store_unreadable"}


@pytest.mark.security
def test_atomic_password_replace_preserves_original_when_publish_fails(
    tmp_path, monkeypatch
):
    server = _load_server(tmp_path, monkeypatch)
    server._save_password_hash("old-password")
    auth_file = _auth_file(server)
    original = open(auth_file, "rb").read()

    def fail_replace(_source, _target):
        raise OSError("synthetic replace failure")

    monkeypatch.setattr(server.os, "replace", fail_replace)
    with pytest.raises(OSError, match="synthetic replace failure"):
        server._save_password_hash("new-password")

    assert open(auth_file, "rb").read() == original
    temp_files = [
        entry.name
        for entry in os.scandir(os.path.dirname(auth_file))
        if entry.name.startswith(".dashboard_auth.") and entry.name.endswith(".tmp")
    ]
    assert temp_files == []


@pytest.mark.security
def test_non_ascii_and_non_string_password_comparisons_fail_closed(
    tmp_path, monkeypatch
):
    server = _load_server(tmp_path, monkeypatch, env_password="密码")

    assert server._verify_any_password("密码") is True
    assert server._verify_any_password("错误") is False
    assert server._verify_any_password(None) is False
    assert server._verify_any_password(123456) is False

    token = server._create_session()
    request = SimpleNamespace(
        cookies={"ombre_session": token},
        headers={
            "x-ombre-csrf": "非 ASCII",
            "origin": "http://testserver",
            "host": "testserver",
        },
        url=SimpleNamespace(scheme="http"),
    )
    response = server._require_dashboard_write(request, "/test")
    assert response.status_code == 403
    assert response.body == b'{"error":"csrf_required"}'

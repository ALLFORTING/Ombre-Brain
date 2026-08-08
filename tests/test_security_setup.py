import asyncio
import hashlib
import importlib
import json
import multiprocessing
import os
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


def _auth_client(server):
    app = Starlette(routes=[
        Route("/auth/setup", server.auth_setup_endpoint, methods=["POST"]),
        Route("/auth/login", server.auth_login, methods=["POST"]),
        Route("/auth/status", server.auth_status, methods=["GET"]),
    ])
    return TestClient(app, base_url="http://testserver")


def _setup_headers(server, token=None, origin="http://testserver"):
    return {
        "Origin": origin,
        "X-Ombre-Setup-Token": server._setup_token if token is None else token,
    }


def _auth_file(server):
    return os.path.join(server.config["buckets_dir"], ".dashboard_auth.json")


@pytest.mark.security
def test_setup_requires_same_origin_and_consumes_token_once(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    client = _auth_client(server)
    token = server._setup_token
    assert isinstance(token, str)
    assert len(token) >= 32

    assert client.post(
        "/auth/setup", headers={"X-Ombre-Setup-Token": token},
        json={"password": "setup-password"},
    ).status_code == 403
    assert client.post(
        "/auth/setup", headers=_setup_headers(server, token, "https://evil.example"),
        json={"password": "setup-password"},
    ).status_code == 403
    assert client.post(
        "/auth/setup", headers=_setup_headers(server, "wrong-token"),
        json={"password": "setup-password"},
    ).status_code == 403
    assert not os.path.lexists(_auth_file(server))

    response = client.post(
        "/auth/setup", headers=_setup_headers(server, token),
        json={"password": "setup-password"},
    )
    assert response.status_code == 200
    assert response.json() == {"ok": True}
    assert server._setup_token is None
    assert client.cookies.get("ombre_session")

    second = client.post(
        "/auth/setup", headers=_setup_headers(server, token),
        json={"password": "another-password"},
    )
    assert second.status_code == 400
    with open(_auth_file(server), encoding="utf-8") as handle:
        assert json.load(handle)["password_hash"]


@pytest.mark.security
def test_setup_rejects_invalid_passwords_without_consuming_token(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    client = _auth_client(server)
    token = server._setup_token

    for password in (None, 123456, "  valid-password", "valid-password ", "short"):
        response = client.post(
            "/auth/setup", headers=_setup_headers(server, token),
            json={"password": password},
        )
        assert response.status_code == 400
        assert server._setup_token == token
        assert not os.path.lexists(_auth_file(server))

    response = client.post(
        "/auth/setup", headers=_setup_headers(server, token),
        json={"password": "valid-password"},
    )
    assert response.status_code == 200


@pytest.mark.security
def test_corrupt_or_valid_auth_store_never_gets_startup_token(tmp_path, monkeypatch):
    buckets = tmp_path / "corrupt-buckets"
    buckets.mkdir()
    (buckets / ".dashboard_auth.json").write_text("{broken", encoding="utf-8")
    monkeypatch.setenv("OMBRE_BUCKETS_DIR", str(buckets))
    monkeypatch.delenv("OMBRE_DASHBOARD_PASSWORD", raising=False)
    monkeypatch.delenv("OMBRE_HOOK_TOKEN", raising=False)
    monkeypatch.delenv("OMBRE_AUTH_TOKEN", raising=False)
    sys.modules.pop("server", None)
    corrupt_server = importlib.import_module("server")
    assert corrupt_server._setup_token is None
    corrupt_client = _auth_client(corrupt_server)
    assert corrupt_client.post(
        "/auth/setup", headers={"Origin": "http://testserver"},
        json={"password": "new-password"},
    ).status_code == 503

    valid_buckets = tmp_path / "valid-buckets"
    valid_buckets.mkdir()
    salt = "a" * 32
    digest = hashlib.sha256(f"{salt}:file-password".encode()).hexdigest()
    (valid_buckets / ".dashboard_auth.json").write_text(
        json.dumps({"password_hash": f"{salt}:{digest}"}), encoding="utf-8"
    )
    monkeypatch.setenv("OMBRE_BUCKETS_DIR", str(valid_buckets))
    sys.modules.pop("server", None)
    valid_server = importlib.import_module("server")
    assert valid_server._setup_token is None


@pytest.mark.security
def test_setup_write_failure_leaves_token_available_for_retry(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    client = _auth_client(server)
    token = server._setup_token
    original = server._create_auth_file_if_absent

    def fail_once(_password_hash):
        raise OSError("synthetic setup publish failure")

    monkeypatch.setattr(server, "_create_auth_file_if_absent", fail_once)
    failed = client.post(
        "/auth/setup", headers=_setup_headers(server, token),
        json={"password": "retry-password"},
    )
    assert failed.status_code == 500
    assert failed.json() == {"error": "setup_failed"}
    assert server._setup_token == token
    assert not os.path.lexists(_auth_file(server))

    monkeypatch.setattr(server, "_create_auth_file_if_absent", original)
    retried = client.post(
        "/auth/setup", headers=_setup_headers(server, token),
        json={"password": "retry-password"},
    )
    assert retried.status_code == 200


@pytest.mark.security
def test_publish_competition_returns_409_without_consuming_loser_token(
    tmp_path, monkeypatch
):
    server = _load_server(tmp_path, monkeypatch)
    client = _auth_client(server)
    token = server._setup_token
    original = server._create_auth_file_if_absent
    monkeypatch.setattr(
        server, "_create_auth_file_if_absent",
        lambda _password_hash: server._AUTH_PUBLISH_EXISTS,
    )

    response = client.post(
        "/auth/setup", headers=_setup_headers(server, token),
        json={"password": "winner-password"},
    )
    assert response.status_code == 409
    assert response.json() == {"error": "setup_conflict"}
    assert server._setup_token == token
    assert not os.path.lexists(_auth_file(server))
    assert not server._sessions

    monkeypatch.setattr(server, "_create_auth_file_if_absent", original)
    assert client.post(
        "/auth/setup", headers=_setup_headers(server, token),
        json={"password": "winner-password"},
    ).status_code == 200


@pytest.mark.security
def test_same_process_setup_race_has_one_success_and_one_session(
    tmp_path, monkeypatch
):
    server = _load_server(tmp_path, monkeypatch)
    token = server._setup_token

    class Request:
        headers = {
            "origin": "http://testserver",
            "host": "testserver",
            "x-ombre-setup-token": token,
        }
        url = type("URL", (), {"scheme": "http"})()

        async def json(self):
            return {"password": "concurrent-password"}

    async def run_race():
        return await asyncio.gather(
            server._auth_setup_impl(Request()), server._auth_setup_impl(Request())
        )

    responses = asyncio.run(run_race())
    assert sorted(response.status_code for response in responses) == [200, 400]
    assert len(server._sessions) == 1
    assert server._auth_store_state()[0] == server._AUTH_STORE_VALID


@pytest.mark.security
def test_session_failure_keeps_published_auth_and_allows_login(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    client = _auth_client(server)
    token = server._setup_token

    def fail_session():
        raise RuntimeError("synthetic session failure")

    monkeypatch.setattr(server, "_create_session", fail_session)
    response = client.post(
        "/auth/setup", headers=_setup_headers(server, token),
        json={"password": "login-password"},
    )
    assert response.status_code == 500
    assert response.json() == {"error": "setup_completed_login_required"}
    assert server._setup_token is None
    assert server._is_setup_needed() is False
    assert server._verify_any_password("login-password") is True

    monkeypatch.setattr(server, "_create_session", lambda: "login-session")
    login = client.post(
        "/auth/login", headers={"Origin": "http://testserver"},
        json={"password": "login-password"},
    )
    assert login.status_code == 200


def _publish_worker(buckets_dir, barrier, result_queue):
    try:
        os.environ["OMBRE_BUCKETS_DIR"] = buckets_dir
        os.environ.pop("OMBRE_DASHBOARD_PASSWORD", None)
        os.environ.pop("OMBRE_HOOK_TOKEN", None)
        os.environ.pop("OMBRE_AUTH_TOKEN", None)
        sys.modules.pop("server", None)
        server = importlib.import_module("server")
        barrier.wait(timeout=30)
        salt = "b" * 32
        digest = hashlib.sha256(f"{salt}:process-password".encode()).hexdigest()
        result_queue.put(server._create_auth_file_if_absent(f"{salt}:{digest}"))
    except BaseException as exc:
        result_queue.put((type(exc).__name__, str(exc)))


@pytest.mark.security
def test_cross_process_publish_race_has_one_winner(tmp_path):
    buckets = tmp_path / "process-buckets"
    buckets.mkdir()
    context = multiprocessing.get_context("spawn")
    barrier = context.Barrier(2)
    result_queue = context.Queue()
    processes = [
        context.Process(
            target=_publish_worker,
            args=(str(buckets), barrier, result_queue),
        )
        for _ in range(2)
    ]
    for process in processes:
        process.start()
    results = [result_queue.get(timeout=60) for _ in processes]
    for process in processes:
        process.join(timeout=60)
    assert all(process.exitcode == 0 for process in processes)
    assert sorted(results) == ["created", "exists"]

    payload = json.loads(
        (buckets / ".dashboard_auth.json").read_text(encoding="utf-8")
    )
    assert payload["password_hash"].endswith(
        hashlib.sha256(b"b" * 32 + b":process-password").hexdigest()
    )


@pytest.mark.security
def test_link_fallback_is_limited_to_compatibility_errors(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    auth_file = _auth_file(server)
    monkeypatch.setattr(
        server.os, "link", lambda _source, _target: (_ for _ in ()).throw(
            OSError(server.errno.EINVAL, "link unsupported")
        ),
    )
    result = server._create_auth_file_if_absent(server._password_hash_record("fallback"))
    assert result == server._AUTH_PUBLISH_CREATED
    assert server._auth_store_state()[0] == server._AUTH_STORE_VALID

    os.remove(auth_file)
    monkeypatch.setattr(
        server.os, "link", lambda _source, _target: (_ for _ in ()).throw(
            OSError(server.errno.EACCES, "permission denied")
        ),
    )
    with pytest.raises(OSError):
        server._create_auth_file_if_absent(server._password_hash_record("no-fallback"))
    assert not os.path.lexists(auth_file)

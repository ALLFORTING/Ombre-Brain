import asyncio
import importlib
import sys
from unittest.mock import Mock

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
        Route(
            "/api/bucket/{bucket_id}",
            server.api_bucket_detail,
            methods=["GET", "PATCH", "DELETE"],
        ),
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


def _authenticated_client(server):
    client = _client(server)
    token = server._create_session()
    client.cookies.set("ombre_session", token)
    client.headers.update({
        "Origin": "http://testserver",
        "X-Ombre-CSRF": server._sessions[token]["csrf_token"],
    })
    return client


def _delete_preview(client, bucket_id):
    response = client.delete(f"/api/bucket/{bucket_id}")
    assert response.status_code == 200
    assert response.json()["status"] == "preview"
    assert response.json()["deleted"] is False
    return response.json()


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
    anonymous = _client(server)

    cases = [
        ("/api/config", {"json": {}}),
        ("/api/host-vault", {"json": {"value": 123}}),
        ("/api/import/upload", {"content": b""}),
        ("/api/import/pause", {}),
        ("/api/import/review", {"json": {"decisions": []}}),
    ]
    for path, kwargs in cases:
        assert anonymous.post(
            path,
            headers={"Origin": "http://testserver"},
            **kwargs,
        ).status_code == 401

        missing_csrf = client.post(
            path, headers={"Origin": "http://testserver"}, **kwargs
        )
        assert missing_csrf.status_code == 403
        assert missing_csrf.json() == {"error": "csrf_required"}

        wrong_csrf = client.post(
            path,
            headers={"Origin": "http://testserver", "X-Ombre-CSRF": "wrong"},
            **kwargs,
        )
        assert wrong_csrf.status_code == 403
        assert wrong_csrf.json() == {"error": "csrf_required"}

        missing_origin = client.post(
            path,
            headers={"X-Ombre-CSRF": headers["X-Ombre-CSRF"]},
            **kwargs,
        )
        assert missing_origin.status_code == 403
        assert missing_origin.json() == {"error": "same_origin_required"}

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

    pause = dashboard.split("async function pauseImport()", 1)[1].split(
        "async function loadImportResults()", 1
    )[0]
    review = dashboard.split("async function reviewAction", 1)[1].split(
        "async function batchReview", 1
    )[0]
    batch = dashboard.split("async function batchReview", 1)[1].split(
        "</script>", 1
    )[0]
    assert "if (!resp) return;" in pause
    assert "if (resp.ok) return;" in pause
    assert "alert(" in pause
    assert "maintenance_in_progress" in dashboard
    assert "if (!resp.ok)" in review
    assert review.index("if (!resp.ok)") < review.index("card.style.display")
    assert "detectPatterns();" in batch
    assert batch.index("if (!resp.ok)") < batch.index("detectPatterns();")
    assert "authFetch('/api/bucket/'" in dashboard
    assert "async function saveDetailContent()" in dashboard
    assert "function beginDetailEdit()" in dashboard
    assert "async function deleteDetailBucket()" in dashboard
    assert "取消" in dashboard
    assert "deletePlanPrompt" in dashboard
    assert "method: 'PATCH'" in dashboard
    assert "method: 'DELETE'" in dashboard

    save = dashboard.split("async function saveDetailContent()", 1)[1].split(
        "async function deleteDetailBucket()", 1
    )[0]
    delete = dashboard.split("async function deleteDetailBucket()", 1)[1].split(
        "function closeDetail()", 1
    )[0]
    assert "await loadBuckets();" in save
    assert "await showDetail(id);" in save
    assert "closeDetail();" in delete
    assert "await loadBuckets();" in delete


def test_dashboard_bucket_patch_updates_all_bucket_states_and_preserves_wikilinks(
    tmp_path, monkeypatch
):
    server = _load_server(tmp_path, monkeypatch)
    client = _authenticated_client(server)
    cases = ({}, {"pinned": True}, {"protected": True}, {"sealed": True})
    for index, options in enumerate(cases):
        bucket_id = asyncio.run(
            server.bucket_mgr.create(
                content=f"old [[Original Link {index}]]",
                name="Dashboard content",
                **options,
            )
        )

        detail = client.get(f"/api/bucket/{bucket_id}")
        assert detail.status_code == 200
        assert detail.json()["content"] != detail.json()["raw_content"]
        assert detail.json()["raw_content"] == f"old [[Original Link {index}]]"

        response = client.patch(
            f"/api/bucket/{bucket_id}",
            json={"content": f"new [[Saved Link {index}]]"},
        )
        assert response.status_code == 200
        assert response.json()["raw_content"] == f"new [[Saved Link {index}]]"
        assert (asyncio.run(server.bucket_mgr.get(bucket_id)))["content"] == (
            f"new [[Saved Link {index}]]"
        )
        history = server.bucket_mgr.get_history(bucket_id)
        assert history[0]["change_type"] == "dashboard_replace"
        assert history[0]["old_content"] == f"old [[Original Link {index}]]"


def test_dashboard_bucket_delete_requires_server_plan_and_preserves_protections(
    tmp_path, monkeypatch
):
    server = _load_server(tmp_path, monkeypatch)
    client = _authenticated_client(server)
    cases = ({}, {"pinned": True}, {"protected": True}, {"sealed": True})
    for index, options in enumerate(cases):
        bucket_id = asyncio.run(
            server.bucket_mgr.create(content=f"delete body {index}", **options)
        )
        response = client.delete(f"/api/bucket/{bucket_id}")
        if options:
            assert response.status_code == 409
            assert response.json()["plan"][0]["protections"]
            assert "confirm_token" not in response.json()
            assert asyncio.run(server.bucket_mgr.get(bucket_id)) is not None
            continue
        assert response.status_code == 200
        preview = response.json()
        assert preview["plan"][0]["bucket_id"] == bucket_id
        assert asyncio.run(server.bucket_mgr.get(bucket_id)) is not None
        confirmed = client.request("DELETE",
            f"/api/bucket/{bucket_id}",
            json={"confirm_token": preview["confirm_token"]},
        )
        assert confirmed.status_code == 200
        assert confirmed.json() == {"id": bucket_id, "deleted": True}
        assert asyncio.run(server.bucket_mgr.get(bucket_id)) is None
        assert server.bucket_mgr.get_history(bucket_id)[0]["change_type"] == "delete"


def test_dashboard_bucket_patch_history_failure_is_fail_closed(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    bucket_id = asyncio.run(server.bucket_mgr.create(content="history authority"))
    server.bucket_mgr.record_history = Mock(side_effect=RuntimeError("history unavailable"))
    client = _authenticated_client(server)

    response = client.patch(
        f"/api/bucket/{bucket_id}",
        json={"content": "must not write"},
    )

    assert response.status_code == 500
    assert response.json() == {"error": "content_update_failed"}
    assert (asyncio.run(server.bucket_mgr.get(bucket_id)))["content"] == (
        "history authority"
    )


def test_dashboard_bucket_delete_history_failure_is_fail_closed(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    bucket_id = asyncio.run(server.bucket_mgr.create(content="delete history authority"))
    server.bucket_mgr.record_history = Mock(side_effect=RuntimeError("history unavailable"))
    client = _authenticated_client(server)

    token = _delete_preview(client, bucket_id)["confirm_token"]
    response = client.request("DELETE",
        f"/api/bucket/{bucket_id}", json={"confirm_token": token}
    )

    assert response.status_code == 500
    assert response.json()["error"] == "bucket_delete_failed"
    assert response.json()["deleted"] is False
    assert (asyncio.run(server.bucket_mgr.get(bucket_id)))["content"] == (
        "delete history authority"
    )


def test_dashboard_delete_token_is_once_only_expires_and_rejects_state_change(
    tmp_path, monkeypatch
):
    server = _load_server(tmp_path, monkeypatch)
    client = _authenticated_client(server)
    target = asyncio.run(server.bucket_mgr.create("target"))
    first = _delete_preview(client, target)["confirm_token"]
    asyncio.run(server.bucket_mgr.update(target, importance=7))
    stale = client.request("DELETE", f"/api/bucket/{target}", json={"confirm_token": first})
    assert stale.status_code == 409
    assert asyncio.run(server.bucket_mgr.get(target)) is not None
    second = _delete_preview(client, target)["confirm_token"]
    server._mutation_confirm_tokens[second]["expires_at"] = 0
    assert client.request("DELETE",
        f"/api/bucket/{target}", json={"confirm_token": second}
    ).status_code == 409
    third = _delete_preview(client, target)["confirm_token"]
    assert client.request("DELETE",
        f"/api/bucket/{target}", json={"confirm_token": third}
    ).json()["deleted"] is True
    assert third not in server._mutation_confirm_tokens
    assert client.request("DELETE",
        f"/api/bucket/{target}", json={"confirm_token": third}
    ).status_code == 404


def test_dashboard_delete_rechecks_incoming_supersession_and_cleans_outgoing(
    tmp_path, monkeypatch
):
    server = _load_server(tmp_path, monkeypatch)
    client = _authenticated_client(server)
    target = asyncio.run(server.bucket_mgr.create("target"))
    source = asyncio.run(server.bucket_mgr.create("source"))
    token = _delete_preview(client, target)["confirm_token"]
    asyncio.run(server.trace(source, superseded_by=target))
    blocked = client.request("DELETE", f"/api/bucket/{target}", json={"confirm_token": token})
    assert blocked.status_code == 409
    assert blocked.json()["plan"][0]["incoming_supersession"] == [source]
    assert asyncio.run(server.bucket_mgr.get(target)) is not None
    asyncio.run(server.trace(source, superseded_by=""))
    successor = asyncio.run(server.bucket_mgr.create("successor"))
    asyncio.run(server.trace(target, superseded_by=successor))
    token = _delete_preview(client, target)["confirm_token"]
    assert client.request("DELETE",
        f"/api/bucket/{target}", json={"confirm_token": token}
    ).json()["deleted"] is True
    reverse = (asyncio.run(server.bucket_mgr.get(successor)))["metadata"]
    assert target not in server._supersedes_ids(reverse)


def test_import_review_delete_uses_same_two_stage_plan(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    client = _authenticated_client(server)
    target = asyncio.run(server.bucket_mgr.create("review target"))
    payload = {"decisions": [{"bucket_id": target, "action": "delete"}]}
    preview = client.post("/api/import/review", json=payload)
    assert preview.status_code == 200
    assert preview.json()["status"] == "preview"
    assert asyncio.run(server.bucket_mgr.get(target)) is not None
    payload["decisions"][0]["confirm_token"] = preview.json()["confirm_token"]
    result = client.post("/api/import/review", json=payload)
    assert result.status_code == 200
    assert result.json()["deleted"] is True
    assert asyncio.run(server.bucket_mgr.get(target)) is None


def test_mcp_delete_preview_token_can_confirm_on_dashboard(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    client = _authenticated_client(server)
    target = asyncio.run(server.bucket_mgr.create("shared delete plan"))
    preview = asyncio.run(server.trace(target, delete=True))
    token = preview.split("confirm_token:", 1)[1].splitlines()[0].strip()
    result = client.request(
        "DELETE", f"/api/bucket/{target}", json={"confirm_token": token}
    )
    assert result.status_code == 200
    assert result.json()["deleted"] is True
    assert token not in server._mutation_confirm_tokens


def test_dashboard_bucket_patch_keeps_auth_csrf_and_origin_protection(
    tmp_path, monkeypatch
):
    server = _load_server(tmp_path, monkeypatch)
    bucket_id = asyncio.run(server.bucket_mgr.create(content="auth boundary"))
    anonymous = _client(server)
    for method, kwargs in (
        ("patch", {"json": {"content": "no auth"}}),
        ("delete", {}),
    ):
        assert getattr(anonymous, method)(
            f"/api/bucket/{bucket_id}",
            headers={"Origin": "http://testserver"},
            **kwargs,
        ).status_code == 401

    for method, kwargs in (
        ("patch", {"json": {"content": "no csrf"}}),
        ("delete", {}),
    ):
        missing_csrf = _authenticated_client(server)
        missing_csrf.headers.pop("X-Ombre-CSRF")
        response = getattr(missing_csrf, method)(
            f"/api/bucket/{bucket_id}",
            **kwargs,
        )
        assert response.status_code == 403
        assert response.json() == {"error": "csrf_required"}

    for method, kwargs in (
        ("patch", {"json": {"content": "no origin"}}),
        ("delete", {}),
    ):
        wrong_origin = _authenticated_client(server)
        wrong_origin.headers["Origin"] = "https://evil.example"
        response = getattr(wrong_origin, method)(
            f"/api/bucket/{bucket_id}",
            **kwargs,
        )
        assert response.status_code == 403
        assert response.json() == {"error": "same_origin_required"}

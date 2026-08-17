import asyncio
import importlib
import json
import os
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient


def _load_server(tmp_path, monkeypatch):
    monkeypatch.setenv("OMBRE_BUCKETS_DIR", str(tmp_path / "buckets"))
    monkeypatch.delenv("OMBRE_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("OMBRE_API_KEY", raising=False)
    sys.modules.pop("server", None)
    return importlib.import_module("server")


def _app(server):
    return Starlette(
        routes=[
            Route(
                "/__operator/rm-runtime-evidence",
                server._rm_runtime_evidence,
                methods=["GET"],
            ),
            Route(
                "/__operator/rm-runtime-evidence/ephemeral-probe",
                server._rm_ephemeral_runtime_evidence,
                methods=["POST"],
            ),
        ]
    )


def _runtime_registry():
    validation = SimpleNamespace(
        authority=SimpleNamespace(value="rm"),
        boot_mode="NORMAL",
        writes_allowed=False,
        frozen=True,
        recovery_required=False,
        legacy_fallback_allowed=False,
    )
    snapshot = SimpleNamespace(
        authority=SimpleNamespace(value="rm"),
        state=SimpleNamespace(value="frozen_rm_acceptance"),
        freeze_status="active",
        rm_available=True,
    )
    backend = SimpleNamespace(name="rm")
    return SimpleNamespace(
        _validate_boot=lambda: validation,
        selected_backend=lambda: backend,
        snapshot=snapshot,
    )


def test_runtime_evidence_requires_configured_bearer_and_rejects_query_token(
    tmp_path, monkeypatch
):
    server = _load_server(tmp_path, monkeypatch)
    server.asset_backend_registry = _runtime_registry()
    monkeypatch.setenv("OMBRE_AUTH_TOKEN", "operator-test-token")
    monkeypatch.setenv("RENDER_INSTANCE_ID", "instance-actual")
    monkeypatch.setenv("RENDER_GIT_COMMIT", "commit-actual")
    monkeypatch.setenv("RENDER_SERVICE_ID", "service-actual")
    monkeypatch.setenv("RM_PROBE_TRUSTED_INSTANCE_ID", "instance-spoof")
    monkeypatch.setenv("RM_PROBE_TRUSTED_GIT_COMMIT", "commit-spoof")
    monkeypatch.setenv("RM_PROBE_TRUSTED_SERVICE_ID", "service-spoof")
    client = TestClient(_app(server))

    assert client.get("/__operator/rm-runtime-evidence").status_code == 401
    assert client.get(
        "/__operator/rm-runtime-evidence?token=operator-test-token"
    ).status_code == 401
    assert client.get(
        "/__operator/rm-runtime-evidence",
        headers={"Authorization": "Bearer wrong"},
    ).status_code == 401

    response = client.get(
        "/__operator/rm-runtime-evidence",
        headers={"Authorization": "Bearer operator-test-token"},
    )
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    payload = response.json()
    assert payload == {
        "status": "ok",
        "authority": "rm",
        "durable_authority": "rm",
        "selected_backend": "rm",
        "cutover_state": "frozen_rm_acceptance",
        "freeze_status": "active",
        "boot_mode": "NORMAL",
        "writes_allowed": False,
        "frozen": True,
        "recovery_required": False,
        "legacy_fallback_allowed": False,
        "rm_available": True,
        "process_boot_id": server._RM_PROCESS_BOOT_ID,
        "process_started_at": server._RM_PROCESS_STARTED_AT,
        "platform_identity": {
            "instance_id": "instance-actual",
            "git_commit": "commit-actual",
            "service_id": "service-actual",
        },
        "runtime_boot_validation_passed": True,
    }
    assert len(payload["process_boot_id"]) == 32
    assert "token" not in response.text.casefold()
    assert "instance-spoof" not in response.text
    assert "commit-spoof" not in response.text
    assert "service-spoof" not in response.text


def test_runtime_evidence_fails_closed_without_auth_configuration(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    server.asset_backend_registry = _runtime_registry()
    response = TestClient(_app(server)).get("/__operator/rm-runtime-evidence")
    assert response.status_code == 503
    assert response.json() == {
        "status": "unavailable",
        "error": "runtime_evidence_unavailable",
    }


def test_runtime_evidence_sanitizes_runtime_failure(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    server.asset_backend_registry = SimpleNamespace(
        _validate_boot=lambda: (_ for _ in ()).throw(RuntimeError("secret-path")),
    )
    monkeypatch.setenv("OMBRE_AUTH_TOKEN", "operator-test-token")
    response = TestClient(_app(server)).get(
        "/__operator/rm-runtime-evidence",
        headers={"Authorization": "Bearer operator-test-token"},
    )
    assert response.status_code == 503
    assert response.json() == {
        "status": "unavailable",
        "error": "runtime_evidence_unavailable",
    }
    assert "secret-path" not in response.text


def test_ephemeral_probe_requires_bearer_and_is_no_store(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    monkeypatch.setenv("OMBRE_AUTH_TOKEN", "operator-test-token")
    client = TestClient(_app(server))

    assert client.post("/__operator/rm-runtime-evidence/ephemeral-probe").status_code == 401
    assert client.post(
        "/__operator/rm-runtime-evidence/ephemeral-probe?token=operator-test-token"
    ).status_code == 401
    response = client.post(
        "/__operator/rm-runtime-evidence/ephemeral-probe",
        headers={"Authorization": "Bearer operator-test-token"},
    )
    assert response.status_code == 503
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["status"] == "INCOMPLETE"
    assert "token" not in response.text.casefold()


def test_host_ephemeral_ticket_probe_cleans_upload_and_download_state(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    monkeypatch.setattr(server, "asset_backend_registry", _runtime_registry())
    result = server._rm_ephemeral_runtime_probe()

    assert result["upload_ticket_recreated"] is True
    assert result["download_ticket_recreated"] is True
    assert result["verification_session_recreated"] is False
    assert result["status"] == "INCOMPLETE"
    assert result["capability_not_exposed"] is True
    assert result["durable_mutation_performed"] is False
    assert server._rm_asset_uploads == {}
    assert server._rm_asset_upload_tokens == {}
    assert server._rm_asset_upload_sources == {}
    assert server._rm_asset_download_tokens == {}
    assert server._rm_asset_download_sources == {}


def test_verification_probe_uses_real_empty_rm_service_and_closes_session(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    from remember_me_host_runtime import create_remember_me_host_bundle

    bundle = create_remember_me_host_bundle(
        data_root=tmp_path / "rm-runtime",
        token_store={},
        ticket_source_store={},
        download_lock=threading.Lock(),
        public_base_url="",
        ttl_seconds=300,
        max_tokens=100,
    )
    monkeypatch.setattr(server, "remember_me_host_bundle", bundle)

    result = server._rm_probe_verification_session()

    assert result == {"status": "PASS", "error": None}
    sessions = bundle.core_adapter._runtime.service._verification_sessions
    assert sessions == {}


def test_repeated_successful_verification_probes_do_not_grow_registry(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    from remember_me_host_runtime import create_remember_me_host_bundle

    bundle = create_remember_me_host_bundle(
        data_root=tmp_path / "rm-runtime",
        token_store={},
        ticket_source_store={},
        download_lock=threading.Lock(),
        public_base_url="",
        ttl_seconds=300,
        max_tokens=100,
    )
    monkeypatch.setattr(server, "remember_me_host_bundle", bundle)
    service = bundle.core_adapter._runtime.service

    for _ in range(25):
        assert server._rm_probe_verification_session() == {"status": "PASS", "error": None}
        assert service._verification_sessions == {}


def test_verification_probe_preserves_unrelated_active_and_closed_sessions(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    from remember_me.core import (
        BeginAssetVerificationRequest,
        CompleteAssetVerificationRequest,
        ListAssetVerificationPageRequest,
    )
    from remember_me_host_runtime import create_remember_me_host_bundle

    bundle = create_remember_me_host_bundle(
        data_root=tmp_path / "rm-runtime",
        token_store={},
        ticket_source_store={},
        download_lock=threading.Lock(),
        public_base_url="",
        ttl_seconds=300,
        max_tokens=100,
    )
    monkeypatch.setattr(server, "remember_me_host_bundle", bundle)
    service = bundle.core_adapter._runtime.service

    closed = service.begin_asset_verification(BeginAssetVerificationRequest(kind="image"))
    service.list_asset_verification_page(
        ListAssetVerificationPageRequest(snapshot_id=closed.snapshot_id, cursor="", limit=500)
    )
    service.complete_asset_verification(
        CompleteAssetVerificationRequest(snapshot_id=closed.snapshot_id)
    )
    active = service.begin_asset_verification(BeginAssetVerificationRequest(kind="image"))
    before = set(service._verification_sessions)

    assert server._rm_probe_verification_session() == {"status": "PASS", "error": None}
    assert set(service._verification_sessions) == before
    assert service._verification_sessions[closed.snapshot_id].closed is True
    assert service._verification_sessions[active.snapshot_id].closed is False


def test_ephemeral_probe_failure_paths_leave_no_process_local_state(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)

    original_claim = server._rm_claim_asset_upload

    def fail_claim(_token):
        raise RuntimeError("injected upload failure")

    monkeypatch.setattr(server, "_rm_claim_asset_upload", fail_claim)
    upload_result = server._rm_probe_upload_ticket()
    assert upload_result == {"status": "FAIL", "error": "upload_ticket_probe_failed"}
    assert server._rm_asset_uploads == {}
    assert server._rm_asset_upload_tokens == {}
    assert server._rm_asset_upload_sources == {}
    monkeypatch.setattr(server, "_rm_claim_asset_upload", original_claim)

    original_store = server._rm_store_asset_download_ticket_locked

    def store_then_fail(*args, **kwargs):
        original_store(*args, **kwargs)
        raise RuntimeError("injected download failure")

    monkeypatch.setattr(server, "_rm_store_asset_download_ticket_locked", store_then_fail)
    download_result = server._rm_probe_download_ticket()
    assert download_result == {"status": "FAIL", "error": "download_ticket_probe_failed"}
    assert server._rm_asset_download_tokens == {}
    assert server._rm_asset_download_sources == {}

    from remember_me_host_runtime import create_remember_me_host_bundle

    bundle = create_remember_me_host_bundle(
        data_root=tmp_path / "rm-runtime",
        token_store={},
        ticket_source_store={},
        download_lock=threading.Lock(),
        public_base_url="",
        ttl_seconds=300,
        max_tokens=100,
    )
    monkeypatch.setattr(server, "remember_me_host_bundle", bundle)
    service = bundle.core_adapter._runtime.service

    def fail_page(_request):
        raise RuntimeError("injected verification failure")

    monkeypatch.setattr(service, "list_asset_verification_page", fail_page)
    verification_result = server._rm_probe_verification_session()
    assert verification_result == {"status": "FAIL", "error": "verification_probe_failed"}
    assert service._verification_sessions == {}


def test_failed_verification_probe_does_not_grow_or_remove_unrelated_sessions(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    from remember_me.core import (
        BeginAssetVerificationRequest,
        CompleteAssetVerificationRequest,
        ListAssetVerificationPageRequest,
    )
    from remember_me_host_runtime import create_remember_me_host_bundle

    bundle = create_remember_me_host_bundle(
        data_root=tmp_path / "rm-runtime",
        token_store={},
        ticket_source_store={},
        download_lock=threading.Lock(),
        public_base_url="",
        ttl_seconds=300,
        max_tokens=100,
    )
    monkeypatch.setattr(server, "remember_me_host_bundle", bundle)
    service = bundle.core_adapter._runtime.service
    closed = service.begin_asset_verification(BeginAssetVerificationRequest(kind="image"))
    service.list_asset_verification_page(
        ListAssetVerificationPageRequest(snapshot_id=closed.snapshot_id, cursor="", limit=500)
    )
    service.complete_asset_verification(
        CompleteAssetVerificationRequest(snapshot_id=closed.snapshot_id)
    )
    active = service.begin_asset_verification(BeginAssetVerificationRequest(kind="image"))
    before = set(service._verification_sessions)

    def fail_page(_request):
        raise RuntimeError("injected verification failure")

    monkeypatch.setattr(service, "list_asset_verification_page", fail_page)
    assert server._rm_probe_verification_session() == {
        "status": "FAIL",
        "error": "verification_probe_failed",
    }
    assert set(service._verification_sessions) == before
    assert service._verification_sessions[closed.snapshot_id].closed is True
    assert service._verification_sessions[active.snapshot_id].closed is False


def test_ephemeral_probe_route_returns_only_sanitized_lifecycle_flags(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    from remember_me_host_runtime import create_remember_me_host_bundle

    bundle = create_remember_me_host_bundle(
        data_root=tmp_path / "rm-runtime",
        token_store={},
        ticket_source_store={},
        download_lock=threading.Lock(),
        public_base_url="",
        ttl_seconds=300,
        max_tokens=100,
    )
    monkeypatch.setattr(server, "remember_me_host_bundle", bundle)
    monkeypatch.setattr(server, "asset_backend_registry", _runtime_registry())
    monkeypatch.setenv("OMBRE_AUTH_TOKEN", "operator-test-token")

    response = TestClient(_app(server)).post(
        "/__operator/rm-runtime-evidence/ephemeral-probe",
        headers={"Authorization": "Bearer operator-test-token"},
    )

    assert response.status_code == 200
    assert response.json() == {
        "status": "PASS",
        "upload_ticket_recreated": True,
        "download_ticket_recreated": True,
        "verification_session_recreated": True,
        "ephemeral_cleanup_complete": True,
        "capability_not_exposed": True,
        "durable_mutation_performed": False,
    }
    lowered = response.text.casefold()
    assert "token" not in lowered
    assert "url" not in lowered
    assert "path" not in lowered
    assert "snapshot" not in lowered


def test_rm_upload_link_calls_frozen_gate_before_creating_ticket(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)

    class Backend:
        name = "rm"

        def assert_public_mutation_allowed(self):
            raise server.AssetBackendError("asset_write_frozen")

    monkeypatch.setattr(server, "_selected_asset_backend", lambda: Backend())
    result = json.loads(
        asyncio.run(server.rm_asset_upload_link(1, "probe.png", "image/png"))
    )
    assert result == {"error": "asset_write_frozen", "ok": False}
    assert not server._rm_asset_uploads


def test_fresh_process_recreates_process_local_rm_ticket(tmp_path):
    code = (
        "import json; "
        "import server; "
        "result=server._rm_probe_upload_ticket(); "
        "print(json.dumps({'status': result.get('status'), 'boot_id': server._RM_PROCESS_BOOT_ID, 'uploads': len(server._rm_asset_uploads)}))"
    )
    environment = os.environ.copy()
    environment["OMBRE_BUCKETS_DIR"] = str(tmp_path / "buckets")
    environment["OMBRE_ASSET_AUTHORITY"] = "legacy"
    environment.pop("OMBRE_RM_DATA_ROOT", None)
    root = Path(__file__).resolve().parents[1]

    observations = []
    for _ in range(2):
        completed = subprocess.run(
            [sys.executable, "-c", code],
            cwd=root,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )
        assert completed.returncode == 0, completed.stderr
        observations.append(json.loads(completed.stdout))

    assert all(item["status"] == "PASS" and item["uploads"] == 0 for item in observations)
    assert observations[0]["boot_id"] != observations[1]["boot_id"]

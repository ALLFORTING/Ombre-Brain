from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
from pathlib import Path
import sqlite3
import tarfile
import threading
import uuid

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.testclient import TestClient

import backup_auto_runtime as auto
import offline_backup_bundle as bundles
import production_backup_capture as capture
from backup_v2_oidc import GitHubActionsBackupV2OidcVerifier
from maintenance_write_gate import MaintenanceWriteCoordinator
from tests.test_stage8h_g1d_backup_v2_runtime import _server

COMMIT = "1" * 40


def claims():
    return {"repository": auto.AUTO_REPOSITORY, "repository_owner": "ALLFORTING",
            "repository_id": auto.AUTO_REPOSITORY_ID, "repository_owner_id": auto.AUTO_OWNER_ID,
            "repository_visibility": "private", "ref": "refs/heads/main", "event_name": "schedule",
            "workflow_ref": auto.AUTO_WORKFLOW_REF, "aud": auto.AUTO_AUDIENCE,
            "run_id": "123", "run_attempt": "1"}


def seed(root):
    folder = root / "permanent" / "test"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "sealed.md").write_text("---\nid: sealed-id\nsealed: 1\ndormant: true\n---\nSECRET BODY", encoding="utf-8")
    (folder / "open.md").write_text("---\nid: open-id\nsealed: 0\n---\nOPEN BODY", encoding="utf-8")
    with sqlite3.connect(root / "history.db") as conn:
        conn.executescript("CREATE TABLE letters(id INTEGER PRIMARY KEY, content TEXT, sealed INTEGER, created_at TEXT);"
                           "CREATE TABLE notes(note_id INTEGER PRIMARY KEY, text TEXT, sealed INTEGER, open_at TEXT);")
        conn.execute("INSERT INTO letters VALUES(1, 'SECRET LETTER', 1, '2026-01-01')")
        conn.execute("INSERT INTO notes VALUES(1, 'SECRET NOTE', 1, '2026-12-01')")


def controller(tmp_path, *, resolver=None, clock=None):
    source = tmp_path / "source"
    source.mkdir()
    seed(source)
    workspace = bundles.prepare_backup_workspace(tmp_path / "workspace")
    return capture.ProductionBackupCaptureController(
        enabled=True, worker_count=1, coordinator=MaintenanceWriteCoordinator(),
        source_root=source, workspace_root=workspace.root, recipient_public_key=None,
        recipient_fingerprint="none", runtime_commit=COMMIT,
        limits=capture.CaptureLimits(2, 30, 1024*1024, 1024*1024, 1, 5),
        oidc_policy=auto.StrictBackupAutoOidcPolicy(), plaintext=True,
        runtime_commit_resolver=resolver, clock=clock)


async def ready(c):
    request_id = str(uuid.uuid4())
    await c.create_capture(request_id=request_id, expected_runtime_commit=COMMIT,
                           expected_recipient_fingerprint="none", claims=claims())
    result = await c.wait_for_terminal(request_id, claims())
    assert result["state"] == "ready", result
    return request_id, result, c.workspace.bundles_root / (result["bundle_id"] + bundles.PLAIN_SUFFIX)


def restore(c, path):
    raw = path.read_bytes()
    return bundles.restore_plain_bundle(c.workspace.root, path.name,
        expected_sha256=hashlib.sha256(raw).hexdigest(), expected_size=len(raw),
        restore_name="a"*32, maximum_bytes=1024*1024)


@pytest.mark.asyncio
async def test_plain_round_trip_and_no_body_report(tmp_path):
    c = controller(tmp_path)
    request_id, result, path = await ready(c)
    report = restore(c, path)
    assert report["reconciliation_matched"]
    summary = report["reconciliation"]
    assert summary["buckets"]["ids"] == ["open-id", "sealed-id"]
    assert summary["buckets"]["sealed_count"] == 1
    assert all(store["count"] == store["sealed_count"] == 1 for store in summary["stores"].values())
    assert not any(word in json.dumps(report) for word in ("SECRET BODY", "SECRET LETTER", "SECRET NOTE", "OPEN BODY"))
    assert c.coordinator.status().state == "open"
    assert c._active_request_id is None
    async with c.delivery(request_id, claims()) as delivery:
        assert delivery.handle.read() == path.read_bytes()
    assert not c._active_deliveries
    with pytest.raises(bundles.BackupBundleError):
        restore(c, path)  # no overwrite
    await c.acknowledge(request_id, claims())
    assert not path.exists()


def rewrite(path, mutation):
    with tarfile.open(path, "r:") as archive:
        items = [(member.name, archive.extractfile(member).read()) for member in archive.getmembers()]
    mutation(items)
    with tarfile.open(path, "w") as archive:
        for name, raw in items:
            info = tarfile.TarInfo(name)
            info.size = len(raw)
            archive.addfile(info, io.BytesIO(raw))


@pytest.mark.parametrize("kind", ["manifest", "content", "path", "sealed", "letters", "notes", "sqlite"])
@pytest.mark.asyncio
async def test_corruption_or_reconciliation_refused(tmp_path, kind):
    c = controller(tmp_path)
    _, _, path = await ready(c)

    def mutate(items):
        if kind == "manifest":
            items[0] = (items[0][0], items[0][1].replace(b'"ob_commit_sha"', b'"bogus_commit"'))
        elif kind == "content":
            index = next(i for i, item in enumerate(items) if item[0].endswith("sealed.md"))
            items[index] = (items[index][0], items[index][1].replace(b"SECRET", b"BROKEN"))
        elif kind == "path":
            items[1] = ("data/../../escape", items[1][1])
        elif kind == "sqlite":
            index = next(i for i, item in enumerate(items) if item[0].endswith("history.db"))
            items[index] = (items[index][0], b"X" * len(items[index][1]))
        else:
            manifest = json.loads(items[0][1])
            if kind == "sealed":
                manifest["reconciliation"]["buckets"]["records"][1]["sealed"] = 0
            else:
                store = manifest["reconciliation"]["stores"]["history.db:" + kind]
                store["records"][0]["state_sha256"] = "0" * 64
            manifest.pop("manifest_sha256")
            manifest["manifest_sha256"] = hashlib.sha256(bundles._canonical_json_bytes(manifest)).hexdigest()
            items[0] = (items[0][0], bundles._canonical_json_bytes(manifest))
    rewrite(path, mutate)
    with pytest.raises(bundles.BackupBundleError):
        restore(c, path)
    assert not list(c.workspace.restored_root.iterdir())


@pytest.mark.parametrize("field,value", [("repository", "attacker/ob-backup"), ("repository_id", "1"),
    ("repository_owner_id", "1"), ("repository_owner", "attacker"), ("repository_visibility", "public"),
    ("workflow_ref", "ALLFORTING/ob-backup/.github/workflows/backup-v2.yml@refs/heads/main"),
    ("aud", capture.V2_AUDIENCE), ("event_name", "pull_request"), ("ref", "refs/heads/other"),
    ("run_id", "0"), ("run_attempt", "0"), ("run_attempt", 1), ("environment", "production"),
    ("job_workflow_ref", "attacker/reusable")])
def test_auto_oidc_precise_rejection(field, value):
    token_claims = claims()
    token_claims[field] = value
    with pytest.raises(capture.CaptureChannelError, match="oidc_denied"):
        auto.StrictBackupAutoOidcPolicy().verify(token_claims)


@pytest.mark.asyncio
async def test_oidc_signature_and_audience():
    from types import SimpleNamespace
    from time import time
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = SimpleNamespace(get_signing_key_from_jwt=lambda token: SimpleNamespace(key=key.public_key()))
    verifier = GitHubActionsBackupV2OidcVerifier(jwk_client=jwk, audience=auto.AUTO_AUDIENCE)
    data = {**claims(), "iss": "https://token.actions.githubusercontent.com", "iat": int(time()),
            "nbf": int(time()) - 1, "exp": int(time()) + 60}
    def request(token):
        return Request({"type": "http", "headers": [(b"authorization", ("Bearer " + token).encode())],
                        "query_string": b""}, receive=lambda: None)
    async def verify(data, signing=key):
        req = request(jwt.encode(data, signing, algorithm="RS256", headers={"kid": "test"}))
        req._body = b""
        return await verifier.verify_request(req)
    assert (await verify(data))["aud"] == auto.AUTO_AUDIENCE
    for modified in ({**data, "aud": capture.V2_AUDIENCE}, {**data, "exp": int(time()) - 1}):
        with pytest.raises(capture.CaptureChannelError):
            await verify(modified)
    with pytest.raises(capture.CaptureChannelError):
        await verify(data, rsa.generate_private_key(public_exponent=65537, key_size=2048))


@pytest.mark.asyncio
async def test_sha_change_during_capture_releases_freeze(tmp_path, monkeypatch):
    sha = [COMMIT]
    c = controller(tmp_path, resolver=lambda: sha[0])
    original = capture.capture_external_source
    def changed(*args, **kwargs):
        result = original(*args, **kwargs)
        sha[0] = "2" * 40
        return result
    monkeypatch.setattr(capture, "capture_external_source", changed)
    request_id = str(uuid.uuid4())
    await c.create_capture(request_id=request_id, expected_runtime_commit=COMMIT,
                           expected_recipient_fingerprint="none", claims=claims())
    await asyncio.shield(c._tasks[request_id])
    result = c._jobs[request_id].public()
    assert result["state"] == "failed" and result["failure_code"] == "capture_identity_mismatch"
    assert c.coordinator.status().state == "open" and c._active_request_id is None
    assert not list(c.workspace.bundles_root.iterdir())
    with pytest.raises(capture.CaptureChannelError, match="capture_identity_mismatch"):
        c.get_job(request_id, claims())
    with pytest.raises(capture.CaptureChannelError, match="capture_identity_mismatch"):
        await c.create_capture(request_id=str(uuid.uuid4()), expected_runtime_commit=COMMIT,
                               expected_recipient_fingerprint="none", claims=claims())


@pytest.mark.asyncio
async def test_ttl_preserves_unknown_and_active_delivery(tmp_path):
    now = [datetime.now(timezone.utc)]
    c = controller(tmp_path, clock=lambda: now[0])
    request_id, _, path = await ready(c)
    unknown = c.workspace.bundles_root / "unknown.obplain.tar"
    unknown.write_bytes(b"KEEP")
    now[0] += timedelta(seconds=6)
    async with c.delivery(request_id, claims()):
        assert await c.cleanup_stale() == 0
    assert await c.cleanup_stale() == 1
    assert not path.exists() and unknown.read_bytes() == b"KEEP"


@pytest.mark.asyncio
async def test_ttl_cannot_prove_changed_owned_package_retains_it(tmp_path):
    now = [datetime.now(timezone.utc)]
    c = controller(tmp_path, clock=lambda: now[0])
    _, _, path = await ready(c)
    path.write_bytes(b"CHANGED")
    now[0] += timedelta(seconds=6)
    with pytest.raises(capture.CaptureChannelError, match="bundle_invalid"):
        await c.cleanup_stale()
    assert path.read_bytes() == b"CHANGED"


def env(tmp_path):
    return {"OMBRE_BACKUP_AUTO_ENABLED": "true", "RENDER_GIT_COMMIT": COMMIT,
        "OMBRE_BACKUP_AUTO_WORKSPACE_ROOT": str(tmp_path / "auto-workspace"),
        "OMBRE_BACKUP_AUTO_FREEZE_TIMEOUT_SECONDS": "2", "OMBRE_BACKUP_AUTO_MAX_FREEZE_SECONDS": "30",
        "OMBRE_BACKUP_AUTO_MAX_SOURCE_BYTES": "1048576", "OMBRE_BACKUP_AUTO_MAX_BUNDLE_BYTES": "1048576",
        "OMBRE_BACKUP_AUTO_MINIMUM_FREE_BYTES": "1", "OMBRE_BACKUP_AUTO_READY_TTL_SECONDS": "5"}


def test_registration_authenticated_metadata_dynamic_sha(tmp_path, monkeypatch):
    server = _server(tmp_path)
    assert auto.register_backup_auto_if_enabled(server, "stdio", environ={}) is None
    config = env(tmp_path)
    async def verified(self, request):
        return claims()
    monkeypatch.setattr(GitHubActionsBackupV2OidcVerifier, "verify_request", verified)
    c = auto.register_backup_auto_if_enabled(server, "streamable-http", environ=config)
    assert auto.register_backup_auto_if_enabled(server, "streamable-http", environ=config) is c
    route = next(r for r in server.mcp._custom_starlette_routes if r.path.endswith("/metadata"))
    from starlette.routing import Route
    with TestClient(Starlette(routes=[Route(route.path, route.endpoint)])) as client:
        result = client.get(auto.PREFIX + "/metadata")
        assert result.json()["runtime_commit"] == COMMIT and result.headers["cache-control"] == "no-store"
        config["RENDER_GIT_COMMIT"] = "2" * 40
        assert client.get(auto.PREFIX + "/metadata").status_code == 400
    assert len(server.mcp._custom_starlette_routes) == 5
    assert "OMBRE_BACKUP_V2_ARMED" not in config


@pytest.mark.asyncio
async def test_route_disconnect_wait_does_not_cancel_plain_capture(tmp_path, monkeypatch):
    c = controller(tmp_path)
    started = threading.Event()
    release = threading.Event()
    original = capture.capture_external_source
    def waiting(*args, **kwargs):
        started.set()
        assert release.wait(5)
        return original(*args, **kwargs)
    monkeypatch.setattr(capture, "capture_external_source", waiting)
    request_id = str(uuid.uuid4())
    await c.create_capture(request_id=request_id, expected_runtime_commit=COMMIT,
                           expected_recipient_fingerprint="none", claims=claims())
    assert await asyncio.to_thread(started.wait, 5)
    waiter = asyncio.create_task(c.wait_for_terminal(request_id, claims()))
    await asyncio.sleep(0)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    release.set()
    assert (await c.wait_for_terminal(request_id, claims()))["state"] == "ready"
    assert c.coordinator.status().state == "open" and c._active_request_id is None


def test_plain_http_contract_and_run_attempt_ownership(tmp_path):
    c = controller(tmp_path)
    identity = [claims()]
    async def verified(request):
        return identity[0]
    routes = capture.build_backup_v2_routes(c, verified, prefix=auto.PREFIX, plaintext=True)
    request_id = str(uuid.uuid4())
    with TestClient(Starlette(routes=routes)) as client:
        body = {"request_id": request_id, "expected_runtime_commit": COMMIT}
        assert client.post(auto.PREFIX + "/captures", json={**body, "source_root": "/any"}).status_code == 400
        assert client.post(auto.PREFIX + "/captures", json=body).status_code == 202
        result = client.portal.call(c.wait_for_terminal, request_id, identity[0])
        assert result["state"] == "ready" and "encrypted_size" not in result
        path = auto.PREFIX + "/captures/" + request_id
        identity[0] = {**claims(), "run_attempt": "2"}
        assert client.get(path).status_code == 400
        assert client.get(path + "/bundle").status_code == 400
        assert client.post(path + "/ack").status_code == 400
        identity[0] = claims()
        response = client.get(path + "/bundle")
        assert response.status_code == 200
        assert response.headers["content-disposition"].endswith('.obplain.tar"')
        assert hashlib.sha256(response.content).hexdigest() == result["bundle_sha256"]
        assert len(response.content) == result["bundle_size"]
        assert client.post(path + "/ack").json()["state"] == "consumed"


@pytest.mark.asyncio
async def test_auto_periodic_cleanup_retains_unproven_materials(tmp_path, caplog):
    from dataclasses import replace
    from types import SimpleNamespace
    c = controller(tmp_path)
    c.limits = replace(c.limits, ready_ttl_seconds=0.02)
    unknown = c.workspace.temp_root / "foreign-material"
    unknown.write_bytes(b"KEEP")
    app = Starlette()
    auto.install_backup_auto_lifespan(app, SimpleNamespace(_backup_auto_controller=c))
    async with app.router.lifespan_context(app):
        _, _, path = await ready(c)
        await asyncio.sleep(0.1)
        assert not path.exists()
    assert unknown.read_bytes() == b"KEEP"
    assert "unproven materials retained: 1" in caplog.text
    assert not any(task.get_name() == "backup-auto-ttl" for task in asyncio.all_tasks())


@pytest.mark.parametrize("failure", ["deadline", "cancel", "format"])
@pytest.mark.asyncio
async def test_plain_failure_worker_exits_and_releases_freeze(tmp_path, monkeypatch, failure):
    from dataclasses import replace
    import time
    c = controller(tmp_path)
    started = threading.Event()
    if failure == "format":
        (c.source_root / "permanent" / "test" / "sealed.md").write_text("---\nsealed: invalid\n---\nSYNTHETIC")
    else:
        def slow(*args, **kwargs):
            started.set()
            signal = kwargs["abort_signal"]
            while True:
                time.sleep(0.005)
                signal.raise_if_aborted()
        monkeypatch.setattr(bundles, "_build_archive", slow)
        if failure == "deadline":
            c.limits = replace(c.limits, max_freeze_seconds=0.05)
    request_id = str(uuid.uuid4())
    await c.create_capture(request_id=request_id, expected_runtime_commit=COMMIT,
                           expected_recipient_fingerprint="none", claims=claims())
    if failure == "cancel":
        assert await asyncio.to_thread(started.wait, 5)
        c._tasks[request_id].cancel()
    result = await c.wait_for_terminal(request_id, claims())
    assert result["state"] == "failed"
    assert result["failure_code"] == {"deadline": "freeze_lease_expired", "cancel": "capture_cancelled",
                                      "format": "manifest_invalid"}[failure]
    assert c.coordinator.status().state == "open" and c._active_request_id is None
    assert c._active_workers == 0
    assert not list(c.workspace.bundles_root.iterdir())
    assert not list(c.workspace.temp_root.iterdir())


@pytest.fixture
def registered_auto(tmp_path, monkeypatch):
    server = _server(tmp_path)
    seed(Path(server.config["buckets_dir"]))
    async def verified(self, request):
        return claims()
    monkeypatch.setattr(GitHubActionsBackupV2OidcVerifier, "verify_request", verified)
    c = auto.register_backup_auto_if_enabled(server, "streamable-http", environ=env(tmp_path))
    return server, c


def auto_app(server):
    from starlette.routing import Route
    return Starlette(routes=[Route(r.path, r.endpoint, methods=list(r.methods))
                             for r in server.mcp._custom_starlette_routes])


def request_action(client, action, request_id):
    path = auto.PREFIX + "/captures/" + request_id
    if action == "metadata":
        return client.get(auto.PREFIX + "/metadata")
    if action == "create":
        return client.post(auto.PREFIX + "/captures",
                           json={"request_id": str(uuid.uuid4()), "expected_runtime_commit": COMMIT})
    if action == "ack":
        return client.post(path + "/ack")
    return client.get(path + ("/bundle" if action == "download" else ""))


@pytest.mark.parametrize("action", ["metadata", "create", "status", "download", "ack"])
@pytest.mark.parametrize("drift", ["replacement", "coordinator", "unpublished"])
def test_auto_all_routes_reject_runtime_drift_without_initializing(registered_auto, monkeypatch, action, drift):
    server, c = registered_auto
    with TestClient(auto_app(server)) as client:
        request_id, _, path = client.portal.call(ready, c)
        if drift == "replacement":
            server._backup_auto_controller = object()
        elif drift == "coordinator":
            c.coordinator = MaintenanceWriteCoordinator()
        else:
            server._runtime_components = None
        monkeypatch.setattr(server, "_get_runtime_components",
                            lambda: pytest.fail("query initialized runtime"), raising=False)
        result = request_action(client, action, request_id)
        assert result.status_code == 400
        assert result.json() == {"status": "capture_runtime_unavailable"}
        assert path.exists() and c._jobs[request_id].state == "ready"
        assert not c._active_deliveries


@pytest.mark.parametrize("action", ["metadata", "create", "status", "download", "ack"])
def test_auto_all_routes_recheck_after_oidc_await(registered_auto, monkeypatch, action):
    server, c = registered_auto
    with TestClient(auto_app(server)) as client:
        request_id, _, path = client.portal.call(ready, c)
        async def drifting(self, request):
            await asyncio.sleep(0)
            server._backup_auto_controller = object()
            return claims()
        monkeypatch.setattr(GitHubActionsBackupV2OidcVerifier, "verify_request", drifting)
        result = request_action(client, action, request_id)
        assert result.json() == {"status": "capture_runtime_unavailable"}
        assert path.exists() and c._jobs[request_id].state == "ready"


@pytest.mark.parametrize("action", ["create", "download", "ack"])
@pytest.mark.asyncio
async def test_auto_rechecks_after_job_lock_wait(registered_auto, action):
    server, c = registered_auto
    request_id, _, path = await ready(c)
    async def operation():
        if action == "create":
            return await c.create_capture(request_id=str(uuid.uuid4()), expected_runtime_commit=COMMIT,
                                          expected_recipient_fingerprint="none", claims=claims())
        if action == "ack":
            return await c.acknowledge(request_id, claims())
        async with c.delivery(request_id, claims()):
            pytest.fail("drifted download admitted")
    async with c._job_lock:
        pending = asyncio.create_task(operation())
        await asyncio.sleep(0)
        server._backup_auto_controller = object()
    with pytest.raises(capture.CaptureChannelError, match="capture_runtime_unavailable"):
        await pending
    assert path.exists() and not c._active_deliveries


@pytest.mark.parametrize("phase", ["drain", "worker"])
@pytest.mark.asyncio
async def test_auto_capture_drift_releases_existing_lease(registered_auto, monkeypatch, phase):
    server, c = registered_auto
    original_coordinator = c.coordinator
    if phase == "drain":
        original_wait = original_coordinator._wait_for_writers
        def drift_after_drain(timeout):
            result = original_wait(timeout)
            server._backup_auto_controller = object()
            return result
        monkeypatch.setattr(original_coordinator, "_wait_for_writers", drift_after_drain)
    else:
        original_capture = capture.capture_external_source
        def drift_after_capture(*args, **kwargs):
            result = original_capture(*args, **kwargs)
            c.coordinator = MaintenanceWriteCoordinator()
            return result
        monkeypatch.setattr(capture, "capture_external_source", drift_after_capture)
    request_id = str(uuid.uuid4())
    await c.create_capture(request_id=request_id, expected_runtime_commit=COMMIT,
                           expected_recipient_fingerprint="none", claims=claims())
    await asyncio.shield(c._tasks[request_id])
    job = c._jobs[request_id]
    assert job.state == "failed" and job.failure_code == "capture_runtime_unavailable"
    assert original_coordinator.status().state == "open"
    assert c._active_request_id is None and c._active_workers == 0
    assert not list(c.workspace.bundles_root.iterdir())
    assert not list(c.workspace.temp_root.iterdir())


@pytest.mark.parametrize("operation", ["ttl", "ack"])
@pytest.mark.parametrize("damage", ["content", "workspace", "bundle_id", "outside", "symlink"])
@pytest.mark.asyncio
async def test_auto_delete_preserves_unproven_material(tmp_path, operation, damage):
    now = [datetime.now(timezone.utc)]
    c = controller(tmp_path, clock=lambda: now[0])
    request_id, _, path = await ready(c)
    retained = path
    if damage == "content":
        path.write_bytes(b"SYNTHETIC REPLACEMENT")
    elif damage == "workspace":
        for name in (bundles.WORKSPACE_MANIFEST, bundles.WORKSPACE_MARKER):
            marker = c.workspace.root / name
            record = json.loads(marker.read_text())
            record["workspace_id"] = "b" * 32
            marker.write_text(json.dumps(record))
    elif damage == "bundle_id":
        c._jobs[request_id].bundle_id = "b" * 32
    elif damage == "outside":
        retained = c.workspace.root / "foreign.obplain.tar"
        retained.write_bytes(b"KEEP")
        c._jobs[request_id].bundle_name = "../foreign.obplain.tar"
    else:
        retained = c.workspace.root / "foreign.obplain.tar"
        path.rename(retained)
        path.symlink_to(retained)
    before = retained.read_bytes()
    now[0] += timedelta(seconds=6)
    with pytest.raises(capture.CaptureChannelError, match="bundle_invalid"):
        if operation == "ttl":
            await c.cleanup_stale()
        else:
            await c.acknowledge(request_id, claims())
    assert retained.read_bytes() == before
    assert c._jobs[request_id].state == "ready"


@pytest.mark.parametrize("operation", ["ttl", "ack", "download"])
@pytest.mark.asyncio
async def test_auto_rechecks_after_bundle_hash_await(registered_auto, monkeypatch, operation):
    from dataclasses import replace
    server, c = registered_auto
    request_id, _, path = await ready(c)
    c.limits = replace(c.limits, ready_ttl_seconds=0.001)
    await asyncio.sleep(0.01)
    name = "_hash_handle" if operation == "download" else "_hash_file"
    original = getattr(capture, name)
    def drift(*args):
        result = original(*args)
        server._backup_auto_controller = object()
        return result
    monkeypatch.setattr(capture, name, drift)
    with pytest.raises(capture.CaptureChannelError, match="capture_runtime_unavailable"):
        if operation == "ttl":
            await c.cleanup_stale()
        elif operation == "ack":
            await c.acknowledge(request_id, claims())
        else:
            async with c.delivery(request_id, claims()):
                pytest.fail("drifted delivery admitted")
    assert path.exists() and c._jobs[request_id].state == "ready"
    assert not c._active_deliveries


@pytest.mark.asyncio
async def test_auto_ack_lost_response_is_confirmed_by_original_job(tmp_path):
    c = controller(tmp_path)
    request_id, _, path = await ready(c)
    first = await c.acknowledge(request_id, claims())
    assert first["state"] == "consumed" and not path.exists()
    assert c.get_job(request_id, claims()) == first
    assert await c.acknowledge(request_id, claims()) == first
    with pytest.raises(capture.CaptureChannelError, match="capture_not_found"):
        await c.acknowledge(request_id, {**claims(), "run_attempt": "2"})


@pytest.mark.parametrize("operation", ["ttl", "ack"])
@pytest.mark.parametrize("damage", ["workspace", "same_bytes_new_file"])
@pytest.mark.asyncio
async def test_auto_delete_revalidates_ownership_after_hash(tmp_path, monkeypatch, operation, damage):
    now = [datetime.now(timezone.utc)]
    c = controller(tmp_path, clock=lambda: now[0])
    request_id, _, path = await ready(c)
    original = capture._hash_file
    def changed(target):
        result = original(target)
        if damage == "workspace":
            for name in (bundles.WORKSPACE_MANIFEST, bundles.WORKSPACE_MARKER):
                marker = c.workspace.root / name
                record = json.loads(marker.read_text())
                record["nonce"] = "b" * 64
                marker.write_text(json.dumps(record))
        else:
            replacement = path.with_name("foreign.part")
            replacement.write_bytes(path.read_bytes())
            replacement.replace(path)
        return result
    monkeypatch.setattr(capture, "_hash_file", changed)
    now[0] += timedelta(seconds=6)
    with pytest.raises(capture.CaptureChannelError, match="bundle_invalid"):
        if operation == "ttl":
            await c.cleanup_stale()
        else:
            await c.acknowledge(request_id, claims())
    assert path.exists() and c._jobs[request_id].state == "ready"


@pytest.mark.asyncio
async def test_auto_stream_runtime_drift_releases_delivery(registered_auto):
    server, c = registered_auto
    request_id, _, path = await ready(c)
    route = next(r for r in server.mcp._custom_starlette_routes if r.path.endswith("/bundle"))
    scope = {"type": "http", "method": "GET", "path_params": {"request_id": request_id},
             "headers": [], "query_string": b"", "asgi": {"spec_version": "2.4"}}
    response = await route.endpoint(Request(scope))
    events = []
    async def send(message):
        events.append(message)
        if message["type"] == "http.response.body":
            server._backup_auto_controller = object()
    async def receive():
        return {"type": "http.request", "body": b""}
    with pytest.raises(capture.CaptureChannelError, match="capture_runtime_unavailable"):
        await response(scope, receive, send)
    assert not any(event.get("more_body") is False for event in events if event["type"] == "http.response.body")
    assert path.exists() and not c._active_deliveries


@pytest.mark.parametrize("event", ["schedule", "workflow_dispatch"])
@pytest.mark.parametrize("present", [False, True])
def test_auto_oidc_job_workflow_ref_absent_or_exact_is_accepted(caplog, event, present):
    data = {**claims(), "event_name": event}
    if present:
        data["job_workflow_ref"] = auto.AUTO_WORKFLOW_REF
    assert auto.StrictBackupAutoOidcPolicy().verify(data) == {"run_id": "123", "run_attempt": "1"}
    assert not [record for record in caplog.records if record.name == "ombre_brain.backup_oidc"]


@pytest.mark.parametrize("value", [
    "", "attacker/ob-backup/.github/workflows/backup-auto.yml@refs/heads/main",
    "ALLFORTING/ob-backup/.github/workflows/other.yml@refs/heads/main",
    "ALLFORTING/ob-backup/.github/workflows/backup-auto.yml@refs/heads/other",
    "ALLFORTING/ob-backup/.github/workflows/backup-auto.yml@refs/tags/main",
    "ALLFORTING/ob-backup/.github/workflows/backup-auto.yml@" + "a" * 40,
    auto.AUTO_WORKFLOW_REF + " ", " " + auto.AUTO_WORKFLOW_REF,
    auto.AUTO_WORKFLOW_REF.lower(), None, True, False, 0, 1, 1.0, [], {},
    [auto.AUTO_WORKFLOW_REF], {"ref": auto.AUTO_WORKFLOW_REF},
])
def test_auto_oidc_job_workflow_ref_wrong_reference_or_type_is_denied(caplog, value):
    import logging
    caplog.set_level(logging.WARNING, logger="ombre_brain.backup_oidc")
    data = {**claims(), "job_workflow_ref": value}
    with pytest.raises(capture.CaptureChannelError, match="oidc_denied") as denied:
        auto.StrictBackupAutoOidcPolicy().verify(data)
    response = capture._route_error(denied.value)
    assert response.status_code == 400 and response.body == b'{"status":"oidc_denied"}'
    records = [record for record in caplog.records if record.name == "ombre_brain.backup_oidc"]
    assert len(records) == 1
    assert records[0].getMessage() == "oidc_denied stage=auto_policy exception=none fields=job_workflow_ref"
    assert records[0].exc_info is None


@pytest.mark.parametrize("field,value", [
    ("repository", "attacker/ob-backup"), ("repository_id", "1"),
    ("repository_owner", "attacker"), ("repository_owner_id", "1"),
    ("repository_visibility", "public"), ("ref", "refs/heads/other"),
    ("workflow_ref", "ALLFORTING/ob-backup/.github/workflows/other.yml@refs/heads/main"),
    ("aud", capture.V2_AUDIENCE), ("event_name", "pull_request"),
    ("run_id", "0"), ("run_attempt", "0"), ("run_attempt", 1),
    ("environment", "production"),
])
def test_matching_job_workflow_ref_never_bypasses_other_identity_checks(caplog, field, value):
    import logging
    caplog.set_level(logging.WARNING, logger="ombre_brain.backup_oidc")
    data = {**claims(), "job_workflow_ref": auto.AUTO_WORKFLOW_REF, field: value}
    with pytest.raises(capture.CaptureChannelError, match="oidc_denied") as denied:
        auto.StrictBackupAutoOidcPolicy().verify(data)
    response = capture._route_error(denied.value)
    assert response.status_code == 400 and response.body == b'{"status":"oidc_denied"}'
    records = [record for record in caplog.records if record.name == "ombre_brain.backup_oidc"]
    assert len(records) == 1 and records[0].getMessage().endswith("fields=" + field)
    assert "job_workflow_ref" not in records[0].getMessage()


@pytest.mark.parametrize("value", ["PRIVATE_SENTINEL_DO_NOT_LOG", {"PRIVATE_SENTINEL_DO_NOT_LOG": True}])
def test_denied_job_workflow_ref_values_never_enter_diagnostics(caplog, value):
    import logging
    caplog.set_level(logging.WARNING, logger="ombre_brain.backup_oidc")
    with pytest.raises(capture.CaptureChannelError, match="oidc_denied"):
        auto.StrictBackupAutoOidcPolicy().verify({**claims(), "job_workflow_ref": value})
    assert "PRIVATE_SENTINEL_DO_NOT_LOG" not in caplog.text
    assert "fields=job_workflow_ref" in caplog.text

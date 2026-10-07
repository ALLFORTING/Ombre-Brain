from __future__ import annotations

from dataclasses import dataclass
import asyncio
import importlib
import sys
from pathlib import Path
from types import SimpleNamespace

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from cryptography.hazmat.primitives import serialization

import backup_v2_runtime
from backup_v2_oidc import GitHubActionsBackupV2OidcVerifier
from maintenance_write_gate import MaintenanceWriteCoordinator, DEFAULT_WRITE_COORDINATOR
from production_backup_capture import (
    CaptureChannelError,
    V2_AUDIENCE,
    V2_REF,
    V2_REPOSITORY,
    V2_WORKFLOW,
    public_key_fingerprint,
)


COMMIT = "1" * 40


class FakeMcp:
    def __init__(self) -> None:
        self._custom_starlette_routes = []
        self.app_constructed = False

    def custom_route(self, path, methods, name=None, include_in_schema=True):
        assert self.app_constructed is False

        def decorator(endpoint):
            self._custom_starlette_routes.append(SimpleNamespace(
                path=path,
                methods=set(methods),
                name=name,
                endpoint=endpoint,
                include_in_schema=include_in_schema,
            ))
            return endpoint

        return decorator


@dataclass
class FakeServer:
    config: dict
    mcp: FakeMcp
    bucket_mgr: SimpleNamespace


def _server(tmp_path: Path) -> FakeServer:
    source = tmp_path / "buckets"
    source.mkdir()
    server = FakeServer(
        config={"buckets_dir": str(source)},
        mcp=FakeMcp(),
        bucket_mgr=SimpleNamespace(write_coordinator=DEFAULT_WRITE_COORDINATOR),
    )

    for name in ("asset_store", "embedding_engine", "asset_embedding_index", "dehydrator"):
        setattr(server, name, SimpleNamespace(write_coordinator=DEFAULT_WRITE_COORDINATOR))
    server.bucket_mgr.relation_store = SimpleNamespace(write_coordinator=DEFAULT_WRITE_COORDINATOR)
    server.remember_me_host_bundle = SimpleNamespace(core_adapter=SimpleNamespace(
        write_coordinator=DEFAULT_WRITE_COORDINATOR))
    server.asset_backend_registry = SimpleNamespace(state_store=None)
    server.decay_engine = SimpleNamespace(bucket_mgr=server.bucket_mgr)
    server.import_engine = SimpleNamespace(
        bucket_mgr=server.bucket_mgr, dehydrator=server.dehydrator,
        embedding_engine=server.embedding_engine)
    return server

def _key_env(tmp_path: Path) -> dict[str, str]:
    private_key = X25519PrivateKey.generate()
    public_key = private_key.public_key()
    public_b64 = public_key.public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )
    import base64

    return {
        "OMBRE_BACKUP_V2_ENABLED": "true",
        "OMBRE_BACKUP_V2_PUBLIC_KEY_B64": base64.b64encode(public_b64).decode("ascii"),
        "OMBRE_BACKUP_V2_RECIPIENT_FINGERPRINT": public_key_fingerprint(public_key),
        "OMBRE_BACKUP_V2_REPOSITORY_ID": "99",
        "OMBRE_BACKUP_V2_REPOSITORY_OWNER_ID": "88",
        "OMBRE_BACKUP_V2_WORKSPACE_ROOT": str(tmp_path / "workspace"),
        "OMBRE_BACKUP_V2_FREEZE_TIMEOUT_SECONDS": "2",
        "OMBRE_BACKUP_V2_MAX_FREEZE_SECONDS": "30",
        "OMBRE_BACKUP_V2_MAX_SOURCE_BYTES": "1048576",
        "OMBRE_BACKUP_V2_MAX_BUNDLE_BYTES": "1048576",
        "OMBRE_BACKUP_V2_MINIMUM_FREE_BYTES": "1",
        "OMBRE_BACKUP_V2_READY_TTL_SECONDS": "60",
        "RENDER_GIT_COMMIT": COMMIT,
    }


def _route_signatures(server: FakeServer) -> set[tuple[str, str]]:
    return {
        (method, route.path)
        for route in server.mcp._custom_starlette_routes
        for method in route.methods
    }


def test_disabled_modes_register_no_routes_and_touch_no_runtime(tmp_path, monkeypatch):
    server = _server(tmp_path)

    def forbidden(*args, **kwargs):
        raise AssertionError("enabled-only config parser was reached")

    monkeypatch.setattr(backup_v2_runtime, "_parse_enabled_config", forbidden)
    for value in (None, "", "false"):
        env = {} if value is None else {"OMBRE_BACKUP_V2_ENABLED": value}
        result = backup_v2_runtime.register_backup_v2_if_enabled(
            server, "streamable-http", environ=env
        )
        assert result.enabled is False
        assert result.registered is False
        assert server.mcp._custom_starlette_routes == []


def test_malformed_enable_and_non_streamable_transport_fail_closed(tmp_path):
    server = _server(tmp_path)
    with pytest.raises(backup_v2_runtime.BackupV2RuntimeConfigError):
        backup_v2_runtime.register_backup_v2_if_enabled(
            server, "streamable-http", environ={"OMBRE_BACKUP_V2_ENABLED": "TRUE"}
        )
    env = _key_env(tmp_path)
    with pytest.raises(backup_v2_runtime.BackupV2RuntimeConfigError):
        backup_v2_runtime.register_backup_v2_if_enabled(server, "sse", environ=env)


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("RENDER_GIT_COMMIT", "A" * 40),
        ("OMBRE_BACKUP_V2_REPOSITORY_ID", "0"),
        ("OMBRE_BACKUP_V2_FREEZE_TIMEOUT_SECONDS", " 2"),
        ("OMBRE_BACKUP_V2_FREEZE_TIMEOUT_SECONDS", "2.0"),
        ("OMBRE_BACKUP_V2_FREEZE_TIMEOUT_SECONDS", "0"),
        ("OMBRE_BACKUP_V2_MAX_BUNDLE_BYTES", str(10 * 1024 * 1024 * 1024 + 1)),
    ],
)
def test_invalid_enabled_configuration_is_rejected(tmp_path, name, value):
    server = _server(tmp_path)
    env = _key_env(tmp_path)
    env[name] = value
    with pytest.raises(backup_v2_runtime.BackupV2RuntimeConfigError):
        backup_v2_runtime.register_backup_v2_if_enabled(server, "streamable-http", environ=env)


def test_fingerprint_mismatch_and_workspace_overlap_are_rejected(tmp_path):
    server = _server(tmp_path)
    env = _key_env(tmp_path)
    env["OMBRE_BACKUP_V2_RECIPIENT_FINGERPRINT"] = "x25519-sha256:" + "0" * 64
    with pytest.raises(backup_v2_runtime.BackupV2RuntimeConfigError):
        backup_v2_runtime.register_backup_v2_if_enabled(server, "streamable-http", environ=env)

    env = _key_env(tmp_path)
    env["OMBRE_BACKUP_V2_WORKSPACE_ROOT"] = str(Path(server.config["buckets_dir"]) / "nested")
    with pytest.raises(backup_v2_runtime.BackupV2RuntimeConfigError):
        backup_v2_runtime.register_backup_v2_if_enabled(server, "streamable-http", environ=env)


@pytest.mark.parametrize("workspace_value", ["equal", "below", "above"])
def test_workspace_overlap_fails_before_preparation(tmp_path, workspace_value, monkeypatch):
    server = _server(tmp_path)
    source = Path(server.config["buckets_dir"])
    env = _key_env(tmp_path)
    env["OMBRE_BACKUP_V2_WORKSPACE_ROOT"] = {
        "equal": str(source),
        "below": str(source / "nested"),
        "above": str(source.parent),
    }[workspace_value]

    import offline_backup_bundle

    calls = []
    monkeypatch.setattr(
        offline_backup_bundle,
        "prepare_backup_workspace",
        lambda path: calls.append(path),
    )
    with pytest.raises(backup_v2_runtime.BackupV2RuntimeConfigError):
        backup_v2_runtime.register_backup_v2_if_enabled(
            server,
            "streamable-http",
            environ=env,
        )
    assert calls == []
    assert not (source / "nested").exists()


def test_windows_case_only_workspace_aliases_are_rejected_deterministically():
    source = Path("/tmp/Backup/Buckets")
    assert backup_v2_runtime._paths_overlap(
        Path("/tmp/backup/buckets"),
        source,
        case_sensitive=False,
    )
    assert backup_v2_runtime._paths_overlap(
        Path("/tmp/backup/buckets/nested"),
        source,
        case_sensitive=False,
    )
    assert backup_v2_runtime._paths_overlap(
        source,
        Path("/tmp/backup"),
        case_sensitive=False,
    )


def test_valid_configuration_registers_exactly_five_routes_once(tmp_path):
    server = _server(tmp_path)
    env = _key_env(tmp_path)
    result = backup_v2_runtime.register_backup_v2_if_enabled(
        server, "streamable-http", environ=env
    )
    assert result.enabled is True
    assert result.registered is True
    assert result.route_count == 5
    assert _route_signatures(server) == backup_v2_runtime.V2_ROUTE_SIGNATURES
    backup_v2_runtime.register_backup_v2_if_enabled(server, "streamable-http", environ=env)
    assert len(server.mcp._custom_starlette_routes) == 5


def test_server_import_alone_does_not_register_v2_routes(tmp_path, monkeypatch):
    monkeypatch.setenv("OMBRE_BUCKETS_DIR", str(tmp_path / "server-buckets"))
    sys.modules.pop("server", None)
    import server

    server = importlib.reload(server)
    paths = {
        route.path
        for route in getattr(server.mcp, "_custom_starlette_routes", ())
    }
    assert "/api/backup/v2/captures" not in paths
    assert "/api/backup/export" not in paths


def test_backup_entry_checks_backup_v2_gate_before_stdio_run(monkeypatch):
    import backup_entry

    calls = []

    class EntryServer(SimpleNamespace):
        pass

    fake_server = EntryServer(
        config={"transport": "stdio"},
        mcp=SimpleNamespace(run=lambda transport: calls.append(("run", transport))),
    )

    def disabled_gate(server_module, transport):
        calls.append(("gate", transport))

    monkeypatch.setattr(backup_entry, "server", fake_server)
    monkeypatch.setattr(backup_entry, "register_backup_v2_if_enabled", disabled_gate)
    backup_entry.run()
    assert calls == [("gate", "stdio"), ("run", "stdio")]

    def enabled_gate(server_module, transport):
        raise backup_v2_runtime.BackupV2RuntimeConfigError(
            "backup_v2_transport_unsupported"
        )

    calls.clear()
    monkeypatch.setattr(backup_entry, "register_backup_v2_if_enabled", enabled_gate)
    with pytest.raises(backup_v2_runtime.BackupV2RuntimeConfigError):
        backup_entry.run()
    assert calls == []


class Request:
    def __init__(self, headers, query=b"", body=None, raw_body=b""):
        self.scope = {"headers": headers, "query_string": query}
        if body is not None:
            self._json = body
        self._raw_body = raw_body

    async def body(self):
        return self._raw_body


def _claims():
    return {
        "iss": "https://token.actions.githubusercontent.com",
        "aud": V2_AUDIENCE,
        "repository": V2_REPOSITORY,
        "repository_owner": "ALLFORTING",
        "repository_id": "99",
        "repository_owner_id": "88",
        "ref": V2_REF,
        "event_name": "workflow_dispatch",
        "workflow_ref": f"{V2_REPOSITORY}/{V2_WORKFLOW}@{V2_REF}",
        "run_id": "123",
        "run_attempt": "1",
        "iat": 2,
        "nbf": 2,
        "exp": 9999999999,
    }


def test_oidc_header_boundaries_and_policy_claim_flow():
    async def exercise():
        verifier = GitHubActionsBackupV2OidcVerifier(
            jwk_client=object(),
            decoder=lambda token, client: _claims(),
        )
        claims = await verifier.verify_request(Request([(b"authorization", b"Bearer abc.def.sig")]))
        assert claims["aud"] == V2_AUDIENCE
        for request in (
            Request([]),
            Request([(b"authorization", b"Bearer a"), (b"authorization", b"Bearer b")]),
            Request([(b"authorization", b"Basic abc")]),
            Request([(b"authorization", b"Bearer abc")], query=b"token=abc"),
            Request([(b"authorization", b"Bearer abc")], body={"token": "abc"}),
            Request(
                [
                    (b"authorization", b"Bearer abc"),
                    (b"content-type", b"application/json"),
                ],
                raw_body=b'{"access_token":"abc"}',
            ),
            Request([(b"authorization", b"Bearer " + b"a" * 8193)]),
        ):
            with pytest.raises(CaptureChannelError) as error:
                await verifier.verify_request(request)
            assert error.value.code == "oidc_denied"

    asyncio.run(exercise())


def test_oidc_rs256_signature_validation_and_stable_failures():
    async def exercise():
        private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        token = jwt.encode(_claims(), private_key, algorithm="RS256", headers={"kid": "k1"})

        class Key:
            key = private_key.public_key()

        class Client:
            calls = 0

            def get_signing_key_from_jwt(self, received):
                self.calls += 1
                assert received == token
                return Key()

        client = Client()
        verifier = GitHubActionsBackupV2OidcVerifier(jwk_client=client)
        claims = await verifier.verify_request(Request([(b"authorization", f"Bearer {token}".encode())]))
        assert claims["aud"] == V2_AUDIENCE
        assert client.calls == 1

        bad_token = jwt.encode(
            _claims(),
            "synthetic-hmac-key",
            algorithm="HS256",
            headers={"kid": "k1"},
        )
        with pytest.raises(CaptureChannelError) as error:
            await verifier.verify_request(Request([(b"authorization", f"Bearer {bad_token}".encode())]))
        assert error.value.code == "oidc_denied"

    asyncio.run(exercise())

@pytest.mark.parametrize("component", [
    "asset_store", "embedding_engine", "asset_embedding_index", "dehydrator",
    "relations", "remember_me", "migration", "controller", "background",
])
def test_split_coordinator_is_rejected(tmp_path, component):
    server = _server(tmp_path)
    controller = SimpleNamespace(coordinator=DEFAULT_WRITE_COORDINATOR)
    other = MaintenanceWriteCoordinator()
    if component == "relations":
        server.bucket_mgr.relation_store.write_coordinator = other
    elif component == "remember_me":
        server.remember_me_host_bundle.core_adapter.write_coordinator = other
    elif component == "migration":
        server.asset_backend_registry.state_store = SimpleNamespace(write_coordinator=other)
    elif component == "controller":
        controller.coordinator = other
    elif component == "background":
        server.decay_engine.bucket_mgr = SimpleNamespace(write_coordinator=other)
    else:
        getattr(server, component).write_coordinator = other
    with pytest.raises(backup_v2_runtime.BackupV2RuntimeConfigError, match="coordinator_mismatch"):
        backup_v2_runtime.require_runtime_coordinator(server, controller)


def test_registered_routes_reject_coordinator_drift_before_oidc(tmp_path, monkeypatch):
    server = _server(tmp_path)
    backup_v2_runtime.register_backup_v2_if_enabled(server, "streamable-http", environ=_key_env(tmp_path))
    server.asset_store.write_coordinator = MaintenanceWriteCoordinator()

    def forbidden(*args, **kwargs):
        raise AssertionError("OIDC must not run on a split boundary")
    monkeypatch.setattr(GitHubActionsBackupV2OidcVerifier, "verify_request", forbidden)
    route = next(r for r in server.mcp._custom_starlette_routes if r.path.endswith("/captures"))
    result = asyncio.run(route.endpoint(Request([])))
    assert result.status_code != 202
    assert DEFAULT_WRITE_COORDINATOR.status().state == "open"


@pytest.mark.parametrize("value,status", [
    ("1" * 40, "valid"), ("", "missing"), ("main", "invalid"),
    ("A" * 40, "invalid"), ("1" * 40 + "\n", "invalid"),
])
def test_docker_build_injection_and_runtime_reader(tmp_path, value, status):
    import json
    import os
    import shlex
    import subprocess
    docker = (Path(__file__).parents[1] / "Dockerfile").read_text()
    line = next(line for line in docker.splitlines() if line.startswith("RUN python -c "))
    code = shlex.split(line)[3]
    output = tmp_path / ".backup-v2-build.json"
    code = code.replace("/app/.backup-v2-build.json", str(output))
    subprocess.run([sys.executable, "-c", code], check=True,
                   env={**os.environ, "ZEABUR_GIT_COMMIT_SHA": value})
    record = json.loads(output.read_text())
    assert record["status"] == status
    assert record["commit"] == (value if status == "valid" else None)
    if status == "valid":
        assert backup_v2_runtime.resolve_runtime_commit({}, metadata_path=output) == value
        assert backup_v2_runtime.resolve_runtime_commit(
            {"RENDER_GIT_COMMIT": value}, metadata_path=output) == value
        with pytest.raises(backup_v2_runtime.BackupV2RuntimeConfigError, match="conflict"):
            backup_v2_runtime.resolve_runtime_commit(
                {"RENDER_GIT_COMMIT": "2" * 40}, metadata_path=output)
    else:
        with pytest.raises(backup_v2_runtime.BackupV2RuntimeConfigError):
            backup_v2_runtime.resolve_runtime_commit({}, metadata_path=output)
        if status == "missing":
            assert backup_v2_runtime.resolve_runtime_commit(
                {"RENDER_GIT_COMMIT": COMMIT}, metadata_path=output) == COMMIT
            with pytest.raises(backup_v2_runtime.BackupV2RuntimeConfigError):
                backup_v2_runtime.resolve_runtime_commit(
                    {"RENDER_GIT_COMMIT": COMMIT, "ZEABUR_SERVICE_ID": "synthetic"},
                    metadata_path=output)


def test_runtime_does_not_accept_generic_sha_override(tmp_path):
    with pytest.raises(backup_v2_runtime.BackupV2RuntimeConfigError, match="missing"):
        backup_v2_runtime.resolve_runtime_commit(
            {"OMBRE_BACKUP_V2_RUNTIME_COMMIT": COMMIT, "ZEABUR_GIT_COMMIT_SHA": COMMIT},
            metadata_path=tmp_path / "absent")


def test_server_registration_uses_running_module(monkeypatch):
    import server
    import types
    running = types.ModuleType("__main__")
    monkeypatch.setitem(sys.modules, "__main__", running)
    calls = []
    monkeypatch.setattr(backup_v2_runtime, "register_backup_v2_if_enabled",
                        lambda module, transport: calls.append((module, transport)))
    fn = types.FunctionType(server._register_backup_v2.__code__, {"__name__": "__main__", "sys": sys})
    fn("streamable-http")
    assert calls == [(running, "streamable-http")]
    source = Path(server.__file__).read_text()
    entry = source[source.index('if __name__ == "__main__":'):]
    assert entry.index("_register_backup_v2(transport)") < entry.index("digest_thread.start()")
    assert "backup_entry" not in entry


def test_enabled_real_initialization_and_rm_share_boundary(tmp_path, monkeypatch, test_config):
    import server
    monkeypatch.setattr(server, "config", test_config)
    monkeypatch.setattr(server, "_runtime_components", None)
    monkeypatch.setenv("OMBRE_BACKUP_V2_ENABLED", "true")
    monkeypatch.setenv("OMBRE_RM_RUNTIME_ENABLED", "true")
    monkeypatch.setenv("OMBRE_RM_DATA_ROOT", str(Path(test_config["buckets_dir"]) / "remember-me"))
    stages = []
    original = server.ensure_bucket_storage
    def storage(config):
        stages.append(DEFAULT_WRITE_COORDINATOR.status().active_writers)
        original(config)
    monkeypatch.setattr(server, "ensure_bucket_storage", storage)
    bootstrap = server._bootstrap_remember_me_host
    def rm(*args):
        stages.append(DEFAULT_WRITE_COORDINATOR.status().active_writers)
        return bootstrap(*args)
    monkeypatch.setattr(server, "_bootstrap_remember_me_host", rm)
    try:
        components = server._get_runtime_components()
    except RuntimeError as exc:
        # Keep the genuine initialization failure visible; verify its precise boundary.
        assert server._runtime_components is None
        assert DEFAULT_WRITE_COORDINATOR.status().state == "open"
        assert DEFAULT_WRITE_COORDINATOR.status().active_writers == 0
        from importlib.metadata import version
        from remember_me_adapter import validate_remember_me_contract, RememberMeAdapterError
        from remember_me_dependency import DEPENDENCY
        exc.add_note(f"installed remember-me={version('remember-me')}; repository pin={DEPENDENCY.version}")
        try:
            validate_remember_me_contract()
        except RememberMeAdapterError as contract_error:
            exc.add_note(str(contract_error))
        exc.add_note("runtime components unpublished; coordinator open; active_writers=0")
        raise
    assert stages == [1, 1]
    assert components["remember_me_host_bundle"] is not None
    assert backup_v2_runtime.require_runtime_coordinator(server) is DEFAULT_WRITE_COORDINATOR
    assert DEFAULT_WRITE_COORDINATOR.status().active_writers == 0


@pytest.mark.asyncio
async def test_initialization_waits_inside_boundary_before_capture(tmp_path, monkeypatch, test_config):
    import server
    import threading
    monkeypatch.setattr(server, "config", test_config)
    monkeypatch.setattr(server, "_runtime_components", None)
    monkeypatch.setenv("OMBRE_BACKUP_V2_ENABLED", "true")
    monkeypatch.setenv("OMBRE_RM_RUNTIME_ENABLED", "false")
    started, release = threading.Event(), threading.Event()
    original = server.ensure_bucket_storage
    def storage(config):
        started.set()
        assert release.wait(3)
        original(config)
    monkeypatch.setattr(server, "ensure_bucket_storage", storage)
    init = asyncio.create_task(asyncio.to_thread(server._get_runtime_components))
    assert await asyncio.to_thread(started.wait, 2)
    assert DEFAULT_WRITE_COORDINATOR.status().active_writers == 1
    frozen = asyncio.Event()
    async def capture_boundary():
        async with DEFAULT_WRITE_COORDINATOR.freeze(
            reason="test", drain_timeout_seconds=2, max_freeze_seconds=3):
            frozen.set()
            assert server._runtime_components is not None
    freeze = asyncio.create_task(capture_boundary())
    try:
        await asyncio.sleep(0.03)
        assert not frozen.is_set()
        release.set()
        await init
        await freeze
        assert frozen.is_set()
    finally:
        release.set()
        await asyncio.gather(init, freeze, return_exceptions=True)
    assert DEFAULT_WRITE_COORDINATOR.status().state == "open"


def test_failed_initialization_does_not_publish_components(monkeypatch):
    import server
    monkeypatch.setattr(server, "_runtime_components", None)
    monkeypatch.setenv("OMBRE_BACKUP_V2_ENABLED", "true")
    def fail(config):
        assert DEFAULT_WRITE_COORDINATOR.status().active_writers == 1
        raise RuntimeError("synthetic")
    monkeypatch.setattr(server, "ensure_bucket_storage", fail)
    with pytest.raises(RuntimeError, match="synthetic"):
        server._get_runtime_components()
    assert server._runtime_components is None
    assert DEFAULT_WRITE_COORDINATOR.status().active_writers == 0


def test_disabled_initialization_scope_retains_lazy_behavior(monkeypatch):
    import server
    monkeypatch.setenv("OMBRE_BACKUP_V2_ENABLED", "false")
    with server._backup_v2_initialization_scope():
        assert DEFAULT_WRITE_COORDINATOR.status().active_writers == 0

def test_build_and_zeabur_runtime_source_conflict(tmp_path):
    import json
    metadata = tmp_path / "build.json"
    metadata.write_text(json.dumps({"source": "zeabur-build", "status": "valid", "commit": COMMIT}))
    for env in ({"ZEABUR_GIT_COMMIT_SHA": "2" * 40},
                {"ZEABUR_GIT_COMMIT_SHA": "invalid"}):
        with pytest.raises(backup_v2_runtime.BackupV2RuntimeConfigError):
            backup_v2_runtime.resolve_runtime_commit(env, metadata_path=metadata)
    assert backup_v2_runtime.resolve_runtime_commit(
        {"ZEABUR_GIT_COMMIT_SHA": COMMIT}, metadata_path=metadata) == COMMIT


def test_disabled_does_not_read_invalid_metadata_or_initialize(tmp_path, monkeypatch):
    server = _server(tmp_path)
    metadata = tmp_path / "invalid.json"
    metadata.write_text("invalid")
    monkeypatch.setattr(backup_v2_runtime, "BUILD_METADATA_PATH", metadata)
    def forbidden():
        raise AssertionError("disabled must remain lazy")
    server._get_runtime_components = forbidden
    result = backup_v2_runtime.register_backup_v2_if_enabled(
        server, "streamable-http", environ={})
    assert result.registered is False


def test_controller_rechecks_drift_at_capture_preflight(tmp_path):
    server = _server(tmp_path)
    env = _key_env(tmp_path)
    backup_v2_runtime.register_backup_v2_if_enabled(server, "streamable-http", environ=env)
    server.import_engine.embedding_engine = SimpleNamespace(write_coordinator=MaintenanceWriteCoordinator())
    with pytest.raises(backup_v2_runtime.BackupV2RuntimeConfigError, match="coordinator_mismatch"):
        server._backup_v2_controller._preflight()
    assert DEFAULT_WRITE_COORDINATOR.status().state == "open"


def test_registration_does_not_silently_replace_existing_controller(tmp_path):
    server = _server(tmp_path)
    env = _key_env(tmp_path)
    backup_v2_runtime.register_backup_v2_if_enabled(server, "streamable-http", environ=env)
    controller = server._backup_v2_controller
    backup_v2_runtime.register_backup_v2_if_enabled(server, "streamable-http", environ=env)
    assert server._backup_v2_controller is controller
    env["RENDER_GIT_COMMIT"] = "2" * 40
    with pytest.raises(backup_v2_runtime.BackupV2RuntimeConfigError, match="route_conflict"):
        backup_v2_runtime.register_backup_v2_if_enabled(server, "streamable-http", environ=env)


@pytest.mark.asyncio
async def test_real_rm_and_background_writes_are_blocked_then_thaw(monkeypatch, test_config):
    import server
    import io
    from PIL import Image
    from maintenance_write_gate import MaintenanceWriteError
    monkeypatch.setattr(server, "config", test_config)
    monkeypatch.setattr(server, "_runtime_components", None)
    monkeypatch.setenv("OMBRE_BACKUP_V2_ENABLED", "true")
    monkeypatch.setenv("OMBRE_RM_RUNTIME_ENABLED", "true")
    monkeypatch.setenv("OMBRE_RM_DATA_ROOT", str(Path(test_config["buckets_dir"]) / "remember-me"))
    components = server._get_runtime_components()
    core = components["remember_me_host_bundle"].core_adapter
    image = io.BytesIO()
    Image.new("RGB", (2, 2), "blue").save(image, format="PNG")
    data = image.getvalue()
    manager = components["decay_engine"].bucket_mgr
    async with DEFAULT_WRITE_COORDINATOR.freeze(
        reason="test", drain_timeout_seconds=2, max_freeze_seconds=3):
        with pytest.raises(MaintenanceWriteError):
            await asyncio.to_thread(core.ingest_image, data, len(data), "synthetic.png", "image/png")
        with pytest.raises(MaintenanceWriteError):
            await asyncio.create_task(manager.create("synthetic background memory"))
    result = await asyncio.to_thread(core.ingest_image, data, len(data), "synthetic.png", "image/png")
    assert result["asset_id"]
    assert await manager.create("synthetic background memory")
    assert DEFAULT_WRITE_COORDINATOR.status().active_writers == 0


@pytest.mark.asyncio
async def test_operator_rechecks_registered_identity_after_waiting_for_job_lock(tmp_path):
    from starlette.requests import Request as StarletteRequest
    server = _server(tmp_path)
    env = _key_env(tmp_path)
    server._require_backup_v2_status_auth = lambda request: None
    backup_v2_runtime.register_backup_v2_if_enabled(server, "streamable-http", environ=env)
    controller = server._backup_v2_controller
    route = next(route for route in server.mcp._custom_starlette_routes if "operator-status" in route.path)
    request = StarletteRequest({"type": "http", "method": "GET", "headers": [],
                                "path_params": {"request_id": "12345678-1234-1234-1234-123456789abc"},
                                "query_string": b"original_run_id=123&original_run_attempt=1"})
    await controller._job_lock.acquire()
    pending = asyncio.create_task(route.endpoint(request))
    try:
        await asyncio.sleep(0)
        assert not pending.done()
        server._backup_v2_controller = object()
    finally:
        controller._job_lock.release()
    response = await asyncio.wait_for(pending, 3)
    assert response.status_code == 503
    assert response.body == b'{"status":"status_unavailable"}'

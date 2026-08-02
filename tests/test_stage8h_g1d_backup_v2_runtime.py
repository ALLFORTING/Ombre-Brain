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
from maintenance_write_gate import MaintenanceWriteCoordinator
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
    return FakeServer(
        config={"buckets_dir": str(source)},
        mcp=FakeMcp(),
        bucket_mgr=SimpleNamespace(write_coordinator=MaintenanceWriteCoordinator()),
    )


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


def test_valid_configuration_registers_exactly_four_routes_once(tmp_path):
    server = _server(tmp_path)
    env = _key_env(tmp_path)
    result = backup_v2_runtime.register_backup_v2_if_enabled(
        server, "streamable-http", environ=env
    )
    assert result.enabled is True
    assert result.registered is True
    assert result.route_count == 4
    assert _route_signatures(server) == backup_v2_runtime.V2_ROUTE_SIGNATURES
    backup_v2_runtime.register_backup_v2_if_enabled(server, "streamable-http", environ=env)
    assert len(server.mcp._custom_starlette_routes) == 4


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

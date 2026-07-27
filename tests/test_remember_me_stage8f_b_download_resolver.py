import concurrent.futures
import hashlib
import importlib
import io
import json
import logging
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from remember_me_core_adapter import (
    RememberMeCoreAdapter,
    RememberMeCoreAdapterError,
)
from remember_me_download_links import (
    RememberMeDownloadLinkError,
    RememberMeObDownloadLinkCollaborator,
)


ROOT = Path(__file__).resolve().parent.parent
ASSET_ID = "a" * 32
TOKEN_A = "A" * 43
TOKEN_B = "B" * 43


def _png_bytes(color="red", size=(8, 6)):
    image = Image.new("RGB", size, color)
    output = io.BytesIO()
    image.save(output, format="PNG", optimize=True)
    image.close()
    return output.getvalue()


def _load_server(tmp_path, monkeypatch, *, rm_enabled=False):
    monkeypatch.setenv("OMBRE_BUCKETS_DIR", str(tmp_path / "buckets"))
    monkeypatch.delenv("OMBRE_API_KEY", raising=False)
    if rm_enabled:
        monkeypatch.setenv("OMBRE_RM_RUNTIME_ENABLED", "true")
        monkeypatch.setenv(
            "OMBRE_RM_DATA_ROOT",
            str(tmp_path / "remember-me-runtime"),
        )
    else:
        monkeypatch.delenv("OMBRE_RM_RUNTIME_ENABLED", raising=False)
        monkeypatch.delenv("OMBRE_RM_DATA_ROOT", raising=False)
    sys.modules.pop("server", None)
    return importlib.import_module("server")


def _client(server):
    app = Starlette(
        routes=[
            Route(
                "/rm/asset-download/{token}",
                server.rm_asset_download_route,
                methods=["GET", "HEAD"],
            ),
        ]
    )
    return TestClient(app)


def _persist_legacy(server, data=b"legacy body", filename="legacy.png"):
    source = server.asset_store.create_temp_path()
    source.write_bytes(data)
    return server.asset_store.persist_upload(
        source,
        hashlib.sha256(data).hexdigest(),
        len(data),
        filename,
        "application/octet-stream",
    )


def test_legacy_ticket_marks_source_without_public_shape_change(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    asset = _persist_legacy(server)

    result = json.loads(server._rm_create_asset_download_link(asset["asset_id"]))
    token = result["download_path"].rsplit("/", 1)[-1]

    assert set(server._rm_asset_download_tokens[token]) == {
        "asset_id",
        "expires_at",
        "get_count",
    }
    assert server._rm_asset_download_sources[token] == "legacy"
    assert "source" not in server._rm_asset_download_tokens[token]
    assert "source" not in result
    assert "backend" not in result


def test_rm_collaborator_marks_source_without_public_shape_change():
    token_store = {}
    source_store = {}
    collaborator = RememberMeObDownloadLinkCollaborator(
        token_store=token_store,
        ticket_source_store=source_store,
        clock=lambda: 1000.0,
        token_factory=lambda: TOKEN_A,
    )

    result = collaborator.create_download_link(
        {
            "asset_id": ASSET_ID,
            "mime_type": "image/png",
            "stored_bytes": 10,
            "stored_sha256": "stored",
            "filename": "sample.png",
        }
    )

    assert token_store == {
        TOKEN_A: {
            "asset_id": ASSET_ID,
            "expires_at": 1300.0,
            "get_count": 0,
        }
    }
    assert source_store == {TOKEN_A: "remember_me"}
    assert "source" not in token_store[TOKEN_A]
    assert "source" not in result
    assert "backend" not in result


def test_collaborator_cleanup_and_source_store_validation():
    with pytest.raises(RememberMeDownloadLinkError) as captured:
        RememberMeObDownloadLinkCollaborator(ticket_source_store=[])
    assert str(captured.value) == "download_unavailable"

    token_store = {
        TOKEN_A: {"asset_id": ASSET_ID, "expires_at": 1.0, "get_count": 0},
        TOKEN_B: {"asset_id": ASSET_ID, "expires_at": 3.0, "get_count": 0},
    }
    source_store = {TOKEN_A: "remember_me", TOKEN_B: "remember_me"}
    collaborator = RememberMeObDownloadLinkCollaborator(
        token_store=token_store,
        ticket_source_store=source_store,
    )
    collaborator._cleanup_expired(2.0)

    assert TOKEN_A not in token_store
    assert TOKEN_A not in source_store
    assert TOKEN_B in token_store
    assert TOKEN_B in source_store


class FailingSetDict(dict):
    def __setitem__(self, key, value):
        raise RuntimeError("private path detail")


def test_collaborator_write_failures_roll_back_ticket_and_source():
    token_store = {}
    source_store = FailingSetDict()
    collaborator = RememberMeObDownloadLinkCollaborator(
        token_store=token_store,
        ticket_source_store=source_store,
        token_factory=lambda: TOKEN_A,
    )

    with pytest.raises(RememberMeDownloadLinkError) as captured:
        collaborator.create_download_link(
            {
                "asset_id": ASSET_ID,
                "mime_type": "image/png",
                "stored_bytes": 10,
                "stored_sha256": "stored",
                "filename": "sample.png",
            }
        )

    assert str(captured.value) == "download_unavailable"
    assert token_store == {}
    assert dict(source_store) == {}

    token_store = FailingSetDict()
    source_store = {}
    collaborator = RememberMeObDownloadLinkCollaborator(
        token_store=token_store,
        ticket_source_store=source_store,
        token_factory=lambda: TOKEN_A,
    )
    with pytest.raises(RememberMeDownloadLinkError):
        collaborator.create_download_link(
            {
                "asset_id": ASSET_ID,
                "mime_type": "image/png",
                "stored_bytes": 10,
                "stored_sha256": "stored",
                "filename": "sample.png",
            }
        )
    assert dict(source_store) == {}


class DownloadService:
    def __init__(self, asset=None, error=None):
        self.asset = asset or _asset_object()
        self.error = error
        self.resolve_calls = 0

    def resolve_asset(self, request):
        self.resolve_calls += 1
        if self.error is not None:
            raise self.error
        return SimpleNamespace(asset=self.asset, blob_key="private/blob")


class BlobStore:
    def __init__(self, data=b"clean"):
        self.data = data
        self.read_calls = 0

    def read(self, blob_key):
        self.read_calls += 1
        assert blob_key == "private/blob"
        return self.data


def _asset_object(stored=b"clean"):
    return SimpleNamespace(
        asset_id=ASSET_ID,
        source_sha256="source",
        stored_sha256=hashlib.sha256(stored).hexdigest(),
        decoded_bytes=len(stored),
        stored_bytes=len(stored),
        mime_type="image/png",
        original_filename="sample.png",
        kind="image",
        width=8,
        height=6,
        created_at="2026-01-01T00:00:00+00:00",
        title="Sample",
        description="",
        tags=(),
        updated_at="2026-01-01T00:00:00+00:00",
    )


def _adapter(service=None, blob_store=None):
    return RememberMeCoreAdapter(
        SimpleNamespace(
            service=service or DownloadService(),
            repository=object(),
            blob_store=blob_store or BlobStore(),
        )
    )


def test_core_adapter_resolve_ob_download_is_single_pass_and_sanitized():
    service = DownloadService()
    blob_store = BlobStore(b"clean")
    metadata, content = _adapter(service, blob_store).resolve_ob_download(
        ASSET_ID
    )

    assert service.resolve_calls == 1
    assert blob_store.read_calls == 1
    assert metadata["asset_id"] == ASSET_ID
    assert metadata["filename"] == "sample.png"
    assert metadata["mime_type"] == "image/png"
    assert metadata["stored_bytes"] == len(b"clean")
    assert metadata["stored_sha256"] == hashlib.sha256(b"clean").hexdigest()
    assert content == b"clean"
    assert isinstance(content, bytes)
    assert "blob_key" not in metadata
    assert "stored_relpath" not in metadata
    assert "path" not in metadata


@pytest.mark.parametrize(
    "blob",
    [b"wrong-size", b"xxxxx", "not bytes"],
)
def test_core_adapter_resolve_ob_download_mismatch_maps_safely(blob):
    asset = _asset_object(b"clean")
    service = DownloadService(asset=asset)
    blob_store = BlobStore(blob)

    with pytest.raises(RememberMeCoreAdapterError) as captured:
        _adapter(service, blob_store).resolve_ob_download(ASSET_ID)

    assert str(captured.value) == "repository_failure"
    assert "private/blob" not in str(captured.value)


def test_core_adapter_resolve_ob_download_preserves_not_found_and_blob_errors():
    for code in ("asset_not_found", "blob_missing"):
        service = DownloadService(error=RememberMeCoreAdapterError(code))
        with pytest.raises(RememberMeCoreAdapterError) as captured:
            _adapter(service, BlobStore()).resolve_ob_download(ASSET_ID)
        assert str(captured.value) == code
        assert "private/blob" not in str(captured.value)

def test_core_adapter_resolve_ob_download_preserves_existing_resolve_blob():
    adapter = _adapter()
    asset, content = adapter.resolve_blob(ASSET_ID)

    assert asset["asset_id"] == ASSET_ID
    assert asset["original_filename"] == "sample.png"
    assert content == b"clean"


def test_legacy_route_get_head_counting_and_headers(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    payload = b"legacy route body"
    asset = _persist_legacy(server, payload, filename="legacy.bin")
    result = json.loads(server._rm_create_asset_download_link(asset["asset_id"]))
    token = result["download_path"].rsplit("/", 1)[-1]
    server._rm_asset_download_sources.pop(token)

    class ForbiddenCore:
        def resolve_ob_download(self, asset_id):
            raise AssertionError("legacy ticket must not use RM Core")

    server.remember_me_host_bundle = SimpleNamespace(core_adapter=ForbiddenCore())

    with _client(server) as client:
        head = client.head(result["download_path"])
        assert head.status_code == 200
        assert head.content == b""
        assert server._rm_asset_download_tokens[token]["get_count"] == 0
        got = client.get(result["download_path"])
        assert got.status_code == 200
        assert got.content == payload
        assert got.headers["content-type"] == "application/octet-stream"
        assert got.headers["cache-control"] == "no-store"
        assert got.headers["pragma"] == "no-cache"
        assert got.headers["x-content-type-options"] == "nosniff"
        assert got.headers["content-length"] == str(len(payload))
        assert "legacy.bin" in got.headers["content-disposition"]
        assert server._rm_asset_download_tokens[token]["get_count"] == 1

        server._rm_asset_download_tokens[token]["get_count"] = (
            server.RM_ASSET_DOWNLOAD_MAX_GETS
        )
        assert client.get(result["download_path"]).status_code == 404


def _ingest_rm_asset(server, content=None):
    content = content or _png_bytes()
    asset = server.remember_me_host_bundle.core_adapter.ingest_image(
        content,
        len(content),
        "rm-file.png",
        "image/png",
    )
    metadata = server.remember_me_host_bundle.core_adapter.get_ob_public_metadata(
        asset["asset_id"]
    )
    link = server.remember_me_host_bundle.download_links.create_download_link(
        metadata
    )
    token = link["download_path"].rsplit("/", 1)[-1]
    return content, metadata, link, token


def test_rm_route_get_head_uses_core_bytes_without_assetstore_fallback(
    tmp_path,
    monkeypatch,
):
    server = _load_server(tmp_path, monkeypatch, rm_enabled=True)
    content, metadata, link, token = _ingest_rm_asset(server)
    assert server._rm_asset_download_sources[token] == "remember_me"

    def fail_legacy(asset_id):
        raise AssertionError("RM ticket must not use AssetStore")

    monkeypatch.setattr(server.asset_store, "resolve_file", fail_legacy)

    with _client(server) as client:
        head = client.head(link["download_path"])
        assert head.status_code == 200
        assert head.content == b""
        assert head.headers["content-length"] == str(metadata["stored_bytes"])
        assert "rm-file.png" in head.headers["content-disposition"]
        assert server._rm_asset_download_tokens[token]["get_count"] == 0

        got = client.get(link["download_path"])
        assert got.status_code == 200
        assert hashlib.sha256(got.content).hexdigest() == metadata["stored_sha256"]
        assert got.headers["content-type"] == "image/png"
        assert got.headers["content-length"] == str(metadata["stored_bytes"])
        assert "source" not in got.headers
        assert "backend" not in got.headers
        assert server._rm_asset_download_tokens[token]["get_count"] == 1


def test_rm_route_failures_retire_ticket_without_fallback_or_leaks(
    tmp_path,
    monkeypatch,
    caplog,
):
    server = _load_server(tmp_path, monkeypatch, rm_enabled=True)
    _, metadata, link, token = _ingest_rm_asset(server)

    class FailingCore:
        def resolve_ob_download(self, asset_id):
            raise RuntimeError("private internal path")

    server.remember_me_host_bundle = SimpleNamespace(core_adapter=FailingCore())
    caplog.set_level(logging.INFO, logger="ombre_brain")
    with _client(server) as client:
        assert client.get(link["download_path"]).status_code == 404
    assert token not in server._rm_asset_download_tokens
    assert token not in server._rm_asset_download_sources
    assert "private internal path" not in caplog.text
    assert metadata["asset_id"] not in caplog.text

    server._rm_asset_download_tokens[token] = {
        "asset_id": metadata["asset_id"],
        "expires_at": time.time() + 300,
        "get_count": 0,
    }
    server._rm_asset_download_sources[token] = "remember_me"
    server.remember_me_host_bundle = None
    with _client(server) as client:
        assert client.get(link["download_path"]).status_code == 404
    assert token not in server._rm_asset_download_tokens
    assert token not in server._rm_asset_download_sources

    server._rm_asset_download_tokens[token] = {
        "asset_id": metadata["asset_id"],
        "expires_at": time.time() + 300,
        "get_count": 0,
    }
    server._rm_asset_download_sources[token] = "unknown"
    with _client(server) as client:
        assert client.get(link["download_path"]).status_code == 404
    assert token not in server._rm_asset_download_tokens
    assert token not in server._rm_asset_download_sources


def test_resolver_lifecycle_and_concurrency(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch, rm_enabled=True)
    real_bundle = server.remember_me_host_bundle
    real_core = real_bundle.core_adapter
    _, metadata, link, token = _ingest_rm_asset(server)
    path = link["download_path"]

    class ObservingCore:
        def __init__(self):
            self.calls = 0

        def resolve_ob_download(self, asset_id):
            self.calls += 1
            assert not server._rm_asset_download_lock.locked()
            return metadata, b"x" * metadata["stored_bytes"]

    observing = ObservingCore()
    metadata = {
        **metadata,
        "stored_sha256": hashlib.sha256(
            b"x" * metadata["stored_bytes"]
        ).hexdigest(),
    }
    server.remember_me_host_bundle = SimpleNamespace(core_adapter=observing)
    server._rm_asset_download_tokens[token]["get_count"] = (
        server.RM_ASSET_DOWNLOAD_MAX_GETS - 1
    )

    with _client(server) as client:
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            statuses = list(
                pool.map(lambda _: client.get(path).status_code, range(2))
            )
    assert sorted(statuses) == [200, 404]

    server.remember_me_host_bundle = real_bundle
    _, metadata, link, token = _ingest_rm_asset(server)

    class ExpiringCore:
        def resolve_ob_download(self, asset_id):
            server._rm_asset_download_tokens[token]["expires_at"] = 0
            return metadata, b"x" * metadata["stored_bytes"]

    metadata = {
        **metadata,
        "stored_sha256": hashlib.sha256(
            b"x" * metadata["stored_bytes"]
        ).hexdigest(),
    }
    server.remember_me_host_bundle = SimpleNamespace(core_adapter=ExpiringCore())
    with _client(server) as client:
        assert client.get(link["download_path"]).status_code == 404

    server.remember_me_host_bundle = real_bundle
    _, metadata, link, token = _ingest_rm_asset(server)

    class DeletingCore:
        def resolve_ob_download(self, asset_id):
            server._rm_asset_download_tokens.pop(token, None)
            server._rm_asset_download_sources.pop(token, None)
            return metadata, b"x" * metadata["stored_bytes"]

    metadata = {
        **metadata,
        "stored_sha256": hashlib.sha256(
            b"x" * metadata["stored_bytes"]
        ).hexdigest(),
    }
    server.remember_me_host_bundle = SimpleNamespace(core_adapter=DeletingCore())
    with _client(server) as client:
        assert client.get(link["download_path"]).status_code == 404


def test_public_contracts_and_isolation_remain_unchanged(tmp_path):
    server_text = (ROOT / "server.py").read_text(encoding="utf-8")
    for handler in (
        "rm_asset_view",
        "rm_asset_upload_link",
        "rm_asset_upload_status",
        "rm_asset_update_metadata",
        "rm_asset_reindex_embeddings",
        "rm_asset_search",
        "rm_asset_inspect",
    ):
        start = server_text.index(f"async def {handler}")
        stop = server_text.find("\n@mcp.", start + 1)
        if stop == -1:
            stop = len(server_text)
        block = server_text[start:stop]
        assert "remember_me_host_bundle" not in block
        assert "RememberMeMcpCompatibilityPresenter" not in block
        assert "RememberMeCoreAdapter" not in block

    assert "return _rm_create_asset_download_link(asset_id)" in server_text
    assert "asset_store.persist_upload" in server_text
    assert server_text.count("@mcp.custom_route") == 37
    assert "OMBRE_RM_RUNTIME_ENABLED" in server_text
    assert "OMBRE_RM_DATA_ROOT" in server_text
    assert "OMBRE_RM_DOWNLOAD" not in server_text

    for relative in (
        "asset_dashboard.py",
        "asset_viewer.py",
        "asset_embedding_index.py",
    ):
        text = (ROOT / relative).read_text(encoding="utf-8")
        assert "remember_me_host_bundle" not in text
        assert "remember_me_host_runtime" not in text

    script = """
import asyncio
import json
import server
from mcp.shared.memory import create_connected_server_and_client_session

async def main():
    async with create_connected_server_and_client_session(server.mcp) as client:
        tools = (await client.list_tools()).tools
    print(json.dumps([tool.name for tool in tools]))

asyncio.run(main())
"""
    env = {
        **__import__("os").environ,
        "PYTHONPATH": str(ROOT),
        "OMBRE_BUCKETS_DIR": str(tmp_path / "buckets"),
    }
    env.pop("OMBRE_RM_RUNTIME_ENABLED", None)
    env.pop("OMBRE_RM_DATA_ROOT", None)
    completed = __import__("subprocess").run(
        [sys.executable, "-c", script],
        cwd=ROOT,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    assert len(json.loads(completed.stdout)) == 21
    env["OMBRE_DIAG_TOOLS"] = "true"
    completed = __import__("subprocess").run(
        [sys.executable, "-c", script],
        cwd=ROOT,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    assert len(json.loads(completed.stdout)) == 36

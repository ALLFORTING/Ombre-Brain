import asyncio
import hashlib
import importlib
import io
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from remember_me_core_adapter import RememberMeCoreAdapterError
from remember_me_download_links import (
    RememberMeDownloadLinkError,
    RememberMeObDownloadLinkCollaborator,
)
from remember_me_mcp_presenter import (
    RememberMeMcpCompatibilityPresenter,
    _DOWNLOAD_PAYLOAD_KEYS,
)


ROOT = Path(__file__).resolve().parent.parent
ASSET_ID = "a" * 32
TOKEN_A = "A" * 43
TOKEN_B = "B" * 43
_UNSET = object()


def _png_bytes(color="blue", size=(8, 6)):
    image = Image.new("RGB", size, color)
    output = io.BytesIO()
    image.save(output, format="PNG", optimize=True)
    image.close()
    return output.getvalue()


def _load_server(tmp_path, monkeypatch, *, rm_enabled=False, bad_data_root=False):
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
        if bad_data_root:
            monkeypatch.setenv("OMBRE_RM_DATA_ROOT", "relative-bad")
        else:
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


def _persist_legacy(server, data=b"legacy body", filename="legacy.bin"):
    source = server.asset_store.create_temp_path()
    source.write_bytes(data)
    return server.asset_store.persist_upload(
        source,
        hashlib.sha256(data).hexdigest(),
        len(data),
        filename,
        "application/octet-stream",
    )


def _metadata(**overrides):
    payload = {
        "asset_id": ASSET_ID,
        "source_sha256": "s" * 64,
        "stored_sha256": "t" * 64,
        "decoded_bytes": 12,
        "stored_bytes": 10,
        "mime_type": "image/png",
        "filename": "asset.png",
        "kind": "image",
        "width": 8,
        "height": 6,
        "created_at": 1000.0,
        "title": "",
        "description": "",
        "tags": [],
        "updated_at": 1000.0,
    }
    payload.update(overrides)
    return payload


def _download_payload(**overrides):
    payload = {
        "ok": True,
        "asset_id": ASSET_ID,
        "filename": "asset.png",
        "mime_type": "image/png",
        "stored_bytes": 10,
        "stored_sha256": "t" * 64,
        "download_path": "/rm/asset-download/" + TOKEN_A,
        "download_url": "/rm/asset-download/" + TOKEN_A,
        "expires_in_seconds": 300,
    }
    payload.update(overrides)
    return payload


def _error_payload(raw, code):
    assert json.loads(raw) == {"error": code, "ok": False}


class CountingCore:
    def __init__(self, result=_UNSET, error=None):
        self.result = _metadata() if result is _UNSET else result
        self.error = error
        self.calls = 0
        self.resolve_blob_calls = 0
        self.resolve_ob_download_calls = 0

    def get(self, asset_id):
        raise AssertionError("get must not be used")

    def get_ob_public_metadata(self, asset_id):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.result

    def update_ob_public_metadata(self, *args, **kwargs):
        raise AssertionError("update must not be used")

    def resolve_blob(self, asset_id):
        self.resolve_blob_calls += 1
        raise AssertionError("resolve_blob must not be used")

    def resolve_ob_download(self, asset_id):
        self.resolve_ob_download_calls += 1
        raise AssertionError("resolve_ob_download must not be used")


class CountingDownloadLinks:
    def __init__(self, payload=None, error=None):
        self.payload = _download_payload() if payload is None else payload
        self.error = error
        self.calls = 0
        self.assets = []

    def create_download_link(self, asset):
        self.calls += 1
        self.assets.append(dict(asset))
        if self.error is not None:
            raise self.error
        return self.payload


class ExplodingMapping:
    def __getitem__(self, key):
        raise RuntimeError("private path detail")


class FailingSetDict(dict):
    def __setitem__(self, key, value):
        raise RuntimeError("private source detail")


def test_default_off_rm_asset_download_link_uses_legacy_only(tmp_path, monkeypatch):
    sys.modules.pop("remember_me_host_runtime", None)
    server = _load_server(tmp_path, monkeypatch, bad_data_root=True)
    data_root = ROOT / "relative-bad"
    content = b"legacy download body"
    asset = _persist_legacy(server, content)

    raw = asyncio.run(server.rm_asset_download_link(asset["asset_id"]))
    payload = json.loads(raw)
    token = payload["download_path"].rsplit("/", 1)[-1]

    assert set(payload) == {"ok", *_DOWNLOAD_PAYLOAD_KEYS}
    assert payload["ok"] is True
    assert "source" not in payload
    assert "backend" not in payload
    assert "path" not in payload
    assert "blob_key" not in payload
    assert "stored_relpath" not in payload
    assert set(server._rm_asset_download_tokens[token]) == {
        "asset_id",
        "expires_at",
        "get_count",
    }
    assert server._rm_asset_download_sources[token] == "legacy"
    assert server.remember_me_host_bundle is None
    assert "remember_me_host_runtime" not in sys.modules
    assert not data_root.exists()
    assert not list(data_root.rglob("assets.sqlite3"))

    with _client(server) as client:
        head = client.head(payload["download_path"])
        assert head.status_code == 200
        assert head.content == b""
        assert server._rm_asset_download_tokens[token]["get_count"] == 0
        got = client.get(payload["download_path"])
        assert got.status_code == 200
        assert got.content == content
        assert got.headers["content-type"] == "application/octet-stream"
        assert got.headers["cache-control"] == "no-store"
        assert got.headers["pragma"] == "no-cache"
        assert got.headers["x-content-type-options"] == "nosniff"
        assert got.headers["content-length"] == str(len(content))
        assert "legacy.bin" in got.headers["content-disposition"]
        assert server._rm_asset_download_tokens[token]["get_count"] == 1

    _error_payload(asyncio.run(server.rm_asset_download_link("missing")), "asset_unavailable")
    _error_payload(asyncio.run(server.rm_asset_download_link("")), "asset_unavailable")


def test_enabled_rm_asset_download_link_uses_presenter_only(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch, rm_enabled=True)
    data = _png_bytes()
    result = server.remember_me_host_bundle.core_adapter.ingest_image(
        data,
        expected_bytes=len(data),
        filename="rm.png",
        mime_type="image/png",
    )
    asset_id = result["asset_id"]
    _persist_legacy(server, b"legacy same phase")

    calls = {"legacy_get": 0, "legacy_resolve": 0, "legacy_creator": 0, "core": 0, "collaborator": 0}
    original_core_get = server.remember_me_host_bundle.core_adapter.get_ob_public_metadata
    original_create = server.remember_me_host_bundle.download_links.create_download_link

    def fail_get(asset_id):
        calls["legacy_get"] += 1
        raise AssertionError("legacy get must not be called")

    def fail_resolve(asset_id):
        calls["legacy_resolve"] += 1
        raise AssertionError("legacy resolve_file must not be called")

    def fail_creator(asset_id):
        calls["legacy_creator"] += 1
        raise AssertionError("legacy creator must not be called")

    def counted_core_get(asset_id):
        calls["core"] += 1
        return original_core_get(asset_id)

    def counted_create(asset):
        calls["collaborator"] += 1
        return original_create(asset)

    monkeypatch.setattr(server.asset_store, "get", fail_get)
    monkeypatch.setattr(server.asset_store, "resolve_file", fail_resolve)
    monkeypatch.setattr(server, "_rm_create_asset_download_link", fail_creator)
    monkeypatch.setattr(
        server.remember_me_host_bundle.core_adapter,
        "get_ob_public_metadata",
        counted_core_get,
    )
    monkeypatch.setattr(
        server.remember_me_host_bundle.download_links,
        "create_download_link",
        counted_create,
    )

    raw = asyncio.run(server.rm_asset_download_link(asset_id))
    payload = json.loads(raw)
    token = payload["download_path"].rsplit("/", 1)[-1]

    assert payload["ok"] is True
    assert set(payload) == {"ok", *_DOWNLOAD_PAYLOAD_KEYS}
    assert "source" not in payload
    assert "backend" not in payload
    assert "path" not in payload
    assert "blob_key" not in payload
    assert "stored_relpath" not in payload
    assert set(server._rm_asset_download_tokens[token]) == {
        "asset_id",
        "expires_at",
        "get_count",
    }
    assert server._rm_asset_download_sources[token] == "remember_me"
    assert calls == {
        "legacy_get": 0,
        "legacy_resolve": 0,
        "legacy_creator": 0,
        "core": 1,
        "collaborator": 1,
    }


def test_enabled_download_link_redeems_rm_core_bytes(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch, rm_enabled=True)
    data = _png_bytes(color="purple")
    result = server.remember_me_host_bundle.core_adapter.ingest_image(
        data,
        expected_bytes=len(data),
        filename="rm-file.png",
        mime_type="image/png",
    )
    raw = asyncio.run(server.rm_asset_download_link(result["asset_id"]))
    payload = json.loads(raw)
    token = payload["download_path"].rsplit("/", 1)[-1]

    def fail_legacy(asset_id):
        raise AssertionError("RM ticket must not use legacy resolver")

    monkeypatch.setattr(server.asset_store, "resolve_file", fail_legacy)

    with _client(server) as client:
        head = client.head(payload["download_path"])
        assert head.status_code == 200
        assert head.content == b""
        assert head.headers["content-length"] == str(payload["stored_bytes"])
        assert "rm-file.png" in head.headers["content-disposition"]
        assert server._rm_asset_download_tokens[token]["get_count"] == 0

        got = client.get(payload["download_path"])
        assert got.status_code == 200
        assert hashlib.sha256(got.content).hexdigest() == payload["stored_sha256"]
        assert got.headers["content-type"] == "image/png"
        assert got.headers["content-length"] == str(payload["stored_bytes"])
        assert got.headers["cache-control"] == "no-store"
        assert got.headers["pragma"] == "no-cache"
        assert got.headers["x-content-type-options"] == "nosniff"
        assert "rm-file.png" in got.headers["content-disposition"]
        assert server._rm_asset_download_tokens[token]["get_count"] == 1


def test_enabled_download_link_never_falls_back_to_legacy(tmp_path, monkeypatch, caplog):
    server = _load_server(tmp_path, monkeypatch, rm_enabled=True)
    legacy = _persist_legacy(server, b"legacy only")
    before_tokens = dict(server._rm_asset_download_tokens)

    def fail_legacy_creator(asset_id):
        raise AssertionError("legacy creator must not be called")

    def fail_asset_store(asset_id):
        raise AssertionError("legacy AssetStore must not be called")

    monkeypatch.setattr(server, "_rm_create_asset_download_link", fail_legacy_creator)
    monkeypatch.setattr(server.asset_store, "get", fail_asset_store)
    monkeypatch.setattr(server.asset_store, "resolve_file", fail_asset_store)

    _error_payload(
        asyncio.run(server.rm_asset_download_link(legacy["asset_id"])),
        "asset_unavailable",
    )
    assert server._rm_asset_download_tokens == before_tokens

    def failing_core(asset_id):
        raise RuntimeError("private internal path C:/secret/blob")

    monkeypatch.setattr(
        server.remember_me_host_bundle.core_adapter,
        "get_ob_public_metadata",
        failing_core,
    )
    raw = asyncio.run(server.rm_asset_download_link("b" * 32))
    _error_payload(raw, "asset_unavailable")
    assert server._rm_asset_download_tokens == before_tokens

    def exploding_presenter(asset_id):
        raise RuntimeError("private presenter path C:/secret/blob")

    server.remember_me_host_bundle.presenter.rm_asset_download_link = exploding_presenter
    raw = asyncio.run(server.rm_asset_download_link("b" * 32))
    _error_payload(raw, "download_unavailable")
    assert server._rm_asset_download_tokens == before_tokens

    combined = raw + "\n" + "\n".join(record.getMessage() for record in caplog.records)
    assert "private" not in combined
    assert "C:/secret" not in combined


def test_presenter_rm_asset_download_link_hardening(monkeypatch):
    metadata = _metadata(extra="private extra")
    collaborator = CountingDownloadLinks(_download_payload(extra="not public"))
    presenter = RememberMeMcpCompatibilityPresenter(CountingCore(metadata), collaborator)
    payload = json.loads(presenter.rm_asset_download_link(ASSET_ID))

    assert payload["ok"] is True
    assert set(payload) == {"ok", *_DOWNLOAD_PAYLOAD_KEYS}
    assert "extra" not in payload
    assert "source" not in payload
    assert "backend" not in payload
    assert "blob_key" not in payload
    assert collaborator.calls == 1
    assert set(collaborator.assets[0]) == set(_metadata())
    assert presenter._core.calls == 1
    assert presenter._core.resolve_blob_calls == 0
    assert presenter._core.resolve_ob_download_calls == 0

    cases = [
        (CountingCore(result=None), CountingDownloadLinks(), "asset_unavailable"),
        (CountingCore(result={"asset_id": ASSET_ID}), CountingDownloadLinks(), "asset_unavailable"),
        (CountingCore(result=ExplodingMapping()), CountingDownloadLinks(), "asset_unavailable"),
        (
            CountingCore(error=RememberMeCoreAdapterError("asset_not_found")),
            CountingDownloadLinks(),
            "asset_unavailable",
        ),
        (CountingCore(error=RuntimeError("private path")), CountingDownloadLinks(), "asset_unavailable"),
        (
            CountingCore(),
            CountingDownloadLinks(error=RememberMeDownloadLinkError("download_store_full")),
            "download_store_full",
        ),
        (
            CountingCore(),
            CountingDownloadLinks(error=RememberMeDownloadLinkError("token_factory_unavailable")),
            "download_unavailable",
        ),
        (CountingCore(), CountingDownloadLinks({"ok": True, "asset_id": ASSET_ID}), "download_unavailable"),
    ]
    for core, links, code in cases:
        presenter = RememberMeMcpCompatibilityPresenter(core, links)
        _error_payload(presenter.rm_asset_download_link(ASSET_ID), code)
        assert core.calls == 1
        assert core.resolve_blob_calls == 0
        assert core.resolve_ob_download_calls == 0
        if code == "asset_unavailable":
            assert links.calls == 0
        else:
            assert links.calls == 1

    import remember_me_mcp_presenter as presenter_module

    original_dumps = presenter_module.json.dumps

    def fail_success(obj, **kwargs):
        if isinstance(obj, dict) and obj.get("ok") is True:
            raise RuntimeError("private json detail")
        return original_dumps(obj, **kwargs)

    monkeypatch.setattr(presenter_module.json, "dumps", fail_success)
    _error_payload(
        RememberMeMcpCompatibilityPresenter(
            CountingCore(),
            CountingDownloadLinks(),
        ).rm_asset_download_link(ASSET_ID),
        "download_unavailable",
    )


def test_presenter_download_link_source_store_and_capacity_fail_closed():
    token_store = {}
    source_store = FailingSetDict()
    collaborator = RememberMeObDownloadLinkCollaborator(
        token_store=token_store,
        ticket_source_store=source_store,
        token_factory=lambda: TOKEN_A,
    )
    presenter = RememberMeMcpCompatibilityPresenter(CountingCore(), collaborator)

    _error_payload(presenter.rm_asset_download_link(ASSET_ID), "download_unavailable")
    assert token_store == {}
    assert dict(source_store) == {}

    token_store = {
        TOKEN_B: {
            "asset_id": ASSET_ID,
            "expires_at": 1300.0,
            "get_count": 0,
        }
    }
    source_store = {TOKEN_B: "remember_me"}
    collaborator = RememberMeObDownloadLinkCollaborator(
        token_store=token_store,
        ticket_source_store=source_store,
        clock=lambda: 1000.0,
        max_tokens=1,
    )
    presenter = RememberMeMcpCompatibilityPresenter(CountingCore(), collaborator)

    _error_payload(presenter.rm_asset_download_link(ASSET_ID), "download_store_full")
    assert set(token_store) == {TOKEN_B}
    assert source_store == {TOKEN_B: "remember_me"}


def test_public_contracts_and_stage8fd_isolation_remain(tmp_path):
    server_text = (ROOT / "server.py").read_text(encoding="utf-8")

    get_start = server_text.index("async def rm_asset_get")
    get_stop = server_text.find("\n@mcp.", get_start + 1)
    get_block = server_text[get_start:get_stop]
    assert "remember_me_host_bundle.presenter.rm_asset_get" in get_block

    download_start = server_text.index("async def rm_asset_download_link")
    download_stop = server_text.find("\n@mcp.", download_start + 1)
    download_block = server_text[download_start:download_stop]
    assert "remember_me_host_bundle.presenter.rm_asset_download_link" in download_block
    assert "_rm_create_asset_download_link(asset_id)" in download_block
    assert "asset_store.get" not in download_block
    assert "asset_store.resolve_file" not in download_block

    for handler in (
        "rm_asset_upload_link",
        "rm_asset_upload_status",
        "rm_asset_update_metadata",
        "rm_asset_search",
        "rm_asset_reindex_embeddings",
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

    view_start = server_text.index("async def rm_asset_view(")
    inspect_start = server_text.index("async def rm_asset_inspect")
    view_block = server_text[view_start:inspect_start]
    assert "remember_me_host_bundle.presenter.rm_asset_view" in view_block
    assert "_rm_verified_view_image" in view_block
    assert "_json_lib.loads(_rm_create_asset_download_link" in view_block
    assert "asset_store.persist_upload" in server_text
    assert server_text.count("@mcp.custom_route") == 37
    assert "OMBRE_RM_DOWNLOAD" not in server_text

    for relative in (
        "asset_dashboard.py",
        "asset_viewer.py",
        "asset_embedding_index.py",
    ):
        text = (ROOT / relative).read_text(encoding="utf-8")
        assert "remember_me_host_bundle" not in text
        assert "remember_me_host_runtime" not in text

    snapshot = json.loads(
        (ROOT / "tests/fixtures/stage8b-ob-rm-mcp-contract.json").read_text(
            encoding="utf-8"
        )
    )
    tools = snapshot["tools"]
    download = next(tool for tool in tools if tool["name"] == "rm_asset_download_link")
    assert download["inputSchema"]["properties"] == {
        "asset_id": {"title": "Asset Id", "type": "string"}
    }
    assert download["inputSchema"]["required"] == ["asset_id"]

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
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT)
    env["OMBRE_BUCKETS_DIR"] = str(tmp_path / "buckets")
    env.pop("OMBRE_RM_RUNTIME_ENABLED", None)
    env.pop("OMBRE_RM_DATA_ROOT", None)
    env.pop("OMBRE_API_KEY", None)
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=ROOT,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    assert len(json.loads(completed.stdout)) == 21

    env["OMBRE_DIAG_TOOLS"] = "true"
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=ROOT,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    assert len(json.loads(completed.stdout)) == 36


def test_default_off_import_ignores_invalid_rm_root(tmp_path):
    script = """
import json
import os
import sys
from pathlib import Path

os.environ.pop("OMBRE_RM_RUNTIME_ENABLED", None)
os.environ["OMBRE_RM_DATA_ROOT"] = "relative-bad"
import server
print(json.dumps({
    "bundle": server.remember_me_host_bundle is None,
    "runtime_imported": "remember_me_host_runtime" in sys.modules,
    "bad_root_exists": (Path.cwd() / "relative-bad").exists(),
    "rm_sqlite_files": (
        [str(path) for path in (Path.cwd() / "relative-bad").rglob("*.sqlite3")]
        if (Path.cwd() / "relative-bad").exists()
        else []
    ),
}))
"""
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT)
    env["OMBRE_BUCKETS_DIR"] = str(tmp_path / "buckets")
    env.pop("OMBRE_API_KEY", None)
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=tmp_path,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    payload = json.loads(completed.stdout)
    assert payload == {
        "bundle": True,
        "runtime_imported": False,
        "bad_root_exists": False,
        "rm_sqlite_files": [],
    }

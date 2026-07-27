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

from remember_me_core_adapter import RememberMeCoreAdapterError
from remember_me_mcp_presenter import (
    RememberMeMcpCompatibilityPresenter,
    _OB_PUBLIC_METADATA_KEYS,
)


ROOT = Path(__file__).resolve().parent.parent
ASSET_ID = "a" * 32


def _png_bytes(color="green", size=(8, 6)):
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
        monkeypatch.setenv("OMBRE_RM_DATA_ROOT", str(tmp_path / "remember-me-runtime"))
    else:
        monkeypatch.delenv("OMBRE_RM_RUNTIME_ENABLED", raising=False)
        if bad_data_root:
            monkeypatch.setenv("OMBRE_RM_DATA_ROOT", "relative-bad")
        else:
            monkeypatch.delenv("OMBRE_RM_DATA_ROOT", raising=False)
    sys.modules.pop("server", None)
    return importlib.import_module("server")


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


def _error_payload(raw):
    assert json.loads(raw) == {"error": "asset_unavailable", "ok": False}


_UNSET = object()


class CountingCore:
    def __init__(self, result=_UNSET, error=None):
        self.result = _metadata() if result is _UNSET else result
        self.error = error
        self.calls = 0

    def get(self, asset_id):
        raise AssertionError("get must not be used by rm_asset_get")

    def get_ob_public_metadata(self, asset_id):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.result

    def update_ob_public_metadata(self, *args, **kwargs):
        raise AssertionError("update must not be used by rm_asset_get")

    def resolve_blob(self, asset_id):
        raise AssertionError("resolve_blob must not be used by rm_asset_get")


class ExplodingMapping:
    def __getitem__(self, key):
        raise RuntimeError("private path detail")


class NullDownloadLinks:
    def create_download_link(self, asset):
        raise AssertionError("download links must not be used")


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


def test_default_off_rm_asset_get_uses_legacy_asset_store(tmp_path, monkeypatch):
    sys.modules.pop("remember_me_host_runtime", None)
    server = _load_server(tmp_path, monkeypatch, bad_data_root=True)
    data_root = tmp_path / "relative-bad"
    asset = _persist_legacy(server)

    raw = asyncio.run(server.rm_asset_get(asset["asset_id"]))
    payload = json.loads(raw)

    assert payload == {"ok": True, **server._rm_asset_public_metadata(asset)}
    assert set(payload) == {"ok", *server._rm_asset_public_metadata(asset)}
    assert "stored_relpath" not in payload
    assert "path" not in payload
    assert "blob_key" not in payload
    assert server.remember_me_host_bundle is None
    assert "remember_me_host_runtime" not in sys.modules
    assert not data_root.exists()
    assert not list(data_root.rglob("assets.sqlite3"))

    _error_payload(asyncio.run(server.rm_asset_get("missing")))
    _error_payload(asyncio.run(server.rm_asset_get("")))


def test_enabled_rm_asset_get_uses_presenter_only(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch, rm_enabled=True)
    data = _png_bytes()
    source_sha = hashlib.sha256(data).hexdigest()
    result = server.remember_me_host_bundle.core_adapter.ingest_image(
        data,
        expected_bytes=len(data),
        filename="rm.png",
        mime_type="image/png",
    )
    assert result["asset_id"]
    asset_id = result["asset_id"]

    legacy_asset = _persist_legacy(server, data=b"legacy body")
    assert legacy_asset["asset_id"] != asset_id

    calls = {"get": 0, "resolve": 0, "presenter": 0}
    original_get = server.asset_store.get
    original_resolve = server.asset_store.resolve_file
    original_presenter_get = server.remember_me_host_bundle.presenter.rm_asset_get
    original_core_get = server.remember_me_host_bundle.core_adapter.get_ob_public_metadata

    def fail_get(asset_id):
        calls["get"] += 1
        raise AssertionError("legacy get must not be called")

    def fail_resolve(asset_id):
        calls["resolve"] += 1
        raise AssertionError("legacy resolve_file must not be called")

    def counted_core_get(asset_id):
        calls["presenter"] += 1
        return original_core_get(asset_id)

    server.asset_store.get = fail_get
    server.asset_store.resolve_file = fail_resolve
    server.remember_me_host_bundle.core_adapter.get_ob_public_metadata = counted_core_get
    try:
        raw = asyncio.run(server.rm_asset_get(asset_id))
    finally:
        server.asset_store.get = original_get
        server.asset_store.resolve_file = original_resolve
        server.remember_me_host_bundle.presenter.rm_asset_get = original_presenter_get
        server.remember_me_host_bundle.core_adapter.get_ob_public_metadata = original_core_get

    payload = json.loads(raw)
    expected = json.loads(original_presenter_get(asset_id))
    assert payload == expected
    assert payload["ok"] is True
    assert set(payload) == {"ok", *_OB_PUBLIC_METADATA_KEYS}
    assert "stored_relpath" not in payload
    assert "blob_key" not in payload
    assert "path" not in payload
    assert calls == {"get": 0, "resolve": 0, "presenter": 1}


def test_enabled_rm_asset_get_never_falls_back_to_legacy(tmp_path, monkeypatch, caplog):
    server = _load_server(tmp_path, monkeypatch, rm_enabled=True)
    legacy = _persist_legacy(server)
    original_get = server.asset_store.get
    original_resolve = server.asset_store.resolve_file

    def fail_get(asset_id):
        raise AssertionError("legacy get must not be called")

    def fail_resolve(asset_id):
        raise AssertionError("legacy resolve_file must not be called")

    server.asset_store.get = fail_get
    server.asset_store.resolve_file = fail_resolve
    try:
        _error_payload(asyncio.run(server.rm_asset_get(legacy["asset_id"])))

        def exploding_presenter(asset_id):
            raise RuntimeError("private internal path C:/secret/blob")

        server.remember_me_host_bundle.presenter.rm_asset_get = exploding_presenter
        raw = asyncio.run(server.rm_asset_get(legacy["asset_id"]))
        _error_payload(raw)
    finally:
        server.asset_store.get = original_get
        server.asset_store.resolve_file = original_resolve

    combined = raw + "\n" + "\n".join(record.getMessage() for record in caplog.records)
    assert "private internal path" not in combined
    assert "C:/secret" not in combined


def test_presenter_rm_asset_get_hardening():
    complete = _metadata(extra="not public")
    presenter = RememberMeMcpCompatibilityPresenter(CountingCore(complete), NullDownloadLinks())
    payload = json.loads(presenter.rm_asset_get(ASSET_ID))
    assert payload["ok"] is True
    assert set(payload) == {"ok", *_OB_PUBLIC_METADATA_KEYS}
    assert "extra" not in payload
    assert presenter._core.calls == 1

    cases = [
        CountingCore(None),
        CountingCore({"asset_id": ASSET_ID}),
        CountingCore(ExplodingMapping()),
        CountingCore(error=RememberMeCoreAdapterError("asset_not_found")),
        CountingCore(error=RuntimeError("private path detail")),
    ]
    for core in cases:
        presenter = RememberMeMcpCompatibilityPresenter(core, NullDownloadLinks())
        _error_payload(presenter.rm_asset_get(ASSET_ID))
        assert core.calls == 1


def test_presenter_json_failure_returns_stable_error(monkeypatch):
    import remember_me_mcp_presenter as presenter_module

    presenter = RememberMeMcpCompatibilityPresenter(
        CountingCore(_metadata()),
        NullDownloadLinks(),
    )

    def fail_json(asset):
        raise RuntimeError("private json detail")

    monkeypatch.setattr(presenter_module, "_json_success", fail_json)
    _error_payload(presenter.rm_asset_get(ASSET_ID))
    assert presenter._core.calls == 1


def test_public_contracts_and_stage8fb_isolation_remain(tmp_path):
    server_text = (ROOT / "server.py").read_text(encoding="utf-8")

    get_start = server_text.index("async def rm_asset_get")
    get_stop = server_text.find("\n@mcp.", get_start + 1)
    get_block = server_text[get_start:get_stop]
    assert "remember_me_host_bundle.presenter.rm_asset_get" in get_block
    assert "asset_store.get" in get_block

    for handler in (
        "rm_asset_upload_link",
        "rm_asset_upload_status",
        "rm_asset_update_metadata",
        "rm_asset_search",
        "rm_asset_reindex_embeddings",
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
    view_start = server_text.index("async def rm_asset_view(")
    inspect_start = server_text.index("async def rm_asset_inspect")
    view_block = server_text[view_start:inspect_start]
    assert "remember_me_host_bundle.presenter.rm_asset_view" in view_block
    assert "_rm_verified_view_image" in view_block
    assert "_rm_create_asset_download_link" in view_block
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
        assert "remember_me_mcp_presenter" not in text

    snapshot = json.loads(
        (ROOT / "tests/fixtures/stage8b-ob-rm-mcp-contract.json").read_text(
            encoding="utf-8"
        )
    )
    tools = snapshot["tools"]
    rm_get = next(tool for tool in tools if tool["name"] == "rm_asset_get")
    assert rm_get["inputSchema"]["properties"] == {
        "asset_id": {"title": "Asset Id", "type": "string"}
    }
    assert rm_get["inputSchema"]["required"] == ["asset_id"]

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

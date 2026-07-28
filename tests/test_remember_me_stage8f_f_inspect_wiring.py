import base64
import hashlib
import importlib
import io
import json
import os
import sqlite3
import subprocess
import sys
from copy import deepcopy
from pathlib import Path

import pytest
from PIL import Image

from remember_me.compat.ombre_brain import MAX_IMAGE_PIXELS
from remember_me_core_adapter import RememberMeCoreAdapterError
from remember_me_mcp_presenter import RememberMeMcpCompatibilityPresenter


ROOT = Path(__file__).resolve().parent.parent
ASSET_ID = "a" * 32
INSPECT_KEYS = {
    "asset_id",
    "title",
    "filename",
    "mime_type",
    "width",
    "height",
    "tags",
    "stored_bytes",
}
LEGACY_HANDLERS = (
    "rm_asset_upload_link",
    "rm_asset_upload_status",
    "rm_asset_reindex_embeddings",
)


def _png_bytes(color="blue", size=(8, 6)):
    image = Image.new("RGB", size, color)
    output = io.BytesIO()
    image.save(output, format="PNG", optimize=True)
    image.close()
    return output.getvalue()


def _jpeg_bytes(color="green", size=(7, 5)):
    image = Image.new("RGB", size, color)
    output = io.BytesIO()
    image.save(output, format="JPEG", quality=90, optimize=True)
    image.close()
    return output.getvalue()


def _load_server(tmp_path, monkeypatch, *, rm_enabled=False, bad_data_root=False):
    monkeypatch.setenv("OMBRE_BUCKETS_DIR", str(tmp_path / "buckets"))
    monkeypatch.setenv("OMBRE_PUBLIC_BASE_URL", "https://example.invalid")
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


def _persist_legacy(server, data, *, filename="legacy.png", mime_type="image/png"):
    source = server.asset_store.create_temp_path()
    source.write_bytes(data)
    return server.asset_store.persist_upload(
        source,
        hashlib.sha256(data).hexdigest(),
        len(data),
        filename,
        mime_type,
    )


def _blob_asset(blob, **overrides):
    payload = {
        "asset_id": ASSET_ID,
        "original_filename": "asset.png",
        "mime_type": "image/png",
        "kind": "image",
        "stored_bytes": len(blob),
        "width": 8,
        "height": 6,
        "title": "Asset",
        "tags": ["one"],
    }
    payload.update(overrides)
    return payload


class CountingCore:
    def __init__(self, *, blob=None, asset=None, error=None):
        self.blob = _png_bytes() if blob is None else blob
        self.asset = _blob_asset(self.blob) if asset is None else asset
        self.error = error
        self.resolve_blob_calls = 0
        self.metadata_calls = 0
        self.resolve_ob_download_calls = 0
        self.get_calls = 0
        self.update_calls = 0

    def resolve_blob(self, asset_id):
        self.resolve_blob_calls += 1
        if self.error is not None:
            if isinstance(self.error, BaseException):
                raise self.error
            raise RememberMeCoreAdapterError(self.error)
        return self.asset, self.blob

    def get_ob_public_metadata(self, asset_id):
        self.metadata_calls += 1
        raise AssertionError("metadata must not be queried by inspect")

    def resolve_ob_download(self, asset_id):
        self.resolve_ob_download_calls += 1
        raise AssertionError("download resolver must not be used by inspect")

    def get(self, asset_id):
        self.get_calls += 1
        raise AssertionError("core get must not be used by inspect")

    def update_ob_public_metadata(self, *args, **kwargs):
        self.update_calls += 1
        raise AssertionError("core update must not be used by inspect")

    def search(self, *args, **kwargs):
        raise AssertionError("search must not be used by inspect")


class CountingLinks:
    def __init__(self):
        self.calls = 0

    def create_download_link(self, asset):
        self.calls += 1
        raise AssertionError("download collaborator must not be used by inspect")


class HostileEnvelopeAsset(dict):
    def __getitem__(self, key):
        if key in {"width", "height"}:
            return super().__getitem__(key)
        raise RuntimeError("private path C:/secret/blob")

    def get(self, key, default=None):
        if key in {"width", "height"}:
            return super().get(key, default)
        raise RuntimeError("private path C:/secret/blob")


def _assert_inspect_success(result, expected_bytes, *, mime_type, filename):
    assert result.isError is False
    assert len(result.content) == 2
    assert result.content[0].type == "text"
    assert result.content[1].type == "image"
    assert result.content[0].text == (
        f"Remember-Me image asset {result.structuredContent['asset_id']}; "
        f"filename: {filename}; MIME type: {mime_type}; "
        f"dimensions: {result.structuredContent['width']} x "
        f"{result.structuredContent['height']}."
    )
    assert result.content[1].mimeType == mime_type
    assert base64.b64decode(result.content[1].data, validate=True) == expected_bytes
    assert set(result.structuredContent) == INSPECT_KEYS
    assert result.structuredContent["filename"] == filename
    assert result.structuredContent["mime_type"] == mime_type
    assert result.structuredContent["stored_bytes"] == len(expected_bytes)
    serialized = result.model_dump_json(by_alias=True)
    assert "rememberMe" not in serialized
    assert "download_url" not in serialized
    assert "download_path" not in serialized
    assert "stored_sha256" not in serialized
    assert "source" not in serialized
    assert "blob_key" not in serialized


def _assert_inspect_error(result, code):
    assert result.isError is True
    assert len(result.content) == 1
    assert result.content[0].type == "text"
    assert all(item.type != "image" for item in result.content)
    assert result.structuredContent == {"ok": False, "error": code}
    serialized = result.model_dump_json(by_alias=True)
    assert "imageBase64" not in serialized
    assert "rememberMe" not in serialized
    assert "private" not in serialized
    assert "C:/secret" not in serialized
    assert "stored_relpath" not in serialized
    assert "blob_key" not in serialized


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("data", "filename", "mime_type"),
    [
        (_png_bytes(color="red"), "legacy.png", "image/png"),
        (_jpeg_bytes(color="yellow"), "legacy.jpg", "image/jpeg"),
    ],
)
async def test_default_off_rm_asset_inspect_keeps_legacy_contract(
    tmp_path,
    monkeypatch,
    data,
    filename,
    mime_type,
):
    sys.modules.pop("remember_me_host_runtime", None)
    server = _load_server(tmp_path, monkeypatch, bad_data_root=True)
    bad_root = tmp_path / "relative-bad"
    asset = _persist_legacy(server, data, filename=filename, mime_type=mime_type)
    before_tokens = deepcopy(server._rm_asset_download_tokens)
    before_sources = deepcopy(server._rm_asset_download_sources)

    result = await server.rm_asset_inspect(asset["asset_id"])

    _assert_inspect_success(result, data, mime_type=mime_type, filename=filename)
    assert server._rm_asset_download_tokens == before_tokens
    assert server._rm_asset_download_sources == before_sources
    assert server.remember_me_host_bundle is None
    assert "remember_me_host_runtime" not in sys.modules
    assert not bad_root.exists()
    assert not list(bad_root.rglob("assets.sqlite3"))


@pytest.mark.asyncio
async def test_default_off_rm_asset_inspect_errors_remain_legacy(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    before_tokens = deepcopy(server._rm_asset_download_tokens)

    _assert_inspect_error(await server.rm_asset_inspect("missing"), "asset_unavailable")
    non_image = _persist_legacy(
        server,
        b"not image",
        filename="document.bin",
        mime_type="application/octet-stream",
    )
    _assert_inspect_error(
        await server.rm_asset_inspect(non_image["asset_id"]),
        "asset_not_image",
    )
    invalid_mime = _persist_legacy(server, _png_bytes(), filename="mime.png")
    with sqlite3.connect(server.asset_store.db_path) as conn:
        conn.execute(
            "UPDATE assets SET mime_type = ? WHERE asset_id = ?",
            ("image/gif", invalid_mime["asset_id"]),
        )
        conn.commit()
    _assert_inspect_error(
        await server.rm_asset_inspect(invalid_mime["asset_id"]),
        "invalid_image_mime",
    )
    malformed = _persist_legacy(server, _png_bytes(color="black"), filename="broken.png")
    server.asset_store.resolve_file(malformed["asset_id"])[1].write_bytes(
        b"x" * malformed["stored_bytes"]
    )
    _assert_inspect_error(
        await server.rm_asset_inspect(malformed["asset_id"]),
        "image_unavailable",
    )
    huge_asset = _blob_asset(_png_bytes(size=(1, 1)), width=MAX_IMAGE_PIXELS + 1, height=1)
    monkeypatch.setattr(
        server,
        "_rm_verified_view_image",
        lambda asset_id: (huge_asset, _png_bytes(size=(1, 1))),
    )
    _assert_inspect_error(
        await server.rm_asset_inspect(huge_asset["asset_id"]),
        "image_too_large",
    )
    assert server._rm_asset_download_tokens == before_tokens


@pytest.mark.asyncio
async def test_enabled_rm_asset_inspect_uses_presenter_only_without_tickets(
    tmp_path,
    monkeypatch,
):
    server = _load_server(tmp_path, monkeypatch, rm_enabled=True)
    data = _png_bytes(color="purple")
    ingested = server.remember_me_host_bundle.core_adapter.ingest_image(
        data,
        expected_bytes=len(data),
        filename="rm.png",
        mime_type="image/png",
    )
    _persist_legacy(server, data, filename="legacy.png")
    before_tokens = deepcopy(server._rm_asset_download_tokens)
    before_sources = deepcopy(server._rm_asset_download_sources)
    calls = {
        "resolve_blob": 0,
        "metadata": 0,
        "collaborator": 0,
        "resolve_ob_download": 0,
        "core_get": 0,
        "core_update": 0,
        "legacy_view": 0,
        "legacy_get": 0,
        "legacy_resolve": 0,
    }
    core = server.remember_me_host_bundle.core_adapter
    links = server.remember_me_host_bundle.download_links
    original_resolve_blob = core.resolve_blob

    def counted_resolve(asset_id):
        calls["resolve_blob"] += 1
        return original_resolve_blob(asset_id)

    def forbidden(name):
        def fail(*args, **kwargs):
            calls[name] += 1
            raise AssertionError(f"{name} must not be called")
        return fail

    monkeypatch.setattr(core, "resolve_blob", counted_resolve)
    monkeypatch.setattr(core, "get_ob_public_metadata", forbidden("metadata"))
    monkeypatch.setattr(core, "resolve_ob_download", forbidden("resolve_ob_download"))
    monkeypatch.setattr(core, "get", forbidden("core_get"))
    monkeypatch.setattr(core, "update_ob_public_metadata", forbidden("core_update"))
    monkeypatch.setattr(links, "create_download_link", forbidden("collaborator"))
    monkeypatch.setattr(server, "_rm_verified_view_image", forbidden("legacy_view"))
    monkeypatch.setattr(server.asset_store, "get", forbidden("legacy_get"))
    monkeypatch.setattr(server.asset_store, "resolve_file", forbidden("legacy_resolve"))

    result = await server.rm_asset_inspect(ingested["asset_id"])
    _, core_bytes = original_resolve_blob(ingested["asset_id"])

    _assert_inspect_success(
        result,
        core_bytes,
        mime_type="image/png",
        filename="rm.png",
    )
    assert server._rm_asset_download_tokens == before_tokens
    assert server._rm_asset_download_sources == before_sources
    assert calls == {
        "resolve_blob": 1,
        "metadata": 0,
        "collaborator": 0,
        "resolve_ob_download": 0,
        "core_get": 0,
        "core_update": 0,
        "legacy_view": 0,
        "legacy_get": 0,
        "legacy_resolve": 0,
    }


@pytest.mark.asyncio
async def test_enabled_rm_asset_inspect_supports_jpeg(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch, rm_enabled=True)
    data = _jpeg_bytes(color="orange")
    ingested = server.remember_me_host_bundle.core_adapter.ingest_image(
        data,
        expected_bytes=len(data),
        filename="rm.jpg",
        mime_type="image/jpeg",
    )

    result = await server.rm_asset_inspect(ingested["asset_id"])

    _assert_inspect_success(
        result,
        data,
        mime_type="image/jpeg",
        filename="rm.jpg",
    )
    assert result.structuredContent["width"] == 7
    assert result.structuredContent["height"] == 5


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "expected"),
    [
        ("asset_not_found", "asset_unavailable"),
        ("blob_missing", "asset_unavailable"),
        ("invalid_asset_id", "asset_unavailable"),
        ("pixel_limit", "image_too_large"),
        (RuntimeError("private path C:/secret/blob"), "image_unavailable"),
    ],
)
async def test_enabled_rm_asset_inspect_never_falls_back_on_failures(
    tmp_path,
    monkeypatch,
    caplog,
    error,
    expected,
):
    server = _load_server(tmp_path, monkeypatch, rm_enabled=True)
    legacy = _persist_legacy(server, _png_bytes(), filename="legacy.png")
    before_tokens = deepcopy(server._rm_asset_download_tokens)
    before_sources = deepcopy(server._rm_asset_download_sources)
    core = server.remember_me_host_bundle.core_adapter

    def fail_resolve(asset_id):
        if isinstance(error, BaseException):
            raise error
        raise RememberMeCoreAdapterError(error)

    def fail_legacy(*args, **kwargs):
        raise AssertionError("legacy must not be called")

    monkeypatch.setattr(core, "resolve_blob", fail_resolve)
    monkeypatch.setattr(core, "get_ob_public_metadata", fail_legacy)
    monkeypatch.setattr(server, "_rm_verified_view_image", fail_legacy)
    monkeypatch.setattr(server.asset_store, "get", fail_legacy)
    monkeypatch.setattr(server.asset_store, "resolve_file", fail_legacy)

    result = await server.rm_asset_inspect(legacy["asset_id"])

    _assert_inspect_error(result, expected)
    assert server._rm_asset_download_tokens == before_tokens
    assert server._rm_asset_download_sources == before_sources
    combined = result.model_dump_json(by_alias=True) + "\n" + "\n".join(
        record.getMessage() for record in caplog.records
    )
    assert "private path" not in combined
    assert "C:/secret" not in combined


@pytest.mark.asyncio
async def test_enabled_rm_asset_inspect_handler_unknown_exception_is_stable(
    tmp_path,
    monkeypatch,
    caplog,
):
    server = _load_server(tmp_path, monkeypatch, rm_enabled=True)
    before_tokens = deepcopy(server._rm_asset_download_tokens)

    def exploding(asset_id):
        raise RuntimeError("private presenter path C:/secret/blob")

    monkeypatch.setattr(server.remember_me_host_bundle.presenter, "rm_asset_inspect", exploding)
    monkeypatch.setattr(
        server,
        "_rm_verified_view_image",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("legacy")),
    )

    result = await server.rm_asset_inspect("b" * 32)

    _assert_inspect_error(result, "image_unavailable")
    assert server._rm_asset_download_tokens == before_tokens
    combined = result.model_dump_json(by_alias=True) + "\n" + "\n".join(
        record.getMessage() for record in caplog.records
    )
    assert "private presenter" not in combined
    assert "C:/secret" not in combined


def test_presenter_rm_asset_inspect_success_and_call_boundaries():
    blob = _png_bytes()
    core = CountingCore(blob=blob)
    links = CountingLinks()
    presenter = RememberMeMcpCompatibilityPresenter(core, links)

    result = presenter.rm_asset_inspect(ASSET_ID)

    _assert_inspect_success(
        result,
        blob,
        mime_type="image/png",
        filename="asset.png",
    )
    assert core.resolve_blob_calls == 1
    assert core.metadata_calls == 0
    assert core.resolve_ob_download_calls == 0
    assert core.get_calls == 0
    assert core.update_calls == 0
    assert links.calls == 0


@pytest.mark.parametrize(
    ("verified", "expected"),
    [
        ((_blob_asset(_png_bytes(), width=MAX_IMAGE_PIXELS, height=1), _png_bytes()), None),
        ((_blob_asset(_png_bytes(), width=MAX_IMAGE_PIXELS + 1, height=1), _png_bytes()), "image_too_large"),
    ],
)
def test_presenter_rm_asset_inspect_pixel_gate(monkeypatch, verified, expected):
    presenter = RememberMeMcpCompatibilityPresenter(CountingCore(), CountingLinks())
    monkeypatch.setattr(presenter, "_verified_image", lambda asset_id: verified)
    if expected is not None:
        monkeypatch.setattr(
            "remember_me_mcp_presenter.base64.b64encode",
            lambda data: (_ for _ in ()).throw(AssertionError("base64 skipped")),
        )

    result = presenter.rm_asset_inspect(ASSET_ID)

    if expected is None:
        assert result.isError is False
        assert result.content[1].type == "image"
    else:
        _assert_inspect_error(result, expected)
    assert presenter._core.metadata_calls == 0
    assert presenter._download_links.calls == 0


@pytest.mark.parametrize("failure", ["base64", "metadata", "image", "mapping"])
def test_presenter_rm_asset_inspect_envelope_failures_are_stable(
    monkeypatch,
    failure,
):
    blob = _png_bytes()
    core = CountingCore(blob=blob)
    links = CountingLinks()
    presenter = RememberMeMcpCompatibilityPresenter(core, links)
    if failure == "base64":
        monkeypatch.setattr(
            "remember_me_mcp_presenter.base64.b64encode",
            lambda data: (_ for _ in ()).throw(RuntimeError("private token C:/secret")),
        )
    elif failure == "metadata":
        monkeypatch.setattr(
            "remember_me_mcp_presenter._flat_image_metadata",
            lambda asset: (_ for _ in ()).throw(RuntimeError("private path C:/secret")),
        )
    elif failure == "image":
        monkeypatch.setattr(
            "remember_me_mcp_presenter.ImageContent",
            lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("private bytes C:/secret")),
        )
    else:
        hostile = HostileEnvelopeAsset(_blob_asset(blob))
        monkeypatch.setattr(presenter, "_verified_image", lambda asset_id: (hostile, blob))

    result = presenter.rm_asset_inspect(ASSET_ID)

    _assert_inspect_error(result, "image_unavailable")
    assert core.metadata_calls == 0
    assert links.calls == 0


def test_public_contracts_and_stage8ff_isolation_remain(tmp_path):
    server_text = (ROOT / "server.py").read_text(encoding="utf-8")

    get_block = server_text[
        server_text.index("async def rm_asset_get"):
        server_text.index("async def rm_asset_update_metadata")
    ]
    download_block = server_text[
        server_text.index("async def rm_asset_download_link"):
        server_text.index("@mcp.resource", server_text.index("async def rm_asset_download_link"))
    ]
    view_block = server_text[
        server_text.index("async def rm_asset_view("):
        server_text.index("async def rm_asset_inspect")
    ]
    inspect_block = server_text[
        server_text.index("async def rm_asset_inspect"):
        server_text.index("@mcp.custom_route", server_text.index("async def rm_asset_inspect"))
    ]
    assert "remember_me_host_bundle.presenter.rm_asset_get" in get_block
    assert "remember_me_host_bundle.presenter.rm_asset_download_link" in download_block
    assert "remember_me_host_bundle.presenter.rm_asset_view" in view_block
    assert "remember_me_host_bundle.presenter.rm_asset_inspect" in inspect_block
    assert "_rm_verified_view_image" in inspect_block
    assert "_rm_create_asset_download_link" not in inspect_block
    assert "asset_store.get" not in inspect_block
    assert "asset_store.resolve_file" not in inspect_block
    assert "meta=ASSET_VIEWER_TOOL_META" not in inspect_block
    docstring = inspect_block.split('"""', 2)[1]
    assert "model needs to read the image or text" in docstring
    assert "view when the goal is only to show the image" in docstring
    assert "Never guess image content from metadata" in docstring

    for handler in LEGACY_HANDLERS:
        start = server_text.index(f"async def {handler}")
        stop = server_text.find("\n@mcp.", start + 1)
        if stop == -1:
            stop = len(server_text)
        block = server_text[start:stop]
        assert "remember_me_host_bundle" not in block
        assert "RememberMeMcpCompatibilityPresenter" not in block
        assert "RememberMeCoreAdapter" not in block

    assert "asset_store.persist_upload" in server_text
    assert server_text.count("@mcp.custom_route") == 37
    assert "OMBRE_RM_DOWNLOAD" not in server_text
    assert "download_url" not in inspect_block
    assert "download_path" not in inspect_block

    for relative in (
        "asset_dashboard.py",
        "asset_viewer.py",
        "asset_embedding_index.py",
    ):
        text = (ROOT / relative).read_text(encoding="utf-8")
        assert "remember_me_host_bundle" not in text
        assert "remember_me_host_runtime" not in text

    snapshot_path = ROOT / "tests/fixtures/stage8b-ob-rm-mcp-contract.json"
    before_snapshot = snapshot_path.read_bytes()
    snapshot = json.loads(before_snapshot.decode("utf-8"))
    tool = next(item for item in snapshot["tools"] if item["name"] == "rm_asset_inspect")
    assert tool["inputSchema"]["properties"] == {
        "asset_id": {"title": "Asset Id", "type": "string"}
    }
    assert tool["inputSchema"]["required"] == ["asset_id"]
    assert before_snapshot == snapshot_path.read_bytes()

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
    env["OMBRE_BUCKETS_DIR"] = str(tmp_path / "count-buckets")
    env.pop("OMBRE_RM_RUNTIME_ENABLED", None)
    env.pop("OMBRE_RM_DATA_ROOT", None)
    output = subprocess.check_output([sys.executable, "-c", script], cwd=ROOT, env=env, text=True)
    names = json.loads(output.strip().splitlines()[-1])
    assert len(names) == 21

    diag_env = dict(env)
    diag_env["OMBRE_DIAG_TOOLS"] = "true"
    output = subprocess.check_output([sys.executable, "-c", script], cwd=ROOT, env=diag_env, text=True)
    diag_names = json.loads(output.strip().splitlines()[-1])
    assert len(diag_names) == 36

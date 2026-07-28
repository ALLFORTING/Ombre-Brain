import asyncio
import base64
import hashlib
import importlib
import io
import json
import os
import re
import sqlite3
import subprocess
import sys
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from asset_viewer import (
    ASSET_VIEWER_HTML,
    ASSET_VIEWER_MIME_TYPE,
    ASSET_VIEWER_RESOURCE_META,
    ASSET_VIEWER_TOOL_META,
    ASSET_VIEWER_URI,
)
from remember_me_core_adapter import RememberMeCoreAdapterError
from remember_me_download_links import (
    RememberMeDownloadLinkError,
    RememberMeObDownloadLinkCollaborator,
)
from remember_me_mcp_presenter import RememberMeMcpCompatibilityPresenter


ROOT = Path(__file__).resolve().parent.parent
ASSET_ID = "a" * 32
TOKEN_A = "A" * 43
TOKEN_B = "B" * 43
VIEW_KEYS = {
    "asset_id",
    "title",
    "filename",
    "mime_type",
    "width",
    "height",
    "tags",
    "stored_bytes",
}


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


def _persist_legacy(
    server,
    data,
    *,
    filename="legacy.png",
    mime_type="image/png",
):
    source = server.asset_store.create_temp_path()
    source.write_bytes(data)
    return server.asset_store.persist_upload(
        source,
        hashlib.sha256(data).hexdigest(),
        len(data),
        filename,
        mime_type,
    )


def _token_from_view(result):
    return re.search(
        r"/rm/asset-download/([A-Za-z0-9_-]+)",
        result.content[0].text,
    ).group(1)


def _assert_view_success(result, expected_bytes, *, mime_type, filename):
    assert result.isError is False
    assert len(result.content) == 1
    assert result.content[0].type == "text"
    assert result.content[0].text.startswith("Remember-Me image: ")
    assert "short-lived download link: " in result.content[0].text
    assert set(result.structuredContent) == VIEW_KEYS
    assert result.structuredContent["filename"] == filename
    assert result.structuredContent["mime_type"] == mime_type
    assert result.structuredContent["stored_bytes"] == len(expected_bytes)
    assert "download_url" not in result.structuredContent
    assert "download_path" not in result.structuredContent
    assert "stored_sha256" not in result.structuredContent
    assert "source" not in result.structuredContent
    assert "blob_key" not in result.structuredContent
    assert set(result.meta) == {"rememberMe"}
    remember_me = result.meta["rememberMe"]
    assert remember_me["schemaVersion"] == 1
    assert remember_me["mimeType"] == mime_type
    assert base64.b64decode(
        remember_me["imageBase64"],
        validate=True,
    ) == expected_bytes
    assert all(item.type != "image" for item in result.content)


def _assert_view_error(result, code):
    assert result.isError is True
    assert len(result.content) == 1
    assert result.content[0].type == "text"
    assert result.structuredContent == {"ok": False, "error": code}
    serialized = result.model_dump_json(by_alias=True)
    assert "imageBase64" not in serialized
    assert "private" not in serialized
    assert "C:/secret" not in serialized
    assert "stored_relpath" not in serialized
    assert "blob_key" not in serialized


def _metadata(blob, **overrides):
    payload = {
        "asset_id": ASSET_ID,
        "source_sha256": "s" * 64,
        "stored_sha256": hashlib.sha256(blob).hexdigest(),
        "decoded_bytes": len(blob),
        "stored_bytes": len(blob),
        "mime_type": "image/png",
        "filename": "asset.png",
        "kind": "image",
        "width": 8,
        "height": 6,
        "created_at": 1000.0,
        "title": "Asset",
        "description": "",
        "tags": ["one"],
        "updated_at": 1000.0,
    }
    payload.update(overrides)
    return payload


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


def _download_payload(blob, **overrides):
    payload = {
        "ok": True,
        "asset_id": ASSET_ID,
        "filename": "asset.png",
        "mime_type": "image/png",
        "stored_bytes": len(blob),
        "stored_sha256": hashlib.sha256(blob).hexdigest(),
        "download_path": "/rm/asset-download/" + TOKEN_A,
        "download_url": "https://example.invalid/rm/asset-download/" + TOKEN_A,
        "expires_in_seconds": 300,
    }
    payload.update(overrides)
    return payload


_DEFAULT_METADATA = object()


class CountingCore:
    def __init__(
        self,
        *,
        blob=None,
        asset=None,
        metadata=_DEFAULT_METADATA,
        error=None,
    ):
        self.blob = _png_bytes() if blob is None else blob
        self.asset = _blob_asset(self.blob) if asset is None else asset
        self.metadata = (
            _metadata(self.blob) if metadata is _DEFAULT_METADATA else metadata
        )
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
        return deepcopy(self.metadata)

    def resolve_ob_download(self, asset_id):
        self.resolve_ob_download_calls += 1
        raise AssertionError("resolve_ob_download must not be used")

    def get(self, asset_id):
        self.get_calls += 1
        raise AssertionError("get must not be used")

    def update_ob_public_metadata(self, *args, **kwargs):
        self.update_calls += 1
        raise AssertionError("update must not be used")


_DEFAULT_DOWNLOAD_PAYLOAD = object()


class CountingLinks:
    def __init__(
        self,
        *,
        blob=None,
        payload=_DEFAULT_DOWNLOAD_PAYLOAD,
        error=None,
    ):
        blob = _png_bytes() if blob is None else blob
        self.payload = (
            _download_payload(blob)
            if payload is _DEFAULT_DOWNLOAD_PAYLOAD
            else payload
        )
        self.error = error
        self.calls = 0
        self.assets = []

    def create_download_link(self, asset):
        self.calls += 1
        self.assets.append(deepcopy(dict(asset)))
        if self.error is not None:
            raise self.error
        return deepcopy(self.payload)


class ExplodingMapping:
    def __getitem__(self, key):
        raise RuntimeError("private mapping path C:/secret/blob")

    def get(self, key, default=None):
        raise RuntimeError("private mapping path C:/secret/blob")


class FailingSetDict(dict):
    def __setitem__(self, key, value):
        raise RuntimeError("private source path C:/secret/blob")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("data", "filename", "mime_type"),
    [
        (_png_bytes(color="red"), "legacy.png", "image/png"),
        (_jpeg_bytes(color="yellow"), "legacy.jpg", "image/jpeg"),
    ],
)
async def test_default_off_rm_asset_view_keeps_legacy_contract(
    tmp_path,
    monkeypatch,
    data,
    filename,
    mime_type,
):
    sys.modules.pop("remember_me_host_runtime", None)
    server = _load_server(tmp_path, monkeypatch, bad_data_root=True)
    bad_root = tmp_path / "relative-bad"
    asset = _persist_legacy(
        server,
        data,
        filename=filename,
        mime_type=mime_type,
    )

    result = await server.rm_asset_view(asset["asset_id"])
    token = _token_from_view(result)

    _assert_view_success(
        result,
        data,
        mime_type=mime_type,
        filename=filename,
    )
    assert set(server._rm_asset_download_tokens[token]) == {
        "asset_id",
        "expires_at",
        "get_count",
    }
    assert server._rm_asset_download_sources[token] == "legacy"
    assert server.remember_me_host_bundle is None
    assert "remember_me_host_runtime" not in sys.modules
    assert not bad_root.exists()
    assert not list(bad_root.rglob("assets.sqlite3"))

    with _client(server) as client:
        head = client.head(f"/rm/asset-download/{token}")
        assert head.status_code == 200
        assert head.content == b""
        assert server._rm_asset_download_tokens[token]["get_count"] == 0
        got = client.get(f"/rm/asset-download/{token}")
        assert got.status_code == 200
        assert got.content == data
        assert got.headers["content-type"] == mime_type
        assert got.headers["content-length"] == str(len(data))
        assert got.headers["cache-control"] == "no-store"
        assert got.headers["pragma"] == "no-cache"
        assert got.headers["x-content-type-options"] == "nosniff"
        assert filename in got.headers["content-disposition"]
        assert server._rm_asset_download_tokens[token]["get_count"] == 1


@pytest.mark.asyncio
async def test_default_off_rm_asset_view_errors_remain_legacy(
    tmp_path,
    monkeypatch,
):
    server = _load_server(tmp_path, monkeypatch)
    _assert_view_error(await server.rm_asset_view("missing"), "asset_unavailable")
    non_image = _persist_legacy(
        server,
        b"not image",
        filename="document.bin",
        mime_type="application/octet-stream",
    )
    _assert_view_error(
        await server.rm_asset_view(non_image["asset_id"]),
        "asset_not_image",
    )
    invalid_mime = _persist_legacy(
        server,
        _png_bytes(),
        filename="mime.png",
        mime_type="image/png",
    )
    with sqlite3.connect(server.asset_store.db_path) as conn:
        conn.execute(
            "UPDATE assets SET mime_type = ? WHERE asset_id = ?",
            ("image/gif", invalid_mime["asset_id"]),
        )
        conn.commit()
    _assert_view_error(
        await server.rm_asset_view(invalid_mime["asset_id"]),
        "invalid_image_mime",
    )
    malformed = _persist_legacy(
        server,
        _png_bytes(color="green"),
        filename="broken.png",
        mime_type="image/png",
    )
    server.asset_store.resolve_file(malformed["asset_id"])[1].write_bytes(
        b"x" * malformed["stored_bytes"]
    )
    _assert_view_error(
        await server.rm_asset_view(malformed["asset_id"]),
        "image_unavailable",
    )


@pytest.mark.asyncio
async def test_enabled_rm_asset_view_uses_presenter_and_rm_ticket_only(
    tmp_path,
    monkeypatch,
):
    server = _load_server(tmp_path, monkeypatch, rm_enabled=True)
    data = _png_bytes(color="purple")
    result = server.remember_me_host_bundle.core_adapter.ingest_image(
        data,
        expected_bytes=len(data),
        filename="rm.png",
        mime_type="image/png",
    )
    asset_id = result["asset_id"]
    _persist_legacy(server, data, filename="legacy.png")

    calls = {
        "resolve_blob": 0,
        "metadata": 0,
        "collaborator": 0,
        "legacy_view": 0,
        "legacy_creator": 0,
        "legacy_get": 0,
        "legacy_resolve": 0,
    }
    original_resolve_blob = server.remember_me_host_bundle.core_adapter.resolve_blob
    original_metadata = (
        server.remember_me_host_bundle.core_adapter.get_ob_public_metadata
    )
    original_create = server.remember_me_host_bundle.download_links.create_download_link

    def counted_resolve(asset_id):
        calls["resolve_blob"] += 1
        return original_resolve_blob(asset_id)

    def counted_metadata(asset_id):
        calls["metadata"] += 1
        return original_metadata(asset_id)

    def counted_create(asset):
        calls["collaborator"] += 1
        return original_create(asset)

    def fail_legacy_view(asset_id):
        calls["legacy_view"] += 1
        raise AssertionError("legacy viewer helper must not be called")

    def fail_legacy_creator(asset_id):
        calls["legacy_creator"] += 1
        raise AssertionError("legacy download creator must not be called")

    def fail_legacy_store(asset_id):
        calls["legacy_get"] += 1
        raise AssertionError("legacy AssetStore must not be called")

    def fail_legacy_resolve(asset_id):
        calls["legacy_resolve"] += 1
        raise AssertionError("legacy AssetStore must not be called")

    monkeypatch.setattr(
        server.remember_me_host_bundle.core_adapter,
        "resolve_blob",
        counted_resolve,
    )
    monkeypatch.setattr(
        server.remember_me_host_bundle.core_adapter,
        "get_ob_public_metadata",
        counted_metadata,
    )
    monkeypatch.setattr(
        server.remember_me_host_bundle.download_links,
        "create_download_link",
        counted_create,
    )
    monkeypatch.setattr(server, "_rm_verified_view_image", fail_legacy_view)
    monkeypatch.setattr(server, "_rm_create_asset_download_link", fail_legacy_creator)
    monkeypatch.setattr(server.asset_store, "get", fail_legacy_store)
    monkeypatch.setattr(server.asset_store, "resolve_file", fail_legacy_resolve)

    viewed = await server.rm_asset_view(asset_id)
    token = _token_from_view(viewed)
    _, core_bytes = original_resolve_blob(asset_id)

    _assert_view_success(
        viewed,
        core_bytes,
        mime_type="image/png",
        filename="rm.png",
    )
    assert set(server._rm_asset_download_tokens[token]) == {
        "asset_id",
        "expires_at",
        "get_count",
    }
    assert server._rm_asset_download_sources[token] == "remember_me"
    assert calls == {
        "resolve_blob": 1,
        "metadata": 1,
        "collaborator": 1,
        "legacy_view": 0,
        "legacy_creator": 0,
        "legacy_get": 0,
        "legacy_resolve": 0,
    }


@pytest.mark.asyncio
async def test_enabled_rm_asset_view_fallback_redeems_rm_core_bytes(
    tmp_path,
    monkeypatch,
):
    server = _load_server(tmp_path, monkeypatch, rm_enabled=True)
    data = _png_bytes(color="orange")
    ingested = server.remember_me_host_bundle.core_adapter.ingest_image(
        data,
        expected_bytes=len(data),
        filename="fallback.png",
        mime_type="image/png",
    )
    metadata = server.remember_me_host_bundle.core_adapter.get_ob_public_metadata(
        ingested["asset_id"]
    )
    viewed = await server.rm_asset_view(ingested["asset_id"])
    token = _token_from_view(viewed)

    def fail_legacy(asset_id):
        raise AssertionError("RM fallback must not use legacy resolver")

    monkeypatch.setattr(server.asset_store, "resolve_file", fail_legacy)

    with _client(server) as client:
        head = client.head(f"/rm/asset-download/{token}")
        assert head.status_code == 200
        assert head.content == b""
        assert head.headers["content-length"] == str(metadata["stored_bytes"])
        assert head.headers["content-type"] == "image/png"
        assert "fallback.png" in head.headers["content-disposition"]
        assert server._rm_asset_download_tokens[token]["get_count"] == 0

        got = client.get(f"/rm/asset-download/{token}")
        assert got.status_code == 200
        assert hashlib.sha256(got.content).hexdigest() == metadata["stored_sha256"]
        assert got.headers["content-type"] == "image/png"
        assert got.headers["content-length"] == str(metadata["stored_bytes"])
        assert got.headers["cache-control"] == "no-store"
        assert got.headers["pragma"] == "no-cache"
        assert got.headers["x-content-type-options"] == "nosniff"
        assert "fallback.png" in got.headers["content-disposition"]
        assert server._rm_asset_download_tokens[token]["get_count"] == 1


@pytest.mark.asyncio
async def test_enabled_rm_asset_view_never_falls_back_to_legacy(
    tmp_path,
    monkeypatch,
    caplog,
):
    server = _load_server(tmp_path, monkeypatch, rm_enabled=True)
    legacy = _persist_legacy(server, _png_bytes(), filename="legacy.png")
    before_tokens = dict(server._rm_asset_download_tokens)

    def fail_legacy(*args, **kwargs):
        raise AssertionError("legacy must not be called")

    monkeypatch.setattr(server, "_rm_verified_view_image", fail_legacy)
    monkeypatch.setattr(server, "_rm_create_asset_download_link", fail_legacy)
    monkeypatch.setattr(server.asset_store, "get", fail_legacy)
    monkeypatch.setattr(server.asset_store, "resolve_file", fail_legacy)

    _assert_view_error(
        await server.rm_asset_view(legacy["asset_id"]),
        "asset_unavailable",
    )
    assert server._rm_asset_download_tokens == before_tokens

    def exploding_presenter(asset_id):
        raise RuntimeError("private presenter path C:/secret/blob")

    server.remember_me_host_bundle.presenter.rm_asset_view = exploding_presenter
    raw = await server.rm_asset_view("b" * 32)
    _assert_view_error(raw, "image_unavailable")
    assert server._rm_asset_download_tokens == before_tokens
    combined = raw.model_dump_json(by_alias=True) + "\n" + "\n".join(
        record.getMessage() for record in caplog.records
    )
    assert "private presenter path" not in combined
    assert "C:/secret" not in combined


def test_presenter_rm_asset_view_success_and_call_boundaries():
    blob = _png_bytes()
    core = CountingCore(blob=blob)
    links = CountingLinks(blob=blob)
    presenter = RememberMeMcpCompatibilityPresenter(core, links)

    result = presenter.rm_asset_view(ASSET_ID)

    _assert_view_success(
        result,
        blob,
        mime_type="image/png",
        filename="asset.png",
    )
    assert core.resolve_blob_calls == 1
    assert core.metadata_calls == 1
    assert core.resolve_ob_download_calls == 0
    assert core.get_calls == 0
    assert core.update_calls == 0
    assert links.calls == 1
    assert set(links.assets[0]) == set(_metadata(blob))
    serialized = result.model_dump_json(by_alias=True)
    for forbidden in (
        "stored_sha256",
        "source_sha256",
        "download_url",
        "download_path",
        "stored_relpath",
        "blob_key",
        "backend",
        "source",
    ):
        assert forbidden not in serialized


@pytest.mark.parametrize(
    ("asset", "blob", "expected"),
    [
        ("not tuple", _png_bytes(), "image_unavailable"),
        (("one item",), _png_bytes(), "image_unavailable"),
        (("not mapping", _png_bytes()), _png_bytes(), "image_unavailable"),
        ((_blob_asset(_png_bytes()), "not bytes"), _png_bytes(), "image_unavailable"),
        ((_blob_asset(b""), b""), b"", "image_unavailable"),
        ((_blob_asset(_png_bytes(), stored_bytes=True), _png_bytes()), _png_bytes(), "image_unavailable"),
        ((_blob_asset(_png_bytes(), width=True), _png_bytes()), _png_bytes(), "image_unavailable"),
        ((_blob_asset(_png_bytes(), height=True), _png_bytes()), _png_bytes(), "image_unavailable"),
        ((_blob_asset(_png_bytes(), stored_bytes=1), _png_bytes()), _png_bytes(), "image_unavailable"),
        ((_blob_asset(_png_bytes(), stored_bytes=1_000_000_000), b"x" * 1), b"x" * 1, "image_unavailable"),
        ((_blob_asset(_png_bytes(), kind="document"), _png_bytes()), _png_bytes(), "asset_not_image"),
        ((_blob_asset(_png_bytes(), mime_type="image/gif"), _png_bytes()), _png_bytes(), "invalid_image_mime"),
        ((_blob_asset(_jpeg_bytes(), mime_type="image/png", stored_bytes=len(_jpeg_bytes()), width=7, height=5), _jpeg_bytes()), _jpeg_bytes(), "image_unavailable"),
        ((_blob_asset(_png_bytes(), mime_type="image/jpeg"), _png_bytes()), _png_bytes(), "image_unavailable"),
        ((_blob_asset(_png_bytes(), width=99), _png_bytes()), _png_bytes(), "image_unavailable"),
        ((_blob_asset(b"not image", stored_bytes=9), b"not image"), b"not image", "image_unavailable"),
        ((_blob_asset(_png_bytes(), tags="tag"), _png_bytes()), _png_bytes(), "image_unavailable"),
        ((_blob_asset(_png_bytes(), tags=["ok", 1]), _png_bytes()), _png_bytes(), "image_unavailable"),
        ((_blob_asset(_png_bytes(), asset_id="A" * 32), _png_bytes()), _png_bytes(), "image_unavailable"),
        ((ExplodingMapping(), _png_bytes()), _png_bytes(), "image_unavailable"),
    ],
)
def test_presenter_verified_image_validation_failures_do_not_create_ticket(
    asset,
    blob,
    expected,
):
    resolved = asset if not isinstance(asset, tuple) or len(asset) != 2 else asset

    class Core(CountingCore):
        def resolve_blob(self, asset_id):
            self.resolve_blob_calls += 1
            return resolved

    core = Core(blob=blob)
    links = CountingLinks(blob=blob)
    presenter = RememberMeMcpCompatibilityPresenter(core, links)

    _assert_view_error(presenter.rm_asset_view(ASSET_ID), expected)
    assert core.resolve_blob_calls == 1
    assert core.metadata_calls == 0
    assert core.resolve_ob_download_calls == 0
    assert core.get_calls == 0
    assert core.update_calls == 0
    assert links.calls == 0


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        ("asset_not_found", "asset_unavailable"),
        ("blob_missing", "asset_unavailable"),
        ("invalid_asset_id", "asset_unavailable"),
        ("pixel_limit", "image_too_large"),
        ("repository_failure", "image_unavailable"),
        (RuntimeError("private path C:/secret/blob"), "image_unavailable"),
    ],
)
def test_presenter_resolve_errors_are_stable(error, expected):
    core = CountingCore(error=error)
    links = CountingLinks()
    presenter = RememberMeMcpCompatibilityPresenter(core, links)

    _assert_view_error(presenter.rm_asset_view(ASSET_ID), expected)
    assert core.resolve_blob_calls == 1
    assert core.metadata_calls == 0
    assert links.calls == 0


@pytest.mark.parametrize(
    ("metadata", "payload", "link_error"),
    [
        (None, None, None),
        ({"asset_id": ASSET_ID}, None, None),
        (ExplodingMapping(), None, None),
        (_metadata(_png_bytes(), asset_id="b" * 32), None, None),
        (_metadata(_png_bytes(), mime_type="image/jpeg"), None, None),
        (_metadata(_png_bytes(), stored_bytes=1), None, None),
        (_metadata(_png_bytes()), _download_payload(_png_bytes(), asset_id="b" * 32), None),
        (_metadata(_png_bytes()), _download_payload(_png_bytes(), mime_type="image/jpeg"), None),
        (_metadata(_png_bytes()), _download_payload(_png_bytes(), stored_bytes=1), None),
        (_metadata(_png_bytes()), {"ok": True, "asset_id": ASSET_ID}, None),
        (_metadata(_png_bytes()), None, RememberMeDownloadLinkError("download_store_full")),
        (_metadata(_png_bytes()), None, RuntimeError("private token detail")),
    ],
)
def test_presenter_download_payload_failures_are_view_download_unavailable(
    metadata,
    payload,
    link_error,
):
    blob = _png_bytes()
    core = CountingCore(blob=blob, metadata=metadata)
    links = CountingLinks(blob=blob, payload=payload, error=link_error)
    presenter = RememberMeMcpCompatibilityPresenter(core, links)

    _assert_view_error(presenter.rm_asset_view(ASSET_ID), "download_unavailable")
    assert core.resolve_blob_calls == 1
    assert core.metadata_calls == 1
    if metadata is None or metadata == {"asset_id": ASSET_ID} or isinstance(metadata, ExplodingMapping):
        assert links.calls == 0
    elif isinstance(metadata, dict) and (
        metadata.get("asset_id") != ASSET_ID
        or metadata.get("mime_type") != "image/png"
        or metadata.get("stored_bytes") != len(blob)
    ):
        assert links.calls == 0
    else:
        assert links.calls == 1
    assert core.resolve_ob_download_calls == 0
    assert core.get_calls == 0
    assert core.update_calls == 0


def test_presenter_view_download_source_write_failure_rolls_back():
    blob = _png_bytes()
    token_store = {}
    source_store = FailingSetDict()
    collaborator = RememberMeObDownloadLinkCollaborator(
        token_store=token_store,
        ticket_source_store=source_store,
        token_factory=lambda: TOKEN_A,
    )
    presenter = RememberMeMcpCompatibilityPresenter(
        CountingCore(blob=blob),
        collaborator,
    )

    _assert_view_error(presenter.rm_asset_view(ASSET_ID), "download_unavailable")
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
    presenter = RememberMeMcpCompatibilityPresenter(
        CountingCore(blob=blob),
        collaborator,
    )

    _assert_view_error(presenter.rm_asset_view(ASSET_ID), "download_unavailable")
    assert set(token_store) == {TOKEN_B}
    assert source_store == {TOKEN_B: "remember_me"}


def test_public_contracts_and_stage8fe_isolation_remain(tmp_path):
    server_text = (ROOT / "server.py").read_text(encoding="utf-8")

    get_block = server_text[
        server_text.index("async def rm_asset_get"):
        server_text.index("async def rm_asset_update_metadata")
    ]
    assert "remember_me_host_bundle.presenter.rm_asset_get" in get_block

    download_block = server_text[
        server_text.index("async def rm_asset_download_link"):
        server_text.index("@mcp.resource", server_text.index("async def rm_asset_download_link"))
    ]
    assert "remember_me_host_bundle.presenter.rm_asset_download_link" in download_block

    view_block = server_text[
        server_text.index("async def rm_asset_view("):
        server_text.index("async def rm_asset_inspect")
    ]
    assert "remember_me_host_bundle.presenter.rm_asset_view" in view_block
    assert "_rm_verified_view_image" in view_block
    assert "_rm_create_asset_download_link" in view_block

    for handler in (
        "rm_asset_upload_link",
        "rm_asset_upload_status",
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

    inspect_start = server_text.index("async def rm_asset_inspect")
    inspect_stop = server_text.find("\n@mcp.", inspect_start + 1)
    if inspect_stop == -1:
        inspect_stop = len(server_text)
    inspect_block = server_text[inspect_start:inspect_stop]
    assert "_rm_verified_view_image" in inspect_block
    assert "remember_me_host_bundle is None" in inspect_block
    assert "remember_me_host_bundle.presenter.rm_asset_inspect" in inspect_block
    inspect_enabled = inspect_block[
        inspect_block.index("try:"):
        inspect_block.index("except Exception:")
    ]
    assert "_rm_verified_view_image" not in inspect_enabled
    assert "asset_store.get" not in inspect_enabled
    assert "asset_store.resolve_file" not in inspect_enabled
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

    assert ASSET_VIEWER_URI == "ui://remember-me/asset-viewer.html"
    assert ASSET_VIEWER_MIME_TYPE == "text/html;profile=mcp-app"
    assert ASSET_VIEWER_TOOL_META["ui"]["resourceUri"] == ASSET_VIEWER_URI
    assert ASSET_VIEWER_TOOL_META["ui/resourceUri"] == ASSET_VIEWER_URI
    assert ASSET_VIEWER_RESOURCE_META == {
        "ui": {
            "csp": {"connectDomains": [], "resourceDomains": []},
            "prefersBorder": True,
        }
    }
    assert ASSET_VIEWER_HTML

    snapshot = json.loads(
        (ROOT / "tests/fixtures/stage8b-ob-rm-mcp-contract.json").read_text(
            encoding="utf-8"
        )
    )
    tool = next(item for item in snapshot["tools"] if item["name"] == "rm_asset_view")
    assert tool["inputSchema"]["properties"] == {
        "asset_id": {"title": "Asset Id", "type": "string"}
    }
    assert tool["inputSchema"]["required"] == ["asset_id"]

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
    assert json.loads(completed.stdout) == {
        "bundle": True,
        "runtime_imported": False,
        "bad_root_exists": False,
        "rm_sqlite_files": [],
    }

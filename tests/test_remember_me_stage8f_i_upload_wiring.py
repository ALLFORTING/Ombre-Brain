import base64
import hashlib
import importlib
import io
import json
import sys
from copy import deepcopy
from types import SimpleNamespace

import pytest
from PIL import Image
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from remember_me_mcp_presenter import RememberMeMcpCompatibilityPresenter


ASSET_ID = "c" * 32
STATUS_KEYS = {
    "ok",
    "state",
    "asset_id",
    "source_sha256",
    "stored_sha256",
    "decoded_bytes",
    "stored_bytes",
    "mime_type",
    "filename",
    "kind",
    "width",
    "height",
    "deduplicated",
}
LINK_KEYS = {
    "ok",
    "upload_id",
    "upload_path",
    "upload_url",
    "status_path",
    "expires_in_seconds",
    "max_bytes",
}
PRIVATE_WORDS = (
    "backend",
    "data_root",
    "stored_relpath",
    "blob_key",
    "path",
    "download_url",
    "download_path",
)


def _png_bytes(color="blue", size=(8, 6)):
    image = Image.new("RGB", size, color)
    output = io.BytesIO()
    image.save(output, format="PNG", optimize=True)
    image.close()
    return output.getvalue()


def _load_server(tmp_path, monkeypatch):
    monkeypatch.setenv("OMBRE_BUCKETS_DIR", str(tmp_path / "buckets"))
    monkeypatch.setenv("OMBRE_PUBLIC_BASE_URL", "https://example.invalid")
    monkeypatch.delenv("OMBRE_API_KEY", raising=False)
    monkeypatch.delenv("OMBRE_RM_RUNTIME_ENABLED", raising=False)
    monkeypatch.delenv("OMBRE_RM_DATA_ROOT", raising=False)
    sys.modules.pop("server", None)
    return importlib.import_module("server")


def _asset_client(server):
    app = Starlette(routes=[
        Route(
            "/rm/asset-upload/{token}",
            server.rm_asset_upload_route,
            methods=["GET", "POST"],
        ),
    ])
    return TestClient(app)


def _payload(raw):
    return json.loads(raw)


class FakeLinks:
    def __init__(self):
        self.calls = 0

    def create_download_link(self, asset):
        self.calls += 1
        return {
            "ok": True,
            "asset_id": asset["asset_id"],
            "filename": asset["filename"],
            "mime_type": asset["mime_type"],
            "stored_bytes": asset["stored_bytes"],
            "stored_sha256": asset["stored_sha256"],
            "download_url": "https://example.invalid/download",
            "download_path": "/rm/asset-download/fake",
            "expires_in_seconds": 300,
        }


class UploadCore:
    def __init__(self, *, result_override=None, error=None):
        self.result_override = result_override
        self.error = error
        self.ingest_ob_public_metadata_calls = []
        self.ingest_image_calls = 0
        self.get_calls = 0
        self.metadata_calls = 0
        self.search_calls = 0
        self.update_calls = 0
        self.resolve_blob_calls = 0
        self.resolve_ob_download_calls = 0
        self.asset = None
        self.content = None

    def ingest_ob_public_metadata(
        self,
        content,
        expected_bytes,
        filename,
        mime_type="application/octet-stream",
        *,
        title="",
        description="",
        tags=(),
    ):
        self.ingest_ob_public_metadata_calls.append(
            {
                "content": content,
                "expected_bytes": expected_bytes,
                "filename": filename,
                "mime_type": mime_type,
                "title": title,
                "description": description,
                "tags": tuple(tags),
            }
        )
        if self.error is not None:
            raise self.error
        source_sha = hashlib.sha256(content).hexdigest()
        payload = {
            "asset_id": ASSET_ID,
            "source_sha256": source_sha,
            "stored_sha256": source_sha,
            "decoded_bytes": expected_bytes,
            "stored_bytes": len(content),
            "mime_type": "image/png",
            "filename": filename,
            "kind": "image",
            "width": 8,
            "height": 6,
            "created_at": "2026-01-01T00:00:00+00:00",
            "title": title,
            "description": description,
            "tags": list(tags),
            "updated_at": "2026-01-01T00:00:00+00:00",
            "deduplicated": False,
            "stored_relpath": "private/path.png",
            "blob_key": "private-blob",
            "backend": "remember_me",
            "source": "remember_me",
            "data_root": "C:/private/root",
        }
        if self.result_override is not None:
            if callable(self.result_override):
                payload = self.result_override(payload)
            else:
                payload = self.result_override
        if isinstance(payload, dict):
            self.asset = deepcopy(payload)
            self.asset["original_filename"] = self.asset.get("filename", filename)
            self.content = bytes(content)
        return payload

    def ingest_image(self, *args, **kwargs):
        self.ingest_image_calls += 1
        raise AssertionError("legacy ingest_image contract must not be used by upload route")

    def get_ob_public_metadata(self, asset_id):
        self.metadata_calls += 1
        return dict(self.asset)

    async def search(self, **kwargs):
        self.search_calls += 1
        item = {
            key: self.asset[key]
            for key in (
                "asset_id",
                "filename",
                "title",
                "description",
                "tags",
                "kind",
                "mime_type",
                "width",
                "height",
                "stored_bytes",
                "created_at",
                "updated_at",
            )
        }
        item["match_reasons"] = ["filename"]
        return {"total": 1, "offset": 0, "limit": 20, "results": [item]}

    def resolve_blob(self, asset_id):
        self.resolve_blob_calls += 1
        asset = dict(self.asset)
        asset["original_filename"] = asset["filename"]
        return asset, self.content

    def resolve_ob_download(self, asset_id):
        self.resolve_ob_download_calls += 1
        raise AssertionError("upload route must not resolve downloads")

    def get(self, asset_id):
        self.get_calls += 1
        raise AssertionError("upload route must not call Core get")

    def update_ob_public_metadata(self, *args, **kwargs):
        self.update_calls += 1
        raise AssertionError("upload route must not update metadata")


def _enable_fake_rm(server, core):
    links = FakeLinks()
    presenter = RememberMeMcpCompatibilityPresenter(core, links)
    server.remember_me_host_bundle = SimpleNamespace(
        core_adapter=core,
        presenter=presenter,
    )
    return links


def _assert_no_private_fields(raw):
    payload = json.loads(raw)
    assert "source" not in payload
    for word in PRIVATE_WORDS:
        assert word not in raw


@pytest.mark.asyncio
async def test_enabled_link_route_status_uses_rm_source_and_core_ingest_only(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    core = UploadCore()
    _enable_fake_rm(server, core)
    monkeypatch.setattr(server.asset_store, "sanitize_filename", lambda name: (_ for _ in ()).throw(AssertionError("legacy sanitizer")))
    monkeypatch.setattr(server.asset_store, "create_temp_path", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("legacy temp")))
    monkeypatch.setattr(server.asset_store, "persist_upload", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("legacy persist")))

    data = _png_bytes()
    link = _payload(await server.rm_asset_upload_link(len(data), "../bad\\name.png", "image/png"))
    assert set(link) == LINK_KEYS
    assert link["ok"] is True
    assert "source" not in json.dumps(link)
    assert server._rm_asset_upload_sources[link["upload_id"]] == "remember_me"
    before_download_tokens = dict(server._rm_asset_download_tokens)
    before_download_sources = dict(server._rm_asset_download_sources)

    with _asset_client(server) as client:
        page = client.get(link["upload_path"])
        uploaded = client.post(link["upload_path"], files={"file": ("ignored.png", data, "image/png")})
        second = client.post(link["upload_path"], files={"file": ("ignored.png", data, "image/png")})
    assert page.status_code == 200
    assert uploaded.status_code == 200
    assert second.status_code == 404

    status_raw = await server.rm_asset_upload_status(link["upload_id"])
    status = _payload(status_raw)
    assert set(status) == STATUS_KEYS
    assert status["state"] == "completed"
    assert status["source_sha256"] == hashlib.sha256(data).hexdigest()
    assert status["filename"] == "_bad_name.png"
    assert status["deduplicated"] is False
    _assert_no_private_fields(status_raw)
    assert server._rm_asset_download_tokens == before_download_tokens
    assert server._rm_asset_download_sources == before_download_sources

    assert len(core.ingest_ob_public_metadata_calls) == 1
    call = core.ingest_ob_public_metadata_calls[0]
    assert call["content"] == data
    assert call["expected_bytes"] == len(data)
    assert call["filename"] == "_bad_name.png"
    assert call["mime_type"] == "image/png"
    assert call["title"] == ""
    assert call["description"] == ""
    assert call["tags"] == ()
    assert core.ingest_image_calls == 0
    assert core.get_calls == 0
    assert core.update_calls == 0
    assert core.resolve_ob_download_calls == 0


@pytest.mark.asyncio
async def test_enabled_rm_upload_can_be_read_by_get_search_view_and_inspect(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    core = UploadCore()
    links = _enable_fake_rm(server, core)
    data = _png_bytes("green")
    link = _payload(await server.rm_asset_upload_link(len(data), "cross.png", "application/octet-stream"))
    with _asset_client(server) as client:
        assert client.post(link["upload_path"], files={"file": ("cross.png", data, "image/png")}).status_code == 200
    status = _payload(await server.rm_asset_upload_status(link["upload_id"]))

    got = _payload(await server.rm_asset_get(status["asset_id"]))
    found = _payload(await server.rm_asset_search(query="cross", limit=20, offset=0))
    viewed = await server.rm_asset_view(status["asset_id"])
    inspected = await server.rm_asset_inspect(status["asset_id"])

    assert got["asset_id"] == status["asset_id"]
    assert found["results"][0]["asset_id"] == status["asset_id"]
    assert viewed.structuredContent["asset_id"] == status["asset_id"]
    assert inspected.content[1].mimeType == "image/png"
    assert base64.b64decode(inspected.content[1].data) == data
    assert server.asset_store.get(status["asset_id"]) is None
    assert links.calls == 1


@pytest.mark.asyncio
async def test_status_is_strictly_source_isolated_and_unknown_source_retires(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    data = _png_bytes()
    legacy = _payload(await server.rm_asset_upload_link(len(data), "legacy.png", "image/png"))
    core = UploadCore()
    _enable_fake_rm(server, core)
    assert _payload(await server.rm_asset_upload_status(legacy["upload_id"])) == {
        "error": "upload_unavailable",
        "ok": False,
        "upload_id": legacy["upload_id"],
    }

    rm = _payload(await server.rm_asset_upload_link(len(data), "rm.png", "image/png"))
    server.remember_me_host_bundle = None
    assert _payload(await server.rm_asset_upload_status(rm["upload_id"])) == {
        "error": "upload_unavailable",
        "ok": False,
        "upload_id": rm["upload_id"],
    }

    unknown = _payload(await server.rm_asset_upload_link(len(data), "unknown.png", "image/png"))
    server._rm_asset_upload_sources[unknown["upload_id"]] = "mystery"
    with _asset_client(server) as client:
        assert client.get(unknown["upload_path"]).status_code == 404
    assert unknown["upload_id"] not in server._rm_asset_uploads
    assert unknown["upload_id"] not in server._rm_asset_upload_sources


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mutate",
    [
        lambda payload: None,
        lambda payload: {key: value for key, value in payload.items() if key != "asset_id"},
        lambda payload: {**payload, "asset_id": "not-hex"},
        lambda payload: {**payload, "source_sha256": "0" * 64},
        lambda payload: {**payload, "decoded_bytes": True},
        lambda payload: {**payload, "stored_bytes": -1},
        lambda payload: {**payload, "filename": 123},
        lambda payload: {**payload, "mime_type": "text/plain"},
        lambda payload: {**payload, "kind": "file"},
        lambda payload: {**payload, "width": 0},
        lambda payload: {**payload, "height": True},
        lambda payload: {**payload, "created_at": 123},
        lambda payload: {**payload, "tags": ["ok", 3]},
        lambda payload: {**payload, "deduplicated": "false"},
    ],
)
async def test_rm_upload_result_hardening_releases_ticket_without_fallback(tmp_path, monkeypatch, mutate):
    server = _load_server(tmp_path, monkeypatch)
    core = UploadCore(result_override=mutate)
    _enable_fake_rm(server, core)
    monkeypatch.setattr(server.asset_store, "persist_upload", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("legacy fallback")))
    data = _png_bytes()
    link = _payload(await server.rm_asset_upload_link(len(data), "bad.png", "image/png"))
    with _asset_client(server) as client:
        response = client.post(link["upload_path"], files={"file": ("bad.png", data, "image/png")})
    assert response.status_code == 500
    assert _payload(await server.rm_asset_upload_status(link["upload_id"]))["state"] == "pending"
    assert len(core.ingest_ob_public_metadata_calls) == 1
    assert core.ingest_image_calls == 0
    assert not server._rm_asset_download_tokens




@pytest.mark.asyncio
async def test_host_upload_filename_sanitizer_matches_legacy_and_enabled_does_not_call_legacy(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    values = [
        "",
        "   ",
        ". .",
        "\tname.png\n",
        "\x00name.png\x7f",
        "../a\\b:c.png",
        "a   b.png",
        "x" * 300 + ".png",
    ]
    for value in values:
        assert server._rm_host_sanitize_upload_filename(value) == server.asset_store.sanitize_filename(value)

    core = UploadCore()
    _enable_fake_rm(server, core)
    monkeypatch.setattr(
        server.asset_store,
        "sanitize_filename",
        lambda name: (_ for _ in ()).throw(AssertionError("legacy sanitizer")),
    )
    link = _payload(await server.rm_asset_upload_link(1, "../a\\b:c.png", "image/png"))
    assert link["ok"] is True
    assert server._rm_asset_uploads[link["upload_id"]]["filename"] == "_a_b_c.png"


@pytest.mark.asyncio
async def test_rm_temp_path_creation_failure_releases_ticket_without_fallback(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    core = UploadCore()
    _enable_fake_rm(server, core)
    data = _png_bytes()
    link = _payload(await server.rm_asset_upload_link(len(data), "temp.png", "image/png"))
    before_download_tokens = dict(server._rm_asset_download_tokens)
    before_download_sources = dict(server._rm_asset_download_sources)
    monkeypatch.setattr(
        server,
        "_rm_create_upload_temp_path",
        lambda: (_ for _ in ()).throw(OSError("private C:/secret/temp path")),
    )
    monkeypatch.setattr(server.asset_store, "create_temp_path", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("legacy temp")))
    monkeypatch.setattr(server.asset_store, "persist_upload", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("legacy persist")))

    with _asset_client(server) as client:
        response = client.post(link["upload_path"], files={"file": ("temp.png", data, "image/png")})
        retry_page = client.get(link["upload_path"])

    assert response.status_code == 500
    assert "private" not in response.text
    assert "C:/secret" not in response.text
    assert _payload(await server.rm_asset_upload_status(link["upload_id"]))["state"] == "pending"
    assert retry_page.status_code == 200
    assert len(core.ingest_ob_public_metadata_calls) == 0
    assert core.ingest_image_calls == 0
    assert server._rm_asset_download_tokens == before_download_tokens
    assert server._rm_asset_download_sources == before_download_sources


@pytest.mark.asyncio
async def test_rm_complete_none_releases_ticket_without_fallback(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    core = UploadCore()
    _enable_fake_rm(server, core)
    data = _png_bytes()
    link = _payload(await server.rm_asset_upload_link(len(data), "complete.png", "image/png"))
    before_download_tokens = dict(server._rm_asset_download_tokens)
    before_download_sources = dict(server._rm_asset_download_sources)
    monkeypatch.setattr(server, "_rm_complete_asset_upload", lambda *args, **kwargs: None)
    monkeypatch.setattr(server.asset_store, "persist_upload", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("legacy fallback")))

    with _asset_client(server) as client:
        response = client.post(link["upload_path"], files={"file": ("complete.png", data, "image/png")})
        retry_page = client.get(link["upload_path"])

    assert response.status_code == 500
    assert _payload(await server.rm_asset_upload_status(link["upload_id"]))["state"] == "pending"
    assert retry_page.status_code == 200
    assert len(core.ingest_ob_public_metadata_calls) == 1
    assert core.ingest_image_calls == 0
    assert server._rm_asset_download_tokens == before_download_tokens
    assert server._rm_asset_download_sources == before_download_sources


def test_public_contract_static_scope_and_counts_remain_unchanged(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    server_text = (server.ROOT if hasattr(server, "ROOT") else None)
    text = __import__("pathlib").Path("server.py").read_text(encoding="utf-8")
    assert "core_adapter.ingest_ob_public_metadata" in text
    assert "core_adapter.ingest_image" not in text[text.index("async def rm_asset_upload_route"):text.index("@mcp.custom_route(\"/rm/asset-download/{token}\"")]
    assert "_rm_asset_upload_sources" in text
    assert "async def rm_asset_reindex_embeddings" in text
    upload_link = text[
        text.index("async def rm_asset_upload_link"):
        text.index("async def rm_asset_upload_status")
    ]
    upload_status = text[
        text.index("async def rm_asset_upload_status"):
        text.index("async def rm_asset_get")
    ]
    upload_route = text[
        text.index("async def rm_asset_upload_route"):
        text.index("@mcp.custom_route(\"/rm/asset-download/{token}\"")
    ]
    for block in (upload_link, upload_status, upload_route):
        assert "RememberMeVectorProviderAdapter" not in block
        assert "embedding_engine" not in block
        assert ".embed(" not in block
    assert server.mcp is not None

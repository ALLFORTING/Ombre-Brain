import asyncio
import hashlib
import importlib
import io
import json
import os
import subprocess
import sys
from copy import deepcopy
from pathlib import Path

import pytest
from PIL import Image

from remember_me_core_adapter import RememberMeCoreAdapterError
from remember_me_mcp_presenter import (
    RememberMeMcpCompatibilityPresenter,
    _OB_PUBLIC_METADATA_KEYS,
)
from rm_cutover_test_support import configure_rm_authority


ROOT = Path(__file__).resolve().parent.parent
ASSET_ID = "a" * 32
SOURCE_SHA = "1" * 64
STORED_SHA = "2" * 64
UPDATE_KEYS = {"ok", *_OB_PUBLIC_METADATA_KEYS}
LEGACY_HANDLERS = (
    "rm_asset_reindex_embeddings",
)


def _png_bytes(color="blue", size=(8, 6)):
    image = Image.new("RGB", size, color)
    output = io.BytesIO()
    image.save(output, format="PNG", optimize=True)
    image.close()
    return output.getvalue()


def _load_server(tmp_path, monkeypatch, *, rm_enabled=False, bad_data_root=False):
    monkeypatch.setenv("OMBRE_BUCKETS_DIR", str(tmp_path / "buckets"))
    monkeypatch.setenv("OMBRE_PUBLIC_BASE_URL", "https://example.invalid")
    monkeypatch.delenv("OMBRE_API_KEY", raising=False)
    if rm_enabled:
        configure_rm_authority(tmp_path, monkeypatch)
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


def _persist_legacy(server, data=None, filename="legacy.png"):
    if data is None:
        data = _png_bytes()
    source = server.asset_store.create_temp_path()
    source.write_bytes(data)
    return server.asset_store.persist_upload(
        source,
        hashlib.sha256(data).hexdigest(),
        len(data),
        filename,
        "image/png",
    )


def _error_payload(raw, code="asset_unavailable"):
    payload = json.loads(raw)
    assert payload == {"error": code, "ok": False}
    assert "C:/secret" not in raw
    assert "private" not in raw
    assert "Traceback" not in raw


def _assert_success_payload(raw):
    payload = json.loads(raw)
    assert payload["ok"] is True
    assert set(payload) == UPDATE_KEYS
    assert "stored_relpath" not in payload
    assert "blob_key" not in payload
    assert "path" not in payload
    assert "download_url" not in payload
    assert "download_path" not in payload
    return payload


class NullDownloadLinks:
    def __init__(self):
        self.calls = 0

    def create_download_link(self, asset):
        self.calls += 1
        raise AssertionError("download links must not be used by update")


_DEFAULT_RESULT = object()


class UpdateCore:
    def __init__(self, result=_DEFAULT_RESULT, error=None):
        self.result = _metadata() if result is _DEFAULT_RESULT else result
        self.error = error
        self.update_calls = []
        self.metadata_calls = 0
        self.resolve_blob_calls = 0
        self.resolve_ob_download_calls = 0
        self.get_calls = 0

    def update_ob_public_metadata(self, asset_id, title=None, description=None, tags=None):
        self.update_calls.append((asset_id, title, description, tags))
        if self.error is not None:
            raise self.error
        result = deepcopy(self.result)
        if isinstance(result, dict):
            if title is not None:
                result["title"] = title
            if description is not None:
                result["description"] = description
            if tags is not None:
                result["tags"] = list(tags)
        return result

    def get_ob_public_metadata(self, asset_id):
        self.metadata_calls += 1
        raise AssertionError("metadata must not be queried after update")

    def resolve_blob(self, asset_id):
        self.resolve_blob_calls += 1
        raise AssertionError("blob must not be read by update")

    def resolve_ob_download(self, asset_id):
        self.resolve_ob_download_calls += 1
        raise AssertionError("download resolver must not be used by update")

    def get(self, asset_id):
        self.get_calls += 1
        raise AssertionError("core get must not be used by update")

    def search(self, *args, **kwargs):
        raise AssertionError("search must not be used by update")


class HostileMapping(dict):
    def __getitem__(self, key):
        raise RuntimeError("private C:/secret metadata")


def _metadata(**overrides):
    payload = {
        "asset_id": ASSET_ID,
        "source_sha256": SOURCE_SHA,
        "stored_sha256": STORED_SHA,
        "decoded_bytes": 12,
        "stored_bytes": 10,
        "mime_type": "image/png",
        "filename": "asset.png",
        "kind": "image",
        "width": 8,
        "height": 6,
        "created_at": "2026-01-01T00:00:00+00:00",
        "title": "Original",
        "description": "Description",
        "tags": ["one"],
        "updated_at": "2026-01-01T00:00:01+00:00",
    }
    payload.update(overrides)
    return payload


def _presenter(core):
    return RememberMeMcpCompatibilityPresenter(core, NullDownloadLinks())


@pytest.mark.asyncio
async def test_default_off_rm_asset_update_metadata_keeps_legacy_contract(tmp_path, monkeypatch, caplog):
    sys.modules.pop("remember_me_host_runtime", None)
    server = _load_server(tmp_path, monkeypatch, bad_data_root=True)
    data_root = tmp_path / "relative-bad"
    asset = _persist_legacy(server)
    before_tokens = deepcopy(server._rm_asset_download_tokens)
    before_sources = deepcopy(server._rm_asset_download_sources)
    calls = []

    async def counted_index(updated):
        calls.append(deepcopy(updated))

    monkeypatch.setattr(server.asset_embedding_index, "index_asset", counted_index)

    raw = await server.rm_asset_update_metadata(
        asset["asset_id"], title="Updated", description="", tags=[]
    )
    payload = _assert_success_payload(raw)
    assert payload["title"] == "Updated"
    assert payload["description"] == ""
    assert payload["tags"] == []
    assert len(calls) == 1
    stored = server.asset_store.get(asset["asset_id"])
    assert stored["title"] == "Updated"
    assert stored["description"] == ""
    assert stored["tags"] == []

    raw = await server.rm_asset_update_metadata(asset["asset_id"], title=None, description=None, tags=None)
    payload = _assert_success_payload(raw)
    assert payload["title"] == "Updated"
    assert payload["description"] == ""
    assert payload["tags"] == []

    async def failing_index(updated):
        raise RuntimeError("private title description tags C:/secret")

    monkeypatch.setattr(server.asset_embedding_index, "index_asset", failing_index)
    raw = await server.rm_asset_update_metadata(asset["asset_id"], title="Still succeeds")
    payload = _assert_success_payload(raw)
    assert payload["title"] == "Still succeeds"
    log_text = "\n".join(record.getMessage() for record in caplog.records)
    assert "Still succeeds" not in log_text
    assert "description" not in log_text
    assert "tags" not in log_text
    assert "C:/secret" not in log_text

    _error_payload(await server.rm_asset_update_metadata("missing", title="x"))
    assert server._rm_asset_download_tokens == before_tokens
    assert server._rm_asset_download_sources == before_sources
    assert server.remember_me_host_bundle is None
    assert "remember_me_host_runtime" not in sys.modules
    assert not data_root.exists()
    assert not list(data_root.rglob("assets.sqlite3"))


@pytest.mark.asyncio
async def test_enabled_rm_asset_update_metadata_uses_core_mutation_only(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch, rm_enabled=True)
    data = _png_bytes()
    created = server.remember_me_host_bundle.core_adapter.ingest_image(
        data,
        expected_bytes=len(data),
        filename="rm.png",
        mime_type="image/png",
    )
    asset_id = created["asset_id"]
    before_tokens = deepcopy(server._rm_asset_download_tokens)
    before_sources = deepcopy(server._rm_asset_download_sources)
    before_metadata = server.remember_me_host_bundle.core_adapter.get_ob_public_metadata(asset_id)
    before_blob = server.remember_me_host_bundle.core_adapter.resolve_blob(asset_id)[1]
    legacy = _persist_legacy(server, filename="same.png")
    legacy_before = deepcopy(legacy)

    calls = {"update": 0, "metadata": 0, "resolve": 0, "download": 0, "get": 0, "links": 0, "legacy_update": 0, "embedding": 0}
    core = server.remember_me_host_bundle.core_adapter
    links = server.remember_me_host_bundle.download_links
    original_update = core.update_ob_public_metadata
    original_metadata = core.get_ob_public_metadata
    original_resolve = core.resolve_blob
    original_download = core.resolve_ob_download
    original_get = core.get
    original_link = links.create_download_link
    original_legacy_update = server.asset_store.update_metadata
    original_embedding = server.asset_embedding_index.index_asset

    def counted_update(*args, **kwargs):
        calls["update"] += 1
        return original_update(*args, **kwargs)

    def fail_metadata(*args, **kwargs):
        calls["metadata"] += 1
        raise AssertionError("post-read forbidden")

    def fail_resolve(*args, **kwargs):
        calls["resolve"] += 1
        raise AssertionError("blob read forbidden")

    def fail_download(*args, **kwargs):
        calls["download"] += 1
        raise AssertionError("download resolver forbidden")

    def fail_get(*args, **kwargs):
        calls["get"] += 1
        raise AssertionError("core get forbidden")

    def fail_link(*args, **kwargs):
        calls["links"] += 1
        raise AssertionError("ticket forbidden")

    def fail_legacy_update(*args, **kwargs):
        calls["legacy_update"] += 1
        raise AssertionError("legacy update forbidden")

    async def fail_embedding(*args, **kwargs):
        calls["embedding"] += 1
        raise AssertionError("legacy embedding forbidden")

    core.update_ob_public_metadata = counted_update
    core.get_ob_public_metadata = fail_metadata
    core.resolve_blob = fail_resolve
    core.resolve_ob_download = fail_download
    core.get = fail_get
    links.create_download_link = fail_link
    server.asset_store.update_metadata = fail_legacy_update
    server.asset_embedding_index.index_asset = fail_embedding
    try:
        raw = await server.rm_asset_update_metadata(
            f" {asset_id} ", title="Updated", description="", tags=[]
        )
    finally:
        core.update_ob_public_metadata = original_update
        core.get_ob_public_metadata = original_metadata
        core.resolve_blob = original_resolve
        core.resolve_ob_download = original_download
        core.get = original_get
        links.create_download_link = original_link
        server.asset_store.update_metadata = original_legacy_update
        server.asset_embedding_index.index_asset = original_embedding

    payload = _assert_success_payload(raw)
    assert payload["asset_id"] == asset_id
    assert payload["title"] == "Updated"
    assert payload["description"] == ""
    assert payload["tags"] == []
    assert calls == {"update": 1, "metadata": 0, "resolve": 0, "download": 0, "get": 0, "links": 0, "legacy_update": 0, "embedding": 0}
    assert server._rm_asset_download_tokens == before_tokens
    assert server._rm_asset_download_sources == before_sources
    after_metadata = original_metadata(asset_id)
    after_blob = original_resolve(asset_id)[1]
    for key in (
        "asset_id", "source_sha256", "stored_sha256", "decoded_bytes", "stored_bytes",
        "filename", "mime_type", "kind", "width", "height", "created_at",
    ):
        assert after_metadata[key] == before_metadata[key]
    assert after_metadata["title"] == "Updated"
    assert after_metadata["description"] == ""
    assert after_metadata["tags"] == []
    assert after_blob == before_blob
    assert server.asset_store.get(legacy_before["asset_id"])["title"] == legacy_before["title"]

    raw = await server.rm_asset_update_metadata(asset_id, title=None, description=None, tags=None)
    payload = _assert_success_payload(raw)
    assert payload["title"] == "Updated"
    assert payload["description"] == ""
    assert payload["tags"] == []


@pytest.mark.asyncio
async def test_enabled_rm_asset_update_metadata_never_falls_back(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch, rm_enabled=True)
    legacy = _persist_legacy(server)
    before_tokens = deepcopy(server._rm_asset_download_tokens)
    before_sources = deepcopy(server._rm_asset_download_sources)
    original_update = server.asset_store.update_metadata
    original_embedding = server.asset_embedding_index.index_asset

    def fail_update(*args, **kwargs):
        raise AssertionError("legacy update must not be called")

    async def fail_embedding(*args, **kwargs):
        raise AssertionError("embedding must not be called")

    server.asset_store.update_metadata = fail_update
    server.asset_embedding_index.index_asset = fail_embedding
    try:
        _error_payload(await server.rm_asset_update_metadata(legacy["asset_id"], title="RM missing"))

        def exploding_presenter(*args, **kwargs):
            raise RuntimeError("private C:/secret title")

        server.remember_me_host_bundle.presenter.rm_asset_update_metadata = exploding_presenter
        raw = await server.rm_asset_update_metadata(legacy["asset_id"], title="x")
        _error_payload(raw)
    finally:
        server.asset_store.update_metadata = original_update
        server.asset_embedding_index.index_asset = original_embedding

    assert server.asset_store.get(legacy["asset_id"])["title"] == legacy["title"]
    assert server._rm_asset_download_tokens == before_tokens
    assert server._rm_asset_download_sources == before_sources


@pytest.mark.parametrize(
    "code",
    [
        "invalid_title",
        "title_too_long",
        "invalid_description",
        "description_too_long",
        "invalid_tags",
        "invalid_tag",
        "too_many_tags",
        "tag_too_long",
    ],
)
def test_presenter_update_metadata_error_allowlist(code):
    core = UpdateCore(error=RememberMeCoreAdapterError("invalid_metadata", ob_code=code))
    links = NullDownloadLinks()
    _error_payload(_presenter(core).rm_asset_update_metadata(ASSET_ID, title="bad"), code)
    assert core.update_calls == [(ASSET_ID, "bad", None, None)]
    assert core.metadata_calls == 0
    assert core.resolve_blob_calls == 0
    assert core.resolve_ob_download_calls == 0
    assert core.get_calls == 0
    assert links.calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "value", "error"),
    [
        ("title", "a" + (" " * 201) + "b", "title_too_long"),
        ("description", "a" + (" " * 4001) + "b", "description_too_long"),
    ],
)
async def test_legacy_metadata_length_prevalidation_preserves_internal_whitespace(
    tmp_path,
    monkeypatch,
    field,
    value,
    error,
):
    server = _load_server(tmp_path, monkeypatch, rm_enabled=True)
    data = _png_bytes()
    asset = server.remember_me_host_bundle.core_adapter.ingest_image(
        data,
        expected_bytes=len(data),
        filename="metadata.png",
        mime_type="image/png",
    )

    raw = await server.rm_asset_update_metadata(asset["asset_id"], **{field: value})

    _error_payload(raw, error)


@pytest.mark.asyncio
async def test_metadata_whitespace_uses_legacy_prevalidation_and_public_rm_normalization(
    tmp_path,
    monkeypatch,
):
    server = _load_server(tmp_path, monkeypatch, rm_enabled=True)
    data = _png_bytes()
    asset = server.remember_me_host_bundle.core_adapter.ingest_image(
        data,
        expected_bytes=len(data),
        filename="metadata.png",
        mime_type="image/png",
    )

    raw = await server.rm_asset_update_metadata(
        asset["asset_id"],
        title="  short   title  ",
        description="\tbrief   description\n",
        tags=["  one   two  "],
    )

    payload = _assert_success_payload(raw)
    assert payload["title"] == "short title"
    assert payload["description"] == "brief description"
    assert payload["tags"] == ["one two"]


@pytest.mark.parametrize(
    "error",
    [
        RememberMeCoreAdapterError("asset_not_found"),
        RememberMeCoreAdapterError("invalid_asset_id"),
        RememberMeCoreAdapterError("repository_failure"),
        RememberMeCoreAdapterError("invalid_metadata"),
        RememberMeCoreAdapterError("invalid_metadata", ob_code="unknown"),
        RuntimeError("private SQL C:/secret"),
    ],
)
def test_presenter_update_metadata_unavailable_errors_do_not_leak(error):
    core = UpdateCore(error=error)
    _error_payload(_presenter(core).rm_asset_update_metadata(ASSET_ID), "asset_unavailable")
    assert len(core.update_calls) == 1
    assert core.metadata_calls == 0
    assert core.resolve_blob_calls == 0
    assert core.resolve_ob_download_calls == 0
    assert core.get_calls == 0


@pytest.mark.parametrize(
    "result",
    [
        None,
        object(),
        {"asset_id": ASSET_ID},
        HostileMapping(_metadata()),
        _metadata(asset_id="not-hex"),
        _metadata(asset_id="b" * 32),
        _metadata(source_sha256="not-hex"),
        _metadata(stored_sha256="ABC" * 22),
        _metadata(decoded_bytes=True),
        _metadata(stored_bytes=False),
        _metadata(width=True),
        _metadata(height=False),
        _metadata(title=object()),
        _metadata(description=object()),
        _metadata(tags="one"),
        _metadata(tags=["one", object()]),
        _metadata(created_at=1000.0),
        _metadata(updated_at=object()),
    ],
)
def test_presenter_update_metadata_rejects_malformed_mutation_results(result):
    core = UpdateCore(result=result)
    raw = _presenter(core).rm_asset_update_metadata(ASSET_ID)
    _error_payload(raw, "asset_unavailable")
    assert len(core.update_calls) == 1
    assert core.metadata_calls == 0
    assert core.resolve_blob_calls == 0
    assert core.resolve_ob_download_calls == 0
    assert core.get_calls == 0


def test_public_contracts_and_stage8fg_isolation_remain(tmp_path):
    server_text = (ROOT / "server.py").read_text(encoding="utf-8")
    get_block = server_text[server_text.index("async def rm_asset_get"):server_text.index("async def rm_asset_update_metadata")]
    update_block = server_text[server_text.index("async def rm_asset_update_metadata"):server_text.index("async def rm_asset_search")]
    search_block = server_text[server_text.index("async def rm_asset_search"):server_text.index("async def rm_asset_reindex_embeddings")]
    download_block = server_text[server_text.index("async def rm_asset_download_link"):server_text.index("@mcp.resource", server_text.index("async def rm_asset_download_link"))]
    view_block = server_text[server_text.index("async def rm_asset_view("):server_text.index("async def rm_asset_inspect")]
    inspect_block = server_text[server_text.index("async def rm_asset_inspect"):server_text.index("@mcp.custom_route", server_text.index("async def rm_asset_inspect"))]
    assert "backend.mcp_get(" in get_block
    assert "backend.mcp_download_link(" in download_block
    assert "backend.mcp_view(" in view_block
    assert "backend.mcp_inspect(" in inspect_block
    assert "backend.mcp_update_metadata(" in update_block
    assert "backend.mcp_search(" in search_block
    assert "backend.update_metadata(" in update_block
    assert "asset_embedding_index.index_asset" in update_block
    assert "backend.search(" in search_block
    assert "asset_embedding_index.search" in search_block
    assert "backend.mcp_update_metadata(" in update_block

    reindex_start = server_text.index("async def rm_asset_reindex_embeddings")
    reindex_stop = server_text.find("\n@mcp.", reindex_start + 1)
    reindex_block = server_text[reindex_start:reindex_stop]
    presenter_call = "await backend.mcp_reindex("
    legacy_call = "await backend.reindex("
    assert presenter_call in reindex_block
    assert legacy_call in reindex_block
    assert reindex_block.index(presenter_call) < reindex_block.index(legacy_call)
    assert "asset_embedding_index" not in reindex_block

    assert "def persist_upload(" in (ROOT / "asset_backend.py").read_text(encoding="utf-8")
    assert server_text.count("@mcp.custom_route") == 37
    assert "OMBRE_RM_UPDATE" not in server_text

    for relative in ("asset_dashboard.py", "asset_viewer.py", "asset_embedding_index.py"):
        text = (ROOT / relative).read_text(encoding="utf-8")
        assert "remember_me_host_bundle" not in text
        assert "remember_me_host_runtime" not in text

    snapshot_path = ROOT / "tests/fixtures/stage8b-ob-rm-mcp-contract.json"
    before_snapshot = snapshot_path.read_bytes()
    snapshot = json.loads(before_snapshot.decode("utf-8"))
    tool = next(item for item in snapshot["tools"] if item["name"] == "rm_asset_update_metadata")
    assert tool["inputSchema"]["properties"] == {
        "asset_id": {"title": "Asset Id", "type": "string"},
        "title": {"anyOf": [{"type": "string"}, {"type": "null"}], "default": None, "title": "Title"},
        "description": {"anyOf": [{"type": "string"}, {"type": "null"}], "default": None, "title": "Description"},
        "tags": {"anyOf": [{"items": {"type": "string"}, "type": "array"}, {"type": "null"}], "default": None, "title": "Tags"},
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

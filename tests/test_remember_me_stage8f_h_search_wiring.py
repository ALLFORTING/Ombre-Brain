import importlib
import json
import os
import subprocess
import sys
from copy import deepcopy
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from remember_me_core_adapter import RememberMeCoreAdapterError
from remember_me_mcp_presenter import RememberMeMcpCompatibilityPresenter
from rm_cutover_test_support import configure_rm_authority, install_fake_rm_backend


ROOT = Path(__file__).resolve().parent.parent
ASSET_ID = "a" * 32
SECOND_ASSET_ID = "b" * 32
SEARCH_KEYS = {"ok", "total", "offset", "limit", "results"}
SEARCH_ITEM_KEYS = {
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
    "match_reasons",
}
SEARCH_ERRORS = (
    "invalid_query",
    "invalid_limit",
    "invalid_offset",
    "invalid_kind",
    "invalid_mime_type",
    "invalid_created_from",
    "invalid_created_to",
    "invalid_date_range",
    "invalid_tags",
    "invalid_tag",
    "tag_too_long",
    "too_many_tags",
)
PRIVATE_FIELDS = (
    "decoded_bytes",
    "source_sha256",
    "stored_sha256",
    "stored_relpath",
    "blob_key",
    "backend",
    "source",
    "path",
    "data_root",
    "download_url",
    "download_path",
)


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


def _persist(server, data=b"asset bytes", filename="asset.bin", mime_type="application/octet-stream"):
    source = server.asset_store.create_temp_path()
    source.write_bytes(data)
    import hashlib

    return server.asset_store.persist_upload(
        source,
        hashlib.sha256(data).hexdigest(),
        len(data),
        filename,
        mime_type,
    )


def _search_item(**overrides):
    payload = {
        "asset_id": ASSET_ID,
        "filename": "asset.png",
        "title": "Search title",
        "description": "Public description",
        "tags": ["one"],
        "kind": "image",
        "mime_type": "image/png",
        "width": 8,
        "height": 6,
        "stored_bytes": 123,
        "created_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-01T00:00:01+00:00",
        "match_reasons": ["title"],
        "decoded_bytes": 456,
        "source_sha256": "1" * 64,
        "stored_sha256": "2" * 64,
        "stored_relpath": "assets/private.png",
        "blob_key": "private-blob",
        "backend": "remember_me",
        "source": "remember_me",
        "path": "C:/secret/file.png",
        "data_root": "C:/secret/root",
        "download_url": "https://secret.invalid/download",
        "download_path": "/secret/download",
    }
    payload.update(overrides)
    return payload


def _search_result(**overrides):
    payload = {
        "total": 1,
        "offset": 0,
        "limit": 20,
        "results": [_search_item()],
    }
    payload.update(overrides)
    return payload


def _payload(raw):
    return json.loads(raw)


def _assert_error(raw, code):
    payload = _payload(raw)
    assert payload == {"error": code, "ok": False}
    assert "C:/secret" not in raw
    assert "Traceback" not in raw
    assert "boom" not in raw
    assert "download" not in payload


class NullDownloadLinks:
    def __init__(self):
        self.calls = 0

    def create_download_link(self, asset):
        self.calls += 1
        raise AssertionError("download links must not be used by search")


DEFAULT_SEARCH_RESULT = object()


class CountingCore:
    def __init__(self, result=DEFAULT_SEARCH_RESULT, error=None):
        self.result = _search_result() if result is DEFAULT_SEARCH_RESULT else result
        self.error = error
        self.search_calls = []
        self.metadata_calls = 0
        self.resolve_blob_calls = 0
        self.resolve_ob_download_calls = 0
        self.get_calls = 0
        self.update_calls = 0

    async def search(self, **kwargs):
        self.search_calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return self.result

    def get_ob_public_metadata(self, asset_id):
        self.metadata_calls += 1
        raise AssertionError("metadata must not be queried by search")

    def resolve_blob(self, asset_id):
        self.resolve_blob_calls += 1
        raise AssertionError("blob must not be read by search")

    def resolve_ob_download(self, asset_id):
        self.resolve_ob_download_calls += 1
        raise AssertionError("download resolver must not be used by search")

    def get(self, asset_id):
        self.get_calls += 1
        raise AssertionError("core get must not be used by search")

    def update_ob_public_metadata(self, *args, **kwargs):
        self.update_calls += 1
        raise AssertionError("core update must not be used by search")


class HostileMapping(dict):
    def __getitem__(self, key):
        raise RuntimeError("private C:/secret detail")

    def get(self, key, default=None):
        raise RuntimeError("private C:/secret detail")


def _presenter(core, links=None):
    return RememberMeMcpCompatibilityPresenter(core, links or NullDownloadLinks())


@pytest.mark.asyncio
async def test_default_off_legacy_search_keeps_lexical_filters_and_pagination(tmp_path, monkeypatch):
    runtime_module_before = sys.modules.get("remember_me_host_runtime")
    server = _load_server(tmp_path, monkeypatch, bad_data_root=True)
    first = _persist(server, b"alpha", "alpha.bin")
    second = _persist(server, b"beta", "beta.bin")
    server.asset_store.update_metadata(first["asset_id"], title="Needle alpha", tags=["Blue"])
    server.asset_store.update_metadata(second["asset_id"], description="Needle beta", tags=["Red"])

    raw = await server.rm_asset_search(query="needle", tags=["Blue"], limit=1, offset=0)
    payload = _payload(raw)

    assert payload["ok"] is True
    assert set(payload) == SEARCH_KEYS
    assert payload["total"] == 1
    assert payload["limit"] == 1
    assert payload["offset"] == 0
    assert payload["results"][0]["asset_id"] == first["asset_id"]
    assert payload["results"][0]["match_reasons"] == ["title_prefix"]
    assert sys.modules.get("remember_me_host_runtime") is runtime_module_before
    assert not (tmp_path / "remember-me-runtime").exists()
    assert server._rm_asset_download_tokens == {}
    assert server._rm_asset_download_sources == {}


@pytest.mark.asyncio
async def test_default_off_legacy_search_keeps_embedding_fallback(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    asset = _persist(server, b"semantic", "semantic.bin")
    server.asset_store.update_metadata(asset["asset_id"], title="Semantic keyword")

    server.embedding_engine.enabled = True
    server.asset_embedding_index.search = AsyncMock(return_value={asset["asset_id"]: 0.75})
    payload = _payload(await server.rm_asset_search(query="semantic"))

    assert payload["ok"] is True
    assert payload["results"][0]["asset_id"] == asset["asset_id"]
    assert "semantic" in payload["results"][0]["match_reasons"]
    assert payload["results"][0]["semantic_score"] == 0.75

    server.asset_embedding_index.search = AsyncMock(side_effect=RuntimeError("private vector outage"))
    failed = _payload(await server.rm_asset_search(query="semantic"))
    assert failed["ok"] is True
    assert failed["results"][0]["asset_id"] == asset["asset_id"]
    assert "private vector outage" not in json.dumps(failed)


@pytest.mark.asyncio
async def test_enabled_search_calls_presenter_once_and_never_legacy(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    core = CountingCore()
    links = NullDownloadLinks()
    install_fake_rm_backend(
        server,
        type(
            "Bundle",
            (),
            {"core_adapter": core, "presenter": _presenter(core, links)},
        )(),
    )
    server.asset_store.search = lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("legacy search"))
    server.asset_store.get = lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("legacy get"))
    server.asset_embedding_index.search = AsyncMock(side_effect=AssertionError("legacy embedding search"))
    server.asset_embedding_index.reindex = AsyncMock(side_effect=AssertionError("legacy reindex"))

    payload = _payload(await server.rm_asset_search(
        query="needle",
        tags=["one"],
        kind="image",
        mime_type="image/png",
        created_from="2026-01-01",
        created_to="2026-01-02",
        limit=20,
        offset=0,
    ))

    assert payload["ok"] is True
    assert set(payload) == SEARCH_KEYS
    assert len(core.search_calls) == 1
    assert core.search_calls[0] == {
        "query": "needle",
        "tags": ["one"],
        "kind": "image",
        "mime_type": "image/png",
        "created_from": "2026-01-01",
        "created_to": "2026-01-02",
        "limit": 20,
        "offset": 0,
    }
    assert core.metadata_calls == 0
    assert core.resolve_blob_calls == 0
    assert core.resolve_ob_download_calls == 0
    assert core.get_calls == 0
    assert core.update_calls == 0
    assert links.calls == 0
    assert server._rm_asset_download_tokens == {}
    assert server._rm_asset_download_sources == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("code", SEARCH_ERRORS)
async def test_presenter_search_maps_allowlisted_errors_without_collaborators(code):
    core = CountingCore(error=RememberMeCoreAdapterError(code))
    links = NullDownloadLinks()

    raw = await _presenter(core, links).rm_asset_search()

    _assert_error(raw, code)
    assert len(core.search_calls) == 1
    assert links.calls == 0
    assert core.metadata_calls == 0
    assert core.resolve_blob_calls == 0
    assert core.resolve_ob_download_calls == 0
    assert core.get_calls == 0
    assert core.update_calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [RememberMeCoreAdapterError("blob_missing"), RuntimeError("boom C:/secret")],
)
async def test_presenter_search_unknown_errors_are_unavailable_without_leakage(error):
    core = CountingCore(error=error)
    raw = await _presenter(core).rm_asset_search()
    _assert_error(raw, "search_unavailable")
    assert len(core.search_calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "result",
    [
        None,
        [],
        HostileMapping(),
        _search_result(total=-1),
        _search_result(total=True),
        _search_result(offset=-1),
        _search_result(offset=True),
        _search_result(limit=0),
        _search_result(limit=True),
        _search_result(limit=51),
        _search_result(limit=20, results=tuple(_search_item(asset_id=("%032x" % index)) for index in range(21))),
        _search_result(results={}),
        _search_result(results=[HostileMapping()]),
        _search_result(results=[_search_item(asset_id="A" * 32)]),
        _search_result(results=[_search_item(asset_id="a" * 31)]),
        _search_result(results=[_search_item(filename=object())]),
        _search_result(results=[_search_item(title=object())]),
        _search_result(results=[_search_item(description=object())]),
        _search_result(results=[_search_item(kind="video")]),
        _search_result(results=[_search_item(mime_type="text/plain")]),
        _search_result(results=[_search_item(width=True)]),
        _search_result(results=[_search_item(height=-1)]),
        _search_result(results=[_search_item(stored_bytes=False)]),
        _search_result(results=[_search_item(tags="one")]),
        _search_result(results=[_search_item(tags=["one", object()])]),
        _search_result(results=[_search_item(match_reasons="title")]),
        _search_result(results=[_search_item(match_reasons=["private_reason"])]),
        _search_result(results=[_search_item(match_reasons=["semantic"])]),
        _search_result(results=[_search_item(match_reasons=["title"], semantic_score=0.5)]),
        _search_result(results=[_search_item(match_reasons=["semantic"], semantic_score=True)]),
        _search_result(results=[_search_item(match_reasons=["semantic"], semantic_score=float("nan"))]),
        _search_result(results=[_search_item(match_reasons=["semantic"], semantic_score=float("inf"))]),
        _search_result(results=[_search_item(match_reasons=["semantic"], semantic_score=-0.1)]),
        _search_result(results=[_search_item(match_reasons=["semantic"], semantic_score=1.1)]),
    ],
)
async def test_presenter_search_rejects_malformed_results(result):
    core = CountingCore(result=result)
    raw = await _presenter(core).rm_asset_search()
    _assert_error(raw, "search_unavailable")
    assert len(core.search_calls) == 1


@pytest.mark.asyncio
async def test_presenter_search_crops_private_fields_and_preserves_order_without_mutation():
    first = _search_item(asset_id=ASSET_ID, match_reasons=["semantic"], semantic_score=1)
    second = _search_item(asset_id=SECOND_ASSET_ID, filename="second.bin", kind="file", mime_type="application/octet-stream", width=0, height=0, match_reasons=[])
    result = _search_result(total=2, results=[first, second])
    original = deepcopy(result)
    core = CountingCore(result=result)

    payload = _payload(await _presenter(core).rm_asset_search())

    assert payload["ok"] is True
    assert payload["total"] == 2
    assert [item["asset_id"] for item in payload["results"]] == [ASSET_ID, SECOND_ASSET_ID]
    assert payload["results"][0]["semantic_score"] == 1.0
    assert "semantic_score" not in payload["results"][1]
    for item in payload["results"]:
        assert set(item) in (SEARCH_ITEM_KEYS, SEARCH_ITEM_KEYS | {"semantic_score"})
        for private in PRIVATE_FIELDS:
            assert private not in item
    raw = json.dumps(payload, ensure_ascii=False)
    assert "C:/secret" not in raw
    assert result == original


@pytest.mark.asyncio
async def test_enabled_search_has_no_legacy_fallback_when_core_has_no_asset(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    legacy = _persist(server, b"legacy bytes", "legacy.bin")
    server.asset_store.update_metadata(legacy["asset_id"], title="Legacy only")
    core = CountingCore(error=RememberMeCoreAdapterError("asset_not_found"))
    install_fake_rm_backend(
        server,
        type(
            "Bundle",
            (),
            {"core_adapter": core, "presenter": _presenter(core)},
        )(),
    )

    raw = await server.rm_asset_search(query="Legacy")

    _assert_error(raw, "search_unavailable")
    assert len(core.search_calls) == 1
    assert server._rm_asset_download_tokens == {}
    assert server._rm_asset_download_sources == {}


@pytest.mark.asyncio
async def test_enabled_handler_unknown_presenter_exception_returns_stable_error(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)

    class Presenter:
        calls = 0

        async def rm_asset_search(self, **kwargs):
            self.calls += 1
            raise RuntimeError("boom C:/secret")

    presenter = Presenter()
    install_fake_rm_backend(
        server,
        type("Bundle", (), {"core_adapter": object(), "presenter": presenter})(),
    )

    raw = await server.rm_asset_search(query="anything")

    _assert_error(raw, "search_unavailable")
    assert presenter.calls == 1
    assert server._rm_asset_download_tokens == {}
    assert server._rm_asset_download_sources == {}


@pytest.mark.asyncio
async def test_core_adapter_preserves_allowlisted_search_errors():
    class Service:
        async def search_assets(self, request):
            raise ValueError("invalid_limit")

    runtime = type("Runtime", (), {"service": Service(), "repository": object(), "blob_store": object()})()
    from remember_me_core_adapter import RememberMeCoreAdapter

    adapter = RememberMeCoreAdapter(runtime)
    with pytest.raises(RememberMeCoreAdapterError) as caught:
        await adapter.search(limit=0)
    assert caught.value.code == "invalid_limit"


def test_public_contracts_and_stage8fh_isolation_remain(tmp_path):
    server_text = (ROOT / "server.py").read_text(encoding="utf-8")
    snapshot_path = ROOT / "tests/fixtures/stage8b-ob-rm-mcp-contract.json"
    before_snapshot = snapshot_path.read_bytes()

    search_start = server_text.index("async def rm_asset_search")
    search_stop = server_text.index("async def rm_asset_reindex_embeddings")
    search_block = server_text[search_start:search_stop]
    assert "backend.mcp_search(" in search_block
    assert "backend.search(" in search_block
    assert "asset_embedding_index.search" in search_block
    rm_search_branch = search_block[
        search_block.index('if backend.name == "rm"'):
        search_block.index("        result = backend.search")
    ]
    assert "backend.search" not in rm_search_branch
    assert "asset_embedding_index" not in rm_search_branch
    assert "embedding_engine" not in rm_search_branch
    assert "_rm_create_asset_download_link" not in search_block
    assert "_rm_asset_download_tokens" not in search_block
    assert "_rm_asset_download_sources" not in search_block

    for handler in (
        "rm_asset_get",
        "rm_asset_download_link",
        "rm_asset_view",
        "rm_asset_inspect",
        "rm_asset_update_metadata",
    ):
        start = server_text.index(f"async def {handler}(")
        stop = server_text.find("\n@mcp.", start + 1)
        if stop == -1:
            stop = len(server_text)
    assert "backend.mcp_" in server_text[start:stop]

    reindex_start = server_text.index("async def rm_asset_reindex_embeddings(")
    reindex_stop = server_text.index("async def rm_asset_download_link", reindex_start)
    reindex_block = server_text[reindex_start:reindex_stop]
    presenter_call = "await backend.mcp_reindex("
    assert presenter_call in reindex_block
    assert reindex_block.index(presenter_call) < reindex_block.index(
        "await backend.reindex("
    )
    assert "asset_embedding_index" not in reindex_block
    assert "RememberMeCoreAdapter" not in reindex_block
    assert "RememberMeMcpCompatibilityPresenter" not in reindex_block

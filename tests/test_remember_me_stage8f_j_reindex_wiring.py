import asyncio
import hashlib
import importlib
import inspect
import io
import json
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from PIL import Image

import remember_me
from remember_me.core import (
    AssetUnavailable,
    InvalidMetadata,
    RememberMeService,
    ReindexEmbeddingsResult,
)
from remember_me.metadata import PROJECT_VERSION
from remember_me.search import NullVectorProvider

from embedding_engine import EmbeddingEngine
from remember_me_adapter import RememberMeAdapter
from remember_me_core_adapter import (
    RememberMeCoreAdapter,
    RememberMeCoreAdapterError,
    RememberMeReindexResult,
)
from remember_me_host_runtime import create_remember_me_host_bundle
from remember_me_mcp_presenter import RememberMeMcpCompatibilityPresenter
from remember_me_vector_provider import RememberMeVectorProviderAdapter


ROOT = Path(__file__).resolve().parent.parent
RM_ROOT = Path(r"D:\Codex\projects\Remember-Me")
RM_SRC = (RM_ROOT / "src").resolve()
RM_COMMIT = "5c430d3f265be059198fe230c1a0682e23e89e32"
ASSET_ID = "a" * 32


class FakeEngine:
    def __init__(
        self,
        *,
        enabled=True,
        base_url="https://API.Example.invalid:443/v1/?token=secret#fragment",
        model=" model-a ",
        api_key="key-a",
        vector=None,
    ):
        self.enabled = enabled
        self.base_url = base_url
        self.model = model
        self.api_key = api_key
        self.vector = [1.0, 0.0] if vector is None else vector
        self.calls = []

    async def embed_text(self, text):
        self.calls.append(text)
        return self.vector


class NullLinks:
    def create_download_link(self, asset):
        raise AssertionError("reindex/search must not create download links")


def _png_bytes():
    output = io.BytesIO()
    image = Image.new("RGB", (4, 3), "green")
    image.save(output, format="PNG")
    image.close()
    return output.getvalue()


def _runtime(service):
    return SimpleNamespace(
        service=service,
        repository=object(),
        blob_store=object(),
    )


def _load_server(tmp_path, monkeypatch):
    monkeypatch.setenv("OMBRE_BUCKETS_DIR", str(tmp_path / "legacy"))
    monkeypatch.setenv("OMBRE_PUBLIC_BASE_URL", "https://example.invalid")
    monkeypatch.delenv("OMBRE_API_KEY", raising=False)
    monkeypatch.delenv("OMBRE_RM_RUNTIME_ENABLED", raising=False)
    monkeypatch.delenv("OMBRE_RM_DATA_ROOT", raising=False)
    sys.modules.pop("server", None)
    return importlib.import_module("server")


def test_target_rm_source_version_and_async_api_provenance():
    module_path = Path(remember_me.__file__).resolve()
    assert module_path.is_relative_to(RM_SRC)
    assert PROJECT_VERSION == "0.1.0.dev5"
    assert inspect.iscoroutinefunction(RememberMeService.search_assets)
    assert inspect.iscoroutinefunction(RememberMeService.reindex_embeddings)
    assert subprocess.check_output(
        ["git", "-C", str(RM_ROOT), "rev-parse", "HEAD"],
        text=True,
    ).strip() == RM_COMMIT


def test_embedding_engine_public_entry_delegates_once():
    engine = object.__new__(EmbeddingEngine)
    engine._generate_embedding = AsyncMock(return_value=[0.25, 0.75])

    result = asyncio.run(engine.embed_text("visible text"))

    assert result == [0.25, 0.75]
    engine._generate_embedding.assert_awaited_once_with("visible text")


def test_provider_identity_is_stable_redacted_and_sensitive_to_inputs():
    first_engine = FakeEngine()
    equivalent_engine = FakeEngine(
        base_url=(
            "https://user:password@api.example.invalid/v1"
            "?different=credential#hidden"
        ),
        api_key="key-b",
    )
    first = RememberMeVectorProviderAdapter(first_engine)
    equivalent = RememberMeVectorProviderAdapter(equivalent_engine)

    assert first.model_id == equivalent.model_id
    assert first.model_id.startswith("ob-openai-compatible:")
    assert first.model_id.endswith(":model-a")
    assert first.model_id
    for secret in (
        "api.example.invalid",
        "user",
        "password",
        "token",
        "secret",
        "key-a",
        "key-b",
    ):
        assert secret not in first.model_id

    changed_endpoint = RememberMeVectorProviderAdapter(
        FakeEngine(base_url="https://other.example.invalid/v1")
    )
    changed_model = RememberMeVectorProviderAdapter(
        FakeEngine(model="model-b")
    )
    changed_backend = RememberMeVectorProviderAdapter(
        FakeEngine(),
        backend="azure_openai",
    )
    assert changed_endpoint.model_id != first.model_id
    assert changed_model.model_id != first.model_id
    assert changed_backend.model_id != first.model_id
    assert changed_backend.model_id.startswith("ob-azure-openai:")


@pytest.mark.asyncio
async def test_provider_embed_delegates_once_and_preserves_empty_vector():
    engine = FakeEngine(vector=[])
    provider = RememberMeVectorProviderAdapter(engine)

    assert provider.enabled is True
    assert await provider.embed("query") == []
    assert engine.calls == ["query"]


@pytest.mark.asyncio
async def test_provider_and_embedding_engine_cancellation_propagate():
    class CancelEngine(FakeEngine):
        async def embed_text(self, text):
            raise asyncio.CancelledError()

    provider = RememberMeVectorProviderAdapter(CancelEngine())
    with pytest.raises(asyncio.CancelledError):
        await provider.embed("query")

    engine = object.__new__(EmbeddingEngine)
    engine._generate_embedding = AsyncMock(side_effect=asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        await engine.embed_text("query")


def test_runtime_factory_injects_exact_provider_and_default_remains_null(tmp_path):
    provider = RememberMeVectorProviderAdapter(FakeEngine())
    owner = RememberMeAdapter()
    runtime = owner.create_runtime(
        tmp_path / "with-provider",
        vector_provider=provider,
    )
    assert runtime.service.vector_provider is provider
    assert owner.create_runtime(
        tmp_path / "with-provider",
        vector_provider=provider,
    ) is runtime

    default_runtime = RememberMeAdapter().create_runtime(
        tmp_path / "without-provider"
    )
    assert isinstance(
        default_runtime.service.vector_provider,
        NullVectorProvider,
    )


def test_host_bundle_shares_one_runtime_and_provider(tmp_path):
    provider = RememberMeVectorProviderAdapter(FakeEngine())
    bundle = create_remember_me_host_bundle(
        data_root=tmp_path / "runtime",
        token_store={},
        ticket_source_store={},
        download_lock=threading.Lock(),
        public_base_url="",
        ttl_seconds=300,
        max_tokens=100,
        vector_provider=provider,
    )

    assert bundle.host_adapter._runtime is bundle.core_adapter._runtime
    assert bundle.core_adapter._runtime.service.vector_provider is provider
    assert bundle.presenter._core is bundle.core_adapter


def test_server_rejects_shared_legacy_and_rm_data_root(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    monkeypatch.setenv("OMBRE_RM_RUNTIME_ENABLED", "true")
    monkeypatch.setenv(
        "OMBRE_RM_DATA_ROOT",
        str(server.asset_store.data_root),
    )

    with pytest.raises(RuntimeError) as caught:
        server._bootstrap_remember_me_host()

    assert str(caught.value) == "remember_me_host_bootstrap_failed"
    assert str(server.asset_store.data_root) not in str(caught.value)


@pytest.mark.asyncio
async def test_core_adapter_search_awaits_service_once():
    result = SimpleNamespace(total=0, offset=0, limit=20, results=())
    service = SimpleNamespace(search_assets=AsyncMock(return_value=result))
    adapter = RememberMeCoreAdapter(_runtime(service))

    assert await adapter.search(query="needle") == {
        "total": 0,
        "offset": 0,
        "limit": 20,
        "results": [],
    }
    service.search_assets.assert_awaited_once()


@pytest.mark.asyncio
async def test_core_adapter_reindex_maps_only_four_counters_and_errors():
    service = SimpleNamespace(
        reindex_embeddings=AsyncMock(
            return_value=ReindexEmbeddingsResult(
                enabled=True,
                model_id="private-model",
                scanned=3,
                indexed=1,
                skipped=1,
                failed=1,
            )
        )
    )
    adapter = RememberMeCoreAdapter(_runtime(service))

    result = await adapter.reindex_embeddings(asset_id=" a ", limit=7)
    assert result == RememberMeReindexResult(3, 1, 1, 1)
    request = service.reindex_embeddings.await_args.args[0]
    assert request.asset_id == "a"
    assert request.limit == 7

    for error, code in (
        (InvalidMetadata("invalid_limit"), "invalid_limit"),
        (AssetUnavailable(), "asset_unavailable"),
        (RuntimeError("private path"), "asset_unavailable"),
    ):
        service.reindex_embeddings = AsyncMock(side_effect=error)
        with pytest.raises(RememberMeCoreAdapterError) as caught:
            await adapter.reindex_embeddings()
        assert caught.value.code == code
        assert "private path" not in str(caught.value)


@pytest.mark.asyncio
async def test_core_adapter_reindex_cancellation_propagates():
    service = SimpleNamespace(
        reindex_embeddings=AsyncMock(side_effect=asyncio.CancelledError())
    )
    adapter = RememberMeCoreAdapter(_runtime(service))
    with pytest.raises(asyncio.CancelledError):
        await adapter.reindex_embeddings()


@pytest.mark.asyncio
async def test_presenter_reindex_exact_legacy_json_and_error_envelopes():
    core = SimpleNamespace(
        get=lambda *_: None,
        get_ob_public_metadata=lambda *_: None,
        update_ob_public_metadata=lambda *_args, **_kwargs: None,
        resolve_blob=lambda *_: None,
        search=AsyncMock(),
        reindex_embeddings=AsyncMock(
            return_value=RememberMeReindexResult(4, 1, 2, 1)
        ),
    )
    presenter = RememberMeMcpCompatibilityPresenter(core, NullLinks())

    raw = await presenter.rm_asset_reindex_embeddings(asset_id="a", limit=9)
    assert raw == (
        '{"failed": 1, "indexed": 1, "ok": true, '
        '"scanned": 4, "skipped": 2}'
    )
    assert "enabled" not in raw
    assert "model_id" not in raw
    core.reindex_embeddings.assert_awaited_once_with(asset_id="a", limit=9)

    core.reindex_embeddings = AsyncMock(
        side_effect=RememberMeCoreAdapterError("invalid_limit")
    )
    assert await presenter.rm_asset_reindex_embeddings(limit=0) == (
        '{"error": "invalid_limit", "ok": false}'
    )
    core.reindex_embeddings = AsyncMock(
        side_effect=RuntimeError("private repository path")
    )
    assert await presenter.rm_asset_reindex_embeddings() == (
        '{"error": "asset_unavailable", "ok": false}'
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "result",
    [
        SimpleNamespace(scanned=True, indexed=1, skipped=0, failed=0),
        SimpleNamespace(scanned=-1, indexed=0, skipped=0, failed=0),
        SimpleNamespace(scanned=2, indexed=1, skipped=0, failed=0),
        SimpleNamespace(scanned=1, indexed="1", skipped=0, failed=0),
    ],
)
async def test_presenter_reindex_rejects_malformed_counters(result):
    core = SimpleNamespace(
        get=lambda *_: None,
        get_ob_public_metadata=lambda *_: None,
        update_ob_public_metadata=lambda *_args, **_kwargs: None,
        resolve_blob=lambda *_: None,
        search=AsyncMock(),
        reindex_embeddings=AsyncMock(return_value=result),
    )
    presenter = RememberMeMcpCompatibilityPresenter(core, NullLinks())
    assert await presenter.rm_asset_reindex_embeddings() == (
        '{"error": "asset_unavailable", "ok": false}'
    )


@pytest.mark.asyncio
async def test_presenter_reindex_cancellation_propagates():
    core = SimpleNamespace(
        get=lambda *_: None,
        get_ob_public_metadata=lambda *_: None,
        update_ob_public_metadata=lambda *_args, **_kwargs: None,
        resolve_blob=lambda *_: None,
        search=AsyncMock(),
        reindex_embeddings=AsyncMock(side_effect=asyncio.CancelledError()),
    )
    presenter = RememberMeMcpCompatibilityPresenter(core, NullLinks())
    with pytest.raises(asyncio.CancelledError):
        await presenter.rm_asset_reindex_embeddings()


@pytest.mark.asyncio
async def test_server_reindex_enabled_calls_presenter_once_without_legacy(
    tmp_path,
    monkeypatch,
):
    server = _load_server(tmp_path, monkeypatch)
    presenter = SimpleNamespace(
        rm_asset_reindex_embeddings=AsyncMock(
            return_value=(
                '{"failed": 0, "indexed": 1, "ok": true, '
                '"scanned": 1, "skipped": 0}'
            )
        )
    )
    server.remember_me_host_bundle = SimpleNamespace(presenter=presenter)
    server.asset_embedding_index.reindex = AsyncMock(
        side_effect=AssertionError("legacy reindex")
    )

    raw = await server.rm_asset_reindex_embeddings(
        asset_id=ASSET_ID,
        limit=3,
    )
    assert json.loads(raw) == {
        "ok": True,
        "scanned": 1,
        "indexed": 1,
        "skipped": 0,
        "failed": 0,
    }
    presenter.rm_asset_reindex_embeddings.assert_awaited_once_with(
        asset_id=ASSET_ID,
        limit=3,
    )
    server.asset_embedding_index.reindex.assert_not_awaited()


@pytest.mark.asyncio
async def test_server_default_off_and_runtime_unavailable_keep_legacy_reindex(
    tmp_path,
    monkeypatch,
):
    server = _load_server(tmp_path, monkeypatch)
    legacy = AsyncMock(
        return_value={
            "scanned": 2,
            "indexed": 1,
            "skipped": 1,
            "failed": 0,
        }
    )
    server.asset_embedding_index.reindex = legacy
    server.remember_me_host_bundle = None

    raw = await server.rm_asset_reindex_embeddings(asset_id=" x ", limit=2)
    assert json.loads(raw) == {
        "ok": True,
        "scanned": 2,
        "indexed": 1,
        "skipped": 1,
        "failed": 0,
    }
    legacy.assert_awaited_once_with(asset_id="x", limit=2)


@pytest.mark.asyncio
async def test_real_rm_reindex_search_closure_and_staleness(tmp_path):
    engine = FakeEngine()
    provider = RememberMeVectorProviderAdapter(engine)
    owner = RememberMeAdapter()
    core = RememberMeCoreAdapter.from_host_adapter(
        owner,
        tmp_path / "rm-runtime",
        vector_provider=provider,
    )
    presenter = RememberMeMcpCompatibilityPresenter(core, NullLinks())
    content = _png_bytes()
    asset = core.ingest_image(
        content,
        len(content),
        "semantic.png",
        "image/png",
        title="Visible title",
    )

    first = json.loads(await presenter.rm_asset_reindex_embeddings())
    assert first == {
        "ok": True,
        "scanned": 1,
        "indexed": 1,
        "skipped": 0,
        "failed": 0,
    }
    second = json.loads(await presenter.rm_asset_reindex_embeddings())
    assert second["skipped"] == 1

    search = json.loads(await presenter.rm_asset_search(query="unrelated phrase"))
    assert search["ok"] is True
    assert search["results"][0]["asset_id"] == asset["asset_id"]
    assert search["results"][0]["semantic_score"] == 1.0

    core.update_metadata(asset["asset_id"], description="changed")
    changed = json.loads(await presenter.rm_asset_reindex_embeddings())
    assert changed["indexed"] == 1
    engine.model = "model-b"
    model_changed = json.loads(await presenter.rm_asset_reindex_embeddings())
    assert model_changed["indexed"] == 1

    core.update_metadata(
        asset["asset_id"],
        title="",
        description="",
        tags=[],
    )
    empty = json.loads(await presenter.rm_asset_reindex_embeddings())
    assert empty["skipped"] == 1
    assert core._runtime.repository.get_embedding(asset["asset_id"]) is None


@pytest.mark.asyncio
async def test_disabled_provider_is_keyword_only_and_reindex_reports_failure(tmp_path):
    engine = FakeEngine(enabled=False)
    provider = RememberMeVectorProviderAdapter(engine)
    core = RememberMeCoreAdapter.from_host_adapter(
        RememberMeAdapter(),
        tmp_path / "rm-runtime",
        vector_provider=provider,
    )
    content = _png_bytes()
    asset = core.ingest_image(
        content,
        len(content),
        "keyword.png",
        "image/png",
        title="Keyword title",
    )

    reindex = await core.reindex_embeddings()
    assert reindex == RememberMeReindexResult(1, 0, 0, 1)
    result = await core.search(query="Keyword")
    assert result["results"][0]["asset_id"] == asset["asset_id"]
    assert "semantic_score" not in result["results"][0]
    assert engine.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", [True, "10", 0, 501])
async def test_reindex_invalid_limits_keep_exact_error_envelope(tmp_path, limit):
    core = RememberMeCoreAdapter.from_host_adapter(
        RememberMeAdapter(),
        tmp_path / "rm-runtime",
        vector_provider=RememberMeVectorProviderAdapter(FakeEngine()),
    )
    presenter = RememberMeMcpCompatibilityPresenter(core, NullLinks())
    assert await presenter.rm_asset_reindex_embeddings(limit=limit) == (
        '{"error": "invalid_limit", "ok": false}'
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("asset_id", ["f" * 32, "bad-id", object()])
async def test_reindex_missing_or_invalid_asset_is_safely_unavailable(
    tmp_path,
    asset_id,
):
    core = RememberMeCoreAdapter.from_host_adapter(
        RememberMeAdapter(),
        tmp_path / "rm-runtime",
        vector_provider=RememberMeVectorProviderAdapter(FakeEngine()),
    )
    presenter = RememberMeMcpCompatibilityPresenter(core, NullLinks())
    assert await presenter.rm_asset_reindex_embeddings(asset_id=asset_id) == (
        '{"error": "asset_unavailable", "ok": false}'
    )


@pytest.mark.asyncio
async def test_core_adapter_search_cancellation_propagates():
    service = SimpleNamespace(
        search_assets=AsyncMock(side_effect=asyncio.CancelledError())
    )
    adapter = RememberMeCoreAdapter(_runtime(service))
    with pytest.raises(asyncio.CancelledError):
        await adapter.search(query="cancel")


@pytest.mark.asyncio
@pytest.mark.parametrize("race", ["metadata", "delete"])
async def test_real_rm_reindex_metadata_and_delete_races_count_failed(
    tmp_path,
    race,
):
    engine = FakeEngine()
    provider = RememberMeVectorProviderAdapter(engine)
    core = RememberMeCoreAdapter.from_host_adapter(
        RememberMeAdapter(),
        tmp_path / race,
        vector_provider=provider,
    )
    content = _png_bytes()
    asset = core.ingest_image(
        content,
        len(content),
        "race.png",
        "image/png",
        title="Before race",
    )

    async def race_embed(text):
        engine.calls.append(text)
        if race == "metadata":
            core.update_metadata(asset["asset_id"], title="After race")
        else:
            core.delete(asset["asset_id"])
        return [1.0, 0.0]

    engine.embed_text = race_embed
    result = await core.reindex_embeddings(asset_id=asset["asset_id"])
    assert result == RememberMeReindexResult(1, 0, 0, 1)
    assert core._runtime.repository.get_embedding(asset["asset_id"]) is None


@pytest.mark.asyncio
async def test_real_rm_batch_failure_isolated_and_counters_balance(tmp_path):
    class PerItemEngine(FakeEngine):
        async def embed_text(self, text):
            self.calls.append(text)
            return [] if len(self.calls) == 1 else [1.0, 0.0]

    engine = PerItemEngine()
    core = RememberMeCoreAdapter.from_host_adapter(
        RememberMeAdapter(),
        tmp_path / "rm-runtime",
        vector_provider=RememberMeVectorProviderAdapter(engine),
    )
    first = _png_bytes()
    second_output = io.BytesIO()
    second_image = Image.new("RGB", (5, 4), "blue")
    second_image.save(second_output, format="PNG")
    second_image.close()
    second = second_output.getvalue()
    core.ingest_image(
        first,
        len(first),
        "first.png",
        "image/png",
        title="First",
    )
    core.ingest_image(
        second,
        len(second),
        "second.png",
        "image/png",
        title="Second",
    )

    result = await core.reindex_embeddings(limit=10)
    assert result.scanned == result.indexed + result.skipped + result.failed
    assert (result.scanned, result.indexed, result.skipped, result.failed) == (
        2,
        1,
        0,
        1,
    )


@pytest.mark.asyncio
async def test_rm_search_before_reindex_does_not_consume_legacy_vectors(tmp_path):
    engine = FakeEngine()
    core = RememberMeCoreAdapter.from_host_adapter(
        RememberMeAdapter(),
        tmp_path / "rm-runtime",
        vector_provider=RememberMeVectorProviderAdapter(engine),
    )
    content = _png_bytes()
    asset = core.ingest_image(
        content,
        len(content),
        "not-indexed.png",
        "image/png",
        title="Keyword only",
    )

    before = await core.search(query="Keyword")
    assert before["results"][0]["asset_id"] == asset["asset_id"]
    assert "semantic_score" not in before["results"][0]
    assert core._runtime.repository.get_embedding(asset["asset_id"]) is None

    await core.reindex_embeddings(asset_id=asset["asset_id"])
    after = await core.search(query="unrelated semantic query")
    assert after["results"][0]["asset_id"] == asset["asset_id"]
    assert after["results"][0]["semantic_score"] == 1.0


def test_static_ownership_has_no_migration_dual_write_or_schema_change():
    server_text = (ROOT / "server.py").read_text(encoding="utf-8")
    reindex_start = server_text.index("async def rm_asset_reindex_embeddings(")
    reindex_stop = server_text.index("async def rm_asset_download_link", reindex_start)
    reindex = server_text[reindex_start:reindex_stop]
    enabled = reindex[:reindex.index(
        "    try:\n        result = await asset_embedding_index.reindex"
    )]

    assert (
        "await remember_me_host_bundle.presenter."
        "rm_asset_reindex_embeddings"
    ) in enabled
    assert "asset_embedding_index" not in enabled
    assert "repository" not in enabled
    assert "embedding_engine" not in enabled
    assert "await asset_embedding_index.reindex" in reindex

    combined = "\n".join(
        (ROOT / name).read_text(encoding="utf-8")
        for name in (
            "remember_me_core_adapter.py",
            "remember_me_mcp_presenter.py",
            "remember_me_host_runtime.py",
            "remember_me_vector_provider.py",
        )
    )
    for forbidden in (
        "ALTER TABLE",
        "legacy embedding",
        "shadow write",
        "dual write",
        "asset_embedding_index",
    ):
        assert forbidden not in combined


def test_nine_tool_names_order_and_input_schema_fixture_are_unchanged():
    expected = (
        "rm_asset_upload_link",
        "rm_asset_upload_status",
        "rm_asset_get",
        "rm_asset_update_metadata",
        "rm_asset_reindex_embeddings",
        "rm_asset_search",
        "rm_asset_download_link",
        "rm_asset_view",
        "rm_asset_inspect",
    )
    from remember_me_adapter import EXPECTED_MCP_TOOLS

    assert EXPECTED_MCP_TOOLS == expected

    server_text = (ROOT / "server.py").read_text(encoding="utf-8")
    markers = {
        "rm_asset_upload_link": 'source = "remember_me"',
        "rm_asset_upload_status": 'source = "remember_me"',
        "rm_asset_get": "remember_me_host_bundle.presenter.rm_asset_get",
        "rm_asset_update_metadata": (
            "remember_me_host_bundle.presenter.rm_asset_update_metadata"
        ),
        "rm_asset_reindex_embeddings": (
            "await remember_me_host_bundle.presenter."
            "rm_asset_reindex_embeddings"
        ),
        "rm_asset_search": (
            "await remember_me_host_bundle.presenter.rm_asset_search"
        ),
        "rm_asset_download_link": (
            "remember_me_host_bundle.presenter.rm_asset_download_link"
        ),
        "rm_asset_view": "remember_me_host_bundle.presenter.rm_asset_view",
        "rm_asset_inspect": (
            "remember_me_host_bundle.presenter.rm_asset_inspect"
        ),
    }
    for name, marker in markers.items():
        start = server_text.index("async def {}(".format(name))
        stop = server_text.find("\n@mcp.", start + 1)
        if stop == -1:
            stop = len(server_text)
        assert marker in server_text[start:stop]

    fixture = json.loads(
        (ROOT / "tests/fixtures/stage8b-ob-rm-mcp-contract.json").read_text(
            encoding="utf-8"
        )
    )
    assert "rm_asset_reindex_embeddings" in json.dumps(fixture)
    assert "rm_asset_search" in json.dumps(fixture)

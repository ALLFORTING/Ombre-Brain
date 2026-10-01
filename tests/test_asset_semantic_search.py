import asyncio
import hashlib
import importlib
import json
import sqlite3
import sys
from unittest.mock import AsyncMock

import pytest

from asset_embedding_index import AssetEmbeddingIndex
from asset_store import AssetStore
from embedding_engine import EmbeddingEngine


QUERY = "找一下验证 GPS 清理的图片"


def _persist(store, data, filename="asset.bin"):
    source = store.create_temp_path()
    source.write_bytes(data)
    return store.persist_upload(
        source,
        hashlib.sha256(data).hexdigest(),
        len(data),
        filename,
        "application/octet-stream",
    )


def _engine_config(root, model="fake-embedding-v1"):
    return {
        "buckets_dir": str(root),
        "dehydration": {},
        "embedding": {
            "enabled": False,
            "api_key": "",
            "model": model,
        },
    }


def _fake_vector(text, *, model=None):
    normalized = text.casefold()
    if text == QUERY or "定位信息" in text or "gps cleanup" in normalized:
        return [1.0, 0.0, 0.0]
    if "exact keyword" in normalized:
        return [0.9, 0.1, 0.0]
    return [0.0, 1.0, 0.0]


def _fake_engine(root, model="fake-embedding-v1"):
    engine = EmbeddingEngine(_engine_config(root, model=model))
    engine.enabled = True
    engine.model = model
    engine._generate_embedding = AsyncMock(side_effect=_fake_vector)
    return engine


def _load_server(tmp_path, monkeypatch):
    monkeypatch.setenv("OMBRE_BUCKETS_DIR", str(tmp_path / "buckets"))
    monkeypatch.delenv("OMBRE_API_KEY", raising=False)
    monkeypatch.delenv("OMBRE_EMBEDDING_API_KEY", raising=False)
    sys.modules.pop("server", None)
    return importlib.import_module("server")


@pytest.mark.asyncio
async def test_asset_and_bucket_embedding_namespaces_are_isolated(tmp_path):
    root = tmp_path / "data"
    store = AssetStore(root)
    engine = _fake_engine(root)
    index = AssetEmbeddingIndex(store, engine)
    asset = _persist(store, b"asset", "gps-photo.bin")
    asset = store.update_metadata(
        asset["asset_id"],
        title="GPS cleanup evidence",
    )

    engine._store_embedding("bucket-only", [1.0, 0.0, 0.0])
    assert await index.index_asset(asset) == "indexed"

    with sqlite3.connect(store.db_path) as conn:
        asset_rows = conn.execute(
            "SELECT asset_id FROM asset_embeddings"
        ).fetchall()
    with sqlite3.connect(engine.db_path) as conn:
        bucket_rows = conn.execute(
            "SELECT bucket_id FROM embeddings"
        ).fetchall()

    assert asset_rows == [(asset["asset_id"],)]
    assert bucket_rows == [("bucket-only",)]
    assert await engine.search_similar(QUERY) == [("bucket-only", 1.0)]
    assert await index.search(QUERY) == {asset["asset_id"]: 1.0}


@pytest.mark.asyncio
async def test_deleted_asset_cannot_leave_a_searchable_embedding(tmp_path):
    store = AssetStore(tmp_path / "data")
    engine = _fake_engine(store.data_root)
    index = AssetEmbeddingIndex(store, engine)
    asset = _persist(store, b"delete", "delete.bin")
    asset = store.update_metadata(asset["asset_id"], title="GPS cleanup")
    assert await index.index_asset(asset) == "indexed"

    with index._connect() as conn:
        conn.execute("DELETE FROM assets WHERE asset_id = ?", (asset["asset_id"],))
    assert index._existing(asset["asset_id"]) is None
    assert await index.search(QUERY) == {}

def test_asset_index_text_is_stable_and_metadata_only(tmp_path):
    store = AssetStore(tmp_path / "data")
    asset = _persist(store, b"private bytes", "camera.jpg")
    asset = store.update_metadata(
        asset["asset_id"],
        title="GPS verification",
        description="Location metadata was removed",
        tags=["Privacy", "JPEG"],
    )
    text = AssetEmbeddingIndex.build_index_text(asset)

    assert text == (
        "Title: GPS verification\n"
        "Description: Location metadata was removed\n"
        "Tags: JPEG, Privacy\n"
        "Filename: camera.jpg\n"
        "Kind: file\n"
        "MIME type: application/octet-stream"
    )
    for forbidden in (
        "stored_relpath",
        asset["stored_sha256"],
        str(store.data_root),
        "private bytes",
        "base64",
    ):
        assert forbidden not in text


@pytest.mark.asyncio
async def test_metadata_update_refreshes_once_and_failure_does_not_rollback(
    tmp_path,
    monkeypatch,
):
    server = _load_server(tmp_path, monkeypatch)
    server.embedding_engine.enabled = True
    server.embedding_engine.model = "fake-embedding-v1"
    generate = AsyncMock(side_effect=_fake_vector)
    server.embedding_engine._generate_embedding = generate
    asset = _persist(server.asset_store, b"metadata refresh", "refresh.bin")

    first = json.loads(
        await server.rm_asset_update_metadata(
            asset["asset_id"],
            title="GPS cleanup evidence",
            tags=["Privacy"],
        )
    )
    second = json.loads(
        await server.rm_asset_update_metadata(
            asset["asset_id"],
            title="GPS cleanup evidence",
            tags=["privacy"],
        )
    )
    assert first["ok"] is True
    assert second["ok"] is True
    assert generate.await_count == 1
    old_index = server.asset_embedding_index._existing(asset['asset_id'])

    server.embedding_engine._generate_embedding = AsyncMock(
        side_effect=RuntimeError("safe fake failure")
    )
    failed_index = json.loads(
        await server.rm_asset_update_metadata(
            asset["asset_id"],
            description="Metadata update still commits",
        )
    )
    assert failed_index["ok"] is True
    assert failed_index["description"] == "Metadata update still commits"
    assert server.asset_store.get(asset["asset_id"])["description"] == (
        "Metadata update still commits"
    )
    assert server.asset_embedding_index._existing(asset["asset_id"]) == old_index
    assert not server.asset_embedding_index.is_current(asset)


@pytest.mark.asyncio
async def test_semantic_search_finds_gps_asset_and_keeps_keyword_first(tmp_path):
    store = AssetStore(tmp_path / "data")
    engine = _fake_engine(store.data_root)
    index = AssetEmbeddingIndex(store, engine)

    semantic = _persist(store, b"semantic", "privacy-photo.bin")
    semantic = store.update_metadata(
        semantic["asset_id"],
        title="照片隐私处理验收",
        description="定位信息和相机元数据已经移除",
        tags=["隐私", "照片"],
    )
    exact = _persist(store, b"exact", "exact.bin")
    exact = store.update_metadata(
        exact["asset_id"],
        title=QUERY,
    )
    unrelated = _persist(store, b"unrelated", "unrelated.bin")
    unrelated = store.update_metadata(
        unrelated["asset_id"],
        title="普通文档",
        description="完全不同的内容",
    )
    for asset in (semantic, exact, unrelated):
        assert await index.index_asset(asset) == "indexed"

    scores = await index.search(QUERY)
    assert semantic["asset_id"] in scores
    assert unrelated["asset_id"] not in scores

    result = store.search(query=QUERY, semantic_scores=scores)
    assert [item["asset_id"] for item in result["results"]] == [
        exact["asset_id"],
        semantic["asset_id"],
    ]
    assert "title_exact" in result["results"][0]["match_reasons"]
    assert result["results"][1]["match_reasons"] == ["semantic"]
    assert result["results"][1]["semantic_score"] == 1.0
    assert result["total"] == 2


@pytest.mark.asyncio
async def test_semantic_and_keyword_results_merge_without_duplicates(tmp_path):
    store = AssetStore(tmp_path / "data")
    engine = _fake_engine(store.data_root)
    index = AssetEmbeddingIndex(store, engine)
    asset = _persist(store, b"both", "gps-cleanup.bin")
    asset = store.update_metadata(
        asset["asset_id"],
        title="GPS cleanup report",
        description="定位信息已移除",
    )
    await index.index_asset(asset)

    scores = await index.search("GPS cleanup")
    result = store.search(query="GPS cleanup", semantic_scores=scores)
    assert result["total"] == 1
    assert result["results"][0]["asset_id"] == asset["asset_id"]
    assert "title_prefix" in result["results"][0]["match_reasons"]
    assert "semantic" in result["results"][0]["match_reasons"]


@pytest.mark.asyncio
async def test_embedding_disabled_or_failed_falls_back_to_keyword_search(
    tmp_path,
    monkeypatch,
):
    server = _load_server(tmp_path, monkeypatch)
    asset = _persist(server.asset_store, b"fallback", "fallback.bin")
    server.asset_store.update_metadata(asset["asset_id"], title="Keyword fallback")

    server.embedding_engine.enabled = False
    disabled = json.loads(await server.rm_asset_search(query="keyword"))
    assert disabled["total"] == 1
    assert disabled["results"][0]["asset_id"] == asset["asset_id"]

    server.embedding_engine.enabled = True
    server.embedding_engine.model = "fake-embedding-v1"
    server.embedding_engine._generate_embedding = AsyncMock(
        side_effect=RuntimeError("fake outage")
    )
    failed = json.loads(await server.rm_asset_search(query="keyword"))
    assert failed["total"] == 1
    assert failed["results"][0]["asset_id"] == asset["asset_id"]


@pytest.mark.asyncio
async def test_single_batch_skip_and_model_change_reindex(tmp_path):
    store = AssetStore(tmp_path / "data")
    engine = _fake_engine(store.data_root)
    index = AssetEmbeddingIndex(store, engine)
    first = _persist(store, b"first", "first.bin")
    first = store.update_metadata(first["asset_id"], title="First indexed asset")
    second = _persist(store, b"second", "second.bin")
    second = store.update_metadata(second["asset_id"], tags=["Second"])
    empty = _persist(store, b"empty", "empty.bin")

    assert await index.reindex(first["asset_id"]) == {
        "scanned": 1,
        "indexed": 1,
        "skipped": 0,
        "failed": 0,
    }
    assert await index.reindex(first["asset_id"]) == {
        "scanned": 1,
        "indexed": 0,
        "skipped": 1,
        "failed": 0,
    }
    engine.model = "fake-embedding-v2"
    rebuilt = await index.reindex(first["asset_id"])
    assert rebuilt["indexed"] == 1

    batch = await index.reindex(limit=10)
    assert batch["scanned"] == 3
    assert batch["indexed"] == 1
    assert batch["skipped"] == 2
    assert batch["failed"] == 0
    with sqlite3.connect(store.db_path) as conn:
        rows = conn.execute(
            "SELECT asset_id, model FROM asset_embeddings ORDER BY asset_id"
        ).fetchall()
    assert rows == sorted(
        [
            (first["asset_id"], "fake-embedding-v2"),
            (second["asset_id"], "fake-embedding-v2"),
        ]
    )
    assert empty["asset_id"] not in {row[0] for row in rows}


@pytest.mark.asyncio
async def test_reindex_tool_and_search_response_do_not_leak_vectors(
    tmp_path,
    monkeypatch,
):
    server = _load_server(tmp_path, monkeypatch)
    server.embedding_engine.enabled = True
    server.embedding_engine.model = "fake-embedding-v1"
    server.embedding_engine._generate_embedding = AsyncMock(
        side_effect=_fake_vector
    )
    asset = _persist(server.asset_store, b"secret file bytes", "safe.bin")
    server.asset_store.update_metadata(
        asset["asset_id"],
        description="定位信息已经移除",
    )

    reindex_text = await server.rm_asset_reindex_embeddings(
        asset_id=asset["asset_id"]
    )
    search_text = await server.rm_asset_search(query=QUERY)
    reindex = json.loads(reindex_text)
    search = json.loads(search_text)
    assert reindex == {
        "ok": True,
        "scanned": 1,
        "indexed": 1,
        "skipped": 0,
        "failed": 0,
    }
    assert search["results"][0]["asset_id"] == asset["asset_id"]

    combined = reindex_text + search_text
    for forbidden in (
        "embedding",
        "api_key",
        "stored_relpath",
        "secret file bytes",
        "data_base64",
        str(tmp_path),
    ):
        assert forbidden not in combined.casefold()

    invalid_limit = json.loads(await server.rm_asset_reindex_embeddings(limit=0))
    missing = json.loads(
        await server.rm_asset_reindex_embeddings(asset_id="f" * 32)
    )
    assert invalid_limit == {"ok": False, "error": "invalid_limit"}
    assert missing == {"ok": False, "error": "asset_unavailable"}

async def _indexed_case(tmp_path):
    store = AssetStore(tmp_path / "data")
    engine = _fake_engine(store.data_root)
    index = AssetEmbeddingIndex(store, engine)
    asset = store.update_metadata(_persist(store, b"race")["asset_id"], title="GPS cleanup old")
    assert await index.index_asset(asset) == "indexed"
    return store, engine, index, asset, index._existing(asset["asset_id"])


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["cancel", "exception", "empty", "nan", "infinity", "bool", "zero", "huge"])
async def test_replacement_failure_preserves_exact_old_row(tmp_path, failure):
    store, engine, index, asset, old = await _indexed_case(tmp_path)
    asset = store.update_metadata(asset["asset_id"], title="changed keyword")
    async def provider(*args, **kwargs):
        # A second connection can acquire the SQLite writer while provider runs.
        with sqlite3.connect(store.db_path, timeout=0) as conn:
            conn.execute("BEGIN IMMEDIATE")
        assert index._lock.acquire(blocking=False)
        index._lock.release()
        assert index.write_coordinator.status().active_writers == 0
        if failure == "cancel":
            raise asyncio.CancelledError()
        if failure == "exception":
            raise RuntimeError("synthetic")
        return {"empty": [], "nan": [float("nan")], "infinity": [float("inf")],
                "bool": [True], "zero": [0.0], "huge": [10 ** 400]}[failure]
    engine._generate_embedding = provider
    if failure == "cancel":
        with pytest.raises(asyncio.CancelledError):
            await index.index_asset(asset)
    else:
        assert await index.index_asset(asset) == "failed"
    assert index._existing(asset["asset_id"]) == old
    assert not index.is_current(asset)
    engine._generate_embedding = AsyncMock(return_value=[1., 0., 0.])
    assert await index.search(QUERY) == {}
    result = store.search(query="changed", semantic_scores=await index.search(QUERY))
    assert result["total"] == 1
    assert "semantic" not in result["results"][0]["match_reasons"]


@pytest.mark.asyncio
@pytest.mark.parametrize("race", ["metadata", "tags", "model", "delete", "reindex"])
async def test_conditional_replacement_rejects_delayed_provider(tmp_path, race):
    store, engine, index, asset, old = await _indexed_case(tmp_path)
    asset = store.update_metadata(asset["asset_id"], title="GPS cleanup replacement")
    async def provider(*args, **kwargs):
        if race == "metadata":
            store.update_metadata(asset["asset_id"], description="newer metadata")
        elif race == "tags":
            store.update_metadata(asset["asset_id"], tags=["newer tag"])
        elif race == "model":
            engine.model = "newer model"
        elif race == "delete":
            store.delete(asset["asset_id"])
        else:
            other = AssetEmbeddingIndex(store, _fake_engine(store.data_root))
            assert await other.index_asset(asset) == "indexed"
        return [0., 1., 0.]
    engine._generate_embedding = provider
    assert await index.index_asset(asset) == ("skipped" if race == "reindex" else "failed")
    if race == "delete":
        assert index._existing(asset["asset_id"]) is None
        assert store.get(asset["asset_id"]) is None
    elif race == "reindex":
        assert json.loads(index._existing(asset["asset_id"])["embedding"]) == [1., 0., 0.]
    else:
        assert index._existing(asset["asset_id"]) == old


@pytest.mark.asyncio
@pytest.mark.parametrize("race", ["metadata", "tags", "model", "index", "delete"])
async def test_final_merge_rechecks_binding_and_preserves_keyword(tmp_path, race):
    store, engine, index, asset, old = await _indexed_case(tmp_path)
    scores = await index.search(QUERY)
    assert scores
    if race == "metadata":
        store.update_metadata(asset["asset_id"], description="later")
    elif race == "tags":
        store.update_metadata(asset["asset_id"], tags=["later"])
    elif race == "model":
        engine.model = "later"
    elif race == "index":
        index.delete(asset["asset_id"])
    else:
        store.delete(asset["asset_id"])
    merged = store.search(query="GPS", semantic_scores=scores, limit=1)
    assert merged["total"] == (0 if race == "delete" else 1)
    if merged["results"]:
        assert "semantic" not in merged["results"][0]["match_reasons"]
        assert merged["results"][0].get("semantic_score", 0) == 0


@pytest.mark.asyncio
async def test_commit_failure_rolls_back_replacement(tmp_path, monkeypatch):
    store, engine, index, asset, old = await _indexed_case(tmp_path)
    asset = store.update_metadata(asset["asset_id"], title="replacement")
    original = index._connect
    class CommitFailure:
        def __init__(self):
            self.conn = original()
            self.writer = False
        def __enter__(self):
            self.conn.__enter__()
            return self
        def execute(self, sql, *args):
            self.writer |= sql == "BEGIN IMMEDIATE"
            return self.conn.execute(sql, *args)
        def __exit__(self, kind, value, tb):
            if self.writer and kind is None:
                self.conn.rollback()
                self.conn.close()
                raise sqlite3.OperationalError("synthetic commit failure")
            return self.conn.__exit__(kind, value, tb)
    monkeypatch.setattr(index, "_connect", CommitFailure)
    with pytest.raises(sqlite3.OperationalError, match="commit failure"):
        await index.index_asset(asset)
    monkeypatch.setattr(index, "_connect", original)
    assert index._existing(asset["asset_id"]) == old


@pytest.mark.asyncio
async def test_cross_process_replacement_wins_over_delayed_provider(tmp_path):
    import os
    import subprocess
    store, engine, index, asset, old = await _indexed_case(tmp_path)
    asset = store.update_metadata(asset["asset_id"], title="GPS cleanup replacement")
    async def provider(*args, **kwargs):
        script = (
            "import asyncio, os; print('SYNTHETIC_CHILD_PID', os.getpid()); from asset_store import AssetStore; "
            "from asset_embedding_index import AssetEmbeddingIndex; "
            "from types import SimpleNamespace; "
            "from unittest.mock import AsyncMock; "
            "s=AssetStore(" + repr(str(store.data_root)) + "); "
            "e=SimpleNamespace(model='fake-embedding-v1',enabled=True,"
            "_generate_embedding=AsyncMock(return_value=[1.,0.,0.])); "
            "i=AssetEmbeddingIndex(s,e); "
            "assert asyncio.run(i.index_asset(s.get(" + repr(asset["asset_id"]) + ")))=='indexed'"
        )
        completed = subprocess.run([sys.executable, "-B", "-c", script],
                                   capture_output=True, text=True, timeout=15, env=os.environ.copy())
        assert completed.returncode == 0, completed.stderr
        print(completed.stdout.strip())
        return [0., 1., 0.]
    engine._generate_embedding = provider
    assert await index.index_asset(asset) == "skipped"
    assert json.loads(index._existing(asset["asset_id"])["embedding"]) == [1., 0., 0.]


@pytest.mark.asyncio
async def test_empty_metadata_clears_index_and_disabled_provider_preserves_stale(tmp_path):
    store, engine, index, asset, old = await _indexed_case(tmp_path)
    asset = store.update_metadata(asset["asset_id"], title="changed")
    engine.enabled = False
    assert await index.index_asset(asset) == "failed"
    assert index._existing(asset["asset_id"]) == old
    asset = store.update_metadata(asset["asset_id"], title="", description="", tags=[])
    assert await index.index_asset(asset) == "skipped"
    assert index._existing(asset["asset_id"]) is None
    assert index.is_current(asset)


@pytest.mark.asyncio
async def test_same_connection_commit_reads_change_at_begin_immediate(tmp_path, monkeypatch):
    store, engine, index, asset, old = await _indexed_case(tmp_path)
    asset = store.update_metadata(asset["asset_id"], title="replacement")
    original = index._connect
    class CommitRace:
        def __init__(self):
            self.conn = original()
        def __enter__(self):
            self.conn.__enter__()
            return self
        def execute(self, sql, *args):
            if sql == "BEGIN IMMEDIATE":
                store.update_metadata(asset["asset_id"], tags=["newer just before commit"])
            return self.conn.execute(sql, *args)
        def __exit__(self, *args):
            return self.conn.__exit__(*args)
    monkeypatch.setattr(index, "_connect", CommitRace)
    assert await index.index_asset(asset) == "failed"
    monkeypatch.setattr(index, "_connect", original)
    assert index._existing(asset["asset_id"]) == old


@pytest.mark.asyncio
async def test_final_merge_uses_one_read_snapshot(tmp_path, monkeypatch):
    import threading
    store, engine, index, asset, old = await _indexed_case(tmp_path)
    scores = await index.search(QUERY)
    original = store._tags_for_assets
    staged = threading.Event()
    def concurrent_write():
        with sqlite3.connect(store.db_path) as conn:
            conn.execute('BEGIN IMMEDIATE')
            conn.execute('UPDATE assets SET title = ? WHERE asset_id = ?',
                         ('later committed', asset['asset_id']))
            staged.set()
    writer = threading.Thread(target=concurrent_write)
    def read_then_concurrent_update(conn, identities):
        tags = original(conn, identities)
        # The default rollback journal delays its commit until this read ends.
        writer.start()
        assert staged.wait(5)
        return tags
    monkeypatch.setattr(store, "_tags_for_assets", read_then_concurrent_update)
    merged = store.search(query="GPS", semantic_scores=scores)
    assert merged["total"] == 1
    assert merged["results"][0]["title"] == "GPS cleanup old"
    assert merged["results"][0]["semantic_score"] == 1.
    writer.join(5)
    assert not writer.is_alive()
    monkeypatch.setattr(store, "_tags_for_assets", original)
    assert not index.is_current(store.get(asset["asset_id"]))
    assert await index.search(QUERY) == {}


@pytest.mark.asyncio
async def test_query_model_change_does_not_publish_mixed_model_scores(tmp_path):
    store, engine, index, asset, old = await _indexed_case(tmp_path)
    async def provider(*args, **kwargs):
        assert kwargs["model"] == "fake-embedding-v1"
        engine.model = "changed-during-query"
        return [1., 0., 0.]
    engine._generate_embedding = provider
    assert await index.search(QUERY) == {}

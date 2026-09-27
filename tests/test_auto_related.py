import importlib
import sys
from unittest.mock import AsyncMock

import pytest


def _load_server(tmp_path, monkeypatch):
    monkeypatch.setenv("OMBRE_BUCKETS_DIR", str(tmp_path / "buckets"))
    monkeypatch.delenv("OMBRE_API_KEY", raising=False)
    sys.modules.pop("server", None)
    server = importlib.import_module("server")
    server.decay_engine.ensure_started = AsyncMock(return_value=None)
    server.embedding_engine.enabled = True
    server.bucket_mgr.embedding_engine.enabled = True
    return server


@pytest.mark.asyncio
async def test_auto_related_links_top_matches_and_skips_sealed(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    target_id = await server.bucket_mgr.create(content="target relation")
    similar_id = await server.bucket_mgr.create(content="similar relation")
    sealed_id = await server.bucket_mgr.create(content="sealed relation")
    far_id = await server.bucket_mgr.create(content="far relation")
    await server.trace(sealed_id, sealed=1)

    server.embedding_engine._store_embedding(target_id, [1.0, 0.0])
    server.embedding_engine._store_embedding(similar_id, [0.95, 0.05])
    server.embedding_engine._store_embedding(sealed_id, [0.99, 0.01])
    server.embedding_engine._store_embedding(far_id, [0.0, 1.0])

    linked = await server._auto_link_related(target_id, threshold=0.75)
    target = await server.bucket_mgr.get(target_id)
    similar = await server.bucket_mgr.get(similar_id)
    sealed = await server.bucket_mgr.get(sealed_id)

    assert len(linked) == 1
    assert linked[0][0] == similar_id
    assert linked[0][1] >= 0.75
    assert similar_id in target["metadata"]["related_buckets"]
    assert sealed_id not in target["metadata"]["related_buckets"]
    assert target_id in similar["metadata"]["related_buckets"]
    assert target_id not in sealed["metadata"].get("related_buckets", "")


@pytest.mark.asyncio
async def test_related_backfill_dry_run_does_not_write(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    first_id = await server.bucket_mgr.create(content="first relation")
    second_id = await server.bucket_mgr.create(content="second relation")
    server.embedding_engine._store_embedding(first_id, [1.0, 0.0])
    server.embedding_engine._store_embedding(second_id, [0.95, 0.05])

    result = await server.related_backfill(dry_run=True, threshold=0.75)
    first = await server.bucket_mgr.get(first_id)

    assert "自动 related dry-run" in result
    assert first_id in result
    assert second_id in result
    assert first["metadata"].get("related_buckets", "") == ""


@pytest.mark.parametrize('state,excluded', [({'dormant':True},True),({'sealed':1},True),
    ({'superseded_by':'C'},True),({'superseded_by':'none'},False),
    ({'superseded_by':None},False),({'superseded_by':'missing'},False),
    ({'resolved':True},False),({'pinned':True},False),({'protected':True},False),
    ({'digested':True},False)])
def test_w8_automatic_eligibility(tmp_path,state,excluded):
    from tests.test_w8_related_integrity import write_bucket
    from related_integrity import scan_relation_store,automatic_eligible
    root=tmp_path/'store'
    write_bucket(root,'B',**state);write_bucket(root,'C')
    assert automatic_eligible(scan_relation_store(root),'B') is not excluded


@pytest.mark.asyncio
async def test_w8_backfill_bilateral_repeated_and_activity(tmp_path,monkeypatch):
    from pathlib import Path
    from related_integrity import scan_relation_store
    from tests.test_w8_related_repair import snapshot
    server=_load_server(tmp_path,monkeypatch)
    ids=[await server.bucket_mgr.create(str(index)) for index in range(5)]
    await server.bucket_mgr.set_dormant(ids[2],True)
    await server.bucket_mgr.update(ids[3],superseded_by=ids[4])
    for identity in ids:server.embedding_engine._store_embedding(identity,[1.0,0.0])
    before=scan_relation_store(server.config['buckets_dir']).endpoints
    await server.related_backfill(dry_run=False,threshold=.75)
    after=scan_relation_store(server.config['buckets_dir']).endpoints
    for identity in (ids[0],ids[1],ids[4]):
        assert set(after[identity].related.ids)==set((ids[0],ids[1],ids[4]))-{identity}
        assert after[identity].metadata['last_active']==before[identity].metadata['last_active']
        assert after[identity].metadata['updated_at']==before[identity].metadata['updated_at']
    assert after[ids[2]].related.ids==after[ids[3]].related.ids==()
    files=snapshot(Path(server.config['buckets_dir']))
    result=await server.related_backfill(dry_run=False,threshold=.75)
    assert 'committed: 0' in result
    assert snapshot(Path(server.config['buckets_dir']))==files
    server.decay_engine.ensure_started.assert_not_called()


@pytest.mark.asyncio
async def test_w8_cold_dry_run_no_runtime_schema_decay_or_pending_recovery(tmp_path,monkeypatch):
    from pathlib import Path
    import sqlite3
    import json
    from related_integrity import RelationStore,RelatedError
    from tests.test_w8_related_integrity import write_bucket
    from tests.test_w8_related_repair import snapshot
    root=tmp_path/'cold';root.mkdir()
    write_bucket(root,'A');write_bucket(root,'B')
    monkeypatch.setenv('OMBRE_BUCKETS_DIR',str(root))
    monkeypatch.delenv('OMBRE_API_KEY',raising=False)
    sys.modules.pop('server',None)
    server=importlib.import_module('server')
    server.config['embedding']={'api_key':'synthetic-key','enabled':True,'independent':True,'model':'fixture'}
    with sqlite3.connect(root/'embeddings.db') as conn:
        conn.execute('CREATE TABLE embeddings(bucket_id TEXT,embedding TEXT,model TEXT)')
        conn.executemany('INSERT INTO embeddings VALUES(?,?,?)',[(i,json.dumps([1,0]),'fixture') for i in ('A','B')])
    monkeypatch.setattr(server,'_get_runtime_components',lambda:pytest.fail('dry-run initialized runtime'))
    before=snapshot(root)
    result=await server.related_backfill(dry_run=True,threshold=.75)
    assert 'A' in result and 'B' in result and 'dry-run' in result
    assert snapshot(root)==before and server._runtime_components is None
    # Now add an accepted interrupted intent. Dry-run must still only observe.
    from bucket_write_lock import initialize_bucket_write_lock
    initialize_bucket_write_lock(root)
    store=RelationStore(root)
    def fail(point,*args):
        if point=='after_intent':raise OSError('pause')
    store.checkpoint=fail
    with pytest.raises(RelatedError):store.mutate('A',add=['B'])
    before=snapshot(root)
    await server.related_backfill(dry_run=True,threshold=.75)
    assert snapshot(root)==before and store.operations()[0]['status']=='pending'

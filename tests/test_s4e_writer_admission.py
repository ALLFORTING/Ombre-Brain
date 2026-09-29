"""Real delayed/standalone writer paths on isolated roots, including tombstones."""
import asyncio
import importlib
import json
from pathlib import Path
import sqlite3
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import frontmatter
import pytest

from bucket_write_lock import bucket_write_scope
from confirmed_delete_admission import DurableDeleteAdmission, DeleteAdmissionError
from tests.test_archive_session_reliability import load, store as archive_store
from tests.test_s4e_guarded_relations import accepted, pending_intent
from tests.test_w8_related_integrity import store, write_bucket, graph


async def trace(ob, identities, token=''):
    result = await ob.mcp.call_tool('trace',{'bucket_id':','.join(identities),'delete':True,'confirm_token':token})
    blocks = result[0] if isinstance(result,tuple) else getattr(result,'content',result)
    return '\n'.join(item.text for item in blocks if hasattr(item,'text'))


async def delete(ob, identity):
    preview = await trace(ob,[identity])
    token = preview.split('confirm_token: ',1)[1].splitlines()[0]
    assert await trace(ob,[identity],token) == '已遗忘记忆桶: '+identity


def finish(manager, context):
    while not manager._confirmed_step(context): pass
    manager.release_confirmed_delete(context)


@pytest.mark.asyncio
@pytest.mark.parametrize('completed',[False,True])
async def test_archive_publication_receipt_loss_cannot_recreate_deleted_source(tmp_path,monkeypatch,completed):
    ob=load(tmp_path/'root',monkeypatch)
    store=archive_store(ob)
    def cancel(point,*a):
        if point=='after_publish': raise asyncio.CancelledError()
    monkeypatch.setattr(store,'checkpoint',cancel)
    monkeypatch.setattr(ob,'ArchiveSessionOperations',lambda root:store)
    with pytest.raises(asyncio.CancelledError): await ob.archive_session('archive source',operation_id='publication')
    op=store.lookup('publication'); identity=op['bucket_id']
    assert op['publication_digest'] is None and op['boot_event_id'] is None
    if completed: await delete(ob,identity)
    else:
        context=accepted(ob.bucket_mgr,identity)
        ob.bucket_mgr.release_confirmed_delete(context)
    monkeypatch.setattr(store,'checkpoint',lambda *a:None)
    result=await ob.archive_session('archive source',operation_id='publication')
    assert '已归档' not in result
    assert (ob.bucket_mgr._find_bucket_file(identity) is None) == completed
    op=store.lookup('publication'); assert op['boot_event_id'] is None
    with sqlite3.connect(ob.bucket_mgr.history_db_path) as conn:
        assert conn.execute('SELECT COUNT(*) FROM boot_delta_events WHERE bucket_id=?',(identity,)).fetchone()[0]==0


@pytest.mark.asyncio
async def test_real_backfill_constructor_and_provider_return_after_delete(tmp_path,monkeypatch):
    import backfill_embeddings as backfill
    ob=load(tmp_path/'root',monkeypatch)
    identity=await ob.bucket_mgr.create('backfill source')
    original=backfill.EmbeddingEngine; engines=[]
    started,release=asyncio.Event(),asyncio.Event()
    def factory(config):
        engine=original(config);engine.enabled=True;engines.append(engine)
        async def provider(*a): started.set();await release.wait();return [.2,.8]
        engine._generate_embedding=provider
        return engine
    monkeypatch.setattr(backfill,'EmbeddingEngine',factory)
    monkeypatch.setattr(backfill,'load_config',lambda:ob.config)
    task=asyncio.create_task(backfill.backfill())
    await asyncio.wait_for(started.wait(),10)
    assert callable(engines[0].write_admission) and callable(engines[0].source_capture)
    await delete(ob,identity);release.set();await asyncio.wait_for(task,10)
    with sqlite3.connect(engines[0].db_path) as conn:
        assert conn.execute('SELECT 1 FROM embeddings WHERE bucket_id=?',(identity,)).fetchone() is None
    assert len(ob.bucket_mgr.get_history(identity))==1


def test_active_deleted_and_provably_new_incarnation(store):
    admission=DurableDeleteAdmission(store.base_dir)
    with bucket_write_scope(store.base_dir): guard=admission.capture('A')
    context=accepted(store)
    with bucket_write_scope(store.base_dir), pytest.raises(DeleteAdmissionError,match='source_pending'):
        admission.admit('A',expected_source=guard,kind='emotion')
    finish(store,context)
    with bucket_write_scope(store.base_dir),pytest.raises(DeleteAdmissionError,match='incarnation_deleted'):
        admission.admit('A',expected_source=guard,kind='embedding')
    write_bucket(store.base_dir,'A')
    with bucket_write_scope(store.base_dir):
        replacement=admission.capture('A')
        assert replacement['incarnation'] != guard['incarnation']
        assert admission.admit('A',expected_source=replacement,kind='embedding')==replacement
        with pytest.raises(DeleteAdmissionError,match='identity_required'):
            admission.admit('A',kind='delayed')


@pytest.mark.parametrize('writer',['add_timestamps','reclassify_domains','migrate_to_domains','reclassify_api'])
@pytest.mark.parametrize('state',['pending','deleted_capture'])
def test_real_maintenance_skips_blocked_incarnation_and_updates_unrelated(store,monkeypatch,capsys,writer,state):
    module=importlib.import_module(writer);root=Path(store.base_dir)
    for name,value in [('BUCKETS_DIR',str(root)),('DYNAMIC_DIR',str(root/'dynamic')),
                       ('DATA_DIR',str(root/'dynamic')),('UNCLASS_DIR',str(root/'dynamic'))]:
        if hasattr(module,name):monkeypatch.setattr(module,name,value)
    source=Path(store._find_bucket_file('A'));before=source.read_bytes()
    if state=='pending':
        context=accepted(store);store.release_confirmed_delete(context)
    else:
        original=DurableDeleteAdmission.capture_path
        def interrupted(self,path):
            identity,guard=original(self,path)
            if identity=='A':finish(store,accepted(store))
            return identity,guard
        monkeypatch.setattr(DurableDeleteAdmission,'capture_path',interrupted)
    if writer=='reclassify_api':
        async def provider(**kwargs):
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(
                dict(domain=['changed'],tags=['changed'],suggested_name='changed',valence=.4,arousal=.2))))])
        monkeypatch.setattr(module,'AsyncOpenAI',lambda **kw:SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=provider))))
        asyncio.run(module.reclassify())
    elif writer=='reclassify_domains':
        monkeypatch.setattr(module,'classify',lambda *a:['changed']);module.reclassify()
    elif writer=='add_timestamps':module.main()
    else:module.migrate()
    output=capsys.readouterr().out
    assert 'skipped' in output and 'A' in output
    if state=='pending':assert source.read_bytes()==before
    else:assert store._find_bucket_file('A') is None and len(store.get_history('A'))==1
    other=Path(store._find_bucket_file('B'))
    if writer=='add_timestamps':assert frontmatter.load(other).get('created_at')
    else:assert other.parent != root/'dynamic'


@pytest.mark.asyncio
async def test_reclassify_provider_cannot_apply_captured_source_after_completed_delete(tmp_path,monkeypatch,capsys):
    module=importlib.import_module('reclassify_api');ob=load(tmp_path/'root',monkeypatch)
    identity=await ob.bucket_mgr.create('classify source',domain=['未分类'])
    root=Path(ob.bucket_mgr.base_dir)
    monkeypatch.setattr(module,'DATA_DIR',str(root/'dynamic'));monkeypatch.setattr(module,'UNCLASS_DIR',str(root/'dynamic'/'未分类'))
    async def provider(**kwargs):
        await delete(ob,identity)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='{"domain":["changed"]}'))])
    monkeypatch.setattr(module,'AsyncOpenAI',lambda **kw:SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=provider))))
    await module.reclassify()
    assert 'skipped' in capsys.readouterr().out and ob.bucket_mgr._find_bucket_file(identity) is None


def test_real_relation_cli_pending_source_and_guarded_intent(store,monkeypatch,tmp_path,capsys):
    import scripts.related_integrity as cli
    from related_integrity import plan_repair,scan_relation_store
    write_bucket(store.base_dir,'A',related_buckets='B')
    plan=plan_repair(scan_relation_store(store.base_dir));path=tmp_path/'plan.json';path.write_text(json.dumps(plan))
    context=accepted(store)
    monkeypatch.setattr(sys,'argv',['related_integrity','apply','--buckets-dir',store.base_dir,'--plan',str(path)])
    assert cli.main()==2
    assert 'source_pending' in capsys.readouterr().err and graph(store)['B']==[]
    store.release_confirmed_delete(context)


def test_real_relation_cli_defers_guarded_intent(store,monkeypatch,tmp_path,capsys):
    import scripts.related_integrity as cli
    from related_integrity import plan_repair,scan_relation_store
    context,row=pending_intent(store,monkeypatch)
    write_bucket(store.base_dir,'B',related='C')
    path=tmp_path/'plan.json';path.write_text(json.dumps(plan_repair(scan_relation_store(store.base_dir))))
    monkeypatch.setattr(sys,'argv',['related_integrity','apply','--buckets-dir',store.base_dir,'--plan',str(path)])
    assert cli.main()==2
    assert 'A' in graph(store) and store.relation_store.lookup(row['plan']['relation_key'])['progress']==0


@pytest.mark.asyncio
@pytest.mark.parametrize('completed',[False,True])
async def test_real_keyed_hold_delayed_provider_cannot_record_emotion(tmp_path,monkeypatch,completed):
    ob=load(tmp_path/'root',monkeypatch,enabled=True)
    started,release=asyncio.Event(),asyncio.Event()
    async def provider(*a,**kw):started.set();await release.wait();return [.2,.8]
    monkeypatch.setattr(ob.bucket_mgr.embedding_engine,'_generate_embedding',provider)
    monkeypatch.setattr(ob.dehydrator,'analyze',AsyncMock(return_value=dict(domain=['work'],tags=[],valence=.7,arousal=.3,importance=5,todos=[])))
    task=asyncio.create_task(ob.hold('delayed hold source',valence=.7,arousal=.3,operation_id='late-hold'))
    await asyncio.wait_for(started.wait(),10)
    request=ob.bucket_mgr.inspect_trace_request('late-hold');identity=request['plan']['items'][0]['plan']['target']
    if completed:await delete(ob,identity)
    else:
        context=accepted(ob.bucket_mgr,identity);ob.bucket_mgr.release_confirmed_delete(context)
    release.set();result=await asyncio.wait_for(task,10)
    assert not any(e.get('bucket_id')==identity for e in ob._load_emotion_timeline())
    assert 'confirmed_delete_' in result
    assert ob.bucket_mgr.inspect_trace_request('late-hold')['phase'] != 'completed'


@pytest.mark.parametrize('completed',[False,True])
def test_letter_and_manual_publication_cannot_bypass_admission(store,monkeypatch,completed):
    import write_memory
    context=accepted(store)
    if completed:finish(store,context)
    monkeypatch.setattr(write_memory,'VAULT_DIR',str(Path(store.base_dir)/'dynamic'))
    monkeypatch.setattr(write_memory,'gen_id',lambda:'A')
    with pytest.raises(DeleteAdmissionError):write_memory.write_memory('replacement','stale',['x'],[])
    with pytest.raises(DeleteAdmissionError):store.record_letter('stale handoff','A')
    with sqlite3.connect(store.history_db_path) as conn:
        assert conn.execute('SELECT COUNT(*) FROM letters').fetchone()[0]==0


@pytest.mark.asyncio
async def test_registered_public_batch_restart_old_token_never_creates_missing_ordinal(tmp_path,monkeypatch):
    ob=load(tmp_path/'root',monkeypatch)
    identities=[await ob.bucket_mgr.create('batch '+str(i)) for i in range(3)]
    ob.bucket_mgr.mutate_related(identities[0],add=[identities[1]])
    engine=ob.bucket_mgr.embedding_engine
    with sqlite3.connect(engine.db_path) as conn:
        for identity in identities:conn.execute('INSERT INTO embeddings(bucket_id,embedding,model,updated_at) VALUES(?,?,?,?)',(identity,'[1.0]',engine.model,'2001'))
    preview=await trace(ob,identities);token=preview.split('confirm_token: ',1)[1].splitlines()[0]
    async def pause(point,*a):
        if point=='child_accepted':raise asyncio.CancelledError()
    monkeypatch.setattr(ob.bucket_mgr,'confirmed_delete_pause',pause)
    with pytest.raises(asyncio.CancelledError):await trace(ob,identities,token)
    assert [row['bucket_id'] for row in ob.bucket_mgr.confirmed_delete_rows()]==identities[:1]
    restarted=load(tmp_path/'root',monkeypatch)
    result=await trace(restarted,identities,token)
    assert 'delete incomplete' in result and 'fresh preview' in result
    rows=restarted.bucket_mgr.confirmed_delete_rows()
    assert len(rows)==1 and rows[0]['ordinal']==0 and rows[0]['status']=='completed'
    assert len(restarted.bucket_mgr.get_history(identities[0]))==1
    assert restarted.bucket_mgr._find_bucket_file(identities[0]) is None
    for identity in identities[1:]:
        assert restarted.bucket_mgr._find_bucket_file(identity) and restarted.bucket_mgr.get_history(identity)==[]
    assert graph(restarted.bucket_mgr)[identities[1]]==[]
    with sqlite3.connect(engine.db_path) as conn:
        assert {r[0] for r in conn.execute('SELECT bucket_id FROM embeddings')}==set(identities[1:])


@pytest.mark.asyncio
@pytest.mark.parametrize('recreate',[False,True])
async def test_real_keyed_trace_provider_cannot_publish_old_effects_after_delete(tmp_path,monkeypatch,recreate):
    ob=load(tmp_path/'root',monkeypatch,enabled=True)
    identity=await ob.bucket_mgr.create('original source')
    started,release=asyncio.Event(),asyncio.Event()
    async def provider(*a,**kw):started.set();await release.wait();return [.2,.8]
    monkeypatch.setattr(ob.bucket_mgr.embedding_engine,'_generate_embedding',provider)
    task=asyncio.create_task(ob.trace(identity,content='updated source',operation_id='late-trace'))
    await asyncio.wait_for(started.wait(),10)
    path=Path(ob.bucket_mgr._find_bucket_file(identity));raw=path.read_bytes()
    await delete(ob,identity)
    if recreate:path.write_bytes(raw)
    release.set();result=await asyncio.wait_for(task,10)
    assert ob.bucket_mgr.inspect_trace_request('late-trace')['phase'] != 'completed'
    assert 'claim' not in result and ('pending' in result or 'confirmed_delete' in result)
    with sqlite3.connect(ob.bucket_mgr.embedding_engine.db_path) as conn:
        assert conn.execute('SELECT 1 FROM embeddings WHERE bucket_id=?',(identity,)).fetchone() is None
    if recreate:assert path.read_bytes()==raw


def test_history_and_boot_effects_reject_deleted_incarnation_but_admit_proven_new_source(store):
    from bucket_manager import BucketIdempotencyError
    with bucket_write_scope(store.base_dir):guard=store.delete_admission.capture('A')
    finish(store,accepted(store))
    write_bucket(store.base_dir,'A')
    for effect in (lambda g:store.record_history('A','stale','replace',_expected_source=g),
                   lambda g:store._record_boot_delta_event('A','content_updated',_expected_source=g)):
        with pytest.raises(BucketIdempotencyError):effect(guard)
        with pytest.raises(BucketIdempotencyError):effect(None)
    with bucket_write_scope(store.base_dir):replacement=store.delete_admission.capture('A')
    store.record_history('A','new history','replace',_expected_source=replacement)
    store._record_boot_delta_event('A','content_updated',_expected_source=replacement)
    assert len(store.get_history('A'))==2
    with sqlite3.connect(store.history_db_path) as conn:
        assert conn.execute('SELECT COUNT(*) FROM boot_delta_events WHERE bucket_id=?',('A',)).fetchone()[0]==1


@pytest.mark.asyncio
async def test_stable_create_with_lost_publication_receipt_cannot_resurrect_deleted_source(tmp_path,monkeypatch):
    from bucket_manager import BucketIdempotencyError
    ob=load(tmp_path/'root',monkeypatch);manager=ob.bucket_mgr
    original=manager._mark_import_operation_applied
    def lost(*a):raise asyncio.CancelledError()
    monkeypatch.setattr(manager,'_mark_import_operation_applied',lost)
    with pytest.raises(asyncio.CancelledError):
        await manager.create('stable source',_o5b_operation_key='old-create')
    identity=manager.inspect_import_operation('old-create')['result_id']
    assert manager._find_bucket_file(identity)
    monkeypatch.setattr(manager,'_mark_import_operation_applied',original)
    await delete(ob,identity)
    with pytest.raises(BucketIdempotencyError,match='identity_required'):
        await manager.create('stable source',_o5b_operation_key='old-create')
    assert manager._find_bucket_file(identity) is None and len(manager.get_history(identity))==1


def test_stable_update_and_feel_mark_cannot_mutate_recreated_deleted_source(store):
    from bucket_manager import BucketIdempotencyError
    with bucket_write_scope(store.base_dir):guard=store.delete_admission.capture('A')
    store.plan_import_operation('old-update',operation_kind='update',target_bucket_id='A',
                                payload={'kwargs':{'tags':['stale']}})
    finish(store,accepted(store));write_bucket(store.base_dir,'A')
    path=Path(store._find_bucket_file('A'));raw=path.read_bytes()
    with pytest.raises(BucketIdempotencyError,match='identity_required'):
        asyncio.run(store.apply_import_operation('old-update'))
    with pytest.raises(BucketIdempotencyError):store.mark_feel_source('A',_expected_source=guard)
    assert path.read_bytes()==raw


@pytest.mark.asyncio
async def test_delayed_admission_reconciles_own_relation_receipt_before_guard_checkpoint(tmp_path,monkeypatch):
    ob=load(tmp_path/'root',monkeypatch,enabled=True);manager=ob.bucket_mgr
    other=await manager.create('related other')
    engine=manager.embedding_engine
    with sqlite3.connect(engine.db_path) as conn:
        conn.execute('INSERT OR REPLACE INTO embeddings(bucket_id,embedding,model,updated_at) VALUES(?,?,?,?)',
                     (other,'[0.2,0.8]',engine.model,'2001'))
    monkeypatch.setattr(engine,'_generate_embedding',AsyncMock(return_value=[.2,.8]))
    monkeypatch.setattr(ob.dehydrator,'analyze',AsyncMock(return_value=dict(domain=['work'],tags=[],valence=.7,arousal=.3,importance=5,todos=[])))
    def lost(point,*a):
        if point=='after_complete':raise asyncio.CancelledError()
    monkeypatch.setattr(manager.relation_store,'checkpoint',lost)
    with pytest.raises(asyncio.CancelledError):await ob.hold('own relation receipt',operation_id='relation-gap')
    monkeypatch.setattr(manager.relation_store,'checkpoint',lambda *a:None)
    result=await ob.hold('own relation receipt',operation_id='relation-gap')
    assert 'confirmed_delete' not in result
    request=manager.inspect_trace_request('relation-gap');assert request['phase']=='completed'
    identity=request['plan']['items'][0]['plan']['target']
    assert graph(manager)[other]==[identity] and graph(manager)[identity]==[other]
    assert result==await ob.hold('own relation receipt',operation_id='relation-gap')


@pytest.mark.asyncio
async def test_delayed_admission_reconciles_own_trigger_marker_before_guard_checkpoint(tmp_path,monkeypatch):
    ob=load(tmp_path/'root',monkeypatch);manager=ob.bucket_mgr
    original=manager.commit_hold_grow_trigger
    def lost(*a):original(*a);raise asyncio.CancelledError()
    monkeypatch.setattr(manager,'commit_hold_grow_trigger',lost)
    with pytest.raises(asyncio.CancelledError):
        await ob.hold('own trigger receipt',trigger_date='2026-10-01',operation_id='trigger-gap')
    monkeypatch.setattr(manager,'commit_hold_grow_trigger',original)
    result=await ob.hold('own trigger receipt',trigger_date='2026-10-01',operation_id='trigger-gap')
    assert 'confirmed_delete' not in result and manager.inspect_trace_request('trigger-gap')['phase']=='completed'
    assert result==await ob.hold('own trigger receipt',trigger_date='2026-10-01',operation_id='trigger-gap')


def test_manual_publication_admits_actual_metadata_identity(store,monkeypatch):
    import write_memory
    context=accepted(store);store.release_confirmed_delete(context)
    original=Path(store._find_bucket_file('A')).read_bytes()
    monkeypatch.setattr(write_memory,'VAULT_DIR',str(Path(store.base_dir)/'dynamic'))
    monkeypatch.setattr(write_memory,'gen_id',lambda:'NEW')
    with pytest.raises(DeleteAdmissionError,match='identity_conflict'):
        write_memory.write_memory('replacement\nid: A','stale',['x'],[])
    assert not (Path(write_memory.VAULT_DIR)/'NEW.md').exists()
    assert Path(store._find_bucket_file('A')).read_bytes()==original

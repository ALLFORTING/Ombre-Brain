"""Confirmed delete admission, durable phases and surface replay semantics."""
import asyncio
import json
import sqlite3
from pathlib import Path

import frontmatter
import pytest

from bucket_manager import BucketIdempotencyError, BucketManager
from related_integrity import RelatedError
from tests.test_w8_related_integrity import load_server
from tests.test_s4e_guarded_relations import accepted


@pytest.fixture
def ob(tmp_path,monkeypatch):
    module=load_server(tmp_path,monkeypatch)
    module.bucket_mgr.embedding_engine=None
    return module


async def preview(ob,ids):
    result=await ob._delete_with_confirmation(ids,'')
    assert result['status']=='preview',result
    return result['confirm_token']


def assert_effects(manager,identity):
    rows=manager.confirmed_delete_rows()
    row=next(r for r in rows if r['bucket_id']==identity)
    assert row['status']=='completed'
    assert set(row['receipts'])=={'history','vector','relation','successor'}
    assert len([h for h in manager.get_history(identity) if h['change_type']=='delete'])==1
    assert row['completed_at'] and row['result_text']=='已遗忘记忆桶: '+identity
    assert manager._find_bucket_file(identity) is None
    return row


@pytest.mark.asyncio
async def test_preview_and_empty_receipt_lookup_do_not_create_table(ob):
    identity=await ob.bucket_mgr.create('private synthetic content')
    await preview(ob,[identity])
    assert ob.bucket_mgr.confirmed_delete_rows(token_hash='unknown')==[]
    with sqlite3.connect(ob.bucket_mgr.history_db_path) as conn:
        assert not conn.execute("SELECT 1 FROM sqlite_master WHERE name='ob_confirmed_delete_operations'").fetchone()


@pytest.mark.asyncio
async def test_normal_delete_response_loss_and_receipt_only_replay(ob):
    identity=await ob.bucket_mgr.create('private synthetic content')
    token=await preview(ob,[identity])
    result=await ob._delete_with_confirmation([identity],token)
    assert result['status']=='deleted'
    row=assert_effects(ob.bucket_mgr,identity)
    assert token not in json.dumps(row) and 'private synthetic content' not in json.dumps(row)
    ob._mutation_confirm_tokens.clear()
    assert await ob._delete_with_confirmation([identity],token)==result
    assert_effects(ob.bucket_mgr,identity)
    assert (await ob._delete_with_confirmation(['wrong'],token))['status']=='invalid'


CHILD_BOUNDARIES=['after_accept_commit','after_history_commit','after_vector_commit',
    'child.history_vector_resolved','after_relation_commit','child.relation_delete_complete',
    'before_successor_publish','after_successor_publish','child.successor_cleanup_complete','child.completed']
RELATION_BOUNDARIES=['before_intent','after_intent','before_publish','after_publish',
    'before_delete','after_delete','after_progress','before_complete','after_complete']


@pytest.mark.asyncio
@pytest.mark.parametrize('recovery',['retry','restart'])
@pytest.mark.parametrize('boundary',CHILD_BOUNDARIES+RELATION_BOUNDARIES)
async def test_cancellation_at_every_durable_phase_recovers_once(ob,monkeypatch,boundary,recovery):
    manager=ob.bucket_mgr
    identity=await manager.create('delete snapshot')
    neighbor=await manager.create('neighbor')
    successor=await manager.create('successor')
    manager.mutate_related(identity,add=[neighbor])
    await manager.update(identity,superseded_by=successor,_supersession_reverse=True)
    token=await preview(ob,[identity])
    seam=manager.relation_store if boundary in RELATION_BOUNDARIES else manager
    attribute='checkpoint' if seam is manager.relation_store else 'confirmed_delete_checkpoint'
    def cancel(point,*args):
        if point==boundary:raise asyncio.CancelledError()
    monkeypatch.setattr(seam,attribute,cancel)
    with pytest.raises(asyncio.CancelledError):await ob._delete_with_confirmation([identity],token)
    assert token not in ob._mutation_confirm_tokens
    assert len(manager.confirmed_delete_rows())==1
    monkeypatch.setattr(seam,attribute,lambda *a:None)
    if recovery=='restart':
        manager=BucketManager({'buckets_dir':manager.base_dir})
    else:
        assert (await ob._delete_with_confirmation([identity],token))['status']=='deleted'
    assert_effects(manager,identity)
    assert identity not in (await manager.get(successor))['metadata'].get('supersedes',[])
    assert identity not in (await manager.get(neighbor))['metadata'].get('related_buckets','')
    assert manager.relation_store.lookup(manager.confirmed_delete_rows()[0]['plan']['relation_key'])['status']=='complete'


@pytest.mark.asyncio
@pytest.mark.parametrize('boundary',['token_consumed','before_accept_commit','before_history_commit'])
async def test_precommit_cancellation_never_mutates_business_state(ob,monkeypatch,boundary):
    identity=await ob.bucket_mgr.create('must remain')
    token=await preview(ob,[identity])
    def cancel(point,*a):
        if point==boundary:raise asyncio.CancelledError()
    monkeypatch.setattr(ob.bucket_mgr,'confirmed_delete_checkpoint',cancel)
    with pytest.raises(asyncio.CancelledError):await ob._delete_with_confirmation([identity],token)
    assert await ob.bucket_mgr.get(identity)
    assert not ob.bucket_mgr.get_history(identity)
    rows=ob.bucket_mgr.confirmed_delete_rows()
    assert bool(rows)==(boundary=='before_history_commit')
    monkeypatch.setattr(ob.bucket_mgr,'confirmed_delete_checkpoint',lambda *a:None)
    retry=await ob._delete_with_confirmation([identity],token)
    assert retry['status']==('deleted' if rows else 'invalid')
    if not rows:
        fresh=await preview(ob,[identity])
        assert (await ob._delete_with_confirmation([identity],fresh))['status']=='deleted'


@pytest.mark.asyncio
async def test_accept_commit_ack_loss_is_reconciled(ob,monkeypatch):
    identity=await ob.bucket_mgr.create('commit acknowledgment')
    token=await preview(ob,[identity])
    def lose_ack(point,*a):
        if point=='after_accept_commit':raise OSError('commit acknowledgment lost')
    monkeypatch.setattr(ob.bucket_mgr,'confirmed_delete_checkpoint',lose_ack)
    assert (await ob._delete_with_confirmation([identity],token))['status']=='deleted'
    assert_effects(ob.bucket_mgr,identity)


@pytest.mark.asyncio
@pytest.mark.parametrize('boundary',['child_accepted','between_children'])
async def test_batch_interruption_old_token_only_recovers_started_children(ob,monkeypatch,boundary):
    ids=[await ob.bucket_mgr.create('batch '+str(i)) for i in range(3)]
    token=await preview(ob,ids)
    async def cancel(point,*a):
        if point==boundary:raise asyncio.CancelledError()
    monkeypatch.setattr(ob.bucket_mgr,'confirmed_delete_pause',cancel)
    with pytest.raises(asyncio.CancelledError):await ob._delete_with_confirmation(ids,token)
    assert [r['bucket_id'] for r in ob.bucket_mgr.confirmed_delete_rows()]==ids[:1]
    assert all([await ob.bucket_mgr.get(i) for i in ids[1:]])
    monkeypatch.setattr(ob.bucket_mgr,'confirmed_delete_pause',lambda *a:asyncio.sleep(0))
    result=await ob._delete_with_confirmation(ids,token)
    assert result['status']=='incomplete'
    assert result['started_ids']==result['completed_ids']==ids[:1]
    assert result['not_started_ids']==result['remaining_existing_ids']==ids[1:]
    assert result['fresh_confirmation_required'] is True
    assert len(ob.bucket_mgr.confirmed_delete_rows())==1
    remaining_token=await preview(ob,ids[1:])
    assert (await ob._delete_with_confirmation(ids[1:],remaining_token))['status']=='deleted'


@pytest.mark.asyncio
async def test_batch_before_first_child_loses_authorization_without_child_rows(ob,monkeypatch):
    ids=[await ob.bucket_mgr.create('batch') for _ in range(2)]
    token=await preview(ob,ids)
    def cancel(point,*a):
        if point=='token_consumed':raise asyncio.CancelledError()
    monkeypatch.setattr(ob.bucket_mgr,'confirmed_delete_checkpoint',cancel)
    with pytest.raises(asyncio.CancelledError):await ob._delete_with_confirmation(ids,token)
    assert ob.bucket_mgr.confirmed_delete_rows()==[]
    assert (await ob._delete_with_confirmation(ids,token))['status']=='invalid'
    assert all([await ob.bucket_mgr.get(i) for i in ids])


@pytest.mark.asyncio
async def test_related_batch_projects_only_frozen_plan_and_receipts(ob):
    ids=[await ob.bucket_mgr.create('related batch') for _ in range(3)]
    ob.bucket_mgr.mutate_related(ids[0],add=ids[1:])
    ob.bucket_mgr.mutate_related(ids[1],add=[ids[2]])
    token=await preview(ob,ids)
    result=await ob._delete_with_confirmation(ids,token)
    assert result['status']=='deleted',result
    assert [r['bucket_id'] for r in ob.bucket_mgr.confirmed_delete_rows()]==ids
    for identity in ids:assert_effects(ob.bucket_mgr,identity)


@pytest.mark.asyncio
async def test_concurrent_same_attempt_has_single_effects(ob):
    identity=await ob.bucket_mgr.create('concurrent')
    token=await preview(ob,[identity])
    results=await asyncio.gather(*(ob._delete_with_confirmation([identity],token) for _ in range(8)))
    assert all(r==results[0] for r in results)
    assert len(ob.bucket_mgr.confirmed_delete_rows())==1
    assert_effects(ob.bucket_mgr,identity)


@pytest.mark.asyncio
@pytest.mark.parametrize('boundary',['child_accepted','after_intent','after_delete','after_progress','after_complete'])
async def test_missing_source_requires_matching_durable_evidence(ob,monkeypatch,boundary):
    identity=await ob.bucket_mgr.create('evidence')
    token=await preview(ob,[identity])
    seam=ob.bucket_mgr if boundary=='child_accepted' else ob.bucket_mgr.relation_store
    if boundary=='child_accepted':
        async def cancel(*a):raise asyncio.CancelledError()
        monkeypatch.setattr(seam,'confirmed_delete_pause',cancel)
    else:
        def cancel(point,*a):
            if point==boundary:raise asyncio.CancelledError()
        monkeypatch.setattr(seam,'checkpoint',cancel)
    with pytest.raises(asyncio.CancelledError):await ob._delete_with_confirmation([identity],token)
    path=ob.bucket_mgr._find_bucket_file(identity)
    if path:Path(path).unlink()
    monkeypatch.setattr(ob.bucket_mgr,'confirmed_delete_pause',lambda *a:asyncio.sleep(0))
    monkeypatch.setattr(ob.bucket_mgr.relation_store,'checkpoint',lambda *a:None)
    result=await ob._delete_with_confirmation([identity],token)
    if boundary=='child_accepted':
        assert result['status']=='incomplete' and result['completed_ids']==[]
        assert result['absent_without_completion_evidence_ids']==[identity]
    else:assert result['status']=='deleted',result
    assert (await ob._delete_with_confirmation([identity],'unknown'))['status']=='invalid'
    assert (await ob._delete_with_confirmation([identity],''))['status']=='blocked'


@pytest.mark.asyncio
@pytest.mark.parametrize('guard',['sealed','pinned','protected'])
async def test_guards_remain_enforced_after_acceptance(ob,monkeypatch,guard):
    identity=await ob.bucket_mgr.create('guarded')
    token=await preview(ob,[identity])
    async def cancel(*a):raise asyncio.CancelledError()
    monkeypatch.setattr(ob.bucket_mgr,'confirmed_delete_pause',cancel)
    with pytest.raises(asyncio.CancelledError):await ob._delete_with_confirmation([identity],token)
    path=Path(ob.bucket_mgr._find_bucket_file(identity)); post=frontmatter.load(path);post[guard]=True;path.write_text(frontmatter.dumps(post))
    monkeypatch.setattr(ob.bucket_mgr,'confirmed_delete_pause',lambda *a:asyncio.sleep(0))
    result=await ob._delete_with_confirmation([identity],token)
    assert result['status']=='incomplete' and result['blocked'][0]['accepted'] is True
    assert path.exists() and not ob.bucket_mgr.get_history(identity)


@pytest.mark.asyncio
@pytest.mark.parametrize('mutation',['update','touch','dormant','archive','delete','related','forward','reverse','todo','history','delta'])
async def test_pending_source_shared_write_admission(ob,mutation):
    identity=await ob.bucket_mgr.create('pending',todos=['do something'])
    other=await ob.bucket_mgr.create('other')
    accepted(ob.bucket_mgr,identity)
    if mutation=='touch':
        path=Path(ob.bucket_mgr._find_bucket_file(identity));before=path.read_bytes()
        assert await ob.bucket_mgr.touch(identity) is None
        assert path.read_bytes()==before
        return
    with pytest.raises((BucketIdempotencyError,RelatedError),match='source_pending'):
        if mutation=='update':await ob.bucket_mgr.update(identity,content='new')
        elif mutation=='touch':await ob.bucket_mgr.touch(identity)
        elif mutation=='dormant':await ob.bucket_mgr.set_dormant(identity,True)
        elif mutation=='archive':await ob.bucket_mgr.archive(identity)
        elif mutation=='delete':await ob.bucket_mgr.delete(identity)
        elif mutation=='related':ob.bucket_mgr.mutate_related(other,add=[identity])
        elif mutation=='forward':await ob.bucket_mgr.update(other,superseded_by=identity)
        elif mutation=='reverse':await ob.bucket_mgr.update(other,supersedes=[identity])
        elif mutation=='history':ob.bucket_mgr.record_history(identity,'old','replace')
        elif mutation=='delta':ob.bucket_mgr._record_boot_delta_event(identity,'changed')
        else:ob.bucket_mgr.complete_todo(identity,'any',lambda p:True)
    assert (await ob.bucket_mgr.get(identity))['content']=='pending'


@pytest.mark.asyncio
async def test_successor_remove_only_and_publish_receipt_reconciliation(ob,monkeypatch):
    source=await ob.bucket_mgr.create('source'); successor=await ob.bucket_mgr.create('successor')
    await ob.bucket_mgr.update(source,superseded_by=successor,_supersession_reverse=True)
    token=await preview(ob,[source])
    def cancel(point,*a):
        if point=='after_successor_publish':raise asyncio.CancelledError()
    monkeypatch.setattr(ob.bucket_mgr,'confirmed_delete_checkpoint',cancel)
    with pytest.raises(asyncio.CancelledError):await ob._delete_with_confirmation([source],token)
    row=ob.bucket_mgr.confirmed_delete_rows()[0]
    assert row['phase']=='relation_delete_complete' and 'successor' not in row['receipts']
    await ob.bucket_mgr.update(successor,name='concurrent metadata',supersedes=['unrelated'])
    monkeypatch.setattr(ob.bucket_mgr,'confirmed_delete_checkpoint',lambda *a:None)
    assert (await ob._delete_with_confirmation([source],token))['status']=='deleted'
    post=(await ob.bucket_mgr.get(successor))['metadata']
    assert post['name']=='concurrent metadata' and post['supersedes']==['unrelated']
    assert_effects(ob.bucket_mgr,source)


@pytest.mark.asyncio
@pytest.mark.parametrize('exists',[False,True])
async def test_vector_delete_receipt_distinguishes_absence_and_replays_once(ob,monkeypatch,exists):
    from embedding_engine import EmbeddingEngine
    engine=EmbeddingEngine({'buckets_dir':ob.bucket_mgr.base_dir})
    ob.bucket_mgr.embedding_engine=engine
    source=await ob.bucket_mgr.create('vector')
    if exists:
        with sqlite3.connect(engine.db_path) as conn:
            conn.execute('INSERT INTO embeddings(bucket_id,embedding,model,updated_at) VALUES(?,?,?,?)',(source,'[1.0]',engine.model,'2001'))
    token=await preview(ob,[source])
    def cancel(point,*a):
        if point=='after_vector_commit':raise asyncio.CancelledError()
    monkeypatch.setattr(ob.bucket_mgr,'confirmed_delete_checkpoint',cancel)
    with pytest.raises(asyncio.CancelledError):await ob._delete_with_confirmation([source],token)
    assert 'vector' not in ob.bucket_mgr.confirmed_delete_rows()[0]['receipts']
    monkeypatch.setattr(ob.bucket_mgr,'confirmed_delete_checkpoint',lambda *a:None)
    assert (await ob._delete_with_confirmation([source],token))['status']=='deleted'
    row=assert_effects(ob.bucket_mgr,source)
    assert row['receipts']['vector']['outcome']==('deleted' if exists else 'already_absent')
    with sqlite3.connect(engine.db_path) as conn:
        assert conn.execute('SELECT COUNT(*) FROM ob_s4_embedding_effects').fetchone()[0]==1


@pytest.mark.asyncio
async def test_identical_source_recreation_before_relation_is_blocked(ob,monkeypatch):
    identity=await ob.bucket_mgr.create('same bytes new incarnation')
    path=Path(ob.bucket_mgr._find_bucket_file(identity));raw=path.read_bytes()
    token=await preview(ob,[identity])
    async def pause(*a):raise asyncio.CancelledError()
    monkeypatch.setattr(ob.bucket_mgr,'confirmed_delete_pause',pause)
    with pytest.raises(asyncio.CancelledError):await ob._delete_with_confirmation([identity],token)
    path.unlink();path.write_bytes(raw)
    monkeypatch.setattr(ob.bucket_mgr,'confirmed_delete_pause',lambda *a:asyncio.sleep(0))
    result=await ob._delete_with_confirmation([identity],token)
    assert result['status']=='incomplete' and result['completed_ids']==[]
    assert path.read_bytes()==raw and not ob.bucket_mgr.get_history(identity)


@pytest.mark.asyncio
async def test_completed_response_replay_preserves_recreated_source(ob):
    identity=await ob.bucket_mgr.create('completed original')
    path=Path(ob.bucket_mgr._find_bucket_file(identity));raw=path.read_bytes()
    token=await preview(ob,[identity])
    result=await ob._delete_with_confirmation([identity],token)
    path.write_bytes(raw)
    assert await ob._delete_with_confirmation([identity],token)==result
    assert path.read_bytes()==raw and len(ob.bucket_mgr.get_history(identity))==1


@pytest.mark.asyncio
async def test_later_batch_child_blocked_no_further_item_is_started(ob,monkeypatch):
    ids=[await ob.bucket_mgr.create('frozen '+str(i)) for i in range(3)]
    token=await preview(ob,ids)
    original_pause=ob.bucket_mgr.confirmed_delete_pause
    async def concurrent_edit(point,context):
        if point=='between_children' and len(ob.bucket_mgr.confirmed_delete_rows())==1:
            await ob.bucket_mgr.update(ids[1],name='concurrent edit')
        return await original_pause(point,context)
    monkeypatch.setattr(ob.bucket_mgr,'confirmed_delete_pause',concurrent_edit)
    result=await ob._delete_with_confirmation(ids,token)
    assert result['status']=='incomplete' and result['completed_ids']==ids[:1]
    assert result['started_ids']==ids[:2] and result['not_started_ids']==ids[2:]
    assert result['blocked'][0]['bucket_id']==ids[1] and result['blocked'][0]['accepted'] is True
    assert (await ob.bucket_mgr.get(ids[1]))['metadata']['name']=='concurrent edit'
    assert (await ob._delete_with_confirmation(ids,token))['not_started_ids']==ids[2:]


@pytest.mark.asyncio
async def test_unknown_remaining_scan_reports_null(ob,monkeypatch):
    ids=[await ob.bucket_mgr.create('unknown scan') for _ in range(2)]
    token=await preview(ob,ids)
    async def cancel(point,*a):
        if point=='between_children':raise asyncio.CancelledError()
    monkeypatch.setattr(ob.bucket_mgr,'confirmed_delete_pause',cancel)
    with pytest.raises(asyncio.CancelledError):await ob._delete_with_confirmation(ids,token)
    path=Path(ob.bucket_mgr.base_dir)/'dynamic'/'corrupt.md'
    path.write_text('---\nid: [broken\n---\n')
    projection=ob._confirmed_delete_projection(ids,ob.bucket_mgr.confirmed_delete_rows())
    assert projection['remaining_existing_ids'] is None
    assert projection['absent_without_completion_evidence_ids'] is None
    assert projection['not_started_ids']==ids[1:] and projection['fresh_confirmation_required']


@pytest.mark.asyncio
async def test_delayed_vector_generation_cannot_publish_into_pending_or_deleted_source(ob,monkeypatch):
    from embedding_engine import EmbeddingEngine
    engine=EmbeddingEngine({'buckets_dir':ob.bucket_mgr.base_dir})
    engine.write_admission=ob.bucket_mgr._confirmed_embedding_admission
    ob.bucket_mgr.embedding_engine=engine
    source=await ob.bucket_mgr.create('delayed vector')
    engine.enabled=True
    started,release=asyncio.Event(),asyncio.Event()
    async def provider(*a):started.set();await release.wait();return [1.0]
    monkeypatch.setattr(engine,'_generate_embedding',provider)
    task=asyncio.create_task(engine.generate_and_store(source,'delayed vector'))
    await started.wait()
    token=await preview(ob,[source])
    assert (await ob._delete_with_confirmation([source],token))['status']=='deleted'
    release.set();assert await task is False
    with sqlite3.connect(engine.db_path) as conn:
        assert conn.execute('SELECT 1 FROM embeddings WHERE bucket_id=?',(source,)).fetchone() is None


@pytest.mark.asyncio
async def test_lazy_restart_resolves_existing_vectors_even_without_engine_injection(ob,monkeypatch):
    from embedding_engine import EmbeddingEngine
    engine=EmbeddingEngine({'buckets_dir':ob.bucket_mgr.base_dir})
    ob.bucket_mgr.embedding_engine=engine
    source=await ob.bucket_mgr.create('startup vector')
    with sqlite3.connect(engine.db_path) as conn:
        conn.execute('INSERT INTO embeddings(bucket_id,embedding,model,updated_at) VALUES(?,?,?,?)',(source,'[1.0]',engine.model,'2001'))
    token=await preview(ob,[source])
    async def cancel(*a):raise asyncio.CancelledError()
    monkeypatch.setattr(ob.bucket_mgr,'confirmed_delete_pause',cancel)
    with pytest.raises(asyncio.CancelledError):await ob._delete_with_confirmation([source],token)
    manager=BucketManager({'buckets_dir':ob.bucket_mgr.base_dir})
    row=assert_effects(manager,source)
    assert row['receipts']['vector']['outcome']=='deleted'
    with sqlite3.connect(engine.db_path) as conn:
        assert conn.execute('SELECT 1 FROM embeddings WHERE bucket_id=?',(source,)).fetchone() is None


@pytest.mark.asyncio
@pytest.mark.parametrize('receipt',['history','vector','successor'])
async def test_completion_requires_verified_effect_receipts(ob,monkeypatch,receipt):
    source=await ob.bucket_mgr.create('verify receipts');successor=await ob.bucket_mgr.create('successor')
    await ob.bucket_mgr.update(source,superseded_by=successor,_supersession_reverse=True)
    token=await preview(ob,[source])
    def cancel(point,*a):
        if point=='child.successor_cleanup_complete':raise asyncio.CancelledError()
    monkeypatch.setattr(ob.bucket_mgr,'confirmed_delete_checkpoint',cancel)
    with pytest.raises(asyncio.CancelledError):await ob._delete_with_confirmation([source],token)
    row=ob.bucket_mgr.confirmed_delete_rows()[0];receipts=row['receipts']
    receipts[receipt]['effect_key']='unbound-effect'
    with sqlite3.connect(ob.bucket_mgr.history_db_path) as conn:
        conn.execute('UPDATE ob_confirmed_delete_operations SET receipts_json=?',(json.dumps(receipts),))
    monkeypatch.setattr(ob.bucket_mgr,'confirmed_delete_checkpoint',lambda *a:None)
    result=await ob._delete_with_confirmation([source],token)
    assert result['status']=='incomplete' and result['completed_ids']==[]
    assert result['blocked'][0]['accepted']


@pytest.mark.asyncio
async def test_completed_commit_ack_loss_returns_persisted_result(ob,monkeypatch):
    source=await ob.bucket_mgr.create('completion acknowledgment')
    token=await preview(ob,[source])
    def lose_ack(point,*a):
        if point=='child.completed':raise OSError('completion acknowledgment lost')
    monkeypatch.setattr(ob.bucket_mgr,'confirmed_delete_checkpoint',lose_ack)
    result=await ob._delete_with_confirmation([source],token)
    assert result['status']=='deleted'
    assert assert_effects(ob.bucket_mgr,source)['last_error_code'] is None
    assert await ob._delete_with_confirmation([source],token)==result


@pytest.mark.asyncio
async def test_heartbeat_renews_bound_lease_and_stale_owner_cannot_renew(ob,monkeypatch):
    import bucket_manager
    source=await ob.bucket_mgr.create('heartbeat')
    context=accepted(ob.bucket_mgr,source)
    monkeypatch.setattr(bucket_manager,'_S4_LEASE_SECONDS',.3)
    with sqlite3.connect(ob.bucket_mgr.history_db_path) as conn:
        conn.execute('UPDATE ob_confirmed_delete_operations SET lease_until=?',(__import__('time').time()+.25,))
    initial=ob.bucket_mgr._confirmed_row(context['delete_id'])['lease_until']
    heartbeat=asyncio.create_task(ob.bucket_mgr._confirmed_heartbeat(context))
    try:
        await asyncio.sleep(.13)
        assert ob.bucket_mgr._confirmed_row(context['delete_id'])['lease_until']>initial
        with sqlite3.connect(ob.bucket_mgr.history_db_path) as conn:
            conn.execute('UPDATE ob_confirmed_delete_operations SET lease_until=0')
        winner=ob.bucket_mgr.claim_confirmed_delete(context['delete_id'],'winner')
        with pytest.raises(BucketIdempotencyError,match='claim_stale'):
            ob.bucket_mgr._confirmed_checkpoint(context,heartbeat=True)
        ob.bucket_mgr.release_confirmed_delete(context)
        assert ob.bucket_mgr._confirmed_row(context['delete_id'])['owner_instance']=='winner'
    finally:
        heartbeat.cancel()
        with pytest.raises(asyncio.CancelledError):await heartbeat


@pytest.mark.asyncio
@pytest.mark.parametrize('failure',['cancel','expire'])
@pytest.mark.parametrize('boundary',['before_delete','before_commit'])
async def test_vector_transaction_cancellation_or_stale_owner_rolls_back(ob,monkeypatch,failure,boundary):
    from embedding_engine import EmbeddingEngine
    engine=EmbeddingEngine({'buckets_dir':ob.bucket_mgr.base_dir})
    ob.bucket_mgr.embedding_engine=engine
    source=await ob.bucket_mgr.create('vector transaction')
    with sqlite3.connect(engine.db_path) as conn:
        conn.execute('INSERT INTO embeddings(bucket_id,embedding,model,updated_at) VALUES(?,?,?,?)',(source,'[1.0]',engine.model,'2001'))
    token=await preview(ob,[source])
    def interrupt(point,*a):
        if point!=boundary:return
        if failure=='cancel':raise asyncio.CancelledError()
        with sqlite3.connect(ob.bucket_mgr.history_db_path) as conn:
            conn.execute('UPDATE ob_confirmed_delete_operations SET lease_until=0')
    monkeypatch.setattr(engine,'embedding_effect_checkpoint',interrupt)
    with pytest.raises(asyncio.CancelledError if failure=='cancel' else BucketIdempotencyError):
        await ob._delete_with_confirmation([source],token)
    with sqlite3.connect(engine.db_path) as conn:
        assert conn.execute('SELECT 1 FROM embeddings WHERE bucket_id=?',(source,)).fetchone()
        exists=conn.execute("SELECT 1 FROM sqlite_master WHERE name='ob_s4_embedding_effects'").fetchone()
        assert not exists or conn.execute('SELECT COUNT(*) FROM ob_s4_embedding_effects').fetchone()[0]==0
    row=ob.bucket_mgr.confirmed_delete_rows()[0]
    assert 'vector' not in row['receipts'] and await ob.bucket_mgr.get(source)
    monkeypatch.setattr(engine,'embedding_effect_checkpoint',lambda *a:None)
    assert (await ob._delete_with_confirmation([source],token))['status']=='deleted'
    assert_effects(ob.bucket_mgr,source)


@pytest.mark.asyncio
async def test_successor_concurrent_edit_during_publish_is_preserved_on_retry(ob,monkeypatch):
    source=await ob.bucket_mgr.create('source');successor=await ob.bucket_mgr.create('successor')
    await ob.bucket_mgr.update(source,superseded_by=successor,_supersession_reverse=True)
    token=await preview(ob,[source])
    path=Path(ob.bucket_mgr._find_bucket_file(successor))
    def concurrent_edit(point,*a):
        if point=='before_successor_publish':
            post=frontmatter.load(path);post['name']='external concurrent edit';post['supersedes']=[source,'unrelated'];path.write_text(frontmatter.dumps(post))
    monkeypatch.setattr(ob.bucket_mgr,'confirmed_delete_checkpoint',concurrent_edit)
    result=await ob._delete_with_confirmation([source],token)
    assert result['status']=='incomplete' and result['blocked'][0]['accepted']
    assert frontmatter.load(path)['name']=='external concurrent edit'
    monkeypatch.setattr(ob.bucket_mgr,'confirmed_delete_checkpoint',lambda *a:None)
    assert (await ob._delete_with_confirmation([source],token))['status']=='deleted'
    assert frontmatter.load(path)['name']=='external concurrent edit'
    assert frontmatter.load(path)['supersedes']==['unrelated']


@pytest.mark.asyncio
async def test_pending_source_remains_readable_without_incidental_mutation(ob):
    source=await ob.bucket_mgr.create('readable pending source')
    accepted(ob.bucket_mgr,source)
    path=Path(ob.bucket_mgr._find_bucket_file(source));before=path.read_bytes()
    assert 'readable pending source' in await ob.dream(detail_ids=source)
    assert (await ob.bucket_mgr.get(source))['content']=='readable pending source'
    assert path.read_bytes()==before


@pytest.mark.asyncio
async def test_incomplete_supersession_scan_rejects_before_token_consumption(ob):
    source=await ob.bucket_mgr.create('source');other=await ob.bucket_mgr.create('other')
    token=await preview(ob,[source])
    path=Path(ob.bucket_mgr._find_bucket_file(other));post=frontmatter.load(path);post['superseded_by']=123;path.write_text(frontmatter.dumps(post))
    result=await ob._delete_with_confirmation([source],token)
    assert result['status']=='blocked' and token in ob._mutation_confirm_tokens
    assert ob.bucket_mgr.confirmed_delete_rows()==[] and await ob.bucket_mgr.get(source)


@pytest.mark.asyncio
async def test_unrelated_writes_and_successor_edits_before_intent_do_not_replan(ob,monkeypatch):
    source=await ob.bucket_mgr.create('source');successor=await ob.bucket_mgr.create('successor')
    await ob.bucket_mgr.update(source,superseded_by=successor,_supersession_reverse=True)
    token=await preview(ob,[source])
    async def cancel(*a):raise asyncio.CancelledError()
    monkeypatch.setattr(ob.bucket_mgr,'confirmed_delete_pause',cancel)
    with pytest.raises(asyncio.CancelledError):await ob._delete_with_confirmation([source],token)
    frozen=ob.bucket_mgr.confirmed_delete_rows()[0]['plan']
    await ob.bucket_mgr.create('unrelated new bucket')
    await ob.bucket_mgr.update(successor,name='concurrent successor',supersedes=[source,'unrelated'])
    before=(await ob.bucket_mgr.get(successor))['metadata']
    from related_integrity import scan_relation_store
    body=scan_relation_store(ob.bucket_mgr.base_dir).endpoint(successor).body
    monkeypatch.setattr(ob.bucket_mgr,'confirmed_delete_pause',lambda *a:asyncio.sleep(0))
    result=await ob._delete_with_confirmation([source],token)
    assert result['status']=='deleted',result
    row=assert_effects(ob.bucket_mgr,source)
    assert row['plan']==frozen and ob.bucket_mgr.relation_store.lookup(frozen['relation_key'])['plan']==frozen['relation_plan']
    metadata=(await ob.bucket_mgr.get(successor))['metadata']
    assert metadata['name']=='concurrent successor' and metadata['supersedes']==['unrelated']
    assert {k:v for k,v in metadata.items() if k!='supersedes'}=={k:v for k,v in before.items() if k!='supersedes'}
    assert scan_relation_store(ob.bucket_mgr.base_dir).endpoint(successor).body==body

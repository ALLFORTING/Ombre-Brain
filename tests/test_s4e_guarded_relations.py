"""The relation journal enforces injected capabilities, including on recovery."""
import asyncio
import sqlite3
from pathlib import Path

import pytest

from bucket_manager import BucketIdempotencyError, BucketManager
from bucket_write_lock import bucket_write_scope
from related_integrity import RelationStore, RelatedError, digest, scan_relation_store
from tests.test_w8_related_integrity import store, write_bucket, graph


def accepted(manager, identity='A'):
    inventory = scan_relation_store(manager.base_dir)
    incarnations = {i: manager._confirmed_file_guard(e.path)['incarnation'] for i,e in inventory.endpoints.items()}
    plan = manager.freeze_confirmed_delete(inventory, identity, incarnations)
    delete_id = manager.accept_confirmed_delete('synthetic-token-hash', digest([identity]), [identity], 0, plan)
    context = manager.claim_confirmed_delete(delete_id, 'bound-owner')
    return context


def pending_intent(manager, monkeypatch, boundary='after_intent'):
    context = accepted(manager)
    manager._confirmed_step(context)
    def cancel(point,*args):
        if point == boundary: raise asyncio.CancelledError()
    monkeypatch.setattr(manager.relation_store,'checkpoint',cancel)
    with pytest.raises(asyncio.CancelledError): manager._confirmed_relation(context)
    monkeypatch.setattr(manager.relation_store,'checkpoint',lambda *a:None)
    return context,manager._confirmed_row(context['delete_id'])


def resume(manager,row,capability,guard=None):
    return manager.relation_store.commit(lambda inv: row['plan']['relation_plan'],
        operation_key=row['plan']['relation_key'], request_digest=digest(row['plan']['relation_plan']['request']),
        execution_guard=guard if guard is not None else manager._confirmed_descriptor(row), capability=capability)


def test_guarded_intent_without_resolver_or_capability_is_deferred(store,monkeypatch):
    context,row=pending_intent(store,monkeypatch)
    plain=RelationStore(store.base_dir)
    plain.recover()
    assert 'A' in graph(store)
    assert plain.lookup(row['plan']['relation_key'])['status']=='pending'
    with pytest.raises(RelatedError,match='execution_deferred'):
        plain.commit(lambda inv:row['plan']['relation_plan'],operation_key=row['plan']['relation_key'],
            request_digest=digest(row['plan']['relation_plan']['request']),execution_guard=store._confirmed_descriptor(row))
    with pytest.raises(RelatedError,match='execution_deferred'):resume(store,row,None)
    assert 'A' in graph(store)


@pytest.mark.parametrize('field,value',[('owner','other'),('epoch',99),('root_binding','wrong-root'),
    ('request_digest','wrong'),('plan_digest','wrong'),('delete_id','wrong'),('lease_until',0)])
def test_bound_capability_mismatch_never_executes(store,monkeypatch,field,value):
    context,row=pending_intent(store,monkeypatch)
    capability=store.confirmed_delete_capability(context);capability[field]=value
    with pytest.raises((RelatedError,BucketIdempotencyError)):resume(store,row,capability)
    assert 'A' in graph(store)
    assert store.relation_store.lookup(row['plan']['relation_key'])['progress']==0


def test_expired_capability_and_valid_capability(store,monkeypatch):
    context,row=pending_intent(store,monkeypatch)
    capability=store.confirmed_delete_capability(context)
    with sqlite3.connect(store.history_db_path) as conn:
        conn.execute('UPDATE ob_confirmed_delete_operations SET lease_until=0')
    with pytest.raises(BucketIdempotencyError,match='claim_stale'):resume(store,row,capability)
    replacement=store.claim_confirmed_delete(row['delete_id'],'replacement')
    assert replacement['epoch']==context['epoch']+1
    resume(store,row,store.confirmed_delete_capability(replacement))
    assert 'A' not in graph(store)
    assert store.relation_store.lookup(row['plan']['relation_key'])['status']=='complete'


def test_generic_recovery_defers_guarded_but_recovers_ordinary(store,monkeypatch):
    _,row=pending_intent(store,monkeypatch)
    def cancel(point,*args):
        if point=='after_intent':raise asyncio.CancelledError()
    monkeypatch.setattr(store.relation_store,'checkpoint',cancel)
    with pytest.raises(asyncio.CancelledError):store.mutate_related('B',add=['C'])
    monkeypatch.setattr(store.relation_store,'checkpoint',lambda *a:None)
    store.recover_related_operations()
    assert graph(store)=={'A':[],'B':['C'],'C':['B']}
    assert store.relation_store.lookup(row['plan']['relation_key'])['status']=='pending'


@pytest.mark.parametrize('at_unlink',[False,True])
def test_incoming_supersession_after_intent_blocks_unlink(store,monkeypatch,at_unlink):
    context,row=pending_intent(store,monkeypatch)
    def introduce(*args):write_bucket(store.base_dir,'C',superseded_by='A')
    if at_unlink:
        monkeypatch.setattr(store.relation_store,'checkpoint',lambda point,*a:introduce() if point=='before_delete' else None)
    else:introduce()
    with pytest.raises(RelatedError,match='incoming_supersession'):
        resume(store,row,store.confirmed_delete_capability(context))
    assert 'A' in graph(store)


@pytest.mark.parametrize('mutation',[lambda m:m.mutate_related('A',add=['B']),
    lambda m:m.mutate_related('B',add=['A']),lambda m:m.relation_store.commit(lambda inv:__import__('related_integrity').plan_delete(inv,'A'))])
def test_ordinary_operation_cannot_involve_pending_source(store,mutation):
    accepted(store)
    with pytest.raises((RelatedError,BucketIdempotencyError),match='source_pending'):mutation(store)
    assert graph(store)=={'A':[],'B':[],'C':[]}


def test_guard_descriptor_is_digest_bound(store,monkeypatch):
    context,row=pending_intent(store,monkeypatch)
    operation=store.relation_store.lookup(row['plan']['relation_key'])
    operation['execution_guard']['plan_digest']='tampered'
    with bucket_write_scope(store.base_dir):store.relation_store._save(operation)
    with pytest.raises(RelatedError,match='guard_conflict'):
        resume(store,row,store.confirmed_delete_capability(context),operation['execution_guard'])
    assert 'A' in graph(store)


@pytest.mark.parametrize('boundary',['after_progress','after_complete'])
def test_durable_unlink_or_completion_preserves_identical_recreated_source(store,monkeypatch,boundary):
    source=Path(store._find_bucket_file('A')); preimage=source.read_bytes()
    context,row=pending_intent(store,monkeypatch,boundary)
    source.write_bytes(preimage)
    if boundary=='after_complete':
        resume(store,row,store.confirmed_delete_capability(context))
    else:
        with pytest.raises((RelatedError,BucketIdempotencyError)):resume(store,row,store.confirmed_delete_capability(context))
    assert source.read_bytes()==preimage
    store.recover_related_operations()
    assert source.read_bytes()==preimage


def test_unrecorded_unlink_ack_loss_still_recovers(store,monkeypatch):
    context,row=pending_intent(store,monkeypatch,'after_delete')
    assert store.relation_store.lookup(row['plan']['relation_key'])['progress']==0
    resume(store,row,store.confirmed_delete_capability(context))
    assert 'A' not in graph(store)
    assert store.relation_store.lookup(row['plan']['relation_key'])['status']=='complete'


@pytest.mark.parametrize('boundary',['before_delete','after_delete','before_complete'])
def test_stale_worker_cannot_effect_checkpoint_or_release(store,monkeypatch,boundary):
    context,row=pending_intent(store,monkeypatch)
    replacement={}
    def takeover(point,*args):
        if point==boundary:
            with sqlite3.connect(store.history_db_path) as conn:
                conn.execute('UPDATE ob_confirmed_delete_operations SET lease_until=0')
            replacement.update(store.claim_confirmed_delete(row['delete_id'],'replacement'))
    monkeypatch.setattr(store.relation_store,'checkpoint',takeover)
    with pytest.raises(RelatedError,match='claim_stale'):resume(store,row,store.confirmed_delete_capability(context))
    operation=store.relation_store.lookup(row['plan']['relation_key'])
    assert operation['status']=='pending' and operation['progress']==(1 if boundary=='before_complete' else 0)
    assert ('A' in graph(store)) == (boundary=='before_delete')
    store.release_confirmed_delete(context)
    assert store._confirmed_row(row['delete_id'])['owner_instance']=='replacement'
    monkeypatch.setattr(store.relation_store,'checkpoint',lambda *a:None)
    resume(store,row,store.confirmed_delete_capability(replacement))
    assert 'A' not in graph(store)


def test_restart_child_runner_claims_guarded_intent(store,monkeypatch):
    context,row=pending_intent(store,monkeypatch)
    store.release_confirmed_delete(context)
    reopened=BucketManager({'buckets_dir':store.base_dir})
    assert reopened._confirmed_row(row['delete_id'])['status']=='completed'
    assert 'A' not in graph(reopened)


def test_ordinary_recorded_unlink_does_not_repeat_but_unrecorded_ack_loss_can(store,monkeypatch):
    from related_integrity import plan_delete
    path=Path(store._find_bucket_file('A')); raw=path.read_bytes()
    def cancel(point,*args):
        if point=='after_progress':raise asyncio.CancelledError()
    monkeypatch.setattr(store.relation_store,'checkpoint',cancel)
    with pytest.raises(asyncio.CancelledError):store.relation_store.commit(lambda inv:plan_delete(inv,'A'))
    path.write_bytes(raw)
    monkeypatch.setattr(store.relation_store,'checkpoint',lambda *a:None)
    with pytest.raises(RelatedError,match='recovery_conflict'):store.recover_related_operations()
    assert path.read_bytes()==raw


@pytest.mark.parametrize('metadata',[{'supersedes':['A']},{'related_buckets':'A'},{'supersedes':123},{'superseded_by':123}])
def test_new_incoming_reference_or_incomplete_scan_blocks_guarded_intent(store,monkeypatch,metadata):
    context,row=pending_intent(store,monkeypatch)
    write_bucket(store.base_dir,'C',**metadata)
    with pytest.raises(RelatedError):resume(store,row,store.confirmed_delete_capability(context))
    assert 'A' in graph(store)


def test_expiry_during_fsync_cannot_publish_relation_effect(store,monkeypatch):
    store.mutate_related('A',add=['B'])
    context,row=pending_intent(store,monkeypatch)
    original=__import__('related_integrity').os.fsync
    def expire(fd):
        original(fd)
        with sqlite3.connect(store.history_db_path) as conn:
            conn.execute('UPDATE ob_confirmed_delete_operations SET lease_until=0')
    monkeypatch.setattr(__import__('related_integrity').os,'fsync',expire)
    with pytest.raises(RelatedError,match='claim_stale'):resume(store,row,store.confirmed_delete_capability(context))
    assert graph(store)['B']==['A'] and 'A' in graph(store)
    assert store.relation_store.lookup(row['plan']['relation_key'])['progress']==0


def test_generic_recovery_defers_ordinary_plan_refused_by_resolver_and_continues(store,monkeypatch):
    def cancel(point,*a):
        if point=='after_intent':raise asyncio.CancelledError()
    monkeypatch.setattr(store.relation_store,'checkpoint',cancel)
    with pytest.raises(asyncio.CancelledError):store.mutate_related('A',add=['B'])
    accepted(store)
    with pytest.raises(asyncio.CancelledError):store.mutate_related('B',add=['C'])
    monkeypatch.setattr(store.relation_store,'checkpoint',lambda *a:None)
    store.recover_related_operations()
    assert graph(store)=={'A':[],'B':['C'],'C':['B']}
    operations=store.relation_store.operations()
    assert operations[0]['status']=='pending' and operations[0]['progress']==0
    assert operations[1]['status']=='complete'


def test_valid_claim_does_not_authorize_relation_before_history_vector_phase(store):
    context=accepted(store);row=store._confirmed_row(context['delete_id'])
    with pytest.raises(RelatedError,match='relation_phase_conflict'):
        resume(store,row,store.confirmed_delete_capability(context))
    assert 'A' in graph(store) and store.relation_store.operations()==[]
    assert not store.get_history('A')

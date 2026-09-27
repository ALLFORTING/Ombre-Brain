import asyncio
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest
import yaml
from bucket_manager import BucketManager
from related_integrity import (RelatedError, RelationStore, plan_mutation, plan_delete,
                               plan_repair, scan_relation_store, digest)
from tests.test_w8_related_integrity import store, write_bucket, graph
from tests.test_w8_related_repair import snapshot


@pytest.mark.parametrize('kind',['add','remove'])
@pytest.mark.parametrize('boundary',['before_intent','after_intent','before_publish','after_publish','after_progress','before_complete','after_complete'])
@pytest.mark.parametrize('nth',[1,2])
def test_failures_at_every_relation_boundary(store,monkeypatch,kind,boundary,nth):
    if kind=='remove':store.mutate_related('A',add=['B'])
    count=0
    def fail(point,operation,step=None):
        nonlocal count
        if point==boundary:
            count+=1
            if count==nth:raise OSError('injected publication failure')
    monkeypatch.setattr(store.relation_store,'checkpoint',fail)
    before=graph(store)
    try:store.mutate_related('A',**({'add':['B']} if kind=='add' else {'remove':['B']}))
    except (RelatedError,OSError):pass
    monkeypatch.setattr(store.relation_store,'checkpoint',lambda *a:None)
    store.recover_related_operations()
    if boundary=='before_intent' and nth==1:
        assert graph(store)==before
    else:
        assert graph(store)['A']==(['B'] if kind=='add' else [])
        assert graph(store)['B']==(['A'] if kind=='add' else [])
    assert all(op['status']=='complete' for op in store.relation_store.operations())


@pytest.mark.parametrize('boundary',['after_intent','before_publish','after_publish','after_progress','before_complete'])
def test_cancelled_error_is_recorded_and_recoverable(store,monkeypatch,boundary):
    def cancel(point,*args):
        if point==boundary:raise asyncio.CancelledError()
    monkeypatch.setattr(store.relation_store,'checkpoint',cancel)
    with pytest.raises(asyncio.CancelledError):store.mutate_related('A',add=['B'])
    assert store.relation_store.operations()[0]['status']=='pending'
    monkeypatch.setattr(store.relation_store,'checkpoint',lambda *a:None)
    store.recover_related_operations()
    assert graph(store)['A']==['B'] and graph(store)['B']==['A']


@pytest.mark.parametrize('kind',['add','remove','delete','repair','merge'])
@pytest.mark.parametrize('boundary',['after_intent','after_publish','after_progress','before_complete','after_complete'])
def test_real_process_kill_rolls_forward(store,kind,boundary):
    if kind in ('remove','delete','merge'):store.mutate_related('A',add=['B'])
    if kind=='repair':write_bucket(store.base_dir,'A','A,B,B,missing')
    code='''import os,sys
from related_integrity import RelationStore,plan_delete,plan_mutation,plan_repair
s=RelationStore(sys.argv[1])
def die(point,*args):
    if point==sys.argv[3]:
        print('ready',flush=True)
        sys.stdin.readline()
s.checkpoint=die
kind=sys.argv[2]
if kind=='add':s.mutate('A',add=['B'])
elif kind=='remove':s.mutate('A',remove=['B'])
elif kind=='delete':s.commit(lambda i:plan_delete(i,'B'))
elif kind=='merge':s.commit(lambda i:plan_delete(i,'B',target_id='C'))
else:s.commit(plan_repair)
'''
    with subprocess.Popen([sys.executable,'-c',code,store.base_dir,kind,boundary],
                          stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True) as child:
        try:
            assert child.stdout.readline().strip()=='ready',child.stderr.read()
            child.kill()
            assert child.wait(timeout=5)==-9
        finally:
            if child.poll() is None:
                child.kill()
            child.communicate(timeout=5)
    # Real startup entry point, not just an in-memory repair.
    reopened=BucketManager({'buckets_dir':store.base_dir})
    result=graph(reopened)
    assert all(op['status']=='complete' for op in reopened.relation_store.operations())
    if kind in ('add','repair'):assert result['A']==['B'] and result['B']==['A']
    elif kind=='remove':assert result['A']==result['B']==[]
    elif kind=='delete':assert 'B' not in result and result['A']==[]
    else:assert 'B' not in result and result['A']==['C'] and result['C']==['A']


def test_startup_is_lazy_and_never_repairs_history(store):
    before=snapshot(store.base_dir)
    store.recover_related_operations()
    assert snapshot(store.base_dir)==before
    assert not store.relation_store.operations()
    write_bucket(store.base_dir,'A','B')
    BucketManager({'buckets_dir':store.base_dir})
    assert graph(store)['B']==[]
    assert not store.relation_store.operations()


def test_recovery_preserves_new_unrelated_metadata_and_activity(store,monkeypatch):
    def fail(point,*args):
        if point=='after_intent':raise OSError('pause')
    monkeypatch.setattr(store.relation_store,'checkpoint',fail)
    with pytest.raises(RelatedError):store.mutate_related('A',add=['B'])
    # Later unrelated edit must survive; recovery must not replay user activity.
    write_bucket(store.base_dir,'A','',tags=['new'],last_active='2003-01-01',updated_at='2003-01-02')
    before=scan_relation_store(store.base_dir).endpoints['A']
    monkeypatch.setattr(store.relation_store,'checkpoint',lambda *a:None)
    store.recover_related_operations()
    after=scan_relation_store(store.base_dir).endpoints['A']
    assert after.metadata=={**before.metadata,'related_buckets':'B'}
    assert after.body==before.body


def test_recovery_conflict_does_not_overwrite_and_blocks_new_operation(store,monkeypatch):
    def fail(point,*args):
        if point=='after_publish':raise OSError('pause')
    monkeypatch.setattr(store.relation_store,'checkpoint',fail)
    with pytest.raises(RelatedError):store.mutate_related('A',add=['B'])
    write_bucket(store.base_dir,'A','C')
    monkeypatch.setattr(store.relation_store,'checkpoint',lambda *a:None)
    before=graph(store)
    with pytest.raises(RelatedError,match='recovery_conflict'):store.mutate_related('C',add=['B'])
    assert graph(store)==before
    assert store.relation_store.operations()[0]['status']=='blocked'


@pytest.mark.parametrize('boundary',['before_delete','after_delete'])
@pytest.mark.asyncio
async def test_delete_failure_cleanup_then_roll_forward(store,monkeypatch,boundary):
    write_bucket(store.base_dir,'A','B')
    def fail(point,*args):
        if point==boundary:raise OSError('unlink boundary')
    monkeypatch.setattr(store.relation_store,'checkpoint',fail)
    with pytest.raises(RelatedError):await store.delete('B')
    assert graph(store)['A']==[]
    monkeypatch.setattr(store.relation_store,'checkpoint',lambda *a:None)
    store.recover_related_operations()
    assert 'B' not in graph(store)


@pytest.mark.asyncio
async def test_delete_vector_failure_no_relation_intent(store):
    async def fail(identity):raise OSError('vector cleanup')
    store.embedding_engine=SimpleNamespace(delete_embedding=fail)
    assert not await store.delete('B')
    assert not store.relation_store.operations() and 'B' in graph(store)


def test_operation_key_collision_response_loss_and_repair_pending_other(store,monkeypatch):
    planner=lambda i:plan_mutation(i,'A',add=['B'])
    store.relation_store.commit(planner,operation_key='request',request_digest='one')
    before=snapshot(store.base_dir)
    assert not store.relation_store.commit(planner,operation_key='request',request_digest='one')['changed']
    assert snapshot(store.base_dir)==before
    with pytest.raises(RelatedError,match='key_conflict'):
        store.relation_store.commit(planner,operation_key='request',request_digest='two')
    plan=plan_repair(scan_relation_store(store.base_dir))
    def fail(point,*args):
        if point=='after_intent':raise OSError('pause')
    monkeypatch.setattr(store.relation_store,'checkpoint',fail)
    with pytest.raises(RelatedError):store.mutate_related('A',remove=['B'])
    monkeypatch.setattr(store.relation_store,'checkpoint',lambda *a:None)
    before=snapshot(store.base_dir)
    with pytest.raises(RelatedError,match='operation_pending'):store.relation_store.apply_repair(plan)
    assert snapshot(store.base_dir)==before


@pytest.mark.asyncio
async def test_merge_child_complete_parent_progress_loss_no_body_replay(tmp_path,monkeypatch):
    import re
    from tests.test_w8_related_integrity import load_server
    server=load_server(tmp_path,monkeypatch)
    target=await server.bucket_mgr.create('target body')
    source=await server.bucket_mgr.create('unique source fragment')
    neighbor=await server.bucket_mgr.create('neighbor')
    server.bucket_mgr.mutate_related(source,add=[neighbor])
    preview=await server.trace(target,merge=source)
    token=re.search(r'confirm_token: (\S+)',preview).group(1)
    original=server.bucket_mgr.write_merge_operation
    def lose_progress(operation_id,plan,**kwargs):
        if 'delete_source' in kwargs.get('completed',[]):raise OSError('parent progress lost')
        return original(operation_id,plan,**kwargs)
    monkeypatch.setattr(server.bucket_mgr,'write_merge_operation',lose_progress)
    failed=await server.trace(target,merge=source,confirm_token=token)
    assert 'partial failure' in failed
    assert await server.bucket_mgr.get(source) is None
    monkeypatch.setattr(server.bucket_mgr,'write_merge_operation',original)
    # Real reinitialization recovers relation child before parent resume.
    server.bucket_mgr=BucketManager(server.config)
    resumed=await server.trace(target,merge=source)
    assert '已合并' in resumed,resumed
    assert (await server.bucket_mgr.get(target))['content'].count('unique source fragment')==1
    assert graph(server.bucket_mgr)[neighbor]==[target]
    assert server.bucket_mgr.read_merge_operations()[0]['status']=='complete'


@pytest.mark.asyncio
async def test_old_unfinished_merge_with_source_still_present_upgrades_safely(tmp_path,monkeypatch):
    import re
    from unittest.mock import AsyncMock
    from tests.test_w8_related_integrity import load_server
    server=load_server(tmp_path,monkeypatch)
    target=await server.bucket_mgr.create('target body')
    source=await server.bucket_mgr.create('source body')
    neighbor=await server.bucket_mgr.create('neighbor')
    server.bucket_mgr.mutate_related(source,add=[neighbor])
    preview=await server.trace(target,merge=source)
    token=re.search(r'confirm_token: (\S+)',preview).group(1)
    original=server._execute_merge_operation
    monkeypatch.setattr(server,'_execute_merge_operation',AsyncMock(return_value='paused'))
    await server.trace(target,merge=source,confirm_token=token)
    record=server.bucket_mgr.read_merge_operations()[0]
    # Stored old schema, not an in-memory plan rewrite on resume.
    import json,sqlite3
    old_plan=dict(record['plan']);old_plan.pop('related_inventory')
    with sqlite3.connect(server.bucket_mgr.history_db_path) as conn:
        conn.execute('UPDATE merge_operations SET plan_json=? WHERE operation_id=?',
                     (json.dumps(old_plan,ensure_ascii=False,sort_keys=True),record['operation_id']))
    monkeypatch.setattr(server,'_execute_merge_operation',original)
    result=await server.trace(target,merge=source)
    assert '已合并' in result,result
    assert graph(server.bucket_mgr)[neighbor]==[target]
    assert any(op['key']==f"merge:{record['operation_id']}:related-delete"
               for op in server.bucket_mgr.relation_store.operations())


def test_unchanged_reverse_endpoint_conflict_is_checked_before_publish(store,monkeypatch):
    write_bucket(store.base_dir,'B','A')
    def fail(point,*args):
        if point=='after_intent':raise OSError('pause')
    monkeypatch.setattr(store.relation_store,'checkpoint',fail)
    with pytest.raises(RelatedError):store.mutate_related('A',add=['B'])
    write_bucket(store.base_dir,'B','C')
    monkeypatch.setattr(store.relation_store,'checkpoint',lambda *a:None)
    with pytest.raises(RelatedError,match='recovery_conflict'):store.recover_related_operations()
    assert graph(store)['A']==[] and graph(store)['B']==['C']


@pytest.mark.parametrize('kind',['remove','delete','repair','merge'])
@pytest.mark.parametrize('boundary',['after_intent','after_publish'])
def test_other_mutations_cancellation_roll_forward(store,monkeypatch,kind,boundary):
    store.mutate_related('A',add=['B'])
    if kind=='repair':write_bucket(store.base_dir,'A','A,B,B,missing')
    plan=plan_repair(scan_relation_store(store.base_dir)) if kind=='repair' else None
    def cancel(point,*args):
        if point==boundary:raise asyncio.CancelledError()
    monkeypatch.setattr(store.relation_store,'checkpoint',cancel)
    with pytest.raises(asyncio.CancelledError):
        if kind=='remove':store.mutate_related('A',remove=['B'])
        elif kind=='delete':store.relation_store.commit(lambda i:plan_delete(i,'B'))
        elif kind=='merge':store.relation_store.commit(lambda i:plan_delete(i,'B',target_id='C'))
        else:store.relation_store.apply_repair(plan)
    monkeypatch.setattr(store.relation_store,'checkpoint',lambda *a:None)
    if kind=='repair':store.relation_store.apply_repair(plan)
    else:store.recover_related_operations()
    current=graph(store)
    if kind=='repair':assert current['A']==['B'] and current['B']==['A']
    elif kind=='remove':assert current['A']==current['B']==[]
    elif kind=='delete':assert 'B' not in current and current['A']==[]
    else:assert 'B' not in current and current['A']==['C'] and current['C']==['A']


def test_recorded_intent_is_bound_to_its_store_root(store,monkeypatch,tmp_path):
    def pause(point,*args):
        if point=='after_intent':raise OSError('pause')
    monkeypatch.setattr(store.relation_store,'checkpoint',pause)
    with pytest.raises(RelatedError):store.mutate_related('A',add=['B'])
    import shutil
    copied=tmp_path/'copied'
    shutil.copytree(store.base_dir,copied)
    with pytest.raises(RelatedError,match='recovery_conflict'):
        RelationStore(copied).recover()
    assert scan_relation_store(copied).endpoints['A'].related.ids==()


@pytest.mark.asyncio
@pytest.mark.parametrize('boundary',['before_intent','after_publish'])
async def test_mixed_trace_reports_ordinary_success_and_relation_failure(tmp_path,monkeypatch,boundary):
    from tests.test_w8_related_integrity import load_server
    server=load_server(tmp_path,monkeypatch)
    source=await server.bucket_mgr.create('original')
    target=await server.bucket_mgr.create('target')
    def fail(point,*args):
        if point==boundary:raise OSError('injected private path must not be returned')
    journal=server.bucket_mgr.relation_store
    monkeypatch.setattr(journal,'checkpoint',fail)
    result=await server.trace(source,content='single fragment',append=True,related=target)
    assert 'related failure' in result and 'ordinary mutation=applied' in result
    assert 'single fragment' in (await server.bucket_mgr.get(source))['content']
    assert 'injected private path' not in result
    if boundary=='before_intent':assert journal.operations()==[]
    else:assert journal.operations()[0]['status']=='pending' and journal.operations()[0]['id'] in result
    monkeypatch.setattr(journal,'checkpoint',lambda *a:None)
    await server.trace(source,related=target)
    assert graph(server.bucket_mgr)[source]==[target] and graph(server.bucket_mgr)[target]==[source]
    assert (await server.bucket_mgr.get(source))['content'].count('single fragment')==1


@pytest.mark.asyncio
async def test_delete_pre_intent_failure_preserves_r5_supersession_cleanup(tmp_path,monkeypatch):
    from tests.test_w8_related_integrity import load_server
    server=load_server(tmp_path,monkeypatch)
    source=await server.bucket_mgr.create('source')
    successor=await server.bucket_mgr.create('successor')
    await server.trace(source,superseded_by=successor)
    prior=(await server.bucket_mgr.get(successor))['metadata'].get('supersedes')
    def fail(point,*args):
        if point=='before_intent':raise OSError('journal unavailable')
    monkeypatch.setattr(server.bucket_mgr.relation_store,'checkpoint',fail)
    bucket=await server.bucket_mgr.get(source)
    ok,response=await server._execute_trace_delete(bucket)
    assert not ok and 'related_commit_failed' in response
    assert (await server.bucket_mgr.get(successor))['metadata'].get('supersedes')==prior
    assert await server.bucket_mgr.get(source)
    assert server.bucket_mgr.relation_store.operations()==[]


@pytest.mark.parametrize('persisted',[False,True])
def test_first_intent_acknowledgment_failure_is_safe_to_retry(store,monkeypatch,persisted):
    original=store.relation_store._save
    def uncertain(operation):
        if persisted:original(operation)
        raise OSError('intent acknowledgment lost')
    monkeypatch.setattr(store.relation_store,'_save',uncertain)
    with pytest.raises(RelatedError,match='related_intent_unconfirmed') as caught:
        store.mutate_related('A',add=['B'])
    assert caught.value.operation_id
    assert graph(store)['A']==graph(store)['B']==[]
    assert bool(store.relation_store.operations()) is persisted
    monkeypatch.setattr(store.relation_store,'_save',original)
    store.mutate_related('A',add=['B'])
    assert graph(store)['A']==['B'] and graph(store)['B']==['A']


@pytest.mark.asyncio
async def test_delete_committed_intent_ack_loss_never_restores_supersession(tmp_path,monkeypatch):
    from tests.test_w8_related_integrity import load_server
    server=load_server(tmp_path,monkeypatch)
    source=await server.bucket_mgr.create('source')
    successor=await server.bucket_mgr.create('successor')
    await server.trace(source,superseded_by=successor)
    journal=server.bucket_mgr.relation_store
    original=journal._save
    def lose_ack(operation):
        original(operation)
        raise OSError('acknowledgment lost')
    monkeypatch.setattr(journal,'_save',lose_ack)
    ok,response=await server._execute_trace_delete(await server.bucket_mgr.get(source))
    assert not ok and 'related_intent_unconfirmed' in response
    assert source not in (await server.bucket_mgr.get(successor))['metadata'].get('supersedes',[])
    monkeypatch.setattr(journal,'_save',original)
    server.bucket_mgr.recover_related_operations()
    assert await server.bucket_mgr.get(source) is None
    assert source not in (await server.bucket_mgr.get(successor))['metadata'].get('supersedes',[])


def test_existing_wal_journal_mode_is_preserved_and_intents_visible(store):
    import sqlite3
    with sqlite3.connect(store.history_db_path) as keeper:
        assert keeper.execute('PRAGMA journal_mode=WAL').fetchone()[0]=='wal'
        keeper.execute('PRAGMA wal_autocheckpoint=0')
        before=Path(store.history_db_path).read_bytes()
        assert store.relation_store.operations()==[]
        assert Path(store.history_db_path).read_bytes()==before
        assert not keeper.execute("SELECT 1 FROM sqlite_master WHERE name='ob_related_operations'").fetchone()
        store.mutate_related('A',add=['B'])
        assert store.relation_store.operations()[0]['status']=='complete'
        assert keeper.execute('PRAGMA journal_mode').fetchone()[0]=='wal'
        assert graph(store)['A']==['B'] and graph(store)['B']==['A']


def test_sigkill_wal_intent_recovers_at_manager_startup(store):
    import sqlite3
    keeper=sqlite3.connect(store.history_db_path)
    try:
        keeper.execute('PRAGMA journal_mode=WAL')
        keeper.execute('PRAGMA wal_autocheckpoint=0')
        code="""import sys
from related_integrity import RelationStore
s=RelationStore(sys.argv[1])
def pause(point,*args):
    if point=='after_publish':
        print('ready',flush=True)
        sys.stdin.readline()
s.checkpoint=pause
s.mutate('A',add=['B'])
"""
        with subprocess.Popen([sys.executable,'-c',code,store.base_dir],stdin=subprocess.PIPE,
                              stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True) as child:
            try:
                assert child.stdout.readline().strip()=='ready',child.stderr.read()
                child.kill();assert child.wait(timeout=5)==-9
            finally:
                if child.poll() is None:child.kill()
                child.communicate(timeout=5)
        assert store.relation_store.operations()[0]['status']=='pending'
        reopened=BucketManager({'buckets_dir':store.base_dir})
        assert graph(reopened)['A']==['B'] and graph(reopened)['B']==['A']
        assert reopened.relation_store.operations()[0]['status']=='complete'
        assert keeper.execute('PRAGMA journal_mode').fetchone()[0]=='wal'
    finally:
        keeper.close()

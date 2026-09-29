"""Real SIGKILL, restart and interprocess claim fencing on synthetic roots."""
import asyncio
import select
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from bucket_manager import BucketManager
from tests.test_w8_related_integrity import store, graph
from tests.test_s4e_guarded_relations import accepted


def line(child):
    assert select.select([child.stdout],[],[],15)[0], 'child did not reach boundary'
    value=child.stdout.readline().strip()
    assert value,value if child.poll() is None else child.stderr.read()
    return value


KILL_CODE = r"""
import asyncio,sys
from bucket_manager import BucketManager
from tests.test_s4e_guarded_relations import accepted
manager=BucketManager({'buckets_dir':sys.argv[1]})
boundary=sys.argv[2]
def pause(point,*args):
    if point==boundary:
        print('READY',flush=True)
        sys.stdin.readline()
manager.confirmed_delete_checkpoint=pause
manager.relation_store.checkpoint=pause
context=accepted(manager)
asyncio.run(manager.execute_confirmed_delete(context['delete_id']))
"""


@pytest.mark.parametrize('boundary',['before_accept_commit','after_accept_commit','before_history_commit',
    'after_history_commit','after_vector_commit','child.history_vector_resolved','before_intent','after_intent',
    'before_publish','after_publish','before_delete','after_delete','after_progress','before_complete',
    'after_complete','after_relation_commit','child.relation_delete_complete','before_successor_publish',
    'after_successor_publish','child.successor_cleanup_complete','child.completed'])
def test_sigkill_and_real_startup_recovery(store,boundary):
    store.mutate_related('A',add=['B'])
    asyncio.run(store.update('A',superseded_by='C',_supersession_reverse=True))
    # Child runner owns the only claim; accepted() helper releases its test claim.
    code=KILL_CODE.replace("asyncio.run(manager.execute_confirmed_delete(context['delete_id']))",
        "manager.release_confirmed_delete(context)\nasyncio.run(manager.execute_confirmed_delete(context['delete_id']))")
    with subprocess.Popen([sys.executable,'-u','-c',code,store.base_dir,boundary],
        stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True) as child:
        try:
            assert line(child)=='READY'
            child.kill();assert child.wait(timeout=5)==-9
        finally:
            if child.poll() is None:child.kill()
            child.communicate(timeout=5)
    rows=store.confirmed_delete_rows()
    if boundary=='before_accept_commit':
        assert rows==[] and 'A' in graph(store)
        assert not store.get_history('A')
        return
    with sqlite3.connect(store.history_db_path) as conn:
        conn.execute('UPDATE ob_confirmed_delete_operations SET lease_until=0')
    reopened=BucketManager({'buckets_dir':store.base_dir})
    row=reopened.confirmed_delete_rows()[0]
    assert row['status']=='completed',row['last_error_code']
    assert 'A' not in graph(reopened) and graph(reopened)['B']==[]
    assert 'A' not in (asyncio.run(reopened.get('C')))['metadata'].get('supersedes',[])
    assert len(reopened.get_history('A'))==1


@pytest.mark.parametrize('count',[2,4])
def test_multiprocess_retry_has_single_receipts_and_effects(store,count):
    context=accepted(store);store.release_confirmed_delete(context)
    code="""import asyncio,sys
from bucket_manager import BucketManager
manager=BucketManager({'buckets_dir':sys.argv[1]})
print(asyncio.run(manager.execute_confirmed_delete(sys.argv[2])),flush=True)
"""
    children=[subprocess.Popen([sys.executable,'-u','-c',code,store.base_dir,context['delete_id']],
        stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True) for _ in range(count)]
    try:
        for child in children:
            output,error=child.communicate(timeout=20)
            assert child.returncode==0,error
            assert '已遗忘记忆桶: A' in output
    finally:
        for child in children:
            if child.poll() is None:child.kill();child.communicate(timeout=5)
    row=store._confirmed_row(context['delete_id'])
    assert row['status']=='completed' and len(store.get_history('A'))==1
    assert len(store.relation_store.operations())==1


def test_live_process_owner_skipped_then_stale_process_is_fenced(store):
    context=accepted(store);store.release_confirmed_delete(context)
    code="""import sys
from bucket_manager import BucketManager,BucketIdempotencyError
manager=BucketManager({'buckets_dir':sys.argv[1]})
# Constructor must skip the parent's live claim.
context=manager.claim_confirmed_delete(sys.argv[2],'child-owner')
print('READY',flush=True)
sys.stdin.readline()
try:manager._confirmed_relation(context)
except BucketIdempotencyError:print('FENCED',flush=True)
manager.release_confirmed_delete(context)
"""
    # Keep an owner active while the new manager is created, then release before claim.
    parent=store.claim_confirmed_delete(context['delete_id'],'parent-live')
    code=code.replace("context=manager.claim_confirmed_delete", "print('OPENED',flush=True)\nsys.stdin.readline()\ncontext=manager.claim_confirmed_delete")
    with subprocess.Popen([sys.executable,'-u','-c',code,store.base_dir,context['delete_id']],
        stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True) as child:
        try:
            assert line(child)=='OPENED'
            assert store._confirmed_row(context['delete_id'])['owner_instance']=='parent-live'
            assert 'A' in graph(store)
            store.release_confirmed_delete(parent)
            child.stdin.write('claim\n');child.stdin.flush()
            assert line(child)=='READY'
            old=store._confirmed_row(context['delete_id'])
            assert old['owner_instance']=='child-owner'
            # A new process may take over only after the old lease expires.
            assert store.claim_confirmed_delete(context['delete_id'],'too-early') is None
            with sqlite3.connect(store.history_db_path) as conn:
                conn.execute('UPDATE ob_confirmed_delete_operations SET lease_until=0')
            winner=store.claim_confirmed_delete(context['delete_id'],'winner')
            assert winner['epoch']==old['epoch']+1
            child.stdin.write('resume\n');child.stdin.flush()
            assert line(child)=='FENCED'
            assert child.wait(timeout=5)==0,child.stderr.read()
            assert store._confirmed_row(context['delete_id'])['owner_instance']=='winner'
            assert 'A' in graph(store) and store.relation_store.operations()==[]
            while not store._confirmed_step(winner):pass
            store.release_confirmed_delete(winner)
            assert store._confirmed_row(context['delete_id'])['status']=='completed'
        finally:
            if child.poll() is None:child.kill()
            child.communicate(timeout=5)

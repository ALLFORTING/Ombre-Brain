"""S-4A: real durable boundaries on isolated storage, including process death."""
import asyncio
import json
import multiprocessing
import os
import sqlite3
import threading
from pathlib import Path
from unittest.mock import AsyncMock

import frontmatter
import pytest
import pytest_asyncio

import server
from bucket_manager import BucketManager, BucketIdempotencyError
from bucket_write_lock import bucket_write_scope
from embedding_engine import EmbeddingEngine

pytestmark = pytest.mark.asyncio


def join_worker(proc):
    """Bound cold interpreter startup and clean up only this owned test worker."""
    proc.join(45)
    if proc.is_alive():
        proc.terminate()
        proc.join(5)
        raise AssertionError('isolated test worker did not finish')


@pytest_asyncio.fixture
async def setup(test_config, monkeypatch):
    # Spawned interpreters import server before entering their test worker.
    # Keep that startup on this small fixture root, not the full-suite root.
    monkeypatch.setenv('OMBRE_BUCKETS_DIR', test_config['buckets_dir'])
    manager = BucketManager(test_config)
    source = await manager.create(name='source', content='original')
    target = await manager.create(name='target', content='target')
    engine = EmbeddingEngine(test_config)
    engine.enabled = True
    engine._generate_embedding = AsyncMock(return_value=[.25, .75])
    manager.embedding_engine = engine
    monkeypatch.setattr(server, 'bucket_mgr', manager)
    with sqlite3.connect(engine.db_path) as conn:
        conn.executescript('''CREATE TABLE vector_writes(id INTEGER PRIMARY KEY);
            CREATE TRIGGER count_vector_writes AFTER INSERT ON embeddings BEGIN
            INSERT INTO vector_writes(id) VALUES(NULL); END;''')
    return manager, engine, source, target, test_config


def counts(manager, engine, source):
    with sqlite3.connect(manager.history_db_path) as conn:
        history = conn.execute('SELECT count(*) FROM bucket_history WHERE bucket_id=?', (source,)).fetchone()[0]
        delta = conn.execute("SELECT count(*) FROM boot_delta_events WHERE bucket_id=? AND event_type='content_updated'", (source,)).fetchone()[0]
    with sqlite3.connect(engine.db_path) as conn:
        vector = conn.execute('SELECT count(*) FROM vector_writes').fetchone()[0]
    return history, delta, vector


async def keyed(source, target='', key='request', **kwargs):
    return await server.trace(bucket_id=source, content='addition', append=True,
                              related=target, operation_id=key, **kwargs)


@pytest.mark.parametrize('key,valid', [('', False), ('x'*128, True), ('x'*129, False),
    ('雪😀', True), ('   ', True), (42, False), (False, False), ([], False)])
async def test_operation_id_boundary(setup, key, valid):
    manager, engine, source, _, _ = setup
    result = await keyed(source, key=key)
    assert ('已修改' in result) == valid
    if not valid:
        assert result == 'operation_id_invalid'
        assert counts(manager, engine, source) == (0, 0, 0)
        assert (await manager.get(source))['content'] == 'original'


async def test_legacy_none_and_deliberate_new_identity(setup):
    manager, engine, source, _, _ = setup
    await keyed(source, key=None)
    await keyed(source, key=None)
    assert (await manager.get(source))['content'].count('addition') == 2
    assert manager.inspect_trace_request('request') is None
    await keyed(source, key='one')
    await keyed(source, key='two')
    assert (await manager.get(source))['content'].count('addition') == 4


async def test_none_keeps_legacy_raw_presence_guard(setup):
    manager, engine, source, _, _ = setup
    assert 'stable' in await server.trace(source, todo_drop=None, operation_id=None)
    assert 'alone' in await server.trace(source, todo_drop='todo_missing', name='', operation_id=None)
    assert counts(manager, engine, source) == (0, 0, 0)


async def test_identity_normalization_conflict_and_namespace(setup):
    manager, engine, source, _, _ = setup
    first = await keyed(source, key='id', tags='one, two', todos='a, b')
    assert await keyed(' ' + source + ' ', key='id', tags='one,two', todos=['a', 'b']) == first
    assert await keyed(source, key='id', tags='different') == 'operation_id_conflict'
    request = manager.inspect_trace_request('id')
    assert json.loads(request['completed_receipt_json']) == request['resolutions']
    assert 'operation_id' not in request['payload']
    with pytest.raises(BucketIdempotencyError, match='operation_id_conflict'):
        manager._claim_trace_request('id', {**request['payload'], 'kind': 'hold'}, request['normalization_context'], 'other')
    assert counts(manager, engine, source) == (1, 1, 1)


@pytest.mark.parametrize('kwargs', [dict(delete=True), dict(merge='other'), dict(todo_done='id'),
    dict(todo_drop='id'), dict(pinned=1), dict(permanent=1), dict(sealed=0),
    dict(superseded_by='none'), dict(confirm_token='old')])
async def test_unsupported_zero_business_writes(setup, kwargs):
    manager, engine, source, _, _ = setup
    before = Path(manager._find_bucket_file(source)).read_bytes()
    assert (await keyed(source, **kwargs)).startswith('unsupported_combination')
    assert Path(manager._find_bucket_file(source)).read_bytes() == before
    assert counts(manager, engine, source) == (0, 0, 0)


async def test_batch_rejected_before_write(setup):
    manager, engine, source, target, _ = setup
    assert (await keyed(source + ',' + target)).startswith('unsupported_combination')
    assert counts(manager, engine, source) == (0, 0, 0)


@pytest.mark.parametrize('phase', ['planned', 'history_committed', 'memory_applied',
    'embedding_resolved', 'delta_resolved', 'relation_resolved', 'completed'])
async def test_cancel_after_effect_before_parent_checkpoint_restart(setup, monkeypatch, phase):
    manager, engine, source, target, config = setup
    original = manager._trace_checkpoint

    def interrupt(context, next_phase=None, **kwargs):
        if next_phase == phase:
            raise asyncio.CancelledError()
        return original(context, next_phase, **kwargs)

    monkeypatch.setattr(manager, '_trace_checkpoint', interrupt)
    with pytest.raises(asyncio.CancelledError):
        await keyed(source, target)
    restarted = BucketManager(config, embedding_engine=engine)
    monkeypatch.setattr(server, 'bucket_mgr', restarted)
    result = await keyed(source, target)
    assert '已修改' in result
    assert (await restarted.get(source))['content'] == 'original\n\naddition'
    assert counts(restarted, engine, source) == (1, 1, 1)
    assert target in (await restarted.get(source))['metadata']['related_buckets']
    assert source in (await restarted.get(target))['metadata']['related_buckets']
    assert restarted.inspect_trace_request('request')['phase'] == 'completed'


async def test_order_and_provider_cancellation(setup, monkeypatch):
    manager, engine, source, target, _ = setup
    phases = []
    checkpoint = manager._trace_checkpoint

    def record(context, phase=None, **kwargs):
        if phase:
            phases.append(phase)
        return checkpoint(context, phase, **kwargs)

    monkeypatch.setattr(manager, '_trace_checkpoint', record)
    entered = asyncio.Event()

    async def provider(*args, **kwargs):
        assert counts(manager, engine, source) == (1, 0, 0)
        assert manager.inspect_trace_request('request')['phase'] == 'memory_applied'
        assert target not in (await manager.get(source))['metadata'].get('related_buckets', [])
        entered.set()
        await asyncio.Event().wait()

    engine._generate_embedding = provider
    active = asyncio.create_task(keyed(source, target))
    await entered.wait()
    active.cancel()
    with pytest.raises(asyncio.CancelledError):
        await active
    assert 'embedding' not in manager.inspect_trace_request('request')['resolutions']
    engine._generate_embedding = AsyncMock(return_value=[1., 2.])
    await keyed(source, target)
    assert phases == ['planned', 'history_committed', 'memory_applied',
                      'embedding_resolved', 'delta_resolved', 'relation_resolved', 'completed']
    assert counts(manager, engine, source) == (1, 1, 1)


async def test_provider_failure_is_best_effort(setup):
    manager, engine, source, target, _ = setup
    engine._generate_embedding = AsyncMock(side_effect=RuntimeError('provider'))
    result = await keyed(source, target)
    assert '已修改' in result
    assert manager.inspect_trace_request('request')['resolutions']['embedding']['outcome'] == 'failed'
    assert await keyed(source, target) == result
    assert engine._generate_embedding.await_count == 1
    assert counts(manager, engine, source) == (1, 1, 0)


async def test_completed_replay_after_change_and_delete(setup):
    manager, engine, source, target, _ = setup
    result = await keyed(source, target)
    await manager.update(source, content='later')
    before = counts(manager, engine, source)
    assert await keyed(source, target) == result
    assert counts(manager, engine, source) == before
    Path(manager._find_bucket_file(source)).unlink()  # isolated fixture; no destructive tool authorization inferred
    assert await keyed(source, target) == result
    assert counts(manager, engine, source) == before


async def test_same_process_waiter_cancel_does_not_cancel_executor(setup):
    manager, engine, source, _, _ = setup
    entered, release = asyncio.Event(), asyncio.Event()

    async def provider(*args, **kwargs):
        entered.set()
        await release.wait()
        return [1., 2.]

    engine._generate_embedding = provider
    active = asyncio.create_task(keyed(source))
    await entered.wait()
    waiter = asyncio.create_task(keyed(source))
    await asyncio.sleep(.01)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    assert not active.done()
    release.set()
    result = await active
    assert await asyncio.gather(*(keyed(source) for _ in range(8))) == [result] * 8
    assert counts(manager, engine, source) == (1, 1, 1)


async def test_target_changed_during_provider_does_not_install_old_vector(setup):
    manager, engine, source, _, _ = setup
    entered, release = asyncio.Event(), asyncio.Event()

    async def provider(*args, **kwargs):
        entered.set()
        await release.wait()
        return [1., 2.]

    engine._generate_embedding = provider
    task = asyncio.create_task(keyed(source))
    await entered.wait()
    path = manager._find_bucket_file(source)
    with bucket_write_scope(manager.base_dir):
        post = frontmatter.load(path)
        post.content = 'later legitimate write'
        post['last_active'] = 'later'
        manager._write_post_atomic(path, post)
    release.set()
    await task
    assert counts(manager, engine, source) == (1, 1, 0)
    assert manager.inspect_trace_request('request')['resolutions']['embedding']['outcome'] == 'superseded_before_refresh'


async def test_preimage_change_before_publish_blocks_stale_plan(setup, monkeypatch):
    manager, engine, source, _, _ = setup
    checkpoint = manager._trace_checkpoint

    def pause(context, phase=None, **kwargs):
        checkpoint(context, phase, **kwargs)
        if phase == 'planned':
            raise asyncio.CancelledError()

    monkeypatch.setattr(manager, '_trace_checkpoint', pause)
    with pytest.raises(asyncio.CancelledError):
        await keyed(source)
    monkeypatch.setattr(manager, '_trace_checkpoint', checkpoint)
    await manager.update(source, content='newer')
    before = counts(manager, engine, source)
    assert await keyed(source) == 'operation_state_conflict'
    assert (await manager.get(source))['content'] == 'newer'
    assert counts(manager, engine, source) == before


@pytest.mark.parametrize('boundary', ['before_intent', 'after_intent', 'after_publish', 'before_complete'])
async def test_relation_interrupt_then_recover(setup, monkeypatch, boundary):
    manager, engine, source, target, config = setup
    def interrupt(name, *args):
        if name == boundary:
            raise asyncio.CancelledError()
    monkeypatch.setattr(manager.relation_store, 'checkpoint', interrupt)
    with pytest.raises(asyncio.CancelledError):
        await keyed(source, target)
    restarted = BucketManager(config, embedding_engine=engine)
    monkeypatch.setattr(server, 'bucket_mgr', restarted)
    await keyed(source, target)
    assert target in (await restarted.get(source))['metadata']['related_buckets']
    assert source in (await restarted.get(target))['metadata']['related_buckets']
    assert counts(restarted, engine, source) == (1, 1, 1)


def _claim_in_process(config, operation_id, payload, normalization, queue):
    manager = BucketManager(config)
    queue.put(manager._claim_trace_request(operation_id, payload, normalization, 'other-process'))


async def test_cross_process_expired_lease_and_delayed_stale_executor(setup):
    manager, engine, source, _, config = setup
    # Start a real request, stop before its provider call, retaining durable plan.
    async def cancel(*args, **kwargs):
        raise asyncio.CancelledError()
    engine._generate_embedding = cancel
    with pytest.raises(asyncio.CancelledError):
        await keyed(source)
    request = manager.inspect_trace_request('request')
    old = manager._claim_trace_request('request', request['payload'], request['normalization_context'], 'old')
    ctx = multiprocessing.get_context('spawn')
    queue = ctx.Queue()
    def spawn_claim():
        proc = ctx.Process(target=_claim_in_process, args=(config, 'request', request['payload'], request['normalization_context'], queue))
        proc.start()
        join_worker(proc)
        assert proc.exitcode == 0
        return queue.get(timeout=2)
    assert await asyncio.to_thread(spawn_claim) is None
    started, release = threading.Event(), threading.Event()
    def delayed_commit():
        started.set()
        assert release.wait(60)
        return manager._commit_trace_embedding(old, [1., 2.])
    delayed = asyncio.create_task(asyncio.to_thread(delayed_commit))
    assert await asyncio.to_thread(started.wait, 2)
    try:
        with sqlite3.connect(manager.history_db_path) as conn:
            conn.execute('UPDATE ob_s4_requests SET lease_until=0 WHERE operation_id=?', ('request',))
        new = await asyncio.to_thread(spawn_claim)
        assert new['epoch'] > old['epoch']
    finally:
        release.set()
    with pytest.raises(BucketIdempotencyError, match='operation_claim_stale'):
        await delayed
    for action in (lambda: manager._trace_checkpoint(old), lambda: manager._trace_effect_commit(old, 'delta'),
                   lambda: manager._commit_trace_relation(old)):
        with pytest.raises(BucketIdempotencyError, match='operation_claim_stale'):
            action()
    manager._release_trace_request(new)
    engine._generate_embedding = AsyncMock(return_value=[1., 2.])
    await keyed(source)
    assert counts(manager, engine, source) == (1, 1, 1)


def _crash_trace(config, source, target, phase):
    async def run():
        manager = BucketManager(config)
        engine = EmbeddingEngine(config)
        engine.enabled = True
        engine._generate_embedding = AsyncMock(return_value=[1., 2.])
        manager.embedding_engine = engine
        server.bucket_mgr = manager
        checkpoint = manager._trace_checkpoint
        def die(context, next_phase=None, **kwargs):
            if next_phase == phase:
                os.kill(os.getpid(), 9)
            checkpoint(context, next_phase, **kwargs)
        manager._trace_checkpoint = die
        await keyed(source, target)
    asyncio.run(run())


@pytest.mark.parametrize('phase', ['history_committed', 'memory_applied', 'embedding_resolved',
                                 'delta_resolved', 'relation_resolved', 'completed'])
async def test_sigkill_checkpoint_loss(setup, monkeypatch, phase):
    manager, engine, source, target, config = setup
    proc = multiprocessing.get_context('spawn').Process(target=_crash_trace, args=(config, source, target, phase))
    proc.start()
    await asyncio.to_thread(join_worker, proc)
    assert proc.exitcode == -9
    # Represents time passing after an ungraceful death, without a 60-second test sleep.
    with sqlite3.connect(manager.history_db_path) as conn:
        conn.execute('UPDATE ob_s4_requests SET lease_until=0')
    restarted = BucketManager(config, embedding_engine=engine)
    monkeypatch.setattr(server, 'bucket_mgr', restarted)
    await keyed(source, target)
    assert (await restarted.get(source))['content'] == 'original\n\naddition'
    assert counts(restarted, engine, source) == (1, 1, 1)


async def test_import_plan_payload_and_digest_survive_additive_schema(setup):
    manager, _, source, _, _ = setup
    original = manager.plan_import_operation('legacy-import', operation_kind='update',
        target_bucket_id=source, payload={'kwargs': {'tags': ['legacy']}})
    await keyed(source)
    await manager.apply_import_operation('legacy-import')
    after = manager._get_import_operation('legacy-import')
    assert after['payload'] == original['payload']
    assert after['payload_digest'] == original['payload_digest']
    assert after['effects_json'] is None
    assert (await manager.get(source))['metadata']['tags'] == ['legacy']


async def test_memory_marker_commit_before_child_receipt(setup, monkeypatch):
    manager, engine, source, target, config = setup
    def interrupt(*args):
        raise asyncio.CancelledError()
    monkeypatch.setattr(manager, '_mark_import_operation_applied', interrupt)
    with pytest.raises(asyncio.CancelledError):
        await keyed(source, target)
    child_key = manager.inspect_trace_request('request')['plan']['keys']['memory']
    assert manager._get_import_operation(child_key)['status'] == 'planned'
    assert (await manager.get(source))['content'] == 'original\n\naddition'
    reopened = BucketManager(config, embedding_engine=engine)
    monkeypatch.setattr(server, 'bucket_mgr', reopened)
    await keyed(source, target)
    assert counts(reopened, engine, source) == (1, 1, 1)
    assert reopened._get_import_operation(child_key)['status'] == 'applied'


async def test_frozen_alias_context_and_todo_identity(setup, monkeypatch):
    manager, engine, source, _, _ = setup
    checkpoint = manager._trace_checkpoint
    def interrupt(context, phase=None, **kwargs):
        checkpoint(context, phase, **kwargs)
        if phase == 'planned':
            raise asyncio.CancelledError()
    monkeypatch.setattr(manager, '_trace_checkpoint', interrupt)
    with pytest.raises(asyncio.CancelledError):
        await keyed(source, key='frozen', todo_items=[{'text': '婷易 task'}], valence=0)
    request = manager.inspect_trace_request('frozen')
    frozen_ids = [r['id'] for r in request['plan']['updates']['todo_provenance']]
    monkeypatch.setattr(manager, '_trace_checkpoint', checkpoint)
    monkeypatch.setitem(server.DISPLAY_ALIASES, '婷', 'different')
    result = await keyed(source, key='frozen', todo_items=[{'text': '婷易 task'}], valence=0.0)
    assert '已修改' in result
    post = frontmatter.load(manager._find_bucket_file(source))
    assert post['todos'] == ['婷 task']
    assert [r['id'] for r in post['todo_provenance']] == frozen_ids
    assert all(identity in result for identity in frozen_ids)


async def test_metadata_and_unrelate_no_embedding(setup):
    manager, engine, source, target, _ = setup
    manager.mutate_related(source, add=[target])
    result = await server.trace(bucket_id=source, operation_id='meta', name='new', tags='a,b',
        unrelate=target, trigger_date='none', provenance_kind='summary', dormant=1)
    assert '已修改' in result
    assert counts(manager, engine, source) == (0, 0, 0)
    state = (await manager.get(source))['metadata']
    assert state['name'] == 'new'
    assert state['tags'] == ['a', 'b'] and state['dormant'] is True
    assert state['provenance_kind'] == 'summary'
    assert target not in state['related_buckets']
    assert source not in (await manager.get(target))['metadata']['related_buckets']


def _execute_in_process(config, source, target, queue):
    async def run():
        engine = EmbeddingEngine(config)
        engine.enabled = True
        engine._generate_embedding = AsyncMock(return_value=[1., 2.])
        server.bucket_mgr = BucketManager(config, embedding_engine=engine)
        queue.put(await keyed(source, target))
    asyncio.run(run())


async def test_two_process_same_request_only_one_durable_write(setup):
    manager, engine, source, target, config = setup
    ctx = multiprocessing.get_context('spawn')
    queue = ctx.Queue()
    processes = [ctx.Process(target=_execute_in_process, args=(config, source, target, queue)) for _ in range(2)]
    for proc in processes:
        proc.start()
    await asyncio.gather(*(asyncio.to_thread(join_worker, proc) for proc in processes))
    assert [proc.exitcode for proc in processes] == [0, 0]
    assert queue.get(timeout=2) == queue.get(timeout=2)
    assert counts(manager, engine, source) == (1, 1, 1)


async def test_heartbeat_preserves_live_provider_claim(setup, monkeypatch):
    import bucket_manager
    manager, engine, source, _, _ = setup
    monkeypatch.setattr(bucket_manager, '_S4_LEASE_SECONDS', .3)
    async def provider(*args, **kwargs):
        await asyncio.sleep(.8)
        request = manager.inspect_trace_request('request')
        assert request['lease_until'] > __import__('time').time()
        assert request['epoch'] == 1
        return [1., 2.]
    engine._generate_embedding = provider
    assert '已修改' in await keyed(source)
    assert counts(manager, engine, source) == (1, 1, 1)

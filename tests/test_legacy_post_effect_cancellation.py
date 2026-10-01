"""Per-invocation cancellation protection; all data and providers are synthetic."""
import asyncio
import json
from pathlib import Path
import sqlite3
from unittest.mock import AsyncMock

import frontmatter
import pytest

from tests.test_archive_session_reliability import load


@pytest.fixture
def ob(tmp_path, monkeypatch):
    module = load(tmp_path / 'buckets', monkeypatch, enabled=True)
    prepare(module, monkeypatch)
    return module


def prepare(module, monkeypatch):
    monkeypatch.setattr(module, '_similarity_doorbell', AsyncMock(return_value=''))
    monkeypatch.setattr(module, '_detect_conflict_warning', AsyncMock(return_value=''))
    module.dehydrator.analyze = AsyncMock(return_value=dict(
        domain=['synthetic'], tags=[], valence=.6, arousal=.3,
        suggested_name='synthetic', todos=[]))


async def invoke(module, family, **changes):
    if family == 'archive':
        args = dict(summary='synthetic session', letter='frozen letter', valence=.7, arousal=.4,
                    topics=['synthetic'])
        args.update(changes)
        return await module.archive_session(**args)
    args = dict(content='synthetic pinned memory', pinned=True, trigger_date='2030-01-01',
                valence=.7, arousal=.4)
    args.update(changes)
    return await module.hold(**args)


def snapshot(module, identity):
    root = Path(module.config['buckets_dir'])
    path = module.bucket_mgr._find_bucket_file(identity)
    post = frontmatter.load(path) if path else None
    with sqlite3.connect(root / 'bucket_history.sqlite3') as conn:
        letters = conn.execute('SELECT content,sealed FROM letters WHERE session_id=?', (identity,)).fetchall()
        delta = conn.execute('SELECT event_type FROM boot_delta_events WHERE bucket_id=?', (identity,)).fetchall()
        journals = conn.execute("SELECT name FROM sqlite_master WHERE name IN "
                                "('ob_archive_session_operations','ob_s4_requests')").fetchall()
    with sqlite3.connect(root / 'embeddings.db') as conn:
        vectors = conn.execute('SELECT embedding FROM embeddings WHERE bucket_id=?', (identity,)).fetchall()
    return dict(identity=identity, metadata=dict(post.metadata) if post else None,
                letters=letters, delta=delta, vectors=vectors, journals=journals,
                emotion=[entry for entry in module._load_emotion_timeline()
                         if entry.get('bucket_id') == identity])


def complete(state, family):
    assert state['metadata']['type'] == ('archived' if family == 'archive' else 'permanent')
    assert state['delta'] == [('created',)]
    assert len(state['emotion']) == 1
    assert state['emotion'][0]['valence'] == .7 and state['emotion'][0]['arousal'] == .4
    assert state['letters'] == ([('frozen letter', 0)] if family == 'archive' else [])
    if family == 'hold':
        assert state['metadata']['trigger_date'] == '2030-01-01'
        assert state['metadata']['trigger_last_seen'] == ''
    assert len(state['vectors']) == 1 and json.loads(state['vectors'][0][0]) == [.1, .2]
    assert not state['journals']


async def drain(module):
    tasks = list(module._LEGACY_POST_EFFECT_TASKS)
    if tasks:
        await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 5)
    await asyncio.sleep(0)
    assert not module._LEGACY_POST_EFFECT_TASKS
    return tasks


def gate_provider(module, monkeypatch):
    entered, release = asyncio.Event(), asyncio.Event()
    async def provider(*args, **kwargs):
        entered.set()
        await release.wait()
        return [.1, .2]
    monkeypatch.setattr(module.bucket_mgr.embedding_engine, '_generate_embedding', provider)
    return entered, release


@pytest.mark.asyncio
@pytest.mark.parametrize('family', ['archive', 'hold'])
async def test_cancel_waiter_twice_completes_original_without_retry(ob, monkeypatch, family):
    entered, release = gate_provider(ob, monkeypatch)
    errors = []
    previous = asyncio.get_running_loop().get_exception_handler()
    asyncio.get_running_loop().set_exception_handler(lambda loop, context: errors.append(context))
    try:
        waiter = asyncio.create_task(invoke(ob, family))
        await asyncio.wait_for(entered.wait(), 5)
        identity = (await ob.bucket_mgr.list_all(include_archive=True))[0]['id']
        worker, = ob._LEGACY_POST_EFFECT_TASKS
        waiter.cancel(); waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        assert not worker.done()
        release.set()
        await drain(ob)
        complete(snapshot(ob, identity), family)
        assert not errors
    finally:
        asyncio.get_running_loop().set_exception_handler(previous)


@pytest.mark.asyncio
@pytest.mark.parametrize('family', ['archive', 'hold'])
async def test_provider_ordinary_failure_retains_required_effects(ob, monkeypatch, family):
    monkeypatch.setattr(ob.bucket_mgr.embedding_engine, '_generate_embedding',
                        AsyncMock(side_effect=RuntimeError('synthetic provider failure')))
    result = await invoke(ob, family)
    assert '已归档' in result if family == 'archive' else result.startswith('📌')
    identity = (await ob.bucket_mgr.list_all(include_archive=True))[0]['id']
    state = snapshot(ob, identity)
    assert state['letters'] == ([('frozen letter', 0)] if family == 'archive' else [])
    assert len(state['emotion']) == 1 and not state['vectors']
    if family == 'hold': assert state['metadata']['trigger_date'] == '2030-01-01'
    await drain(ob)


@pytest.mark.asyncio
@pytest.mark.parametrize('family', ['archive', 'hold'])
@pytest.mark.parametrize('change', ['body', 'trigger', 'delete', 'recreate'])
async def test_delayed_effect_rejects_changed_or_deleted_source(ob, monkeypatch, family, change, caplog):
    if family == 'archive' and change == 'trigger':
        change = 'body'
    entered, release = gate_provider(ob, monkeypatch)
    waiter = asyncio.create_task(invoke(ob, family))
    await asyncio.wait_for(entered.wait(), 5)
    bucket, = await ob.bucket_mgr.list_all(include_archive=True)
    identity = bucket['id']
    worker, = ob._LEGACY_POST_EFFECT_TASKS
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError): await waiter
    # Disable new generation only for the intervening synthetic writer.
    engine = ob.bucket_mgr.embedding_engine
    if change in ('delete', 'recreate'):
        original = Path(bucket['path']).read_bytes()
        # Simulate an external deletion of synthetic storage; pinned buckets
        # intentionally reject the ordinary delete API.
        Path(bucket['path']).unlink()
        engine.delete_embedding(identity)
        if change == 'recreate':
            path = Path(bucket['path']); path.parent.mkdir(parents=True, exist_ok=True)
            post = frontmatter.loads(original.decode())
            post.content = 'replacement incarnation'
            path.write_text(frontmatter.dumps(post), encoding='utf-8')
    else:
        engine.enabled = False
        if change == 'trigger':
            await ob.bucket_mgr.update(identity, trigger_date='2031-02-03', trigger_last_seen='2031-02-03')
        else:
            await ob.bucket_mgr.update(identity, content='later body')
        engine.enabled = True
    release.set()
    await drain(ob)
    assert worker.exception() is not None
    state = snapshot(ob, identity)
    assert not state['letters'] and not state['emotion'] and not state['vectors']
    if change == 'trigger':
        assert state['metadata']['trigger_date'] == '2031-02-03'
        assert state['metadata']['trigger_last_seen'] == '2031-02-03'
    if change == 'delete': assert state['metadata'] is None
    assert 'legacy post-effects failed' in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize('family', ['archive', 'hold'])
async def test_independent_concurrent_same_body_calls(ob, family):
    results = await asyncio.gather(invoke(ob, family), invoke(ob, family))
    assert results[0] != results[1]
    buckets = await ob.bucket_mgr.list_all(include_archive=True)
    assert len(buckets) == 2
    for bucket in buckets: complete(snapshot(ob, bucket['id']), family)
    await drain(ob)


@pytest.mark.asyncio
async def test_frozen_archive_inputs(ob, monkeypatch):
    entered, release = gate_provider(ob, monkeypatch)
    topics = ['original topic']
    waiter = asyncio.create_task(invoke(ob, 'archive', topics=topics))
    await asyncio.wait_for(entered.wait(), 5)
    topics.append('later topic')
    release.set(); await waiter; await drain(ob)
    bucket, = await ob.bucket_mgr.list_all(include_archive=True)
    assert bucket['metadata']['topics'] == ['original topic']


@pytest.mark.asyncio
@pytest.mark.parametrize('family', ['archive', 'hold'])
async def test_maintenance_waits_after_waiter_disconnect(ob, monkeypatch, family):
    from maintenance_write_gate import DEFAULT_WRITE_COORDINATOR
    entered, release = gate_provider(ob, monkeypatch)
    waiter = asyncio.create_task(invoke(ob, family))
    await asyncio.wait_for(entered.wait(), 5)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError): await waiter
    assert DEFAULT_WRITE_COORDINATOR.status().active_writers == 1
    async def freeze():
        async with DEFAULT_WRITE_COORDINATOR.freeze(drain_timeout_seconds=5, max_freeze_seconds=10, reason='test'):
            assert not ob._LEGACY_POST_EFFECT_TASKS
    freezing = asyncio.create_task(freeze())
    await asyncio.sleep(.05)
    assert not freezing.done()
    release.set(); await drain(ob); await freezing
    assert DEFAULT_WRITE_COORDINATOR.status().active_writers == 0


@pytest.mark.asyncio
@pytest.mark.parametrize('family', ['archive', 'hold'])
async def test_direct_executor_cancel_has_no_recovery_guarantee(ob, monkeypatch, family, caplog):
    entered, release = gate_provider(ob, monkeypatch)
    waiter = asyncio.create_task(invoke(ob, family))
    await asyncio.wait_for(entered.wait(), 5)
    identity = (await ob.bucket_mgr.list_all(include_archive=True))[0]['id']
    worker, = ob._LEGACY_POST_EFFECT_TASKS
    worker.cancel()
    with pytest.raises(asyncio.CancelledError): await waiter
    await drain(ob)
    state = snapshot(ob, identity)
    assert not state['letters'] and not state['emotion']
    if family == 'hold': assert not state['metadata']['trigger_date']
    assert 'executor cancelled; recovery unavailable' in caplog.text


@pytest.mark.asyncio
async def test_storage_failures_are_observed_without_success(ob, monkeypatch, caplog):
    monkeypatch.setattr(ob.bucket_mgr, 'record_letter', lambda *a, **k: (_ for _ in ()).throw(OSError('synthetic')))
    result = await invoke(ob, 'archive')
    assert result == 'archive_session: archive_session_storage_failed'
    assert 'legacy post-effects failed: OSError' in caplog.text
    await drain(ob)


@pytest.mark.asyncio
async def test_pinned_trigger_false_is_failure(ob, monkeypatch):
    monkeypatch.setattr(ob.bucket_mgr, 'update', AsyncMock(return_value=False))
    with pytest.raises(Exception, match='legacy_trigger_write_failed'):
        await invoke(ob, 'hold')
    await drain(ob)


@pytest.mark.asyncio
async def test_other_hold_modes_do_not_enter_protection(ob, monkeypatch):
    monkeypatch.setattr(ob, '_await_legacy_post_effects', AsyncMock(side_effect=AssertionError('protected branch')))
    await ob.hold('ordinary synthetic')
    await ob.hold('feel synthetic', pinned=True, feel=True)
    assert not ob._LEGACY_POST_EFFECT_TASKS


@pytest.mark.asyncio
async def test_provider_timeouts_are_preserved(ob):
    from embedding_engine import EmbeddingEngine
    from dehydrator import Dehydrator
    config = dict(ob.config, embedding=dict(enabled=True, independent=True, api_key='synthetic'),
                  dehydration=dict(ob.config['dehydration'], api_key='synthetic'))
    engine = EmbeddingEngine(config)
    analyzer = Dehydrator(config)
    assert engine.client.timeout == 30.0
    assert analyzer.client.timeout == 60.0
    await engine.client.close(); await analyzer.client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('family', ['archive', 'hold'])
async def test_protection_is_established_before_publication(ob, monkeypatch, family):
    entered, release = asyncio.Event(), asyncio.Event()
    if family == 'archive':
        from bucket_write_lock import BucketWriteLockError
        original = ob.ArchiveSessionOperations.publish_legacy
        first = True
        def publication(store, *a, **kw):
            nonlocal first
            if first:
                first = False; entered.set()
                raise BucketWriteLockError('bucket writer mutex timeout')
            return original(store, *a, **kw)
        monkeypatch.setattr(ob.ArchiveSessionOperations, 'publish_legacy', publication)
    else:
        original = ob.bucket_mgr.create
        async def creation(*a, **kw):
            entered.set(); await release.wait()
            return await original(*a, **kw)
        monkeypatch.setattr(ob.bucket_mgr, 'create', creation)
    waiter = asyncio.create_task(invoke(ob, family))
    await asyncio.wait_for(entered.wait(), 5)
    assert not await ob.bucket_mgr.list_all(include_archive=True)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError): await waiter
    release.set(); await drain(ob)
    bucket, = await ob.bucket_mgr.list_all(include_archive=True)
    complete(snapshot(ob, bucket['id']), family)


@pytest.mark.asyncio
@pytest.mark.parametrize('action', ['cancel', 'change'])
async def test_related_await_does_not_overwrite_later_trigger_delivery(ob, monkeypatch, action):
    engine = ob.bucket_mgr.embedding_engine
    engine.enabled = False
    neighbor = await ob.bucket_mgr.create('neighbor')
    engine._store_embedding(neighbor, [.1, .2]); engine.enabled = True
    original = engine.get_embedding
    entered, release = asyncio.Event(), asyncio.Event()
    async def delayed(identity):
        if identity != neighbor:
            entered.set(); await release.wait()
        return await original(identity)
    monkeypatch.setattr(engine, 'get_embedding', delayed)
    waiter = asyncio.create_task(invoke(ob, 'hold'))
    await asyncio.wait_for(entered.wait(), 5)
    identity = next(b['id'] for b in await ob.bucket_mgr.list_all() if b['id'] != neighbor)
    if action == 'change':
        await ob.bucket_mgr.update(identity, trigger_last_seen='2030-01-01')
    else:
        waiter.cancel(); waiter.cancel()
        with pytest.raises(asyncio.CancelledError): await waiter
    release.set()
    if action == 'change':
        with pytest.raises(Exception, match='confirmed_delete_source_changed'): await waiter
    await drain(ob)
    state = snapshot(ob, identity)
    if action == 'change':
        assert state['metadata']['trigger_last_seen'] == '2030-01-01'
        assert not state['metadata']['related_buckets']
    else:
        complete(state, 'hold')
        assert neighbor in state['metadata']['related_buckets']
        assert identity in (await ob.bucket_mgr.get(neighbor))['metadata']['related_buckets']


@pytest.mark.asyncio
@pytest.mark.parametrize('family', ['archive', 'hold'])
async def test_failure_after_disconnected_waiter_is_retrieved(ob, monkeypatch, family, caplog):
    entered, release = gate_provider(ob, monkeypatch)
    if family == 'archive':
        def fail(*a, **k): raise OSError('synthetic')
        monkeypatch.setattr(ob.bucket_mgr, 'record_letter', fail)
    else:
        monkeypatch.setattr(ob.bucket_mgr, 'update', AsyncMock(return_value=False))
    errors = []
    loop = asyncio.get_running_loop(); previous = loop.get_exception_handler()
    loop.set_exception_handler(lambda loop, context: errors.append(context))
    try:
        waiter = asyncio.create_task(invoke(ob, family))
        await asyncio.wait_for(entered.wait(), 5)
        worker, = ob._LEGACY_POST_EFFECT_TASKS
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError): await waiter
        release.set(); await drain(ob)
        assert worker.exception() is not None
        assert 'legacy post-effects failed' in caplog.text
        assert not errors
    finally:
        loop.set_exception_handler(previous)


@pytest.mark.parametrize('family', ['archive', 'hold'])
def test_real_process_exit_has_no_automatic_resume(ob, monkeypatch, tmp_path, family):
    import os
    import subprocess
    import sys
    root = tmp_path / 'child'
    code = '''
import asyncio, os
from unittest.mock import AsyncMock
import server
server.decay_engine.ensure_started = AsyncMock()
server._similarity_doorbell = AsyncMock(return_value='')
server._detect_conflict_warning = AsyncMock(return_value='')
server.dehydrator.analyze = AsyncMock(return_value=dict(domain=['synthetic'], tags=[], valence=.5, arousal=.3, suggested_name='synthetic', todos=[]))
server.bucket_mgr.embedding_engine.enabled = True
async def provider(*a, **k): os._exit(73)
server.bucket_mgr.embedding_engine._generate_embedding = provider
async def main():
    if os.environ['TEST_FAMILY'] == 'archive':
        await server.archive_session('child synthetic', letter='child letter', valence=.7, arousal=.4)
    else:
        await server.hold('child synthetic', pinned=True, trigger_date='2030-01-01', valence=.7, arousal=.4)
asyncio.run(main())
'''
    environment = dict(os.environ, OMBRE_BUCKETS_DIR=str(root), TEST_FAMILY=family)
    child = subprocess.run([sys.executable, '-c', code], env=environment, capture_output=True, timeout=30)
    assert child.returncode == 73, child.stderr.decode()
    restarted = load(root, monkeypatch)
    files = list(root.rglob('*.md')); assert len(files) == 1
    identity = frontmatter.load(files[0])['id']
    state = snapshot(restarted, identity)
    assert not state['letters'] and not state['emotion'] and not state['journals']
    if family == 'hold': assert not state['metadata']['trigger_date']
    assert not restarted._LEGACY_POST_EFFECT_TASKS

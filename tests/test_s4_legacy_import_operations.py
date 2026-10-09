"""Legacy S-4C durable boundaries on isolated storage, including process death."""
import asyncio
import copy
import hashlib
import json
import multiprocessing
import os
import signal
import socket
import sqlite3
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import frontmatter
import pytest
import pytest_asyncio
import httpx
import uvicorn
from starlette.applications import Starlette
from starlette.routing import Route

import import_memory
import bucket_manager
import server
from bucket_manager import BucketManager, BucketIdempotencyError
from bucket_write_lock import bucket_write_scope
from embedding_engine import EmbeddingEngine
from import_memory import ImportEngine, ImportState, _IMPORT_PUBLIC_FIELDS

pytestmark = pytest.mark.asyncio
SOURCE = 'User: A conversation source for the isolated legacy import safety tests.'
ITEM = dict(content='An extracted memory with a pending task.', name='memory', domain=['事务'],
            tags=['import'], importance=5, valence=.5, arousal=.3, todos=['向婷易回信'], preserve_raw=False)


def make_engine(config, items=None):
    embedding = EmbeddingEngine(config)
    embedding.enabled = True
    embedding._generate_embedding = AsyncMock(return_value=[.25, .75])
    manager = BucketManager(config, embedding_engine=embedding)
    manager.search = AsyncMock(return_value=[])
    engine = ImportEngine(config, manager, SimpleNamespace(api_available=True, model='extract-model'), embedding)
    engine._extract_memories = AsyncMock(return_value=copy.deepcopy([ITEM] if items is None else items))
    return engine, manager, embedding


@pytest_asyncio.fixture
async def setup(test_config, monkeypatch):
    monkeypatch.setenv('OMBRE_BUCKETS_DIR', test_config['buckets_dir'])
    engine, manager, embedding = make_engine(test_config)
    with sqlite3.connect(embedding.db_path) as conn:
        conn.executescript('''CREATE TABLE vector_writes(id INTEGER PRIMARY KEY);
            CREATE TRIGGER count_vector_writes AFTER INSERT ON embeddings BEGIN
            INSERT INTO vector_writes(id) VALUES(NULL); END;''')
    return engine, manager, embedding, test_config


def state_of(engine):
    return engine.state.read_legacy()[0]


def item_of(engine):
    return state_of(engine)['chunks'][0]['items'][0]


def counts(manager, embedding):
    files = len(list(Path(manager.base_dir).glob('dynamic/**/*.md')))
    with sqlite3.connect(manager.history_db_path) as conn:
        delta = conn.execute('SELECT count(*) FROM boot_delta_events').fetchone()[0]
        history = conn.execute('SELECT count(*) FROM bucket_history').fetchone()[0]
    with sqlite3.connect(embedding.db_path) as conn:
        vectors = conn.execute('SELECT count(*) FROM vector_writes').fetchone()[0]
    return files, delta, vectors, history


def expire(engine):
    with bucket_write_scope(engine.state.root):
        saved = state_of(engine)
        saved['lease_until'] = 0
        engine.state.publish(saved, ownership_only=True)


def forbid_replanning(engine, manager):
    engine._extract_memories = AsyncMock(side_effect=AssertionError('extraction must be frozen'))
    manager.search = AsyncMock(side_effect=AssertionError('decision must be frozen'))


@pytest.mark.parametrize('preserve', [False, True])
@pytest.mark.parametrize('phase', ['chunk.extraction_frozen', 'item.planned', 'item.memory_applied',
                                  'item.effects_resolved', 'item.completed', 'chunk.completed', 'run.completed'])
async def test_checkpoint_restart_and_completed_replay(setup, monkeypatch, preserve, phase):
    engine, manager, embedding, config = setup
    checkpoint = engine.state.checkpoint
    tripped = False
    def interrupt(claim, current=None, change=None):
        nonlocal tripped
        result = checkpoint(claim, current, change)
        if current == phase and not tripped:
            tripped = True
            raise asyncio.CancelledError()
        return result
    monkeypatch.setattr(engine.state, 'checkpoint', interrupt)
    with pytest.raises(asyncio.CancelledError):
        await engine.start(SOURCE, 'source.txt', preserve)
    assert not engine.is_running and state_of(engine)['owner'] is None
    restarted, manager2, embedding2 = make_engine(config)
    restarted._extract_memories = AsyncMock(side_effect=AssertionError('extraction frozen'))
    if phase != 'chunk.extraction_frozen':
        manager2.search = AsyncMock(side_effect=AssertionError('decision frozen'))
    result = await restarted.start(SOURCE, 'source.txt', preserve, True)
    assert result['status'] == 'completed'
    assert await restarted.start(SOURCE, 'source.txt', preserve, True) == result
    assert counts(manager2, embedding2) == (1, 1, 1, 0)
    assert result['memories_created'] == 1 and result['memories_raw'] == int(preserve)
    assert set(result) == set(_IMPORT_PUBLIC_FIELDS)
    assert state_of(restarted)['source_hash'] == hashlib.sha256(SOURCE.encode()).hexdigest()[:16]
    assert 'payload' not in item_of(restarted)['plan']


@pytest.mark.parametrize('effect', ['marker', 'delta', 'embedding'])
async def test_effect_receipt_parent_gap(setup, monkeypatch, effect):
    engine, manager, embedding, config = setup
    if effect == 'marker':
        original = manager._mark_import_operation_applied
        monkeypatch.setattr(manager, '_mark_import_operation_applied', lambda key: (_ for _ in ()).throw(asyncio.CancelledError()))
    else:
        owner = manager if effect == 'delta' else embedding
        name = 'commit_legacy_import_delta' if effect == 'delta' else 'store_trace_embedding'
        original = getattr(owner, name)
        def interrupt(*args, **kwargs):
            original(*args, **kwargs)
            raise asyncio.CancelledError()
        monkeypatch.setattr(owner, name, interrupt)
    with pytest.raises(asyncio.CancelledError):
        await engine.start(SOURCE, 'source.txt')
    restarted, manager2, embedding2 = make_engine(config)
    forbid_replanning(restarted, manager2)
    assert (await restarted.start(SOURCE, 'source.txt', resume=True))['status'] == 'completed'
    assert counts(manager2, embedding2) == (1, 1, 1, 0)
    if effect == 'embedding':
        embedding2._generate_embedding.assert_not_awaited()


async def test_extraction_cancellation_pending_empty_and_finally(setup):
    engine, manager, embedding, config = setup
    entered, release = asyncio.Event(), asyncio.Event()
    async def blocked(*args):
        entered.set()
        await release.wait()
        return []
    engine._extract_memories = AsyncMock(side_effect=blocked)
    task = asyncio.create_task(engine.start(SOURCE, 'source.txt'))
    await asyncio.wait_for(entered.wait(), 5)
    assert state_of(engine)['chunks'][0]['items'] is None
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not engine.is_running and state_of(engine)['owner'] is None
    restarted, manager2, embedding2 = make_engine(config, [])
    assert (await restarted.start(SOURCE, 'source.txt', resume=True))['status'] == 'completed'
    assert state_of(restarted)['chunks'][0]['items'] == []
    assert state_of(restarted)['chunks'][0]['outcome'] == 'extracted'
    assert counts(manager2, embedding2) == (0, 0, 0, 0)


@pytest.mark.parametrize('failure', [BucketIdempotencyError('operation_claim_stale'), OSError('checkpoint I/O')])
async def test_infrastructure_failure_is_not_empty_extraction(setup, failure):
    engine, _, _, _ = setup
    engine._extract_memories = AsyncMock(side_effect=failure)
    with pytest.raises(type(failure)):
        await engine.start(SOURCE, 'source.txt')
    saved = state_of(engine)
    assert saved['chunks'][0]['items'] is None and saved['processed'] == 0
    assert saved['status'] == 'error' and not engine.is_running


async def test_freeze_failure_does_not_write_any_item(setup, monkeypatch):
    engine, manager, embedding, _ = setup
    checkpoint = engine.state.checkpoint
    def failed(claim, phase=None, change=None):
        if phase == 'chunk.extraction_frozen':
            raise OSError('freeze failed')
        return checkpoint(claim, phase, change)
    monkeypatch.setattr(engine.state, 'checkpoint', failed)
    with pytest.raises(OSError):
        await engine.start(SOURCE, 'source.txt')
    assert state_of(engine)['chunks'][0]['items'] is None
    assert counts(manager, embedding) == (0, 0, 0, 0)


@pytest.mark.parametrize('todos', [[], ['existing'], ['existing', 'new']])
async def test_reuse_freeze_noop_and_todos(setup, monkeypatch, todos):
    engine, manager, embedding, config = setup
    target = await manager.create(ITEM['content'], domain=['事务'], todos=['existing'])
    manager.search = AsyncMock(return_value=[await manager.get(target)])
    engine._extract_memories = AsyncMock(return_value=[dict(ITEM, todos=todos)])
    path = Path(manager._find_bucket_file(target))
    before = path.read_bytes()
    baseline = counts(manager, embedding)
    checkpoint = engine.state.checkpoint
    def stopped(claim, phase=None, change=None):
        result = checkpoint(claim, phase, change)
        if phase == 'item.planned':
            raise asyncio.CancelledError()
        return result
    monkeypatch.setattr(engine.state, 'checkpoint', stopped)
    with pytest.raises(asyncio.CancelledError):
        await engine.start(SOURCE, 'source.txt')
    assert item_of(engine)['plan']['target'] == target
    restarted, manager2, embedding2 = make_engine(config)
    forbid_replanning(restarted, manager2)
    result = await restarted.start(SOURCE, 'source.txt', resume=True)
    assert result['memories_merged'] == 1 and result['memories_created'] == 0
    if 'new' not in todos:
        assert path.read_bytes() == before
        assert item_of(restarted)['resolutions']['memory'] == 'not_requested'
        assert counts(manager2, embedding2) == baseline
    else:
        assert (await manager2.get(target))['metadata']['todos'] == ['existing', 'new']
        assert counts(manager2, embedding2) == (baseline[0], baseline[1]+1, baseline[2], 0)


@pytest.mark.parametrize('change', ['delete', 'sealed', 'body', 'metadata'])
async def test_frozen_reuse_target_conflict_never_switches(setup, monkeypatch, change):
    engine, manager, _, _ = setup
    target = await manager.create(ITEM['content'], domain=['事务'])
    manager.search = AsyncMock(return_value=[await manager.get(target)])
    checkpoint = engine.state.checkpoint
    def stop(claim, phase=None, change_fn=None):
        result = checkpoint(claim, phase, change_fn)
        if phase == 'item.planned':
            raise asyncio.CancelledError()
        return result
    monkeypatch.setattr(engine.state, 'checkpoint', stop)
    with pytest.raises(asyncio.CancelledError):
        await engine.start(SOURCE, 'source.txt')
    path = Path(manager._find_bucket_file(target))
    if change == 'delete':
        path.unlink()
    else:
        post = frontmatter.load(path)
        if change == 'sealed':
            post['sealed'] = 1
        elif change == 'body':
            post.content = 'edited by another operation'
        else:
            post['importance'] = 9
        path.write_text(frontmatter.dumps(post), encoding='utf-8')
    monkeypatch.setattr(engine.state, 'checkpoint', checkpoint)
    forbid_replanning(engine, manager)
    with pytest.raises(BucketIdempotencyError):
        await engine.start(SOURCE, 'source.txt', resume=True)
    assert state_of(engine)['status'] == 'error' and item_of(engine)['plan']['target'] == target


async def test_same_process_concurrent_resume_and_heartbeat(setup, monkeypatch):
    engine, manager, embedding, config = setup
    # Item processing runs without yielding to the heartbeat; on slow hosts it took
    # ~0.11s and expired a 0.15s lease. The wait below stays twice the lease, so the
    # claim check still proves the heartbeat renewed it.
    lease = 1.0
    monkeypatch.setattr(import_memory, '_LEGACY_LEASE_SECONDS', lease)
    entered, release = asyncio.Event(), asyncio.Event()
    async def blocked(*args):
        entered.set()
        await release.wait()
        return [ITEM]
    engine._extract_memories = AsyncMock(side_effect=blocked)
    first = asyncio.create_task(engine.start(SOURCE, 'source.txt'))
    await asyncio.wait_for(entered.wait(), 5)
    other, _, _ = make_engine(config)
    second = asyncio.create_task(other.start(SOURCE, 'source.txt', resume=True))
    await asyncio.sleep(2 * lease)
    assert engine.state.claim(state_of(engine)['run_id'], 'other-owner') is None
    assert (await other.start(SOURCE, 'source.txt'))['error'] == 'Import already running'
    release.set()
    assert await first == await second
    engine._extract_memories.assert_awaited_once()
    other._extract_memories.assert_not_awaited()
    assert counts(manager, embedding) == (1, 1, 1, 0)


@pytest.mark.parametrize('provider', ['extraction', 'embedding'])
async def test_stale_epoch_delayed_provider_is_fenced(setup, provider):
    engine, manager, embedding, _ = setup
    entered, release = asyncio.Event(), asyncio.Event()
    async def blocked(*args, **kwargs):
        entered.set()
        await release.wait()
        return [ITEM] if provider == 'extraction' else [.25, .75]
    if provider == 'extraction':
        engine._extract_memories = AsyncMock(side_effect=blocked)
    else:
        embedding._generate_embedding = AsyncMock(side_effect=blocked)
    task = asyncio.create_task(engine.start(SOURCE, 'source.txt'))
    await asyncio.wait_for(entered.wait(), 5)
    saved = state_of(engine)
    old = dict(run_id=saved['run_id'], owner=saved['owner'], epoch=saved['epoch'])
    expire(engine)
    newer = engine.state.claim(saved['run_id'], 'new-owner')
    before = counts(manager, embedding)
    with pytest.raises(BucketIdempotencyError, match='operation_claim_stale'):
        engine.state.checkpoint(old)
    release.set()
    with pytest.raises(BucketIdempotencyError, match='operation_claim_stale'):
        await task
    assert counts(manager, embedding) == before
    assert state_of(engine)['owner'] == 'new-owner' and not engine.is_running
    engine.state.release(newer)


def v1_bytes(status='paused', processed=0, total=1):
    return ('  ' + json.dumps(dict(source_file='source.txt', source_hash=hashlib.sha256(SOURCE.encode()).hexdigest()[:16],
        total_chunks=total, processed=processed, api_calls=1, memories_created=1, memories_merged=0,
        memories_raw=0, errors=[], status=status, started_at='old', updated_at='old'), ensure_ascii=False) + '\r\n').encode()


class Request:
    method = 'POST'
    headers = {'content-type': 'text/plain'}
    def __init__(self, **query):
        self.query_params = dict(filename='source.txt', **query)
    async def body(self):
        return SOURCE.encode()


@pytest.mark.parametrize('status,processed,total', [('running',0,1), ('paused',0,1), ('error',0,1),
                                                   ('running',1,1), ('paused',1,2)])
async def test_v1_rejected_before_lazy_runtime_zero_bytes(setup, monkeypatch, status, processed, total):
    engine, manager, embedding, config = setup
    raw = v1_bytes(status, processed, total)
    Path(engine.state.state_file).write_bytes(raw)
    before = {p: p.read_bytes() for p in Path(manager.base_dir).rglob('*') if p.is_file()}
    monkeypatch.setattr(server, 'config', config)
    monkeypatch.setattr(server, 'import_engine', server._LazyRuntimeComponent('import_engine'))
    monkeypatch.setattr(server, '_get_runtime_components', lambda: (_ for _ in ()).throw(AssertionError('must reject before runtime')))
    monkeypatch.setattr(server, '_require_dashboard_write', lambda *args: None)
    response = await server.api_import_upload(Request(resume='1'))
    assert response.status_code == 409
    assert json.loads(response.body) == {'error': 'legacy_resume_unverifiable'}
    assert before == {p: p.read_bytes() for p in Path(manager.base_dir).rglob('*') if p.is_file()}
    assert not list(Path(manager.base_dir).glob('import_state.v1.*'))
    assert (await engine.start(SOURCE, 'source.txt', resume=True)) == {'error': 'legacy_resume_unverifiable'}


async def test_v1_snapshot_exact_deduplicated_and_not_resume_source(setup, monkeypatch):
    engine, manager, _, config = setup
    raw = v1_bytes()
    Path(engine.state.state_file).write_bytes(raw)
    original = engine.state.publish
    monkeypatch.setattr(engine.state, 'publish', lambda *args, **kwargs: (_ for _ in ()).throw(OSError('acceptance failed')))
    with pytest.raises(OSError):
        engine.accept_legacy(SOURCE, 'source.txt')
    snapshots = list(Path(manager.base_dir).glob('import_state.v1.*.json'))
    assert len(snapshots) == 1 and snapshots[0].read_bytes() == raw
    assert Path(engine.state.state_file).read_bytes() == raw
    monkeypatch.setattr(engine.state, 'publish', original)
    result = await engine.start(SOURCE, 'source.txt')
    assert result['status'] == 'completed' and state_of(engine)['schema_version'] == 2
    assert snapshots[0].read_bytes() == raw
    assert len(list(Path(manager.base_dir).glob('import_state.v1.*.json'))) == 1
    Path(engine.state.state_file).unlink()
    restarted, _, _ = make_engine(config)
    assert (await restarted.start(SOURCE, 'source.txt', resume=True)) == {'error': 'legacy_resume_unverifiable'}
    assert snapshots[0].read_bytes() == raw


async def test_completed_v1_and_v2_new_upload_identity(setup):
    engine, manager, embedding, _ = setup
    raw = v1_bytes('completed', 1, 1)
    Path(engine.state.state_file).write_bytes(raw)
    assert (await engine.start(SOURCE, 'source.txt', resume=True))['status'] == 'completed'
    assert Path(engine.state.state_file).read_bytes() == raw
    engine._extract_memories.assert_not_awaited()
    assert counts(manager, embedding) == (0, 0, 0, 0)
    await engine.start(SOURCE, 'source.txt', True)
    first_id = state_of(engine)['run_id']
    await engine.start(SOURCE, 'source.txt', True)
    assert state_of(engine)['run_id'] != first_id
    assert counts(manager, embedding) == (2, 2, 2, 0)
    completed = engine.get_status()
    target = item_of(engine)['plan']['target']
    Path(manager._find_bucket_file(target)).unlink()
    before = {p: p.read_bytes() for p in Path(manager.base_dir).rglob('*') if p.is_file()}
    assert await engine.start(SOURCE, 'source.txt', True, True) == completed
    assert before == {p: p.read_bytes() for p in Path(manager.base_dir).rglob('*') if p.is_file()}


@pytest.mark.parametrize('capture', [None, '0'])
async def test_http_durable_acceptance_and_task_lifetime(setup, monkeypatch, capture):
    engine, _, _, config = setup
    monkeypatch.setattr(server, 'config', config)
    monkeypatch.setattr(server, 'import_engine', engine)
    monkeypatch.setattr(server, '_require_dashboard_write', lambda *args: None)
    entered, release = asyncio.Event(), asyncio.Event()
    async def blocked(*args):
        entered.set()
        await release.wait()
        return [ITEM]
    engine._extract_memories = AsyncMock(side_effect=blocked)
    request = Request(**({} if capture is None else {'raw_evidence_capture': capture}))
    response = await server.api_import_upload(request)
    assert json.loads(response.body) == {'status': 'started', 'filename': 'source.txt', 'size_bytes': len(SOURCE.encode())}
    assert state_of(engine)['phase'] == 'run.accepted'
    await asyncio.wait_for(entered.wait(), 5)
    task = next(t for t in server._IMPORT_BACKGROUND_TASKS if not t.done())
    assert not task.cancelled()
    assert (await server.api_import_upload(Request(resume='1'))).status_code == 200
    release.set()
    await asyncio.gather(*list(server._IMPORT_BACKGROUND_TASKS))
    await asyncio.sleep(0)
    assert not server._IMPORT_BACKGROUND_TASKS
    engine._extract_memories.assert_awaited_once()
    assert state_of(engine)['status'] == 'completed'


async def test_http_acceptance_failure_never_started(setup, monkeypatch):
    engine, _, _, config = setup
    monkeypatch.setattr(server, 'config', config)
    monkeypatch.setattr(server, 'import_engine', engine)
    monkeypatch.setattr(server, '_require_dashboard_write', lambda *args: None)
    monkeypatch.setattr(engine.state, 'publish', lambda *args, **kwargs: (_ for _ in ()).throw(OSError('disk full')))
    response = await server.api_import_upload(Request())
    assert response.status_code == 500 and json.loads(response.body) == {'error': 'import_failed'}
    assert not server._IMPORT_BACKGROUND_TASKS and not Path(engine.state.state_file).exists()


async def test_capture_upload_does_not_overwrite_accepted_legacy_state(setup, monkeypatch):
    engine, _, _, config = setup
    engine.accept_legacy(SOURCE, 'source.txt')
    before = Path(engine.state.state_file).read_bytes()
    monkeypatch.setattr(server, 'config', config)
    monkeypatch.setattr(server, 'import_engine', server._LazyRuntimeComponent('import_engine'))
    monkeypatch.setattr(server, '_get_runtime_components', lambda: (_ for _ in ()).throw(AssertionError('active legacy admission guard')))
    monkeypatch.setattr(server, '_require_dashboard_write', lambda *args: None)
    response = await server.api_import_upload(Request(raw_evidence_capture='1'))
    assert response.status_code == 409 and json.loads(response.body) == {'error': 'Import already running'}
    assert Path(engine.state.state_file).read_bytes() == before and not server._IMPORT_BACKGROUND_TASKS


async def test_release_failure_preserves_lease_recovery_and_running_flag(setup, monkeypatch):
    engine, _, _, config = setup
    engine._extract_memories = AsyncMock(side_effect=asyncio.CancelledError())
    monkeypatch.setattr(engine.state, 'release', lambda *args: (_ for _ in ()).throw(OSError('release unavailable')))
    with pytest.raises(asyncio.CancelledError):
        await engine.start(SOURCE, 'source.txt')
    assert not engine.is_running and state_of(engine)['owner'] is not None
    expire(engine)
    restarted, _, _ = make_engine(config)
    assert (await restarted.start(SOURCE, 'source.txt', resume=True))['status'] == 'completed'


async def test_item_preserve_raw_bypasses_reuse_and_resumes(setup, monkeypatch):
    engine, manager, _, _ = setup
    engine._extract_memories = AsyncMock(return_value=[dict(ITEM, preserve_raw=True)])
    manager.search = AsyncMock(side_effect=AssertionError('preserved item must not search'))
    result = await engine.start(SOURCE, 'source.txt')
    assert result['memories_raw'] == result['memories_created'] == 1
    assert state_of(engine)['preserve_raw'] is False and item_of(engine)['plan']['preserve_raw'] is True
    assert await engine.start(SOURCE, 'source.txt', resume=True) == result
    manager.search.assert_not_awaited()


async def test_provenance_metadata_refinement_does_not_add_delta(setup):
    engine, manager, embedding, _ = setup
    target = await manager.create(ITEM['content'], todos=['task'], domain=['事务'])
    path = Path(manager._find_bucket_file(target))
    post = frontmatter.load(path)
    post['todos'] = 'task'
    path.write_text(frontmatter.dumps(post), encoding='utf-8')
    before = counts(manager, embedding)
    manager.search = AsyncMock(return_value=[await manager.get(target)])
    engine._extract_memories = AsyncMock(return_value=[dict(ITEM, todos=['task'])])
    assert (await engine.start(SOURCE, 'source.txt'))['memories_merged'] == 1
    assert counts(manager, embedding) == before
    assert item_of(engine)['resolutions']['memory'] == 'applied'
    assert item_of(engine)['resolutions']['delta'] == 'not_requested'
    assert (await manager.get(target))['metadata']['todos'] == ['task']


async def test_every_legacy_write_rejects_stale_epoch(setup):
    engine, manager, embedding, _ = setup
    accepted = engine.accept_legacy(SOURCE, 'source.txt')
    entered, release = asyncio.Event(), asyncio.Event()
    async def blocked(*args, **kwargs):
        entered.set()
        await release.wait()
        return [.25, .75]
    embedding._generate_embedding = AsyncMock(side_effect=blocked)
    running = asyncio.create_task(engine.run_legacy(accepted))
    await asyncio.wait_for(entered.wait(), 5)
    saved = state_of(engine)
    old = dict(run_id=saved['run_id'], owner=saved['owner'], epoch=saved['epoch'])
    context = dict(state=engine.state, claim=old, chunk=0, item=0)
    expire(engine)
    newer = engine.state.claim(saved['run_id'], 'new-owner')
    before = counts(manager, embedding)
    actions = [lambda: manager.ensure_legacy_import_operation(context),
               lambda: manager.commit_legacy_import_delta(context),
               lambda: manager.commit_legacy_import_embedding(context, embedding, [.25,.75]),
               lambda: engine.state.checkpoint(old), lambda: engine.state.heartbeat(old),
               lambda: engine._legacy_plan_item(context, ITEM, None, False)]
    for action in actions:
        with pytest.raises(BucketIdempotencyError, match='operation_claim_stale'):
            action()
    with pytest.raises(BucketIdempotencyError, match='operation_claim_stale'):
        await manager.apply_import_operation(item_of(engine)['plan']['memory_key'], _legacy_import_context=context)
    assert counts(manager, embedding) == before
    release.set()
    with pytest.raises(BucketIdempotencyError, match='operation_claim_stale'):
        await running
    assert state_of(engine)['owner'] == 'new-owner'
    engine.state.release(newer)


async def test_extractor_parameters_filter_and_chunking(setup):
    engine, _, _, _ = setup
    provider = AsyncMock(return_value=SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps([
        dict(ITEM, importance=99, todos=['x', 'x']), {'content': ''}, 'invalid'])))]))
    engine.dehydrator.client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=provider)))
    items = await ImportEngine._extract_memories(engine, 'x'*13000)
    args = provider.call_args.kwargs
    assert args['messages'][1]['content'] == 'x'*12000
    assert args['model'] == 'extract-model' and args['max_tokens'] == 2048 and args['temperature'] == 0.0
    assert len(items) == 1 and items[0]['importance'] == 10 and items[0]['todos'] == ['x']
    chunks = import_memory.chunk_turns(import_memory.detect_and_parse(SOURCE, 'source.txt'))
    assert chunks == [dict(content='[用户] A conversation source for the isolated legacy import safety tests.',
                           timestamp_start='', timestamp_end='', turn_count=1)]


def worker(config, boundary=None, start_event=None):
    engine, manager, embedding = make_engine(config)
    if start_event is not None:
        if not start_event.wait(20):
            raise AssertionError('worker start barrier timed out')
    if boundary:
        if boundary == 'marker':
            manager._mark_import_operation_applied = lambda *args: os.kill(os.getpid(), signal.SIGKILL)
        elif boundary in ('delta', 'embedding'):
            owner = manager if boundary == 'delta' else embedding
            method = 'commit_legacy_import_delta' if boundary == 'delta' else 'store_trace_embedding'
            original = getattr(owner, method)
            def crash(*args, **kwargs):
                original(*args, **kwargs)
                os.kill(os.getpid(), signal.SIGKILL)
            setattr(owner, method, crash)
        else:
            original = engine.state.checkpoint
            def crash(claim, phase=None, change=None):
                result = original(claim, phase, change)
                if phase == boundary:
                    os.kill(os.getpid(), signal.SIGKILL)
                return result
            engine.state.checkpoint = crash
    result = asyncio.run(engine.start(SOURCE, 'source.txt', preserve_raw=True, resume=True))
    assert result['status'] == 'completed'


def join_worker(process):
    process.join(45)
    if process.is_alive():
        process.terminate()
        process.join(5)
        raise AssertionError('isolated worker did not finish')


@pytest.mark.parametrize('boundary', ['chunk.extraction_frozen', 'item.planned', 'marker', 'delta', 'embedding',
                                     'item.completed', 'chunk.completed', 'run.completed'])
async def test_sigkill_durable_gaps(setup, boundary):
    engine, _, _, config = setup
    engine.accept_legacy(SOURCE, 'source.txt', True)
    process = multiprocessing.get_context('spawn').Process(target=worker, args=(config, boundary))
    process.start()
    join_worker(process)
    assert process.exitcode == -signal.SIGKILL
    expire(engine)
    restarted, manager2, embedding2 = make_engine(config)
    restarted._extract_memories = AsyncMock(side_effect=AssertionError('frozen extraction'))
    assert (await restarted.start(SOURCE, 'source.txt', True, True))['status'] == 'completed'
    assert counts(manager2, embedding2) == (1, 1, 1, 0)


async def test_two_process_same_run(setup):
    engine, manager, embedding, config = setup
    engine.accept_legacy(SOURCE, 'source.txt', True)
    ctx = multiprocessing.get_context('spawn')
    start_event = ctx.Event()
    processes = [ctx.Process(target=worker, args=(config, None, start_event)) for _ in range(2)]
    try:
        for process in processes:
            process.start()
        start_event.set()
        for process in processes:
            join_worker(process)
            assert process.exitcode == 0
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join(5)
    assert counts(manager, embedding) == (1, 1, 1, 0)


# Literal pre-S-4A journal at c8736ce, including the O5C nullable marker column.
_PRE_S4A_IMPORT_SCHEMA = '''CREATE TABLE ob_import_operations (
    operation_key TEXT PRIMARY KEY,
    operation_kind TEXT NOT NULL CHECK (operation_kind IN ('create', 'update')),
    target_bucket_id TEXT,
    payload_json TEXT NOT NULL,
    payload_digest TEXT NOT NULL,
    result_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('planned', 'applied')),
    memory_mutation_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
)'''
_OLD_IMPORT_COLUMNS = ('operation_key', 'operation_kind', 'target_bucket_id', 'payload_json',
                       'payload_digest', 'result_id', 'status', 'memory_mutation_id', 'created_at', 'updated_at')


def seed_pre_s4a_journal(manager):
    with sqlite3.connect(manager.history_db_path) as conn:
        conn.execute(_PRE_S4A_IMPORT_SCHEMA)
        conn.execute('CREATE INDEX idx_ob_import_operations_target ON ob_import_operations(target_bucket_id)')
        for key, status in [('pre-s4a-planned', 'planned'), ('pre-s4a-applied', 'applied')]:
            payload, digest = manager._canonical_import_payload(dict(content='Old memory: ' + key,
                tags=['old'], importance=5, domain=['事务'], valence=.5, arousal=.3, name='old-memory'))
            conn.execute('INSERT INTO ob_import_operations VALUES (?,?,?,?,?,?,?,?,?,?)',
                (key, 'create', None, payload, digest, manager._operation_result_id(key), status,
                 None if status == 'planned' else 'old-mutation-id', '2026-01-02T03:04:05', '2026-01-03T04:05:06'))
    return old_import_rows(manager)


def old_import_rows(manager):
    with sqlite3.connect(manager.history_db_path) as conn:
        return conn.execute('SELECT ' + ','.join(_OLD_IMPORT_COLUMNS)
            + " FROM ob_import_operations WHERE operation_key LIKE 'pre-s4a-%' ORDER BY operation_key").fetchall()


def import_columns(manager):
    with sqlite3.connect(manager.history_db_path) as conn:
        return conn.execute('PRAGMA table_info(ob_import_operations)').fetchall()


def trace_import_sql(monkeypatch, manager, *, deny_alter=False):
    connect, statements = sqlite3.connect, []
    def tracked(database, *args, **kwargs):
        conn = connect(database, *args, **kwargs)
        if str(database) == manager.history_db_path:
            conn.set_trace_callback(lambda sql: statements.append(' '.join(sql.split())))
            if deny_alter:
                conn.set_authorizer(lambda action, *args: sqlite3.SQLITE_DENY
                    if action == sqlite3.SQLITE_ALTER_TABLE else sqlite3.SQLITE_OK)
        return conn
    monkeypatch.setattr(bucket_manager.sqlite3, 'connect', tracked)
    return statements


async def test_compat_fresh_schema_and_completed_replay(setup, monkeypatch):
    engine, manager, embedding, _ = setup
    statements = trace_import_sql(monkeypatch, manager)
    assert (await engine.start(SOURCE, 'source.txt'))['status'] == 'completed'
    column = next(row for row in import_columns(manager) if row[1] == 'effects_json')
    assert column[2:5] == ('TEXT', 0, None)
    assert not any(sql.startswith('ALTER ') for sql in statements)
    assert counts(manager, embedding) == (1, 1, 1, 0)
    before = Path(manager.history_db_path).read_bytes()
    monkeypatch.setattr(manager, '_ensure_import_operation_table', lambda: (_ for _ in ()).throw(AssertionError('completed replay must not ensure')))
    assert (await engine.start(SOURCE, 'source.txt', resume=True))['status'] == 'completed'
    assert Path(manager.history_db_path).read_bytes() == before


async def test_compat_real_pre_s4a_normal_runtime_direct_acceptance(setup, monkeypatch):
    _, manager, _, config = setup
    old_rows = seed_pre_s4a_journal(manager)
    statements = trace_import_sql(monkeypatch, manager)
    monkeypatch.setenv('OMBRE_RM_RUNTIME_ENABLED', '0')
    config = copy.deepcopy(config)
    config['embedding'].update(enabled=False, api_key='')
    config['dehydration']['api_key'] = ''
    monkeypatch.setattr(server, 'config', config)
    monkeypatch.setattr(server, '_runtime_components', None)
    runtime = server._get_runtime_components()
    engine, current = runtime['import_engine'], runtime['bucket_mgr']
    assert 'effects_json' not in [row[1] for row in import_columns(current)]
    assert old_import_rows(current) == old_rows
    engine._extract_memories = AsyncMock(return_value=copy.deepcopy([ITEM]))
    accepted = engine.accept_legacy(SOURCE, 'source.txt')
    assert state_of(engine)['phase'] == 'run.accepted'
    engine._extract_memories.assert_not_awaited()
    assert [sql for sql in statements if sql.startswith('ALTER ')] == [
        'ALTER TABLE ob_import_operations ADD COLUMN effects_json TEXT']
    assert old_import_rows(current) == old_rows
    with sqlite3.connect(current.history_db_path) as conn:
        assert conn.execute("SELECT effects_json FROM ob_import_operations WHERE operation_key LIKE 'pre-s4a-%'").fetchall() == [(None,), (None,)]
        assert not conn.execute("SELECT 1 FROM sqlite_master WHERE name='ob_s4_requests'").fetchone()
    assert (await engine.run_legacy(accepted))['status'] == 'completed'
    assert old_import_rows(current) == old_rows
    with sqlite3.connect(current.history_db_path) as conn:
        row = conn.execute('SELECT effects_json FROM ob_import_operations WHERE operation_key=?',
                           (item_of(engine)['plan']['memory_key'],)).fetchone()
        assert json.loads(row[0])['delta']['outcome'] == 'applied'
        assert not conn.execute("SELECT 1 FROM sqlite_master WHERE name='ob_s4_requests'").fetchone()


async def test_compat_ensure_idempotent_and_old_operation_replay(setup, monkeypatch):
    _, manager, embedding, _ = setup
    old_rows = seed_pre_s4a_journal(manager)
    statements = trace_import_sql(monkeypatch, manager)
    manager._ensure_import_operation_table()
    assert old_import_rows(manager) == old_rows
    column = next(row for row in import_columns(manager) if row[1] == 'effects_json')
    assert column[2:5] == ('TEXT', 0, None)
    before = Path(manager.history_db_path).read_bytes()
    manager._ensure_import_operation_table()
    assert Path(manager.history_db_path).read_bytes() == before and old_import_rows(manager) == old_rows
    assert [sql for sql in statements if sql.startswith('ALTER ')] == [
        'ALTER TABLE ob_import_operations ADD COLUMN effects_json TEXT']
    original = manager.inspect_import_operation('pre-s4a-planned')
    result = await manager.apply_import_operation('pre-s4a-planned')
    assert await manager.apply_import_operation('pre-s4a-planned') == result
    after = manager.inspect_import_operation('pre-s4a-planned')
    for field in ('payload', 'payload_digest', 'result_id', 'created_at'):
        assert after[field] == original[field]
    assert after['effects_json'] is None and after['status'] == 'applied'
    assert counts(manager, embedding) == (1, 1, 1, 0)


def schema_worker(config, mode, ready, start, results):
    manager = BucketManager(config)
    connect, statements = sqlite3.connect, []
    def tracked(database, *args, **kwargs):
        conn = connect(database, *args, **kwargs)
        if str(database) == manager.history_db_path:
            conn.set_trace_callback(lambda sql: statements.append(' '.join(sql.split())))
        return conn
    sqlite3.connect = tracked
    try:
        ready.put('ready')
        if not start.wait(45):
            raise AssertionError('schema worker barrier timed out')
        if mode == 'ensure':
            manager._ensure_import_operation_table()
            outcome = 'ensured'
        else:
            engine = ImportEngine(config, manager, SimpleNamespace(model='extract-model'))
            try:
                engine.accept_legacy(SOURCE, 'source.txt')
                outcome = 'accepted'
            except BucketIdempotencyError as exc:
                outcome = str(exc)
        results.put((outcome, [sql for sql in statements if sql.startswith('ALTER ')]))
    finally:
        sqlite3.connect = connect


@pytest.mark.parametrize('mode', ['ensure', 'accept'])
async def test_compat_concurrent_process_ensure_and_acceptance(setup, mode):
    engine, manager, embedding, config = setup
    old_rows = seed_pre_s4a_journal(manager)
    ctx = multiprocessing.get_context('spawn')
    ready, results, start = ctx.Queue(), ctx.Queue(), ctx.Event()
    processes = [ctx.Process(target=schema_worker, args=(config, mode, ready, start, results)) for _ in range(2)]
    try:
        for process in processes:
            process.start()
        for _ in processes:
            assert await asyncio.to_thread(ready.get, True, 45) == 'ready'
        start.set()
        for process in processes:
            await asyncio.to_thread(join_worker, process)
            assert process.exitcode == 0
        outcomes = [results.get(timeout=5) for _ in processes]
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join(5)
        ready.close()
        results.close()
    assert sorted(outcome for outcome, _ in outcomes) == (
        ['ensured', 'ensured'] if mode == 'ensure' else ['Import already running', 'accepted'])
    assert [sql for _, sqls in outcomes for sql in sqls] == [
        'ALTER TABLE ob_import_operations ADD COLUMN effects_json TEXT']
    assert manager.legacy_import_receipts_available() and old_import_rows(manager) == old_rows
    assert (await engine.start(SOURCE, 'source.txt', resume=mode == 'accept'))['status'] == 'completed'
    assert counts(manager, embedding) == (1, 1, 1, 0) and old_import_rows(manager) == old_rows


@pytest.mark.parametrize('entry', ['engine', 'http'])
async def test_compat_sqlite_ddl_failure_stops_before_acceptance(setup, monkeypatch, caplog, entry):
    engine, manager, embedding, config = setup
    old_rows = seed_pre_s4a_journal(manager)
    before = Path(manager.history_db_path).read_bytes()
    trace_import_sql(monkeypatch, manager, deny_alter=True)
    if entry == 'engine':
        assert await engine.start(SOURCE, 'source.txt') == {'error': 'legacy_import_schema_upgrade_failed'}
    else:
        monkeypatch.setattr(server, 'config', config)
        monkeypatch.setattr(server, 'import_engine', engine)
        monkeypatch.setattr(server, '_require_dashboard_write', lambda *args: None)
        response = await server.api_import_upload(Request())
        assert response.status_code == 409
        assert json.loads(response.body) == {'error': 'legacy_import_schema_upgrade_failed'}
    assert 'Legacy import schema upgrade failed' in caplog.text and 'not authorized' in caplog.text
    assert Path(manager.history_db_path).read_bytes() == before and old_import_rows(manager) == old_rows
    assert not Path(engine.state.state_file).exists() and not server._IMPORT_BACKGROUND_TASKS
    assert not engine.is_running and counts(manager, embedding) == (0, 0, 0, 0)
    engine._extract_memories.assert_not_awaited()
    manager.search.assert_not_awaited()


async def test_compat_already_keyed_upgraded_schema_and_rows_unchanged(setup, monkeypatch):
    engine, manager, _, _ = setup
    old_rows = seed_pre_s4a_journal(manager)
    source = await manager.create('Keyed compatibility source')
    monkeypatch.setattr(server, 'bucket_mgr', manager)
    await server.trace(bucket_id=source, content='keyed addition', append=True, operation_id='compat-keyed')
    assert manager.inspect_trace_request('compat-keyed')['status'] == 'completed'
    assert old_import_rows(manager) == old_rows
    with sqlite3.connect(manager.history_db_path) as conn:
        keyed_rows = conn.execute('SELECT * FROM ob_s4_requests ORDER BY operation_id').fetchall()
    statements = trace_import_sql(monkeypatch, manager)
    before = Path(manager.history_db_path).read_bytes()
    accepted = engine.accept_legacy(SOURCE, 'source.txt')
    assert Path(manager.history_db_path).read_bytes() == before
    assert not any(sql.startswith('ALTER ') for sql in statements)
    assert (await engine.run_legacy(accepted))['status'] == 'completed'
    assert old_import_rows(manager) == old_rows
    with sqlite3.connect(manager.history_db_path) as conn:
        assert conn.execute('SELECT * FROM ob_s4_requests ORDER BY operation_id').fetchall() == keyed_rows


async def test_compat_capability_defense_still_stops_zero_write(setup, monkeypatch):
    engine, manager, _, _ = setup
    manager._ensure_import_operation_table()
    monkeypatch.setattr(manager, 'legacy_import_receipts_available', lambda: False)
    before = Path(manager.history_db_path).read_bytes()
    assert (await engine.start(SOURCE, 'source.txt')) == {'error': 'legacy_effect_receipts_unavailable'}
    assert Path(manager.history_db_path).read_bytes() == before
    assert not Path(engine.state.state_file).exists()


@pytest.mark.parametrize('kind', ['items', 'chunks'])
async def test_only_first_unfinished_item_or_chunk_runs(setup, monkeypatch, kind):
    engine, manager, embedding, config = setup
    source = SOURCE
    if kind == 'items':
        engine._extract_memories = AsyncMock(return_value=[ITEM, dict(ITEM, name='second', content='Second memory')])
    else:
        source = json.dumps({'messages': [dict(role='user', content='甲'*12000), dict(role='user', content='乙'*12000)]})
    checkpoint = engine.state.checkpoint
    def stop(claim, phase=None, change=None):
        result = checkpoint(claim, phase, change)
        if phase == 'item.completed':
            raise asyncio.CancelledError()
        return result
    monkeypatch.setattr(engine.state, 'checkpoint', stop)
    with pytest.raises(asyncio.CancelledError):
        await engine.start(source, 'source.json', True)
    first_target = item_of(engine)['plan']['target']
    first_bytes = Path(manager._find_bucket_file(first_target)).read_bytes()
    restarted, manager2, embedding2 = make_engine(config)
    if kind == 'items':
        forbid_replanning(restarted, manager2)
    result = await restarted.start(source, 'source.json', True, True)
    assert result['status'] == 'completed' and result['memories_created'] == 2
    assert Path(manager._find_bucket_file(first_target)).read_bytes() == first_bytes
    assert counts(manager2, embedding2) == (2, 2, 2, 0)
    assert restarted._extract_memories.await_count == int(kind == 'chunks')


@pytest.mark.parametrize('mismatch', ['source', 'filename', 'preserve', 'pipeline'])
async def test_resume_binding_mismatch_is_zero_write(setup, mismatch):
    engine, manager, _, _ = setup
    engine.accept_legacy(SOURCE, 'source.txt')
    before = {p: p.read_bytes() for p in Path(manager.base_dir).rglob('*') if p.is_file()}
    if mismatch == 'pipeline':
        engine.dehydrator.model = 'different-model'
    result = await engine.start(SOURCE + ('changed' if mismatch == 'source' else ''),
        'other.txt' if mismatch == 'filename' else 'source.txt', mismatch == 'preserve', True)
    assert result['error'] in ('legacy_source_conflict', 'legacy_pipeline_conflict')
    assert before == {p: p.read_bytes() for p in Path(manager.base_dir).rglob('*') if p.is_file()}


async def test_v1_completed_http_and_status_do_not_initialize_runtime(setup, monkeypatch):
    engine, _, _, config = setup
    raw = v1_bytes('completed', 1, 1)
    Path(engine.state.state_file).write_bytes(raw)
    monkeypatch.setattr(server, 'config', config)
    monkeypatch.setattr(server, 'import_engine', server._LazyRuntimeComponent('import_engine'))
    monkeypatch.setattr(server, '_get_runtime_components', lambda: (_ for _ in ()).throw(AssertionError('pure completed replay')))
    monkeypatch.setattr(server, '_require_dashboard_write', lambda *args: None)
    monkeypatch.setattr(server, '_require_auth', lambda *args: None)
    assert (await server.api_import_upload(Request(resume='1'))).status_code == 200
    status = json.loads((await server.api_import_status(Request())).body)
    assert status['status'] == 'completed' and set(status) == set(_IMPORT_PUBLIC_FIELDS)
    assert Path(engine.state.state_file).read_bytes() == raw and not server._IMPORT_BACKGROUND_TASKS


@pytest.mark.parametrize('preserve', [False, True])
async def test_v2_completed_http_replay_is_pure_before_lazy_runtime(setup, monkeypatch, preserve):
    engine, manager, _, config = setup
    completed = await engine.start(SOURCE, 'source.txt', preserve)
    await manager.delete(item_of(engine)['plan']['target'])
    root = Path(config['buckets_dir'])
    before = {str(path.relative_to(root)): path.read_bytes() for path in root.rglob('*') if path.is_file()}
    monkeypatch.setattr(server, 'config', config)
    monkeypatch.setattr(server, 'import_engine', server._LazyRuntimeComponent('import_engine'))
    monkeypatch.setattr(server, '_get_runtime_components', lambda: (_ for _ in ()).throw(AssertionError('pure completed replay')))
    monkeypatch.setattr(server, '_require_dashboard_write', lambda *args: None)
    monkeypatch.setattr(server, '_require_auth', lambda *args: None)
    response = await server.api_import_upload(Request(resume='1', preserve_raw=str(int(preserve))))
    assert response.status_code == 200
    assert json.loads((await server.api_import_status(Request())).body) == completed
    conflict = await server.api_import_upload(Request(resume='1', preserve_raw=str(int(not preserve))))
    assert conflict.status_code == 409 and json.loads(conflict.body) == {'error': 'legacy_source_conflict'}
    assert not server._IMPORT_BACKGROUND_TASKS
    assert {str(path.relative_to(root)): path.read_bytes() for path in root.rglob('*') if path.is_file()} == before


async def test_non_exact_search_hit_creates_without_semantic_merge(setup):
    engine, manager, _, _ = setup
    target = await manager.create('Similar topic but a distinct fact', domain=['事务'])
    manager.search = AsyncMock(return_value=[dict(await manager.get(target), score=100)])
    assert (await engine.start(SOURCE, 'source.txt'))['memories_created'] == 1
    assert item_of(engine)['plan']['decision'] == 'create'
    assert item_of(engine)['plan']['target'] != target


async def test_snapshot_existing_conflict_never_overwrites(setup):
    engine, manager, _, _ = setup
    raw = v1_bytes()
    Path(engine.state.state_file).write_bytes(raw)
    snapshot = Path(manager.base_dir) / ('import_state.v1.' + hashlib.sha256(raw).hexdigest() + '.json')
    snapshot.write_bytes(b'existing evidence must survive')
    assert (await engine.start(SOURCE, 'source.txt')) == {'error': 'legacy_snapshot_conflict'}
    assert Path(engine.state.state_file).read_bytes() == raw
    assert snapshot.read_bytes() == b'existing evidence must survive'


async def test_real_tcp_response_loss_reconnect_preserves_runner(setup, monkeypatch):
    engine, manager, embedding, config = setup
    monkeypatch.setattr(server, 'config', config)
    monkeypatch.setattr(server, 'import_engine', engine)
    monkeypatch.setattr(server, '_require_dashboard_write', lambda *args: None)
    monkeypatch.setattr(server, '_require_auth', lambda *args: None)
    entered, release = asyncio.Event(), asyncio.Event()
    accepted_response, send_response = asyncio.Event(), asyncio.Event()
    async def blocked(*args):
        entered.set()
        await release.wait()
        return [ITEM]
    engine._extract_memories = AsyncMock(side_effect=blocked)
    app = Starlette(routes=[Route('/api/import/upload', server.api_import_upload, methods=['POST']),
                            Route('/api/import/status', server.api_import_status)])
    first = True
    async def delayed_response(scope, receive, send):
        nonlocal first
        delay = scope['type'] == 'http' and first
        if delay:
            first = False
        async def delayed_send(message):
            if delay and message['type'] == 'http.response.start':
                accepted_response.set()
                await send_response.wait()
            await send(message)
        await app(scope, receive, delayed_send)
    sock = socket.socket()
    sock.bind(('127.0.0.1', 0))
    sock.listen(128)
    runner = uvicorn.Server(uvicorn.Config(delayed_response, log_level='warning', access_log=False, lifespan='off'))
    serving = asyncio.create_task(runner.serve(sockets=[sock]))
    try:
        async with asyncio.timeout(10):
            while not runner.started:
                if serving.done():
                    await serving
                await asyncio.sleep(.01)
        port = sock.getsockname()[1]
        _, writer = await asyncio.open_connection('127.0.0.1', port)
        payload = SOURCE.encode()
        writer.write((f'POST /api/import/upload?filename=source.txt HTTP/1.1\r\nHost: localhost\r\n'
                      f'Content-Type: text/plain\r\nContent-Length: {len(payload)}\r\n\r\n').encode() + payload)
        await writer.drain()
        await asyncio.wait_for(accepted_response.wait(), 5)
        await asyncio.wait_for(entered.wait(), 5)
        original_run = state_of(engine)['run_id']
        writer.close()
        await writer.wait_closed()
        send_response.set()
        assert any(not task.done() for task in server._IMPORT_BACKGROUND_TASKS)
        release.set()
        await asyncio.gather(*list(server._IMPORT_BACKGROUND_TASKS))
        async with httpx.AsyncClient(base_url=f'http://127.0.0.1:{port}') as client:
            response = await client.post('/api/import/upload?filename=source.txt&resume=1', content=payload)
            assert response.status_code == 200 and response.json()['status'] == 'started'
            status = (await client.get('/api/import/status')).json()
            assert status['status'] == 'completed' and set(status) == set(_IMPORT_PUBLIC_FIELDS)
        assert state_of(engine)['run_id'] == original_run
        engine._extract_memories.assert_awaited_once()
        assert counts(manager, embedding) == (1, 1, 1, 0)
    finally:
        release.set()
        send_response.set()
        await asyncio.gather(*list(server._IMPORT_BACKGROUND_TASKS), return_exceptions=True)
        runner.should_exit = True
        try:
            await asyncio.wait_for(serving, 10)
        finally:
            sock.close()

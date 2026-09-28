"""S-4B: real isolated storage, HTTP waiter cancellation and durable recovery."""
import asyncio
import copy
import json
import multiprocessing
import os
import sqlite3
import time
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import frontmatter
import pytest
import pytest_asyncio

import server
from bucket_manager import BucketManager, BucketIdempotencyError
from embedding_engine import EmbeddingEngine

pytestmark = pytest.mark.asyncio
ANALYSIS = dict(domain=['work'], tags=['auto'], valence=.6, arousal=.4,
                suggested_name='entry', todos=['task'], importance=5)
ITEMS = [dict(content='same diary item', name='first', **{k: v for k, v in ANALYSIS.items() if k != 'suggested_name'}),
         dict(content='same diary item', name='second', **{k: v for k, v in ANALYSIS.items() if k != 'suggested_name'})]
DIARY = 'A diary entry long enough to use the ordered digest path.'


def install(config):
    manager = BucketManager(config)
    engine = EmbeddingEngine(config)
    manager.embedding_engine = engine
    engine._generate_embedding = AsyncMock(return_value=[.25, .75])
    engine.search_similar = AsyncMock(return_value=[])
    server.bucket_mgr, server.embedding_engine, server.config = manager, engine, config
    server.decay_engine = Mock(ensure_started=AsyncMock())
    server.dehydrator = Mock(analyze=AsyncMock(return_value=copy.deepcopy(ANALYSIS)),
                             digest=AsyncMock(return_value=copy.deepcopy(ITEMS)))
    server._similarity_doorbell = AsyncMock(return_value='')
    server._detect_conflict_warning = AsyncMock(return_value='')
    return manager, engine


@pytest_asyncio.fixture
async def setup(test_config, monkeypatch):
    monkeypatch.setenv('OMBRE_BUCKETS_DIR', test_config['buckets_dir'])
    # Restore the module globals after each test (other legacy tests share server).
    for name in ('bucket_mgr', 'embedding_engine', 'config', 'decay_engine', 'dehydrator',
                 '_similarity_doorbell', '_detect_conflict_warning'):
        monkeypatch.setattr(server, name, getattr(server, name))
    manager, engine = install(test_config)
    return manager, engine, test_config


async def call(kind='hold', key='request', **kwargs):
    if kind == 'grow':
        return await server.grow(kwargs.pop('content', DIARY), operation_id=key, **kwargs)
    return await server.hold(kwargs.pop('content', 'held memory'), operation_id=key, **kwargs)


def request_item(manager, key='request', ordinal=0):
    return manager.inspect_trace_request(key)['plan']['items'][ordinal]['plan']


def business_counts(manager):
    with sqlite3.connect(manager.history_db_path) as conn:
        delta = conn.execute('SELECT count(*) FROM boot_delta_events').fetchone()[0]
    return len(list(Path(manager.base_dir).rglob('*.md'))), delta


@pytest.mark.parametrize('kind', ['hold', 'grow'])
@pytest.mark.parametrize('key,valid', [('', False), ('x'*128, True), ('x'*129, False),
    ('雪😀', True), ('   ', True), (42, False), (False, False), ([], False)])
async def test_key_boundaries(setup, kind, key, valid):
    manager, _, _ = setup
    result = await call(kind, key)
    if valid:
        assert manager.inspect_trace_request(key)['status'] == 'completed'
    else:
        assert result == 'operation_id_invalid'
        assert business_counts(manager) == (0, 0)


async def test_supersedes_rejected_before_any_access(setup, monkeypatch):
    manager, _, _ = setup
    monkeypatch.setattr(manager, 'get', AsyncMock(side_effect=AssertionError('no get')))
    monkeypatch.setattr(manager, 'update', AsyncMock(side_effect=AssertionError('no update')))
    monkeypatch.setattr(manager, 'inspect_trace_request', Mock(side_effect=AssertionError('no journal')))
    assert await call(supersedes_id=' target ') == 'unsupported_combination: operation_id does not support supersedes_id.'
    server.decay_engine.ensure_started.assert_not_awaited()
    server.dehydrator.analyze.assert_not_awaited()
    assert business_counts(manager) == (0, 0)


@pytest.mark.parametrize('kind', ['hold', 'grow'])
async def test_completed_replay_payload_conflict_and_deletion(setup, kind):
    manager, _, _ = setup
    first = await call(kind)
    before = business_counts(manager)
    assert await call(kind) == first
    assert await call(kind, content='changed payload') == 'operation_id_conflict'
    for path in Path(manager.base_dir).rglob('*.md'):
        path.unlink()
    assert await call(kind) == first
    assert server.dehydrator.analyze.await_count == (1 if kind == 'hold' else 0)
    assert server.dehydrator.digest.await_count == (1 if kind == 'grow' else 0)
    assert before[0] == 1


@pytest.mark.parametrize('left,right', [('hold','grow'), ('grow','hold'), ('trace','hold'),
                                     ('trace','grow'), ('hold','trace'), ('grow','trace')])
async def test_cross_kind_namespace(setup, left, right):
    manager, _, _ = setup
    target = await manager.create('trace target')
    async def run(kind):
        return await server.trace(target, tags='tag', operation_id='shared') if kind == 'trace' else await call(kind, 'shared')
    await run(left)
    before = business_counts(manager)
    assert await run(right) == 'operation_id_conflict'
    assert business_counts(manager) == before


@pytest.mark.parametrize('mode', ['ordinary', 'pinned', 'feel'])
async def test_hold_create_reuse_and_independent_feel(setup, mode):
    manager, _, _ = setup
    kwargs = dict(pinned=mode == 'pinned', feel=mode == 'feel', valence=.7, arousal=.2)
    first = await call(key='one', **kwargs)
    assert await call(key='one', **kwargs) == first
    await call(key='two', **kwargs)
    one, two = request_item(manager, 'one'), request_item(manager, 'two')
    assert (one['target'] == two['target']) == (mode == 'ordinary')
    bucket = await manager.get(one['target'])
    assert bucket['metadata']['type'] == {'ordinary':'dynamic', 'pinned':'permanent', 'feel':'feel'}[mode]
    if mode == 'pinned':
        assert bucket['metadata']['pinned'] is True and bucket['metadata']['importance'] == 10
    timeline = server._read_emotion_timeline_for_write(server._emotion_timeline_path())
    assert len(timeline) == 2 and len({item['_s4_effect'] for item in timeline}) == 2
    public = server._with_emotion_timeline('', True)
    assert '_s4_effect' not in public and 's4:' not in public
    assert public.count('"source":"hold"') == 2


async def test_reuse_trigger_conflict_and_todo_union(setup):
    manager, _, _ = setup
    target = await manager.create('held memory', tags=['old'], importance=7, domain=['work'], todos=['old task'])
    first = await call(trigger_date='2026-10-01', tags='new', importance=9)
    assert 'reused=true' in first and 'ignored_fields' in first and 'trigger_date' in first
    meta = (await manager.get(target))['metadata']
    assert meta['tags'] == ['old'] and meta['importance'] == 7
    assert meta['todos'] == ['old task', 'task']
    ids = [r['id'] for r in meta['todo_provenance']]
    assert await call(trigger_date='2026-10-01', tags='new', importance=9) == first
    assert [r['id'] for r in (await manager.get(target))['metadata']['todo_provenance']] == ids
    assert 'rejected without writing' in await call(key='conflict', trigger_date='2026-10-02')
    assert (await manager.get(target))['metadata']['trigger_date'] == '2026-10-01'


async def test_digest_freezes_all_items_before_sequential_planning(setup, monkeypatch):
    manager, _, _ = setup
    original = manager.plan_hold_grow_item
    seen = []
    def planning(context, ordinal, values, candidate=None):
        request = manager.inspect_trace_request('request')
        assert len(request['plan']['items']) == 2
        assert request['plan']['items'][ordinal]['plan'] is None
        if ordinal == 0:
            assert all(item['plan'] is None for item in request['plan']['items'])
        else:
            assert 'result' in request['resolutions']['items']['0']
            assert candidate['id'] == request['plan']['items'][0]['plan']['target']
        seen.append(ordinal)
        return original(context, ordinal, values, candidate)
    monkeypatch.setattr(manager, 'plan_hold_grow_item', planning)
    text = await call('grow')
    assert seen == [0, 1] and text.startswith('2条|新建1/复用1')
    assert server.dehydrator.digest.await_count == 1
    assert business_counts(manager)[0] == 1


@pytest.mark.parametrize('kind,provider', [('hold','analyze'), ('grow','digest'), ('hold','embedding')])
async def test_waiter_cancellation_preserves_runner_and_provider_freeze(setup, kind, provider):
    manager, engine, _ = setup
    entered, release = asyncio.Event(), asyncio.Event()
    async def blocked(*args, **kwargs):
        entered.set()
        await release.wait()
        return [.25, .75] if provider == 'embedding' else copy.deepcopy(ANALYSIS if provider == 'analyze' else ITEMS)
    mock = AsyncMock(side_effect=blocked)
    if provider == 'embedding':
        engine.enabled = True
        engine._generate_embedding = mock
    else:
        setattr(server.dehydrator, provider, mock)
    waiter = asyncio.create_task(call(kind))
    await asyncio.wait_for(entered.wait(), 5)
    runner = next(active[1] for key, active in server._S4_HOLD_GROW_RUNNERS.items() if key[-1] == 'request')
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    assert not runner.done()
    release.set()
    first, concurrent = await asyncio.gather(call(kind), call(kind))
    assert first == concurrent == await call(kind)
    assert mock.await_count == 1
    assert business_counts(manager)[0] == 1


@pytest.mark.parametrize('kind', ['hold','grow'])
@pytest.mark.parametrize('step', ['memory','delta','embedding','trigger','related','emotion','source','result'])
async def test_effect_parent_checkpoint_gap_resume(setup, monkeypatch, kind, step):
    manager, engine, config = setup
    engine.enabled = True
    source = await manager.create('source for feeling', domain=['work']) if kind == 'hold' else ''
    kwargs = dict(feel=True, source_bucket=source, valence=.7, arousal=.2, trigger_date='2026-10-01') if kind == 'hold' else {}
    checkpoint = manager._trace_checkpoint
    interrupted = False
    def cancel(context, phase=None, **changes):
        nonlocal interrupted
        current = changes.get('resolutions', {}).get('items', {}).get('0', {})
        if not interrupted and step in current:
            interrupted = True
            raise asyncio.CancelledError()
        return checkpoint(context, phase, **changes)
    monkeypatch.setattr(manager, '_trace_checkpoint', cancel)
    with pytest.raises(asyncio.CancelledError):
        await call(kind, **kwargs)
    assert interrupted
    original_plan = copy.deepcopy(request_item(manager))
    restart = BucketManager(config, embedding_engine=engine)
    monkeypatch.setattr(server, 'bucket_mgr', restart)
    server.dehydrator.analyze = AsyncMock(side_effect=AssertionError('analysis frozen'))
    server.dehydrator.digest = AsyncMock(side_effect=AssertionError('items frozen'))
    first = await call(kind, **kwargs)
    assert first == await call(kind, **kwargs)
    assert request_item(restart)['target'] == original_plan['target']
    assert business_counts(restart)[0] == (2 if kind == 'hold' else 1)
    server.dehydrator.analyze.assert_not_awaited()
    server.dehydrator.digest.assert_not_awaited()
    if kind == 'hold':
        timeline = server._read_emotion_timeline_for_write(server._emotion_timeline_path())
        assert len(timeline) == 1
        assert 'source_marked=true' in first


async def test_grow_first_unfinished_item_no_reexecution(setup, monkeypatch):
    manager, engine, config = setup
    checkpoint = manager._trace_checkpoint
    def cancel(context, phase=None, **changes):
        if 'result' in changes.get('resolutions', {}).get('items', {}).get('0', {}):
            checkpoint(context, phase, **changes)
            raise asyncio.CancelledError()
        return checkpoint(context, phase, **changes)
    monkeypatch.setattr(manager, '_trace_checkpoint', cancel)
    with pytest.raises(asyncio.CancelledError):
        await call('grow')
    old_result = manager.inspect_trace_request('request')['resolutions']['items']['0']['result']
    restart = BucketManager(config, embedding_engine=engine)
    monkeypatch.setattr(server, 'bucket_mgr', restart)
    commit = restart.commit_hold_grow_memory
    def only_unfinished(context, ordinal):
        assert ordinal == 1
        return commit(context, ordinal)
    monkeypatch.setattr(restart, 'commit_hold_grow_memory', only_unfinished)
    server.dehydrator.digest = AsyncMock(side_effect=AssertionError('no digest'))
    assert old_result in await call('grow')
    assert business_counts(restart)[0] == 1


@pytest.mark.parametrize('provider', ['analyze', 'embedding'])
async def test_stale_provider_result_is_fenced(setup, provider):
    manager, engine, _ = setup
    entered, release = asyncio.Event(), asyncio.Event()
    async def blocked(*args, **kwargs):
        entered.set()
        await release.wait()
        return [.25, .75] if provider == 'embedding' else copy.deepcopy(ANALYSIS)
    if provider == 'embedding':
        engine.enabled = True
        engine._generate_embedding = blocked
    else:
        server.dehydrator.analyze = blocked
    active = asyncio.create_task(call())
    await asyncio.wait_for(entered.wait(), 5)
    request = manager.inspect_trace_request('request')
    with sqlite3.connect(manager.history_db_path) as conn:
        conn.execute("UPDATE ob_s4_requests SET lease_until=0 WHERE operation_id='request'")
    newer = manager._claim_s4_request('request', request['payload'], request['normalization_context'], 'newer')
    release.set()
    assert await active == 'operation_claim_stale'
    after = manager.inspect_trace_request('request')
    if provider == 'analyze':
        assert 'analysis' not in after['plan'] and business_counts(manager) == (0, 0)
    else:
        assert engine.trace_embedding_receipt(request_item(manager)['keys']['embedding']) is None
        assert 'embedding_candidate' not in after['resolutions']['items']['0']
    manager._release_trace_request(newer)


# Literal S-4A schema at aac3cfad, including its trace-only CHECK.
LEGACY_SCHEMA = """CREATE TABLE ob_s4_requests (
operation_id TEXT PRIMARY KEY COLLATE BINARY,
kind TEXT NOT NULL CHECK(kind='trace'), schema_version INTEGER NOT NULL,
root_binding TEXT NOT NULL, payload_json TEXT NOT NULL, payload_digest TEXT NOT NULL,
normalization_context_json TEXT NOT NULL, phase TEXT NOT NULL, status TEXT NOT NULL,
plan_json TEXT NOT NULL DEFAULT '{}', resolutions_json TEXT NOT NULL DEFAULT '{}',
result_text TEXT, completed_receipt_json TEXT, owner_instance TEXT,
epoch INTEGER NOT NULL DEFAULT 0, lease_until REAL NOT NULL DEFAULT 0,
created_at TEXT NOT NULL, completed_at TEXT, last_error_code TEXT)"""


async def old_rows(manager):
    target = await manager.create('old trace target')
    payload = dict(kind='trace', bucket_id=target, name='', content='addition', domain=[], tags=[], related=[], unrelate=[],
                   provenance_kind=None, trigger_date='', todos=None, todo_items=None,
                   valence=-1., arousal=-1., importance=-1, resolved=-1, digested=-1, dormant=-1, append=False)
    encoded, digest = manager._canonical_import_payload(payload)
    context = json.dumps({'version':1, 'aliases':list(server.DISPLAY_ALIASES.items())})
    with sqlite3.connect(manager.history_db_path) as conn:
        conn.execute(LEGACY_SCHEMA)
        for key, phase, status in [('old-pending','accepted','pending'), ('old-completed','completed','completed')]:
            conn.execute('INSERT INTO ob_s4_requests VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (key, 'trace', 1, str(Path(manager.base_dir).resolve()), encoded, digest, context, phase, status,
                 '{}' if status == 'pending' else '{"keys":{"memory":"unchanged-old-key"}}',
                 '{}' if status == 'pending' else '{"legacy":"receipt"}',
                 None if status == 'pending' else 'old completed response',
                 None if status == 'pending' else '{"legacy":"receipt"}',
                 None, 7, 0, 'old-created', None if status == 'pending' else 'old-completed', 'old-error'))
        before = conn.execute('SELECT * FROM ob_s4_requests ORDER BY operation_id').fetchall()
    return target, before


async def test_real_s4a_schema_migration_preserves_each_field_replay_and_namespace(setup):
    manager, _, _ = setup
    target, before = await old_rows(manager)
    manager._ensure_trace_request_table()
    manager._ensure_trace_request_table()
    with sqlite3.connect(manager.history_db_path) as conn:
        assert conn.execute('SELECT * FROM ob_s4_requests ORDER BY operation_id').fetchall() == before
    assert await server.trace(target, content='addition', operation_id='old-completed') == before[0][11]
    assert '已修改' in await server.trace(target, content='addition', operation_id='old-pending')
    for kind in ('hold','grow'):
        claimed = manager._claim_s4_request(kind, {'kind':kind, 'content':'x'}, {'version':1,'aliases':[]}, 'owner')
        manager._release_trace_request(claimed)
    with pytest.raises(BucketIdempotencyError, match='operation_id_conflict'):
        manager._claim_s4_request('hold', {'kind':'grow','content':'x'}, {'version':1,'aliases':[]}, 'other')


async def test_migration_failure_rolls_back_original_table_and_rows(setup, monkeypatch):
    manager, _, _ = setup
    _, before = await old_rows(manager)
    connect = sqlite3.connect
    class Interrupted(sqlite3.Connection):
        def execute(self, sql, *args):
            if sql.startswith('INSERT INTO ob_s4_requests SELECT'):
                raise sqlite3.OperationalError('interrupted schema replacement')
            return super().execute(sql, *args)
    monkeypatch.setattr(sqlite3, 'connect', lambda *a, **k: connect(*a, factory=Interrupted, **k))
    with pytest.raises(sqlite3.OperationalError):
        manager._ensure_trace_request_table()
    with connect(manager.history_db_path) as conn:
        assert conn.execute('SELECT * FROM ob_s4_requests ORDER BY operation_id').fetchall() == before
        assert "CHECK(kind='trace')" in conn.execute("SELECT sql FROM sqlite_master WHERE name='ob_s4_requests'").fetchone()[0]


def process_worker(config, kind, step, queue=None, ready=None):
    async def run():
        manager, _ = install(config)
        if step:
            checkpoint = manager._trace_checkpoint
            def kill(context, phase=None, **changes):
                current = changes.get('resolutions', {}).get('items', {}).get('0', {})
                if step == 'items_frozen' and phase == 'items_frozen':
                    checkpoint(context, phase, **changes)
                    os.kill(os.getpid(), 9)
                if step in current:
                    os.kill(os.getpid(), 9)
                return checkpoint(context, phase, **changes)
            manager._trace_checkpoint = kill
        if ready is not None:
            ready.set()
        result = await call(kind, feel=kind == 'hold', valence=.7, arousal=.2) if kind == 'hold' else await call(kind)
        if queue:
            queue.put(result)
    asyncio.run(run())


def join(proc, ready):
    # Cold imports on /mnt/d are separate from the bounded operation itself.
    startup_deadline = time.monotonic() + 120
    while proc.is_alive() and not ready.is_set():
        if time.monotonic() >= startup_deadline:
            proc.terminate()
            proc.join(5)
            raise AssertionError('owned test worker startup timeout')
        ready.wait(.1)
    proc.join(45)
    if proc.is_alive():
        proc.terminate()
        proc.join(5)
        raise AssertionError('owned test worker operation timeout')


@pytest.mark.parametrize('kind,step', [('hold','memory'), ('hold','delta'), ('hold','emotion'),
                                     ('grow','memory'), ('grow','delta'), ('grow','items_frozen')])
async def test_sigkill_effect_without_parent_receipt(setup, kind, step):
    manager, _, _ = setup
    ctx = multiprocessing.get_context('spawn')
    ready = ctx.Event()
    proc = ctx.Process(target=process_worker, args=(server.config, kind, step, None, ready))
    proc.start()
    await asyncio.to_thread(join, proc, ready)
    assert proc.exitcode == -9
    with sqlite3.connect(manager.history_db_path) as conn:
        conn.execute('UPDATE ob_s4_requests SET lease_until=0')
    server.dehydrator.analyze = AsyncMock(side_effect=AssertionError('frozen analysis'))
    server.dehydrator.digest = AsyncMock(side_effect=AssertionError('frozen digest'))
    text = await call(kind, feel=True, valence=.7, arousal=.2) if kind == 'hold' else await call(kind)
    assert text == (await call(kind, feel=True, valence=.7, arousal=.2) if kind == 'hold' else await call(kind))
    assert business_counts(manager) == (1, 1)


@pytest.mark.parametrize('kind', ['hold','grow'])
async def test_two_process_same_key(setup, kind):
    manager, _, config = setup
    ctx = multiprocessing.get_context('spawn')
    queue = ctx.Queue()
    ready = [ctx.Event() for _ in range(2)]
    procs = [ctx.Process(target=process_worker, args=(config, kind, None, queue, signal)) for signal in ready]
    for proc in procs:
        proc.start()
    await asyncio.gather(*(asyncio.to_thread(join, proc, signal) for proc, signal in zip(procs, ready)))
    assert [proc.exitcode for proc in procs] == [0, 0]
    assert queue.get(timeout=2) == queue.get(timeout=2)
    assert business_counts(manager) == (1, 1)


@pytest.mark.parametrize('kind', ['hold','grow'])
async def test_none_calls_original_body_without_journal(setup, kind, monkeypatch):
    manager, _, _ = setup
    monkeypatch.setattr(manager, 'inspect_trace_request', Mock(side_effect=AssertionError('no S4 record')))
    monkeypatch.setattr(server, '_hold_grow_keyed', AsyncMock(side_effect=AssertionError('no keyed path')))
    first = await call(kind, None)
    second = await call(kind, None)
    assert '新建' in first and '复用' in second
    assert business_counts(manager)[0] == 1


async def test_none_supersedes_legacy_and_feel_priority(setup):
    manager, _, _ = setup
    target = await manager.create('old body', domain=['work'])
    assert await call(key=None, content='new body', supersedes_id=target) == f'fact evolved in place: {target}'
    assert (await manager.get(target))['content'] == 'new body'
    await call(key=None, pinned=True, feel=True)
    buckets = await manager.list_all()
    assert any(b['metadata']['type'] == 'feel' for b in buckets)


async def test_reuse_target_is_frozen_after_decision(setup, monkeypatch):
    manager, engine, config = setup
    target = await manager.create('held memory', domain=['work'])
    checkpoint = manager._trace_checkpoint
    def cancel(context, phase=None, **changes):
        result = checkpoint(context, phase, **changes)
        if phase == 'items_running':
            raise asyncio.CancelledError()
        return result
    monkeypatch.setattr(manager, '_trace_checkpoint', cancel)
    with pytest.raises(asyncio.CancelledError):
        await call()
    restart = BucketManager(config, embedding_engine=engine)
    monkeypatch.setattr(server, 'bucket_mgr', restart)
    forbidden = AsyncMock(side_effect=AssertionError('retry must not search'))
    monkeypatch.setattr(restart, 'search', forbidden)
    assert f'bucket_id={target} reused=true' in await call()
    forbidden.assert_not_awaited()


async def test_related_selection_is_frozen_before_publication(setup, monkeypatch):
    manager, engine, _ = setup
    old = await manager.create('original neighbor', domain=['work'])
    engine.store_trace_embedding('seed-old', old, [1., 0.], 'seed', engine.model, 'seed')
    engine.enabled = True
    engine._generate_embedding = AsyncMock(return_value=[1., 0.])
    checkpoint = manager._trace_checkpoint
    def cancel(context, phase=None, **changes):
        current = changes.get('resolutions', {}).get('items', {}).get('0', {})
        result = checkpoint(context, phase, **changes)
        if 'selection' in current and 'related' not in current:
            raise asyncio.CancelledError()
        return result
    monkeypatch.setattr(manager, '_trace_checkpoint', cancel)
    with pytest.raises(asyncio.CancelledError):
        await call()
    target = request_item(manager)['target']
    frozen = manager.inspect_trace_request('request')['resolutions']['items']['0']['selection']
    assert [pair[0] for pair in frozen] == [old]
    monkeypatch.setattr(manager, '_trace_checkpoint', checkpoint)
    engine.enabled = False
    new = await manager.create('later better neighbor', domain=['work'])
    engine.store_trace_embedding('change-old', old, [0., 1.], 'seed', engine.model, 'seed')
    engine.store_trace_embedding('seed-new', new, [1., 0.], 'seed', engine.model, 'seed')
    engine.enabled = True
    await call()
    related = (await manager.get(target))['metadata']['related_buckets']
    assert old in related and new not in related
    assert manager.inspect_trace_request('request')['resolutions']['items']['0']['selection'] == frozen


async def test_every_publication_rejects_stale_owner_epoch(setup):
    manager, engine, _ = setup
    source = await manager.create('source')
    engine.enabled = True
    entered, release = asyncio.Event(), asyncio.Event()
    async def blocked(*args, **kwargs):
        entered.set()
        await release.wait()
        return [.25, .75]
    engine._generate_embedding = blocked
    active = asyncio.create_task(call(feel=True, source_bucket=source, trigger_date='2026-10-01', valence=.7, arousal=.2))
    await asyncio.wait_for(entered.wait(), 5)
    request = manager.inspect_trace_request('request')
    old = dict(operation_id='request', owner=request['owner_instance'], epoch=request['epoch'])
    with sqlite3.connect(manager.history_db_path) as conn:
        conn.execute("UPDATE ob_s4_requests SET lease_until=0 WHERE operation_id='request'")
    newer = manager._claim_s4_request('request', request['payload'], request['normalization_context'], 'new')
    plan = request_item(manager)
    before = business_counts(manager)
    actions = [lambda: manager.commit_hold_grow_memory(old, 0),
               lambda: manager.commit_hold_grow_delta(old, 0),
               lambda: manager.commit_hold_grow_trigger(old, 0),
               lambda: server._hold_grow_related(manager, old, 0),
               lambda: manager.mark_feel_source(source, _s4_effect={'context':old,'key':plan['keys']['source'],'logical_time':plan['logical_time']}),
               lambda: server._record_emotion_snapshot(.7, .2, 'hold', plan['target'], _strict=True,
                         _effect_key=plan['keys']['emotion'], _s4_context=old),
               lambda: manager._trace_checkpoint(old)]
    for action in actions:
        with pytest.raises(BucketIdempotencyError, match='operation_claim_stale'):
            action()
    assert business_counts(manager) == before
    assert (await manager.get(source))['metadata'].get('digested') is not True
    release.set()
    assert await active == 'operation_claim_stale'
    manager._release_trace_request(newer)


@pytest.mark.parametrize('key', [None, 'request'])
@pytest.mark.parametrize('length', [29, 30])
async def test_grow_threshold_legacy_and_keyed(setup, key, length):
    await call('grow', key, content='x'*length)
    assert server.dehydrator.analyze.await_count == (length < 30)
    assert server.dehydrator.digest.await_count == (length >= 30)


async def test_noop_reuse_does_not_backfill_legacy_todo_ids(setup):
    manager, _, _ = setup
    target = await manager.create('held memory', domain=['work'], todos=['task'])
    path = Path(manager._find_bucket_file(target))
    post = frontmatter.load(path)
    for record in post['todo_provenance']:
        record.pop('id')
    path.write_text(frontmatter.dumps(post), encoding='utf-8')
    before = path.read_bytes()
    assert 'written_fields=[]' in await call(key=None)
    assert path.read_bytes() == before
    assert 'written_fields=[]' in await call()
    assert request_item(manager)['updates'] == {}
    assert path.read_bytes() == before

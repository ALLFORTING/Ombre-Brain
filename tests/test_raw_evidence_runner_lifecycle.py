"""Raw Evidence worker lifecycle on synthetic roots; no receipt/coordination changes."""
import asyncio
import importlib
import json
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
from unittest.mock import AsyncMock, Mock

import pytest

import import_memory
from bucket_manager import BucketManager
from import_memory import ImportEngine
from raw_evidence_import import RawEvidenceImportCoordinator
from raw_evidence_store import RawEvidenceStore

RAW = b'User: synthetic source\nAI: synthetic answer'
ITEM = dict(content='synthetic accepted memory', name='synthetic', domain=['test'],
            tags=[], importance=5, valence=.5, arousal=.3)


def make_engine(config):
    engine = ImportEngine(config, BucketManager(config), Mock(api_available=True))
    engine._extract_memories = AsyncMock(return_value=[dict(ITEM)])
    return engine


@pytest.fixture
def runtime(test_config, tmp_path, monkeypatch):
    config = dict(test_config, raw_evidence_root=str(tmp_path / 'raw'))
    monkeypatch.setenv('OMBRE_BUCKETS_DIR', config['buckets_dir'])
    monkeypatch.setattr(import_memory, 'detect_and_parse',
                        lambda *_: [dict(role='user', content='synthetic source')])
    return make_engine(config), config


def records(config):
    store = RawEvidenceStore(config['raw_evidence_root'])
    with sqlite3.connect(store.registry_path) as conn:
        run_id = conn.execute('SELECT run_id FROM import_runs').fetchone()[0]
    return dict(run=store.get_import_run(run_id), items=store.list_import_items(run_id),
                lineage=store.list_lineage(run_id=run_id))


def identities(rows):
    items = {(x['item_key'], x['operation_key'], x['result_id'])
             for x in rows['items'] if x['item_kind'] in ('memory', 'memory_raw')}
    lineage = {(x['lineage_id'], x['run_item_key'], x['operation_key'], x['memory_id'],
                x['memory_mutation_id']) for x in rows['lineage']}
    return items, lineage


async def start(engine, resume=False):
    return await engine.start_raw_evidence(RAW, filename='lifecycle.txt', resume=resume)


async def restored(engine, config, before, *, frozen=True):
    if frozen:
        engine._extract_memories = AsyncMock(side_effect=AssertionError('frozen extraction repeated'))
    else:
        engine._extract_memories = AsyncMock(return_value=[dict(ITEM)])
    result = await start(engine, resume=True)
    after = records(config)
    assert result['status'] == 'completed' and not engine.is_running
    assert after['run']['run_id'] == before['run']['run_id']
    assert after['run']['status'] == 'completed'
    old_items, old_lineage = identities(before)
    new_items, new_lineage = identities(after)
    assert old_items <= new_items and old_lineage <= new_lineage
    if frozen:
        assert (old_items, old_lineage) == (new_items, new_lineage)
    assert all(x['status'] == 'complete' for x in after['lineage'])
    for item in after['items']:
        if item['item_kind'] in ('memory', 'memory_raw'):
            assert item['status'] == 'succeeded'
            op = engine.bucket_mgr.inspect_import_operation(item['operation_key'])
            assert op['marker'] and op['result_id'] == item['result_id']
            assert (await engine.bucket_mgr.get(item['result_id']))['content'] == ITEM['content']
    print('RAW_LIFECYCLE_IDS', json.dumps(dict(run_id=after['run']['run_id'],
          items=sorted(new_items), lineage=sorted(new_lineage))))
    return after


@pytest.mark.asyncio
@pytest.mark.parametrize('boundary', ['extraction', 'before_effect', 'after_effect', 'checkpoint'])
async def test_task_cancel_same_instance_resume(runtime, monkeypatch, boundary):
    engine, config = runtime
    entered, release = asyncio.Event(), asyncio.Event()
    original = engine.bucket_mgr.apply_import_operation
    if boundary in ('before_effect', 'after_effect'):
        async def blocked(*args, **kwargs):
            result = None
            if boundary == 'after_effect':
                result = await original(*args, **kwargs)
            entered.set()
            await release.wait()
            return result if boundary == 'after_effect' else await original(*args, **kwargs)
        monkeypatch.setattr(engine.bucket_mgr, 'apply_import_operation', blocked)
    else:
        if boundary == 'checkpoint':
            monkeypatch.setattr(import_memory, 'chunk_turns', lambda *_: [
                dict(content='one'), dict(content='two')])
        calls = 0
        async def extract(*_):
            nonlocal calls
            calls += 1
            if boundary == 'extraction' or calls == 2:
                entered.set()
                await release.wait()
            return [dict(ITEM)]
        engine._extract_memories = AsyncMock(side_effect=extract)
    worker = asyncio.create_task(start(engine))
    await asyncio.wait_for(entered.wait(), 10)
    before = records(config)
    progress = engine.state.data['processed']
    assert progress == (1 if boundary == 'checkpoint' else 0)
    assert engine.is_running
    assert await start(engine, resume=True) == {'error': 'Import already running'}
    assert engine.is_running and not worker.done()
    worker.cancel('worker cancellation')
    with pytest.raises(asyncio.CancelledError, match='worker cancellation'):
        await worker
    paused = records(config)
    assert not engine.is_running and engine.get_status()['status'] == 'paused'
    assert paused['run']['status'] == 'paused'
    assert paused['run']['processed_chunks'] == progress
    assert identities(paused) == identities(before)
    assert [(x['item_key'], x['status']) for x in paused['items']] == [
        (x['item_key'], x['status']) for x in before['items']]
    assert [(x['lineage_id'], x['status']) for x in paused['lineage']] == [
        (x['lineage_id'], x['status']) for x in before['lineage']]
    monkeypatch.setattr(engine.bucket_mgr, 'apply_import_operation', original)
    await restored(engine, config, before, frozen=boundary in ('before_effect', 'after_effect'))


@pytest.mark.asyncio
async def test_ordinary_failure_keeps_failed_and_resumes(runtime, monkeypatch):
    engine, config = runtime
    original = engine.bucket_mgr.apply_import_operation
    monkeypatch.setattr(engine.bucket_mgr, 'apply_import_operation',
                        AsyncMock(side_effect=RuntimeError('synthetic worker failure')))
    assert (await start(engine))['status'] == 'error'
    before = records(config)
    assert not engine.is_running and before['run']['status'] == 'failed'
    assert any(x['status'] == 'failed' for x in before['items'])
    monkeypatch.setattr(engine.bucket_mgr, 'apply_import_operation', original)
    await restored(engine, config, before)


@pytest.mark.asyncio
@pytest.mark.parametrize('failed_save', ['progress', 'registry', 'both', 'inspection'])
async def test_cancel_cleanup_save_failure_is_logged_and_does_not_mask(runtime, monkeypatch, caplog, failed_save):
    engine, config = runtime
    entered = asyncio.Event()
    original = engine.bucket_mgr.apply_import_operation
    async def blocked(*args, **kwargs):
        entered.set()
        await asyncio.Event().wait()
    monkeypatch.setattr(engine.bucket_mgr, 'apply_import_operation', blocked)
    worker = asyncio.create_task(start(engine))
    await asyncio.wait_for(entered.wait(), 10)
    before = records(config)
    with monkeypatch.context() as patch:
        if failed_save in ('progress', 'both'):
            patch.setattr(engine.state, 'save', Mock(side_effect=OSError('synthetic disk failure')))
        if failed_save in ('registry', 'both'):
            patch.setattr(RawEvidenceImportCoordinator, 'update_run',
                          Mock(side_effect=OSError('synthetic registry failure')))
        if failed_save == 'inspection':
            patch.setattr(RawEvidenceStore, 'get_import_run',
                          Mock(side_effect=OSError('synthetic inspection failure')))
        worker.cancel('original cancellation')
        with pytest.raises(asyncio.CancelledError, match='original cancellation'):
            await worker
    assert not engine.is_running
    assert 'O5B cancellation' in caplog.text and 'failed' in caplog.text
    assert engine.get_status()['status'] == (
        'running' if failed_save in ('progress', 'both', 'inspection') else 'paused')
    assert engine.state.data['status'] == engine.get_status()['status']
    rows = records(config)
    assert rows['run']['status'] == (
        'processing' if failed_save in ('registry', 'both', 'inspection') else 'paused')
    assert identities(rows) == identities(before)
    monkeypatch.setattr(engine.bucket_mgr, 'apply_import_operation', original)
    await restored(engine, config, before)


@pytest.mark.asyncio
async def test_failure_progress_save_error_still_releases_owner(runtime, monkeypatch, caplog):
    engine, _ = runtime
    entered = asyncio.Event()
    async def fail(*args, **kwargs):
        entered.set()
        await asyncio.sleep(0)
        raise RuntimeError('ordinary failure')
    monkeypatch.setattr(engine.bucket_mgr, 'apply_import_operation', fail)
    worker = asyncio.create_task(start(engine))
    await asyncio.wait_for(entered.wait(), 10)
    monkeypatch.setattr(engine.state, 'save', Mock(side_effect=OSError('progress save failed')))
    with pytest.raises(OSError, match='progress save failed'):
        await worker
    assert not engine.is_running and 'O5B failure progress save failed' in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize('completed_record', ['both', 'progress', 'registry'])
async def test_cancel_does_not_downgrade_durable_completed(runtime, monkeypatch, completed_record):
    engine, config = runtime
    entered = asyncio.Event()
    original = engine._process_raw_evidence_chunks
    async def completed(*args):
        result = await original(*args)
        if completed_record == 'progress':
            RawEvidenceImportCoordinator(config).update_run(args[1], status='processing')
        elif completed_record == 'registry':
            engine.state.data['status'] = 'running'
            engine.state.save()
        entered.set()
        await asyncio.Event().wait()
        return result
    monkeypatch.setattr(engine, '_process_raw_evidence_chunks', completed)
    worker = asyncio.create_task(start(engine))
    await asyncio.wait_for(entered.wait(), 10)
    before = records(config)
    saved_before = Path(engine.state.state_file).read_bytes()
    # Work has completed, but its owner is still active until the wrapper exits.
    assert engine.is_running and await start(engine) == {'error': 'Import already running'}
    worker.cancel()
    with pytest.raises(asyncio.CancelledError): await worker
    assert not engine.is_running
    assert records(config) == before
    assert Path(engine.state.state_file).read_bytes() == saved_before


@pytest.mark.asyncio
async def test_handler_cancel_retains_worker_then_worker_cancel_allows_restart(runtime, monkeypatch):
    from starlette.applications import Starlette
    from starlette.routing import Route
    from starlette.requests import Request
    engine, config = runtime
    server = importlib.import_module('server')
    monkeypatch.setattr(server, 'config', config)
    monkeypatch.setattr(server, 'import_engine', engine)
    monkeypatch.setattr(server, '_require_dashboard_write', lambda *args: None)
    monkeypatch.setattr(server, '_IMPORT_BACKGROUND_TASKS', set())
    entered, response_started, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    original = engine.bucket_mgr.apply_import_operation
    async def blocked(*args, **kwargs):
        entered.set(); await release.wait()
        return await original(*args, **kwargs)
    monkeypatch.setattr(engine.bucket_mgr, 'apply_import_operation', blocked)
    app = Starlette(routes=[Route('/api/import/upload', server.api_import_upload, methods=['POST'])])
    query = b'raw_evidence_capture=1&filename=lifecycle.txt&resume=1'
    scope = dict(type='http', http_version='1.1', method='POST', scheme='http',
                 path='/api/import/upload', raw_path=b'/api/import/upload', query_string=query,
                 headers=[(b'content-type', b'text/plain')], server=('localhost', 80), client=('127.0.0.1', 1))
    async def receive(): return dict(type='http.request', body=RAW, more_body=False)
    async def send(message):
        if message['type'] == 'http.response.start':
            response_started.set(); await asyncio.Event().wait()
    handler = asyncio.create_task(app(scope, receive, send))
    try:
        await asyncio.wait_for(response_started.wait(), 10)
        await asyncio.wait_for(entered.wait(), 10)
        worker = next(t for t in server._IMPORT_BACKGROUND_TASKS if not t.done())
        before = records(config)
        handler.cancel('HTTP handler cancelled')
        with pytest.raises(asyncio.CancelledError): await handler
        assert not worker.done() and engine.is_running
        assert records(config) == before
        rejection = await server.api_import_upload(Request(scope, receive))
        assert rejection.status_code == 409 and engine.is_running
        worker.cancel('background worker cancelled')
        with pytest.raises(asyncio.CancelledError): await worker
        await asyncio.sleep(0)
        assert not server._IMPORT_BACKGROUND_TASKS and not engine.is_running
        assert engine.get_status()['status'] == 'paused'
        release.set()
        response = await server.api_import_upload(Request(scope, receive))
        assert response.status_code == 200
        await asyncio.gather(*list(server._IMPORT_BACKGROUND_TASKS))
        await asyncio.sleep(0)
        assert not engine.is_running and not server._IMPORT_BACKGROUND_TASKS
        after = records(config)
        assert after['run']['status'] == 'completed'
        assert identities(after) == identities(before)
    finally:
        release.set()
        if not handler.done(): handler.cancel()
        for task in list(server._IMPORT_BACKGROUND_TASKS): task.cancel()
        await asyncio.gather(handler, *list(server._IMPORT_BACKGROUND_TASKS), return_exceptions=True)


def kill_worker(config, boundary):
    import_memory.detect_and_parse = lambda *_: [dict(role='user', content='synthetic source')]
    engine = make_engine(config)
    original = engine.bucket_mgr.apply_import_operation
    async def crash(*args, **kwargs):
        if boundary == 'after_effect': await original(*args, **kwargs)
        os.kill(os.getpid(), signal.SIGKILL)
    engine.bucket_mgr.apply_import_operation = crash
    asyncio.run(start(engine))


@pytest.mark.asyncio
@pytest.mark.parametrize('boundary', ['before_effect', 'after_effect'])
async def test_sigkill_same_root_existing_resume(runtime, boundary):
    engine, config = runtime
    child = subprocess.Popen([sys.executable, '-B', '-c',
        'from tests.test_raw_evidence_runner_lifecycle import kill_worker; import json,sys; kill_worker(json.loads(sys.argv[1]),sys.argv[2])',
        json.dumps(config), boundary], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        out, err = await asyncio.to_thread(child.communicate, timeout=60)
    finally:
        if child.poll() is None: child.kill(); child.communicate(timeout=5)
    assert child.returncode == -signal.SIGKILL, (out, err)
    before = records(config)
    assert before['run']['status'] == 'processing'
    fresh = make_engine(config)
    assert not fresh.is_running
    await restored(fresh, config, before)

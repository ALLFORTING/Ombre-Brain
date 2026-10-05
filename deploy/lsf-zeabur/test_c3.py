"""One bounded WSL batch with real SDK/socket/MCP/storage; preserve failed roots."""
import asyncio
import contextlib
import hashlib
import json
import socket
import subprocess
import sys
import os
import shutil
from pathlib import Path
import pytest
HERE = Path(__file__).resolve().parent
TOKEN = 'synthetic-local-c3-token-with-no-external-permission'

def run_child(root, action, ok=True):
    proc = subprocess.run([sys.executable,'-B',str(__file__),'--worker',action,str(root)],capture_output=True,text=True,timeout=150)
    result = json.loads(proc.stdout)
    expected = Path(os.environ['PYTHONPATH']).resolve()
    assert Path(result['child_rm_source']).resolve().is_relative_to(expected)
    assert result['child_rm_version'] == '0.1.0'
    assert (proc.returncode==0)==ok, result
    return result

def c2_snapshot(root):
    import sqlite3
    marker = (root/'.c2-v1.json').read_bytes()
    state = json.loads(marker)
    files = {r['path']:hashlib.sha256((root/'buckets'/r['path']).read_bytes()).hexdigest() for r in state['buckets']}
    rows = {}
    with sqlite3.connect(root/'buckets/bucket_history.sqlite3') as conn:
        for table, records in state['sql_rows'].items():
            rows[table] = [conn.execute(f'SELECT * FROM {table} WHERE id=?',(r[0],)).fetchone() for r in records]
    with sqlite3.connect(root/'buckets/embeddings.db') as conn:
        rows['vectors'] = [conn.execute('SELECT bucket_id,embedding,model,updated_at FROM embeddings WHERE bucket_id=?',(r[0],)).fetchone() for r in state['vectors']]
    return dict(marker_sha256=hashlib.sha256(marker).hexdigest(),files=files,rows=rows)

async def worker(action, root):
    if action == 'prepare':
        from c3_run import configure_c3
        configure_c3(TOKEN,root,18993)
        from run import runtime_sources, verify_c2_sources
        runtime = runtime_sources()
        verify_c2_sources()
        from initialize import lock_volume, initialize
        lock = lock_volume(root)
        handle = None
        try:
            initialize(root)
            import server as ob
            from launcher import start
            import provider_stub as stub
            handle = await start(stub.app,18995)
            from c2_seed import initialize_c2
            result = await initialize_c2(ob,root,lock)
            return dict(prepared=True,c2=result,runtime=runtime)
        finally:
            if handle:
                from launcher import stop
                await stop(handle)
            lock.close()
    from c3_run import serve
    ready = asyncio.get_running_loop().create_future()
    stop = asyncio.Event()
    if action == 'locked':
        from initialize import lock_volume
        lock = lock_volume(root)
        try:
            await serve(TOKEN,root,18993,ready,stop)
        finally:
            lock.close()
        return {}
    if action == 'refuse':
        await serve(TOKEN,root,18993,ready,stop)
        return {}
    import httpx
    from c3_inputs import SCENARIOS
    import provider_stub as stub
    before = c2_snapshot(root)
    task = asyncio.create_task(serve(TOKEN,root,18993,ready,stop))
    try:
        done,_ = await asyncio.wait([task,ready],timeout=30,return_when=asyncio.FIRST_COMPLETED)
        if ready not in done:
            if task in done:
                await task
            raise RuntimeError('c3_start_timeout')
        info = ready.result()
        controller = info['controller']
        results = {}
        async with httpx.AsyncClient(timeout=45,headers={'Accept':'application/json, text/event-stream'}) as client:
            url = 'http://127.0.0.1:18993/mcp?token='+TOKEN
            response = await client.post(url,json=dict(jsonrpc='2.0',id=1,method='initialize',params=dict(protocolVersion='2025-03-26',capabilities={},clientInfo=dict(name='C3-local',version='1'))))
            assert response.status_code==200, response.status_code
            session = response.headers['mcp-session-id']
            client.headers.update({'Mcp-Session-Id':session,'Mcp-Protocol-Version':'2025-03-26'})
            assert (await client.post(url,json=dict(jsonrpc='2.0',method='notifications/initialized'))).status_code==202
            number = 1
            async def call(name, arguments):
                nonlocal number
                number += 1
                response = await client.post(url,json=dict(jsonrpc='2.0',id=number,method='tools/call',params=dict(name=name,arguments=arguments)))
                assert response.status_code==200
                lines = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith('data: ')]
                result = next(row['result'] for row in lines if row.get('id')==number)
                assert not result.get('isError'), result
                return '\n'.join(c.get('text','') for c in result['content'])
            async def control(action, data=None):
                u = 'http://127.0.0.1:18994/'+action
                response = await client.get(u) if data is None else await client.post(u,json=data)
                assert response.status_code==200, response.text
                return response.json()
            assert (await control('status'))['armed'] is None
            import tempfile
            assert tempfile.gettempdir() == '/tmp'
            assert os.environ['PYTHONPATH'] == str(Path(info['sources']['remember_me_source']).parents[1])
            rejected = await client.post('http://127.0.0.1:18994/arm',json={'scenario':'rate'},headers={'Origin':'https://example.invalid'})
            assert rejected.status_code==403
            assert (await control('status'))['armed'] is None
            normal = await call('hold',dict(content='obweb-ls C3 normal passthrough',operation_id='obweb-ls-c3-normal'))
            assert 'bucket_id=' in normal
            results['passthrough'] = True
            if action=='recover':
                initial_counts = dict(controller.counts),dict(stub.COUNTS)
                for name,fixed in SCENARIOS.items():
                    row = controller.ob.bucket_mgr.inspect_trace_request(fixed['operation_id'])
                    assert row and row['status']=='completed'
                    replay = await call(fixed['tool'],dict(content=fixed['content'],operation_id=fixed['operation_id']))
                    assert replay==row['result_text']
                    if name in ('rate','parse','connection'):
                        assert 'reason='+dict(rate='rate_limited',parse='parse_error',connection='connection_error')[name] in replay
                    else:
                        matches = [b for b in await controller.ob.bucket_mgr.list_all() if b['content']==fixed['content']]
                        assert len(matches)==1
                        if name=='fallback':
                            assert '原因=parse_error；已使用默认 metadata' in replay
                    results[name] = dict(status=row['status'],replay_identical=True,result=replay)
                assert initial_counts==(dict(controller.counts),dict(stub.COUNTS))
                assert c2_snapshot(root)==before
                logs = info['logs'].projection()
                assert not logs['raw_value_leaked']
                return dict(recovery_only=True,root=str(root),results=results,provider_counts=dict(controller.counts),stub_counts=dict(stub.COUNTS),c2_preserved=before,logs=logs,runtime=info['sources'],original_runtime_assertions='completed before deque JSON emission failure; see attempt3')
            for name, fixed in SCENARIOS.items():
                assert (await control('arm',dict(scenario=name)))['armed']==name
                args = dict(content=fixed['content'],operation_id=fixed['operation_id'])
                buckets_before = {str(p) for p in (root/'buckets').rglob('*.md')}
                if name == 'disconnect':
                    reader, writer = await asyncio.open_connection('127.0.0.1',18993)
                    payload = json.dumps(dict(jsonrpc='2.0',id=777,method='tools/call',params=dict(name='hold',arguments=args))).encode()
                    header = f'POST /mcp?token={TOKEN} HTTP/1.1\r\nHost: 127.0.0.1:18993\r\nAccept: application/json, text/event-stream\r\nContent-Type: application/json\r\nMcp-Session-Id: {session}\r\nMcp-Protocol-Version: 2025-03-26\r\nContent-Length: {len(payload)}\r\n\r\n'.encode()
                    writer.write(header+payload)
                    await writer.drain()
                    async with asyncio.timeout(45):
                        while not any(e['event']=='waiting' and e['scenario']==name for e in controller.events):
                            await asyncio.sleep(.02)
                        writer.close()
                        await writer.wait_closed()
                        while not any(e['event']=='http.disconnect' and e['scenario']==name for e in controller.events):
                            await asyncio.sleep(.02)
                        await control('release',{})
                        while not (row:=controller.ob.bucket_mgr.inspect_trace_request(fixed['operation_id'])) or row['status']!='completed':
                            await asyncio.sleep(.02)
                    first = row['result_text']
                else:
                    first = await call(fixed['tool'],args)
                await control('status')
                row = controller.ob.bucket_mgr.inspect_trace_request(fixed['operation_id'])
                assert row['status']=='completed' and row['result_text']==first
                counts_before = dict(controller.counts),dict(stub.COUNTS)
                replay = await call(fixed['tool'],args)
                assert replay==first and counts_before==(dict(controller.counts),dict(stub.COUNTS))
                if name in ('rate','parse','connection'):
                    expected = dict(rate='rate_limited',parse='parse_error',connection='connection_error')[name]
                    assert 'reason='+expected in first, first
                    assert {str(p) for p in (root/'buckets').rglob('*.md')}==buckets_before
                    assert controller.counts[name]==(1 if name=='parse' else 3), dict(controller.counts)
                else:
                    matches = [b for b in await controller.ob.bucket_mgr.list_all() if b['content']==fixed['content']]
                    assert len(matches)==1
                    if name=='fallback':
                        assert '原因=parse_error；已使用默认 metadata' in first
                        assert matches[0]['metadata']['domain']==['未分类']
                results[name] = dict(completed=True,replay_identical=True,provider_attempts=controller.counts[name],result=first)
                await control('disarm',{})
            assert c2_snapshot(root)==before
            logs = info['logs'].projection()
            assert not logs['raw_value_leaked'], logs
            projected = json.dumps(controller.projection())
            assert TOKEN not in projected and 'synthetic-no-external-permission' not in projected
            assert all(fixed['content'] not in projected for fixed in SCENARIOS.values())
            events = controller.projection()['events']
            assert all(e['scenario']=='disconnect' for e in events if e['event']=='http.disconnect')
            assert {'waiting','http.disconnect','released','completed'} <= {e['event'] for e in events if e['scenario']=='disconnect'}
            assert not any(e['event']=='gate_timeout_unaccepted' for e in events)
            return dict(results=results,events=list(controller.events),c2_preserved=before,logs=logs,runtime=info['sources'])
    finally:
        stop.set()
        await task
        for port in (18993,18994,18995):
            with socket.socket() as sock:
                assert sock.connect_ex(('127.0.0.1',port))!=0

def test_c3_bounded_batch(tmp_path):
    root = tmp_path/'lsf'
    resume = False
    mode = 'exercise'
    continuation = HERE/'evidence/c3-runtime-continuation.json'
    if continuation.exists():
        instructions = json.loads(continuation.read_text(encoding='utf-8-sig'))
        prior = Path(instructions['root'])
        if prior.is_dir():
            root = prior
            resume = True
            mode = instructions.get('mode','exercise')
    if resume and (HERE/'evidence/c3-refusals-completed.json').exists():
        if not (root/'.c2-v1.json').exists():
            run_child(root,'prepare')
        result = run_child(root,mode)
        (HERE/'evidence/c3-formal-safe-evidence.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
        return
    run_child(root,'prepare')
    snapshot = c2_snapshot(root)
    original = (root/'.c2-v1.json').read_bytes()
    for field, value, expected in [('baseline','wrong','c2_identity_conflict'),('status','started','c3_existing_c2_complete_required')]:
        probe = tmp_path/field
        shutil.copytree(root,probe)
        marker = probe/'.c2-v1.json'
        state = json.loads(original);state[field]=value
        marker.write_text(json.dumps(state))
        before = c2_snapshot(probe)
        refused = run_child(probe,'refuse',False)
        assert refused['error']==expected, refused
        assert c2_snapshot(probe)==before
    refused = run_child(root,'locked',False)
    assert refused['error']=='test_volume_already_in_use'
    assert c2_snapshot(root)==snapshot
    result = run_child(root,'exercise')
    evidence = HERE/'evidence/c3-formal-safe-evidence.json'
    evidence.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    print('C3_SAFE_EVIDENCE='+json.dumps(result,ensure_ascii=False))

if __name__=='__main__' and '--worker' in sys.argv:
    import remember_me
    import importlib.metadata as metadata
    child_identity = dict(child_rm_source=remember_me.__file__,child_rm_version=metadata.version('remember-me'))
    try:
        with contextlib.redirect_stdout(sys.stderr):
            result = asyncio.run(worker(sys.argv[2],Path(sys.argv[3])))
        result.update(child_identity)
        print(json.dumps(result,ensure_ascii=False))
    except Exception as exc:
        import traceback
        frames=traceback.extract_tb(exc.__traceback__)
        print(json.dumps(dict(error=str(exc) if isinstance(exc,RuntimeError) else type(exc).__name__,location=f'{Path(frames[-1].filename).name}:{frames[-1].lineno}',**child_identity)))
        sys.exit(1)


@pytest.fixture
def startup_entry(monkeypatch):
    monkeypatch.syspath_prepend(str(HERE))
    import c3_run
    monkeypatch.setattr(c3_run, '_STARTUP_STAGE', 'entry')
    return c3_run


def test_startup_source_pins_valid(startup_entry):
    startup_entry.verify_c2_sources()
    startup_entry.verify_c3_sources()


@pytest.mark.parametrize('kind', ['c2', 'c3'])
def test_startup_tampering_rejected(startup_entry, monkeypatch, tmp_path, kind):
    if kind == 'c2':
        import environment
        pins = json.loads((HERE/'c2-source-hashes.json').read_text())
        for name in pins['sha256']:
            target = tmp_path/name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(environment.SOURCE/name, target)
        with (tmp_path/'server.py').open('ab') as stream:
            stream.write(b'\n# deliberately altered test copy\n')
        monkeypatch.setattr(environment, 'SOURCE', tmp_path)
        with pytest.raises(RuntimeError, match='^c2_source_hash_mismatch$'):
            startup_entry.verify_c2_sources()
    else:
        pins = json.loads((HERE/'c3-artifact-hashes.json').read_text())
        (tmp_path/'c3-artifact-hashes.json').write_text(json.dumps(pins))
        for name in pins['sha256']:
            shutil.copyfile(HERE/name, tmp_path/name)
        with (tmp_path/'c3_run.py').open('ab') as stream:
            stream.write(b'\n# deliberately altered test copy\n')
        monkeypatch.setattr(startup_entry, 'HERE', tmp_path)
        with pytest.raises(RuntimeError, match='^c3_artifact_hash_mismatch$'):
            startup_entry.verify_c3_sources()


@pytest.mark.parametrize('exception_type', [RuntimeError, ValueError, OSError])
def test_startup_diagnostic_redacts_unknown_errors(startup_entry, monkeypatch, capsys, exception_type):
    sentinel = 'PRIVATE_SENTINEL_never_emit_0123456789'
    monkeypatch.setenv('PRIVATE_DIAGNOSTIC_SENTINEL', sentinel)
    monkeypatch.setattr(startup_entry, '_STARTUP_STAGE', sentinel)
    code = compile('raise exception_type(sentinel)', '/private/'+sentinel+'.py', 'exec')
    try:
        exec(code, {'exception_type': exception_type, 'sentinel': sentinel})
    except Exception as exc:
        startup_entry.emit_startup_diagnostic(exc)
    captured = capsys.readouterr()
    output = json.loads(captured.err)
    assert captured.out == ''
    assert set(output) == {'stage', 'error_code', 'location'}
    assert output == {'stage': 'entry', 'error_code': 'startup_runtime_error', 'location': 'external:1'}
    assert sentinel not in captured.err and 'PRIVATE_DIAGNOSTIC_SENTINEL' not in captured.err
    assert '/private/' not in captured.err


@pytest.mark.asyncio
async def test_startup_c2_failure_stage_is_preserved(startup_entry, monkeypatch, tmp_path, capsys):
    import observe
    sentinel = 'PRIVATE_STARTUP_FAILURE_DO_NOT_EMIT'
    monkeypatch.setattr(startup_entry, 'configure_c3', lambda *args: None)
    monkeypatch.setattr(observe, 'install_logging', lambda *args: None)
    monkeypatch.setattr(startup_entry, 'runtime_sources', lambda: {})
    def fail():
        raise RuntimeError('c2_source_hash_mismatch')
    monkeypatch.setattr(startup_entry, 'verify_c2_sources', fail)
    with pytest.raises(RuntimeError) as failure:
        await startup_entry.serve(sentinel, tmp_path, 8080)
    startup_entry.emit_startup_diagnostic(failure.value)
    output = capsys.readouterr().err
    row = json.loads(output)
    assert row['stage'] == 'c2_sources'
    assert row['error_code'] == 'c2_source_hash_mismatch'
    assert sentinel not in output


@pytest.mark.parametrize('case, stage, code', [
    ('opt_in', 'opt_in', 'test_opt_in_required'),
    ('token', 'token', 'fresh_query_token_required'),
    ('port', 'port', 'startup_runtime_error'),
])
def test_startup_cli_safe_stderr(case, stage, code):
    sentinel = 'PRIVATE_ENV_SENTINEL_DO_NOT_EMIT_0123456789'
    env = {'PATH': '/usr/local/bin:/usr/bin:/bin', 'LANG': 'C.UTF-8',
           'PYTHONDONTWRITEBYTECODE': '1', 'PRIVATE_ENV_SENTINEL': sentinel}
    if case != 'opt_in':
        env.update(OB_LSF_TEST_SERVICE='true', OMBRE_MCP_ALLOW_QUERY_TOKEN='true',
                   OMBRE_MCP_QUERY_TOKEN='invalid!'+sentinel)
    if case == 'port':
        env['OMBRE_MCP_QUERY_TOKEN'] = sentinel + '_valid_token'
        env['PORT'] = sentinel
    proc = subprocess.run([sys.executable, '-B', str(HERE/'c3_run.py')],
                          env=env, text=True, capture_output=True, timeout=15)
    assert proc.returncode == 1 and proc.stdout == ''
    row = json.loads(proc.stderr)
    assert set(row) == {'stage', 'error_code', 'location'}
    assert row['stage'] == stage and row['error_code'] == code
    assert row['location'].startswith('c3_run.py:')
    assert sentinel not in proc.stderr and 'OMBRE_MCP_QUERY_TOKEN' not in proc.stderr

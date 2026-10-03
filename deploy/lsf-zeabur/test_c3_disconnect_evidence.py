"""One new local disconnect evidence batch; no old-root recovery or fault retry."""
import asyncio
import contextlib
import hashlib
import json
import os
import socket
import subprocess
import sys
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
PARENT = '508f01fc7f996c9e5477f141ae26eaab53fba1de'
EVIDENCE = HERE/'evidence/c3-disconnect-new-evidence.json'
TOKEN = 'synthetic-local-c3-token-with-no-external-permission'

async def exercise(root):
    from c3_run import serve
    from c3_inputs import SCENARIOS
    from test_c3 import c2_snapshot
    import httpx
    import provider_stub as stub
    evidence = dict(format=1,parent=PARENT,new_experiment=True,root=str(root),
                    started_utc=datetime.now(timezone.utc).isoformat(),stage='starting',accepted=False)
    # Exclusive output refuses accidental reruns and preserves the original new evidence.
    with EVIDENCE.open('x',encoding='utf-8') as stream:
        json.dump(evidence,stream)
    def save():
        data = json.dumps(evidence,ensure_ascii=False,indent=2)+'\n'
        for value in (TOKEN,'synthetic-no-external-permission'):
            assert value not in data
        EVIDENCE.write_text(data)
        assert json.loads(EVIDENCE.read_text())==json.loads(data)
    before = c2_snapshot(root)
    marker = (root/'.c2-v1.json').read_bytes()
    evidence['c2_before'] = before
    ready = asyncio.get_running_loop().create_future()
    stop = asyncio.Event()
    task = asyncio.create_task(serve(TOKEN,root,18993,ready,stop))
    watcher = None
    writer = None
    controller = None
    succeeded = False
    try:
        done,_ = await asyncio.wait([task,ready],timeout=30,return_when=asyncio.FIRST_COMPLETED)
        if ready not in done:
            if task in done:
                await task
            raise RuntimeError('new_disconnect_start_timeout')
        info = ready.result()
        controller = info['controller']
        evidence['runtime'] = info['sources']
        assert Path(info['sources']['remember_me_source']).resolve().is_relative_to(Path(os.environ['PYTHONPATH']).resolve())
        fixed = SCENARIOS['disconnect']
        assert controller.ob.bucket_mgr.inspect_trace_request(fixed['operation_id']) is None
        old_buckets = await controller.ob.bucket_mgr.list_all()
        assert not any(b['content']==fixed['content'] for b in old_buckets)
        before_ids = {b['id'] for b in old_buckets}
        evidence.update(key_unused_confirmed=True,input_sha256=fixed['sha256'],operation_id=fixed['operation_id'],stage='key_checked')
        save()
        async with httpx.AsyncClient(timeout=45,headers={'Accept':'application/json, text/event-stream'}) as client:
            url = 'http://127.0.0.1:18993/mcp?token='+TOKEN
            response = await client.post(url,json=dict(jsonrpc='2.0',id=1,method='initialize',params=dict(protocolVersion='2025-03-26',capabilities={},clientInfo=dict(name='C3-new-disconnect',version='1'))))
            assert response.status_code==200
            session = response.headers['mcp-session-id']
            client.headers.update({'Mcp-Session-Id':session,'Mcp-Protocol-Version':'2025-03-26'})
            assert (await client.post(url,json=dict(jsonrpc='2.0',method='notifications/initialized'))).status_code==202
            async def status():
                response = await client.get('http://127.0.0.1:18994/status')
                assert response.status_code==200
                return response.json()
            state = await status()
            assert state['armed'] is None and not state['counts'] and not stub.COUNTS
            response = await client.post('http://127.0.0.1:18994/arm',json={'scenario':'disconnect'})
            assert response.status_code==200
            generation = response.json()['generation']
            evidence.update(generation=generation,stage='armed',provider_before=dict(controller.counts),stub_before=dict(stub.COUNTS))
            save()
            # This is the real shipped CLI process. It alone performs gate release.
            watcher = await asyncio.create_subprocess_exec(sys.executable,'-B',str(HERE/'c3_control.py'),'watch-disconnect',stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
            evidence['cli'] = dict(command=['python','-B','c3_control.py','watch-disconnect'],pid=watcher.pid,pythonpath=os.environ['PYTHONPATH'])
            save()
            args = dict(content=fixed['content'],operation_id=fixed['operation_id'])
            call = dict(jsonrpc='2.0',id=777,method='tools/call',params=dict(name='hold',arguments=args))
            payload = json.dumps(call,ensure_ascii=False).encode()
            reader,writer = await asyncio.open_connection('127.0.0.1',18993)
            header = f'POST /mcp?token={TOKEN} HTTP/1.1\r\nHost: 127.0.0.1:18993\r\nAccept: application/json, text/event-stream\r\nContent-Type: application/json\r\nMcp-Session-Id: {session}\r\nMcp-Protocol-Version: 2025-03-26\r\nContent-Length: {len(payload)}\r\n\r\n'.encode()
            async with asyncio.timeout(45):
                writer.write(header+payload)
                await writer.drain()
                while True:
                    state = await status()
                    if any(e['event']=='waiting' and e['scenario']=='disconnect' and e['generation']==generation for e in state['events']):
                        break
                    assert watcher.returncode is None
                    await asyncio.sleep(.02)
                evidence.update(stage='waiting',waiting_projection=state)
                save()
                evidence['client_close_utc'] = datetime.now(timezone.utc).isoformat()
                evidence['client_close_monotonic'] = asyncio.get_running_loop().time()
                # Actual TCP client closure. No model-stop, page refresh or HTTP500.
                writer.close()
                await writer.wait_closed()
                writer = None
                output,errors = await watcher.communicate()
                evidence['cli'].update(exit_code=watcher.returncode,stdout=output.decode(),stderr=errors.decode())
                save()
                assert watcher.returncode==0
                cli_result = json.loads(output)
                assert cli_result['accepted'] and cli_result['generation']==generation
                assert {'waiting','http.disconnect','released','completed'} <= set(cli_result['events'])
                state = await status()
            assert not state['gate_expired']
            events = state['events']
            assert isinstance(controller.events,deque) and isinstance(events,list)
            assert json.loads(json.dumps(list(controller.events)))==list(controller.events)
            own = [dict(e,sequence=i) for i,e in enumerate(events) if e['scenario']=='disconnect' and e['generation']==generation]
            names = [e['event'] for e in own]
            required = ['waiting','http.disconnect','released','completed']
            positions = [names.index(name) for name in required]
            assert positions==sorted(positions) and len(set(positions))==4
            assert all(names.count(name)==1 for name in required)
            assert all(own[i]['monotonic']<=own[i+1]['monotonic'] for i in range(len(own)-1))
            assert not any('timeout' in name for name in names)
            assert controller.counts=={'disconnect':1}
            assert not set(controller.counts)-{'disconnect'}
            row = controller.ob.bucket_mgr.inspect_trace_request(fixed['operation_id'])
            assert row['status']=='completed'
            first = row['result_text']
            buckets = await controller.ob.bucket_mgr.list_all()
            matches = [b for b in buckets if b['content']==fixed['content']]
            assert len(matches)==1
            target = matches[0]['id']
            assert {b['id'] for b in buckets}-before_ids=={target}
            assert len(row['plan']['items'])==1
            assert await controller.ob.embedding_engine.get_embedding(target)
            counts = dict(controller.counts),dict(stub.COUNTS)
            snapshot_after_first = c2_snapshot(root)
            evidence.update(stage='completed_before_replay',events=own,event_time_basis='actual monotonic seconds from live C3 observer',deque_serialization_verified=True,cli_result=cli_result,receipt=first,receipt_sha256=hashlib.sha256(first.encode()).hexdigest(),bucket_id=target,one_new_bucket=True,one_planned_item=True,provider_after_first=counts[0],stub_after_first=counts[1],c2_after_first=snapshot_after_first)
            save()
            # Fresh real MCP HTTP client reads/replays exactly the original key and body.
            async with httpx.AsyncClient(timeout=45,headers=dict(client.headers)) as recovered:
                response = await recovered.post(url,json=dict(call,id=778))
                assert response.status_code==200
                rows = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith('data: ')]
                result = next(r['result'] for r in rows if r.get('id')==778)
                assert not result.get('isError')
                replay = '\n'.join(c.get('text','') for c in result['content'])
            assert replay==first
            assert counts==(dict(controller.counts),dict(stub.COUNTS))
            after_buckets = await controller.ob.bucket_mgr.list_all()
            assert {b['id'] for b in after_buckets}=={b['id'] for b in buckets}
            readback = await controller.ob.bucket_mgr.get(target)
            assert readback['content']==fixed['content']
            assert (root/'.c2-v1.json').read_bytes()==marker
            after = c2_snapshot(root)
            assert before==snapshot_after_first==after
            # serve ran the unchanged full C2 validator before opening any listener.
            evidence.update(stage='replayed',exact_replay=True,body_readback=True,provider_after_replay=dict(controller.counts),stub_after_replay=dict(stub.COUNTS),c2_after=after,c2_marker_bytes_unchanged=True,c2_snapshot_unchanged=True)
            logs = info['logs'].projection()
            assert not logs['raw_value_leaked']
            evidence['logs'] = logs
            response = await client.post('http://127.0.0.1:18994/disarm',json={})
            assert response.status_code==200
            save()
        succeeded = True
    except BaseException as exc:
        evidence.update(failure_type=type(exc).__name__,failed_stage=evidence['stage'])
        if controller is not None:
            evidence['failure_projection'] = controller.projection()
        save()
        raise
    finally:
        if writer is not None:
            writer.close()
            await writer.wait_closed()
        if watcher is not None and watcher.returncode is None:
            watcher.terminate()
            await watcher.communicate()
        stop.set()
        try:
            await task
        finally:
            ports = {}
            for port in (18993,18994,18995):
                with socket.socket() as sock:
                    ports[str(port)] = sock.connect_ex(('127.0.0.1',port))
            evidence['listener_exit_codes'] = ports
            evidence['listeners_closed'] = all(code==111 for code in ports.values())
            evidence['finished_utc'] = datetime.now(timezone.utc).isoformat()
            evidence['accepted'] = succeeded and evidence['listeners_closed']
            evidence['stage'] = 'finished' if evidence['accepted'] else 'unaccepted'
            save()
            assert evidence['listeners_closed']
    return dict(accepted=evidence['accepted'],root=str(root),evidence=EVIDENCE.name)

def test_new_disconnect_cli_evidence():
    assert not EVIDENCE.exists(), 'new experiment already attempted; do not rerun'
    import tempfile
    assert tempfile.gettempdir() == '/tmp'
    # Avoid pytest numbered-directory rotation, preserving all earlier failed roots.
    root = Path(tempfile.mkdtemp(prefix='c3-disconnect-evidence-'))/'fresh-c3-disconnect'
    assert not root.exists()
    from test_c3 import run_child
    prepared = run_child(root,'prepare')
    assert prepared['prepared']
    proc = subprocess.run([sys.executable,'-B',str(__file__),'--worker',str(root)],capture_output=True,text=True,timeout=110)
    result = json.loads(proc.stdout)
    assert proc.returncode==0, result
    assert result['accepted']
    evidence=json.loads(EVIDENCE.read_text())
    assert evidence['accepted'] and evidence['key_unused_confirmed']
    assert evidence['cli']['exit_code']==0 and evidence['deque_serialization_verified']

if __name__=='__main__' and '--worker' in sys.argv:
    try:
        with contextlib.redirect_stdout(sys.stderr):
            result=asyncio.run(exercise(Path(sys.argv[2])))
        print(json.dumps(result))
    except BaseException as exc:
        import traceback
        frames=traceback.extract_tb(exc.__traceback__)
        print(json.dumps(dict(error=type(exc).__name__,location=f'{Path(frames[-1].filename).name}:{frames[-1].lineno}',evidence=EVIDENCE.name)))
        sys.exit(1)

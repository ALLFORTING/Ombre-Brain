"""C3 entry: reuse pinned C2 primitives, accept complete volumes only."""
import asyncio
import hashlib
import json
import os
import re
import signal
import sys
import tempfile
from collections import deque
from pathlib import Path
from environment import configure
from run import runtime_sources, verify_c2_sources, TestSurface

HERE = Path(__file__).resolve().parent
BASELINE = '6be6e84d11f3942061aa6061ea9f431155b5d29b'

def verify_c3_sources():
    pins = json.loads((HERE/'c3-artifact-hashes.json').read_text())
    required = {'c3_run.py','c3_provider.py','c3_inputs.py','test_c3.py','Dockerfile','Dockerfile.dockerignore'}
    if pins.get('baseline') != BASELINE or not required <= set(pins.get('sha256',{})):
        raise RuntimeError('c3_artifact_identity_mismatch')
    for name, expected in pins['sha256'].items():
        path = HERE/name
        if not path.resolve().is_relative_to(HERE) or hashlib.sha256(path.read_bytes().replace(b'\r\n',b'\n')).hexdigest() != expected:
            raise RuntimeError('c3_artifact_hash_mismatch')

def configure_c3(token, root, port):
    # Command-level RM path must survive environment isolation for child processes.
    inherited = os.environ.get('PYTHONPATH')
    configure(token, root, port)
    if inherited:
        os.environ['PYTHONPATH'] = inherited
    os.environ.pop('TMPDIR', None)
    tempfile.tempdir = None
    if tempfile.gettempdir() != '/tmp':
        raise RuntimeError('c3_linux_default_tmp_required')

async def drain_business(ob):
    loop = asyncio.get_running_loop()
    def pending():
        return {t for _,t in ob._S4_HOLD_GROW_RUNNERS.values() if t.get_loop() is loop and not t.done()} | {
            t for t in ob._LEGACY_POST_EFFECT_TASKS if t.get_loop() is loop and not t.done()}
    # Keep provider and service lock available until runners and post effects stop.
    try:
        async with asyncio.timeout(45):
            while tasks := pending():
                await asyncio.gather(*tasks,return_exceptions=True)
    except TimeoutError:
        tasks = pending()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks,return_exceptions=True)
        raise RuntimeError('c3_background_drain_timeout') from None

async def serve(token, root, port, ready=None, stop_event=None):
    configure_c3(token, root, port)
    from observe import install_logging
    logs = install_logging([token,'synthetic-no-external-permission'])
    from initialize import lock_volume, initialize
    lock = lock_volume(root)
    handles = []
    provider = None
    ob = None
    try:
        sources = runtime_sources()
        verify_c2_sources()
        verify_c3_sources()
        marker = root/'.c2-v1.json'
        if not marker.is_file() or json.loads(marker.read_text()).get('status') != 'complete':
            raise RuntimeError('c3_existing_c2_complete_required')
        # Existing original initialization identity must also validate unchanged.
        if not (root/'.synthetic-initialized.json').is_file():
            raise RuntimeError('c3_existing_seed_required')
        assert initialize(root) is False
        import server as ob
        from environment import SOURCE
        assert Path(ob.__file__).resolve() == SOURCE/'server.py'
        assert ob.config['buckets_dir'] == str(root/'buckets')
        assert ob._selected_asset_backend().name == 'legacy'
        async def disabled_scheduler():
            return None
        ob.decay_engine.ensure_started = disabled_scheduler
        ob.decay_engine.start = disabled_scheduler
        from c2_seed import initialize_c2
        c2 = await initialize_c2(ob,root,lock)
        assert c2['initialized'] is False
        from c3_provider import Controller, TransportEvents
        from launcher import start, build
        controller = Controller(ob)
        provider = await asyncio.start_server(controller.handle,'127.0.0.1',18995)
        handles.append(await start(controller.app(),18994))
        surface = TestSurface(TransportEvents(build(ob,deque(maxlen=256)),controller))
        handles.append(await start(surface,port,'0.0.0.0'))
        surface.ready = True
        event = stop_event or asyncio.Event()
        loop = asyncio.get_running_loop()
        installed = []
        if stop_event is None:
            for sig in (signal.SIGTERM,signal.SIGINT):
                loop.add_signal_handler(sig,event.set)
                installed.append(sig)
        waiter = asyncio.create_task(event.wait())
        try:
            if ready is not None:
                ready.set_result(dict(sources=sources,c2=c2,logs=logs,controller=controller))
            completed,_ = await asyncio.wait([waiter,*[h[1] for h in handles]],return_when=asyncio.FIRST_COMPLETED)
            if waiter not in completed:
                raise RuntimeError('test_service_unexpected_exit')
        finally:
            surface.ready = False
            waiter.cancel()
            await asyncio.gather(waiter,return_exceptions=True)
            for sig in installed:
                loop.remove_signal_handler(sig)
    finally:
        from launcher import stop
        errors = []
        try:
            # Stop accepting HTTP work first; detached keyed runners remain shielded.
            for handle in reversed(handles):
                try:
                    await stop(handle)
                except BaseException as exc:
                    errors.append(type(exc).__name__)
            if ob is not None:
                try:
                    await drain_business(ob)
                except BaseException as exc:
                    errors.append(type(exc).__name__)
            if provider is not None:
                provider.close()
                await provider.wait_closed()
                await controller.close()
        finally:
            lock.close()
        if errors:
            raise RuntimeError('c3_shutdown_failed')

def main():
    token = os.environ.get('OMBRE_MCP_QUERY_TOKEN','')
    if os.environ.get('OB_LSF_TEST_SERVICE') != 'true' or os.environ.get('OMBRE_MCP_ALLOW_QUERY_TOKEN') != 'true':
        raise RuntimeError('test_opt_in_required')
    if not re.fullmatch(r'[A-Za-z0-9_-]{43,}',token):
        raise RuntimeError('fresh_query_token_required')
    port = int(os.environ.get('PORT','8080'))
    if not 1 <= port <= 65535 or port in (18994,18995):
        raise RuntimeError('invalid_platform_port')
    root = Path('/data/lsf')
    if not root.is_dir() or not os.path.ismount(root):
        raise RuntimeError('dedicated_volume_mount_required')
    if os.getuid() == 0:
        os.chown(root,10001,10001)
        os.setgroups([])
        os.setgid(10001)
        os.setuid(10001)
    asyncio.run(serve(token,root,port))

if __name__ == '__main__':
    try:
        main()
    except Exception:
        print('L-SF C3 startup/runtime failed; inspect safe preflight evidence',file=sys.stderr)
        sys.exit(1)

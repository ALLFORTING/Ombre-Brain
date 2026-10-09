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

# Diagnostic values are closed sets; never render exception text or environment.
_STARTUP_STAGE = 'entry'
_SAFE_STAGES = frozenset({
    'entry', 'opt_in', 'token', 'port', 'volume_mount', 'volume_permissions',
    'environment', 'volume_lock', 'runtime_dependencies', 'c2_sources',
    'c3_artifacts', 'volume_markers', 'server_import', 'c2_volume_validation',
    'provider_start', 'mcp_start', 'running',
})
_SAFE_CODES = frozenset({
    'test_opt_in_required', 'fresh_query_token_required', 'invalid_platform_port',
    'dedicated_volume_mount_required', 'invalid_test_root', 'test_volume_symlink',
    'unexpected_test_config', 'c3_linux_default_tmp_required',
    'test_volume_already_in_use', 'c2_source_identity_mismatch',
    'c2_source_path_refused', 'c2_source_hash_mismatch',
    'c3_artifact_identity_mismatch', 'c3_artifact_hash_mismatch',
    'c3_existing_c2_complete_required', 'c3_existing_seed_required',
    'test_volume_identity_mismatch', 'incomplete_seed_requires_review',
    'c3_background_drain_timeout', 'test_service_unexpected_exit',
    'c3_shutdown_failed',
    # c2_seed.py volume validation; fixed strings, never exception payloads.
    'c2_caller_lock_required', 'c2_path_redirected', 'c2_root_mismatch',
    'c2_partial_requires_review', 'c2_embedding_configuration', 'c2_schema_mismatch',
    'c2_identity_conflict', 'c2_known_compatible_invalid', 'c2_manifest_conflict',
    'c2_incomplete_manifest', 'c2_id_collision', 'c2_manifest_path',
    'c2_fixture_changed', 'c2_manifest_table', 'c2_receipt_changed',
    'c2_index_changed', 'c2_compat_metadata', 'c2_compat_schema', 'c2_compat_vector',
})
_SAFE_FILES = frozenset({
    'c3_run.py', 'run.py', 'environment.py', 'initialize.py',
    'c2_seed.py', 'launcher.py', 'c3_provider.py', 'server.py',
})


def emit_startup_diagnostic(exc):
    code = 'startup_runtime_error'
    if (type(exc) is RuntimeError and len(exc.args) == 1
            and type(exc.args[0]) is str and exc.args[0] in _SAFE_CODES):
        code = exc.args[0]
    stage = _STARTUP_STAGE if _STARTUP_STAGE in _SAFE_STAGES else 'entry'
    frame = exc.__traceback__
    while frame is not None and frame.tb_next is not None:
        frame = frame.tb_next
    filename, line = 'external', 0
    if frame is not None:
        candidate = Path(frame.tb_frame.f_code.co_filename).name
        filename = candidate if candidate in _SAFE_FILES else 'external'
        line = frame.tb_lineno
    print(json.dumps({'stage': stage, 'error_code': code,
                      'location': f'{filename}:{line}'}, sort_keys=True),
          file=sys.stderr, flush=True)

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
    global _STARTUP_STAGE
    _STARTUP_STAGE = 'environment'
    configure_c3(token, root, port)
    from observe import install_logging
    logs = install_logging([token,'synthetic-no-external-permission'])
    from initialize import lock_volume, initialize
    _STARTUP_STAGE = 'volume_lock'
    lock = lock_volume(root)
    handles = []
    provider = None
    ob = None
    try:
        _STARTUP_STAGE = 'runtime_dependencies'
        sources = runtime_sources()
        _STARTUP_STAGE = 'c2_sources'
        verify_c2_sources()
        _STARTUP_STAGE = 'c3_artifacts'
        verify_c3_sources()
        _STARTUP_STAGE = 'volume_markers'
        marker = root/'.c2-v1.json'
        if not marker.is_file() or json.loads(marker.read_text()).get('status') != 'complete':
            raise RuntimeError('c3_existing_c2_complete_required')
        # Existing original initialization identity must also validate unchanged.
        if not (root/'.synthetic-initialized.json').is_file():
            raise RuntimeError('c3_existing_seed_required')
        assert initialize(root) is False
        _STARTUP_STAGE = 'server_import'
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
        _STARTUP_STAGE = 'c2_volume_validation'
        c2 = await initialize_c2(ob,root,lock)
        assert c2['initialized'] is False
        from c3_provider import Controller, TransportEvents
        from launcher import start, build
        _STARTUP_STAGE = 'provider_start'
        controller = Controller(ob)
        provider = await asyncio.start_server(controller.handle,'127.0.0.1',18995)
        handles.append(await start(controller.app(),18994))
        surface = TestSurface(TransportEvents(build(ob,deque(maxlen=256)),controller))
        _STARTUP_STAGE = 'mcp_start'
        handles.append(await start(surface,port,'0.0.0.0'))
        surface.ready = True
        _STARTUP_STAGE = 'running'
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
    global _STARTUP_STAGE
    _STARTUP_STAGE = 'opt_in'
    token = os.environ.get('OMBRE_MCP_QUERY_TOKEN','')
    if os.environ.get('OB_LSF_TEST_SERVICE') != 'true' or os.environ.get('OMBRE_MCP_ALLOW_QUERY_TOKEN') != 'true':
        raise RuntimeError('test_opt_in_required')
    _STARTUP_STAGE = 'token'
    if not re.fullmatch(r'[A-Za-z0-9_-]{43,}',token):
        raise RuntimeError('fresh_query_token_required')
    _STARTUP_STAGE = 'port'
    port = int(os.environ.get('PORT','8080'))
    if not 1 <= port <= 65535 or port in (18994,18995):
        raise RuntimeError('invalid_platform_port')
    _STARTUP_STAGE = 'volume_mount'
    root = Path('/data/lsf')
    if not root.is_dir() or not os.path.ismount(root):
        raise RuntimeError('dedicated_volume_mount_required')
    _STARTUP_STAGE = 'volume_permissions'
    if os.getuid() == 0:
        os.chown(root,10001,10001)
        os.setgroups([])
        os.setgid(10001)
        os.setuid(10001)
    asyncio.run(serve(token,root,port))

if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        emit_startup_diagnostic(exc)
        sys.exit(1)

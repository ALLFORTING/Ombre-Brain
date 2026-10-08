"""Fresh synthetic backup runtime; no capture, listener or authentication bypass."""
from __future__ import annotations
import asyncio
import functools
import os
from pathlib import Path
import signal
import sys
import tempfile
from types import SimpleNamespace

SOURCE = Path(__file__).resolve().parents[2]
FORBIDDEN = '/data/lsf'


def check_path(value):
    if not isinstance(value, (str, bytes, os.PathLike)):
        return
    raw = os.fsdecode(value)
    for name in (raw, os.path.abspath(raw)):
        name = '/'+name.lstrip('/') if name.startswith('/') else name
        if name == FORBIDDEN or name.startswith(FORBIDDEN + '/'):
            raise PermissionError('synthetic_original_path_denied')


def install_guards():
    """Python defense in depth, not a replacement for platform isolation."""
    def audit(event, args):
        if event in {'socket.connect', 'socket.getaddrinfo'}:
            raise PermissionError('synthetic_network_denied')
        if event in {'open', 'sqlite3.connect'} and args:
            check_path(args[0])
    sys.addaudithook(audit)
    for name in ('open', 'stat', 'lstat', 'listdir', 'scandir', 'mkdir', 'makedirs',
                 'unlink', 'remove', 'rmdir', 'chdir', 'chmod', 'chown', 'readlink', 'access', 'utime'):
        original = getattr(os, name)
        @functools.wraps(original)
        def guarded(path, *args, _original=original, **kwargs):
            check_path(path)
            if kwargs.get('dir_fd') is not None:
                check_path(os.path.join(os.readlink('/proc/self/fd/'+str(kwargs['dir_fd'])), os.fsdecode(path)))
            return _original(path, *args, **kwargs)
        setattr(os, name, guarded)
    for name in ('rename', 'replace', 'link', 'symlink'):
        original = getattr(os, name)
        @functools.wraps(original)
        def guarded_pair(src, dst, *args, _original=original, **kwargs):
            check_path(src)
            check_path(dst)
            for item, key in ((src, 'src_dir_fd'), (dst, 'dst_dir_fd')):
                if kwargs.get(key) is not None:
                    check_path(os.path.join(os.readlink('/proc/self/fd/'+str(kwargs[key])), os.fsdecode(item)))
            return _original(src, dst, *args, **kwargs)
        setattr(os, name, guarded_pair)


def isolated_environment(root, provenance):
    os.environ.clear()
    os.environ.update({
        'PATH': '/usr/local/bin:/usr/bin:/bin', 'LANG': 'C.UTF-8',
        'PYTHONDONTWRITEBYTECODE': '1', 'HOME': str(root/'home'),
        'XDG_CACHE_HOME': str(root/'cache'), 'OMBRE_BUCKETS_DIR': str(root/'source'),
        'OMBRE_RM_DATA_ROOT': str(root/'remember-me'), 'OMBRE_RM_RUNTIME_ENABLED': 'false',
        'OMBRE_RAW_EVIDENCE_ROOT': str(root/'raw-evidence'),
        'OMBRE_ASSET_AUTHORITY': 'legacy', 'OMBRE_TRANSPORT': 'streamable-http',
        'OMBRE_HOOK_SKIP': 'true', 'OMBRE_DIAG_TOOLS': 'false',
        'OMBRE_BACKUP_V2_ENABLED': 'false', 'OMBRE_BACKUP_V2_ARMED': 'false',
        'OMBRE_BACKUP_AUTO_ENABLED': 'true',
        'OMBRE_BACKUP_AUTO_WORKSPACE_ROOT': str(root/'workspace'),
        'OMBRE_BACKUP_AUTO_FREEZE_TIMEOUT_SECONDS': '2',
        'OMBRE_BACKUP_AUTO_MAX_FREEZE_SECONDS': '30',
        'OMBRE_BACKUP_AUTO_MAX_SOURCE_BYTES': '10485760',
        'OMBRE_BACKUP_AUTO_MAX_BUNDLE_BYTES': '10485760',
        'OMBRE_BACKUP_AUTO_MINIMUM_FREE_BYTES': '1',
        'OMBRE_BACKUP_AUTO_READY_TTL_SECONDS': '86400', **provenance,
    })
    os.chdir(root)


async def seed(manager):
    # Use the real guarded storage APIs and their actual SQLite schema.
    for sealed in (False, True):
        await manager.create('SYNTHETIC sealed BODY' if sealed else 'SYNTHETIC ordinary BODY',
                             tags=['backup-synthetic'], bucket_type='permanent', sealed=sealed)
        manager.record_letter('SYNTHETIC LETTER', session_id='backup-synthetic-'+str(int(sealed)), sealed=sealed)
        manager.record_note('SYNTHETIC NOTE', sealed=sealed, open_at='2099-01-01')

def prepare_runtime(provenance, *, root=None):
    """Isolated worker only. Tests supply synthetic immutable build metadata."""
    sys.path.insert(0, str(SOURCE))
    from backup_v2_runtime import resolve_runtime_commit
    commit = resolve_runtime_commit(provenance)
    if 'server' in sys.modules:
        raise RuntimeError('synthetic_existing_server_refused')
    if root is None:
        root = Path(tempfile.mkdtemp(prefix='ob-backup-synthetic-', dir='/tmp'))
    else:
        root = Path(root)
        check_path(root)
        if not root.is_absolute() or not str(root).startswith('/tmp/') or root.exists():
            raise RuntimeError('synthetic_fresh_root_required')
        if root.parent.resolve(strict=True) != root.parent:
            raise RuntimeError('synthetic_redirected_parent')
        root = Path(tempfile.mkdtemp(prefix=root.name+'-', dir=root.parent))
    isolated_environment(root, provenance)
    import utils
    original_load = utils.load_config
    def load_isolated(config_path=None):
        config = original_load(str(root/'absent-config.yaml'))
        config.setdefault('embedding', {})['enabled'] = False
        for name in ('embedding', 'dehydration'):
            config.setdefault(name, {})['api_key'] = ''
            config[name]['base_url'] = 'http://127.0.0.1:9/v1'
        return config
    utils.load_config = load_isolated
    import server
    from backup_auto_runtime import register_backup_auto_if_enabled, install_backup_auto_lifespan, PREFIX
    controller = register_backup_auto_if_enabled(server, 'streamable-http')
    asyncio.run(seed(server.bucket_mgr))
    from offline_backup_bundle import prepare_backup_workspace
    restore = prepare_backup_workspace(root/'restore')
    from starlette.applications import Starlette
    app = Starlette(routes=[route for route in server.mcp._custom_starlette_routes
                            if route.path.startswith(PREFIX+'/')])
    install_backup_auto_lifespan(app, server)
    return SimpleNamespace(root=root, server=server, controller=controller, app=app,
                           restore=restore, commit=commit)


async def serve(runtime, stop, ready=None):
    async with runtime.app.router.lifespan_context(runtime.app):
        if ready is not None:
            ready.set()
        print('synthetic_ready_no_automatic_capture', flush=True)
        await stop.wait()
    print('synthetic_stopped', flush=True)


def main():
    enabled = os.environ.get('OB_BACKUP_SYNTHETIC_ENABLED', 'false')
    if enabled == 'false':
        print('synthetic_disabled', flush=True)
        return 0
    if enabled != 'true' or os.environ.get('OB_BACKUP_SYNTHETIC_PLATFORM_ISOLATION_VERIFIED') != 'true':
        print('synthetic_isolation_unverified', flush=True)
        return 1
    provenance = {name: os.environ[name] for name in ('ZEABUR_SERVICE_ID', 'ZEABUR_GIT_COMMIT_SHA')
                  if name in os.environ}
    try:
        install_guards()
        runtime = prepare_runtime(provenance)
        async def waiting():
            stop = asyncio.Event()
            loop = asyncio.get_running_loop()
            for signum in (signal.SIGTERM, signal.SIGINT):
                loop.add_signal_handler(signum, stop.set)
            try:
                await serve(runtime, stop)
            finally:
                for signum in (signal.SIGTERM, signal.SIGINT):
                    loop.remove_signal_handler(signum)
        asyncio.run(waiting())
        return 0
    except Exception:
        print('synthetic_startup_or_runtime_failed', flush=True)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())

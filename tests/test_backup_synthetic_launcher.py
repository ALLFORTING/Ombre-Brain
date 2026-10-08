"""Isolated Linux workers; only synthetic files, JWT/JWKS and ASGI transport."""
import ast
import os
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
ENTRY = ROOT/'deploy/backup-synthetic/run.py'


def worker(case, root):
    import asyncio
    import hashlib
    import importlib.util
    import json
    import shutil
    import socket
    import sqlite3
    import threading
    import uuid
    spec = importlib.util.spec_from_file_location('synthetic_entry', ENTRY)
    entry = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(entry)
    if case in ('disabled', 'isolation'):
        entry.prepare_runtime = lambda *a, **k: (_ for _ in ()).throw(AssertionError('side effect'))
        if case == 'isolation':
            os.environ['OB_BACKUP_SYNTHETIC_ENABLED'] = 'true'
        assert entry.main() == (0 if case == 'disabled' else 1)
        return
    if case == 'image':
        image = root/'image'
        image.mkdir()
        lines = (ROOT/'deploy/backup-synthetic/Dockerfile.dockerignore').read_text().splitlines()
        for line in lines:
            if line.startswith('!/') and line.endswith(('.py', '.txt', '.html')):
                relative = line[2:]
                destination = image/relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(ROOT/relative, destination)
        entry.SOURCE = image
    entry.install_guards()
    if case.startswith('guard_'):
        forbidden = '/data/lsf/synthetic-sentinel'
        actions = {
            'open': lambda: open(forbidden, 'rb'), 'write': lambda: open(forbidden, 'wb'),
            'stat': lambda: os.stat(forbidden), 'scan': lambda: os.scandir('/data/lsf'),
            'list': lambda: os.listdir('/data/lsf'), 'mkdir': lambda: os.mkdir(forbidden),
            'chdir': lambda: os.chdir('/data/lsf'), 'chmod': lambda: os.chmod(forbidden, 0o700),
            'rename': lambda: os.rename(str(root/'absent'), forbidden),
            'sqlite': lambda: sqlite3.connect(forbidden),
            'traversal': lambda: open('/data/lsf/../escape', 'rb'),
            'double_slash': lambda: open('//data/lsf/sentinel', 'rb'),
            'network': lambda: socket.create_connection(('api.github.com', 443)),
        }
        try:
            actions[case.removeprefix('guard_')]()
        except PermissionError as exc:
            assert str(exc) in ('synthetic_original_path_denied', 'synthetic_network_denied')
        else:
            raise AssertionError('guard did not reject')
        return
    sys.path.insert(0, str(entry.SOURCE))
    import backup_v2_runtime as registration
    build = root/'synthetic-build.json'
    commit = '1'*40
    build.write_text(json.dumps({'source': 'zeabur-build', 'status': 'valid', 'commit': commit}))
    registration.BUILD_METADATA_PATH = build
    provenance = {'ZEABUR_SERVICE_ID': 'synthetic-only', 'ZEABUR_GIT_COMMIT_SHA': commit}
    if case in ('missing_sha', 'drift_sha', 'existing_root', 'forbidden_root'):
        if case == 'missing_sha':
            registration.BUILD_METADATA_PATH = root/'absent-build'
        if case == 'drift_sha':
            provenance['ZEABUR_GIT_COMMIT_SHA'] = '2'*40
        target = root/'run'
        if case == 'existing_root':
            target.mkdir(); (target/'keep').write_bytes(b'KEEP')
        if case == 'forbidden_root':
            target = Path('/data/lsf')
        try:
            entry.prepare_runtime(provenance, root=target)
        except (registration.BackupV2RuntimeConfigError, RuntimeError, PermissionError):
            pass
        else:
            raise AssertionError('invalid initialization accepted')
        if case == 'existing_root':
            assert (target/'keep').read_bytes() == b'KEEP'
        elif case != 'forbidden_root':
            assert not target.exists()
        return
    # Test-only real signature verifier with a generated in-memory signing key.
    import backup_auto_runtime as auto
    import jwt
    from cryptography.hazmat.primitives.asymmetric import rsa
    from types import SimpleNamespace
    from time import time
    from backup_v2_oidc import GitHubActionsBackupV2OidcVerifier
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwks = SimpleNamespace(get_signing_key_from_jwt=lambda token: SimpleNamespace(key=key.public_key()))
    verifier = GitHubActionsBackupV2OidcVerifier(jwk_client=jwks, audience=auto.AUTO_AUDIENCE)
    auto.GitHubActionsBackupV2OidcVerifier = lambda audience: verifier
    runtime = entry.prepare_runtime(provenance, root=root/'run')
    c = runtime.controller
    assert Path(runtime.server.__file__).parent == entry.SOURCE
    assert runtime.server.bucket_mgr.write_coordinator is c.coordinator
    assert registration.require_runtime_coordinator(runtime.server, c, published_only=True) is c.coordinator
    assert not os.environ.get('GH_TOKEN') and not os.environ.get('OMBRE_API_KEY')
    assert not c._jobs and not c._tasks
    assert runtime.restore.root.parent == c.source_root.parent == c.workspace.root.parent
    assert len({runtime.restore.root, c.source_root, c.workspace.root}) == 3
    claims = {'repository': auto.AUTO_REPOSITORY, 'repository_id': auto.AUTO_REPOSITORY_ID,
              'repository_owner': 'ALLFORTING', 'repository_owner_id': auto.AUTO_OWNER_ID,
              'repository_visibility': 'private', 'ref': 'refs/heads/main', 'event_name': 'workflow_dispatch',
              'workflow_ref': auto.AUTO_WORKFLOW_REF, 'aud': auto.AUTO_AUDIENCE,
              'run_id': '123', 'run_attempt': '1', 'iss': 'https://token.actions.githubusercontent.com',
              'iat': int(time()), 'nbf': int(time())-1, 'exp': int(time())+300}
    token = jwt.encode(claims, key, algorithm='RS256', headers={'kid': 'synthetic-only'})
    if case in ('sigterm', 'sigint'):
        import signal
        os.environ['OB_BACKUP_SYNTHETIC_ENABLED'] = 'true'
        os.environ['OB_BACKUP_SYNTHETIC_PLATFORM_ISOLATION_VERIFIED'] = 'true'
        entry.prepare_runtime = lambda *args, **kwargs: runtime
        signum = signal.SIGTERM if case == 'sigterm' else signal.SIGINT
        timer = threading.Timer(1, lambda: os.kill(os.getpid(), signum))
        timer.start()
        try:
            assert entry.main() == 0
        finally:
            timer.join()
        assert not c._jobs and not c._tasks
        assert c.coordinator.status().state == 'open'
        return
    async def scenario():
        import httpx
        import production_backup_capture as capture
        import offline_backup_bundle as bundles
        from starlette.requests import Request
        stop = asyncio.Event(); ready = asyncio.Event()
        task = asyncio.create_task(entry.serve(runtime, stop, ready))
        await asyncio.wait_for(ready.wait(), 5)
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=runtime.app), base_url='http://synthetic') as client:
                assert (await client.get(auto.PREFIX+'/metadata')).status_code == 400
                metadata = await client.get(auto.PREFIX+'/metadata', headers={'Authorization': 'Bearer '+token})
                assert metadata.status_code == 200 and metadata.json()['runtime_commit'] == commit
                if case == 'idle':
                    assert not c._jobs
                    return
                if case == 'wrong_oidc':
                    bad = jwt.encode({**claims, 'ref': 'refs/heads/test'}, key, algorithm='RS256', headers={'kid':'synthetic-only'})
                    reply = await client.post(auto.PREFIX+'/captures', headers={'Authorization':'Bearer '+bad},
                                              json={'request_id': str(uuid.uuid4()), 'expected_runtime_commit': commit})
                    assert reply.status_code == 400 and reply.json()['status'] == 'oidc_denied'
                    assert not c._jobs
                    return
                request_id = str(uuid.uuid4())
                release = threading.Event(); entered = threading.Event()
                if case in ('failure', 'stop_worker'):
                    original = capture.capture_external_source
                    def substituted(*args, **kwargs):
                        if case == 'failure':
                            raise capture.CaptureChannelError('source_changed')
                        entered.set()
                        assert release.wait(10)
                        return original(*args, **kwargs)
                    capture.capture_external_source = substituted
                reply = await client.post(auto.PREFIX+'/captures', headers={'Authorization':'Bearer '+token},
                                          json={'request_id':request_id, 'expected_runtime_commit':commit})
                assert reply.status_code == 202, reply.text
                if case == 'stop_worker':
                    assert await asyncio.to_thread(entered.wait, 5)
                    stop.set(); await asyncio.sleep(0.05)
                    assert not task.done()  # Real lifespan waits for the actual capture worker.
                    release.set(); await asyncio.wait_for(task, 10)
                    assert c.coordinator.status().state == 'open' and not c._active_workers
                    assert c._jobs[request_id].state == 'ready'
                    return
                result = await c.wait_for_terminal(request_id, claims)
                if case == 'failure':
                    assert result['state'] == 'failed' and result['failure_code'] == 'source_changed'
                    assert c.coordinator.status().state == 'open' and c._active_request_id is None
                    return
                assert result['state'] == 'ready', result
                download = await client.get(auto.PREFIX+'/captures/'+request_id+'/bundle', headers={'Authorization':'Bearer '+token})
                assert download.status_code == 200
                assert len(download.content) == result['bundle_size']
                assert hashlib.sha256(download.content).hexdigest() == result['bundle_sha256']
                package = runtime.restore.bundles_root/(result['bundle_id']+bundles.PLAIN_SUFFIX)
                with package.open('xb') as stream:
                    stream.write(download.content)
                if case == 'corrupt':
                    package.write_bytes(download.content[:-1]+b'X')
                    try:
                        bundles.restore_plain_bundle(runtime.restore.root, package.name,
                            expected_sha256=result['bundle_sha256'], expected_size=result['bundle_size'],
                            restore_name=uuid.uuid4().hex, maximum_bytes=10485760)
                    except bundles.BackupBundleError:
                        assert not list(runtime.restore.restored_root.iterdir())
                        return
                    raise AssertionError('corrupt restore accepted')
                report = bundles.restore_plain_bundle(runtime.restore.root, package.name,
                    expected_sha256=result['bundle_sha256'], expected_size=result['bundle_size'],
                    restore_name=uuid.uuid4().hex, maximum_bytes=10485760)
                assert report['reconciliation_matched']
                summary = report['reconciliation']
                assert summary['buckets']['count'] == 2 and summary['buckets']['sealed_count'] == 1
                for table in ('letters','notes'):
                    row = next(value for name, value in summary['stores'].items() if name.endswith(':'+table) and value['count'])
                    assert row['count'] == 2 and row['sealed_count'] == 1
                assert all(word not in json.dumps(report) for word in ('SYNTHETIC LETTER','SYNTHETIC NOTE','SYNTHETIC sealed BODY'))
                assert c.coordinator.status().state == 'open' and not c._active_deliveries
                assert (c.workspace.bundles_root/package.name).exists()  # No ack/cleanup test here.
        finally:
            stop.set()
            await asyncio.wait_for(task, 15)
        assert not any(t.get_name() == 'backup-auto-ttl' for t in asyncio.all_tasks() if not t.done())
    asyncio.run(scenario())


@pytest.mark.parametrize('case', ['disabled','isolation','guard_open','guard_write','guard_stat','guard_scan',
    'guard_list','guard_mkdir','guard_chdir','guard_chmod','guard_rename','guard_sqlite','guard_traversal',
    'guard_network','guard_double_slash','missing_sha','drift_sha','existing_root','forbidden_root',
    'idle','wrong_oidc','success','failure','corrupt','stop_worker','sigterm','sigint','image'])
def test_isolated_synthetic_worker(case, tmp_path):
    result = subprocess.run([sys.executable, '-B', str(Path(__file__).resolve()), case, str(tmp_path)],
                            cwd=ROOT, env={'PATH':'/usr/local/bin:/usr/bin:/bin','PYTHONDONTWRITEBYTECODE':'1', 'GH_TOKEN':'synthetic-only-no-permission', 'OMBRE_BUCKETS_DIR':'/data/lsf'},
                            capture_output=True, text=True, timeout=45)
    assert result.returncode == 0, result.stdout+result.stderr
    assert 'CASE_PASS' in result.stdout


def test_image_context_and_no_deployed_auth_bypass():
    docker = (ROOT/'deploy/backup-synthetic/Dockerfile').read_text(encoding='utf-8-sig')
    ignore = (ROOT/'deploy/backup-synthetic/Dockerfile.dockerignore').read_text()
    assert ignore.splitlines()[0] == '**'
    assert '!/tests/' not in ignore and '!/config.yaml' not in ignore and '!/.env' not in ignore
    assert 'USER 10002:10002' in docker and 'OB_BACKUP_SYNTHETIC_ENABLED=false' in docker
    assert 'COPY . ' not in docker and 'VOLUME ' not in docker
    entry = ENTRY.read_text(encoding='utf-8-sig')
    assert 'c3_run' not in entry and 'c2_seed' not in entry and 'create_capture(' not in entry
    assert 'jwk_client' not in entry and 'jwt.encode' not in entry


if __name__ == '__main__':
    worker(sys.argv[1], Path(sys.argv[2]))
    print('CASE_PASS')

"""Read-only C3 artifact/source/scope/credential audit; no services or repairs."""
import ast
import hashlib
import json
import re
import subprocess
from pathlib import Path
HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
BASELINE = '6be6e84d11f3942061aa6061ea9f431155b5d29b'
def sha(path):
    return hashlib.sha256(path.read_bytes().replace(b'\r\n',b'\n')).hexdigest()
def git(*args):
    return subprocess.check_output(['git','-c','core.autocrlf=true',*args],cwd=ROOT)
def main():
    pins = json.loads((HERE/'c3-artifact-hashes.json').read_text())
    assert pins['baseline']==BASELINE
    for key in ('sha256','delivery_sha256'):
        for name, expected in pins[key].items():
            path=HERE/name
            assert path.resolve().is_relative_to(HERE)
            assert sha(path)==expected, name
    protected = ['run.py','environment.py','launcher.py','provider_stub.py','initialize.py','c2_seed.py','c2_vectors.py','c2-source-hashes.json']
    for name in protected:
        blob=git('show',f'{BASELINE}:deploy/lsf-zeabur/{name}')
        assert (HERE/name).read_bytes().replace(b'\r\n',b'\n')==blob.replace(b'\r\n',b'\n'), name
    changed=set(git('diff','--name-only',BASELINE).decode().splitlines()) | set(git('ls-files','--others','--exclude-standard').decode().splitlines())
    assert all(p.startswith('deploy/lsf-zeabur/') for p in changed), sorted(changed)
    for path in HERE.glob('c3_*.py'):
        ast.parse(path.read_text(encoding='utf-8-sig'),filename=str(path))
    ast.parse((HERE/'test_c3.py').read_text(encoding='utf-8-sig'))
    whitelist=(HERE/'Dockerfile.dockerignore').read_text().splitlines()
    for name in pins['sha256']:
        assert '!deploy/lsf-zeabur/'+name in whitelist, name
    assert '!deploy/lsf-zeabur/c3-artifact-hashes.json' in whitelist
    assert '/c3_run.py' in (HERE/'Dockerfile').read_text()
    banned=['synthetic-local-c3-token-with-no-external-permission','synthetic-local-test-token-with-no-external-permission','synthetic-no-external-permission']
    for path in (HERE/'evidence').glob('c3*'):
        text=path.read_text(encoding='utf-8-sig')
        assert not any(value in text for value in banned), path.name
        assert not re.search(r'(?i)(authorization:\s*bearer\s+\S+|[?&]token=[A-Za-z0-9_-]{20,}|sk-[A-Za-z0-9]{20,})',text), path.name
    print(json.dumps(dict(baseline=BASELINE,scope=sorted(changed),protected_unchanged=protected,hashes_valid=True,python_syntax_valid=True,docker_whitelist_valid=True,credential_scan_passed=True),indent=2))
if __name__=='__main__':
    main()

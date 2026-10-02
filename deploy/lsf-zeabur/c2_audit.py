"""C2 source/hash/scope audit. --refresh updates test-only hash inventories."""
import ast
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

HERE=Path(__file__).resolve().parent
SOURCE=HERE.parents[1]
BASELINE="ab7a348b11e6c7d8725d8d0c4ad1f0a26e19cec6"
BUSINESS=("server.py","bucket_manager.py","embedding_engine.py","dehydrator.py",
          "utils.py","requirements.txt","constraints-py312-linux.txt")
RUNTIME=("run.py","environment.py","initialize.py","seed.py","launcher.py",
         "provider_stub.py","c2_seed.py","c2_vectors.py","observe.py")

def sha(data):
    # Checkout CRLF is not an implementation change; the image receives Git LF.
    return hashlib.sha256(data.replace(b"\r\n",b"\n")).hexdigest()

def git(*args):
    pointer=(SOURCE/".git").read_text().strip().removeprefix("gitdir: ")
    if re.match(r"^[A-Za-z]:/",pointer):
        pointer="/mnt/"+pointer[0].lower()+pointer[2:]
    return subprocess.check_output(["git","-c","core.autocrlf=true","-c","safe.directory="+str(SOURCE),
        "--git-dir="+pointer,"--work-tree="+str(SOURCE),*args])

def main():
    assert git("rev-parse","HEAD").decode().strip()==BASELINE or git("rev-parse","HEAD^").decode().strip()==BASELINE
    assert not git("diff",BASELINE,"--",".",":(exclude)deploy/lsf-zeabur")
    assert not git("ls-files","--others","--exclude-standard","--",".",":(exclude)deploy/lsf-zeabur")
    for p in HERE.glob("*.py"):
        ast.parse(p.read_text(),filename=p.name)
    pins={name:sha(git("show",BASELINE+":"+name)) for name in BUSINESS}
    assert all(sha((SOURCE/p).read_bytes())==h for p,h in pins.items())
    for name in RUNTIME:
        pins["deploy/lsf-zeabur/"+name]=sha((HERE/name).read_bytes())
    source_file=HERE/"c2-source-hashes.json"
    if "--refresh" in sys.argv:
        source_file.write_text(json.dumps({"baseline":BASELINE,"newline":"LF-normalized",
            "sha256":pins},sort_keys=True,indent=2)+"\n")
        (HERE/"artifact-hashes.json").write_text(json.dumps([
            dict(path=p.relative_to(HERE).as_posix(),sha256=sha(p.read_bytes()))
            for p in sorted(HERE.rglob("*")) if p.is_file() and
            not any(part.startswith(".") and part != ".gitignore" for part in p.relative_to(HERE).parts)
            and p.name!="artifact-hashes.json" and "__pycache__" not in p.parts
        ],indent=2)+"\n")
    assert json.loads(source_file.read_text())["sha256"]==pins
    for row in json.loads((HERE/"artifact-hashes.json").read_text()):
        assert sha((HERE/row["path"]).read_bytes())==row["sha256"], row["path"]
    allowed=[line[1:] for line in (HERE/"Dockerfile.dockerignore").read_text().splitlines()
             if line.startswith("!") and not line.endswith("/")]
    assert all((SOURCE/p).is_file() for p in allowed)
    assert "deploy/lsf-zeabur/c2_seed.py" in allowed
    assert "deploy/lsf-zeabur/c2_vectors.py" in allowed
    assert "deploy/lsf-zeabur/c2-source-hashes.json" in allowed
    assert all("test_c2" not in p and "evidence/" not in p for p in allowed)
    print(json.dumps(dict(result="PASS",baseline=BASELINE,business_diff=False,
        syntax=True,source_hashes=True,artifact_hashes=True,docker_allowlist=True,
        docker="BLOCKED_NOT_EXECUTED",zeabur="BLOCKED_NOT_EXECUTED",claude="BLOCKED_NOT_EXECUTED")))

if __name__=="__main__":
    main()

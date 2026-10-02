"""Static bounded audit of deployment artifacts; no production test suite."""
import ast
import hashlib
import json
import re
import subprocess
from pathlib import Path
ROOT = Path(__file__).resolve().parent
SOURCE = ROOT.parents[1]
BASELINE = "885807cf460bec47af09812a523677d9dbb33eba"

def main():
    cases = []
    def passed(name):
        cases.append({"case": name, "result": "PASS"})
    for path in ROOT.glob("*.py"):
        ast.parse(path.read_text(encoding="utf-8"), filename=path.name)
    passed("deployment_python_syntax")
    pointer = (SOURCE / ".git").read_text().strip().removeprefix("gitdir: ")
    gitdir = "/mnt/" + pointer[0].lower() + pointer[2:] if re.match(r"^[A-Za-z]:/", pointer) else pointer
    def git(*args):
        return subprocess.check_output(["git", "-c", "core.autocrlf=true", "-c", "safe.directory=" + str(SOURCE), "--git-dir=" + gitdir, "--work-tree=" + str(SOURCE), "-C", str(SOURCE), *args], text=True).strip()
    assert git("rev-parse", "HEAD") == BASELINE
    assert not git("diff", BASELINE, "--", ".", ":(exclude)deploy/lsf-zeabur")
    passed("exact_baseline_no_business_diff")
    instructions = (ROOT / "FIRST_GROUP_CLAUDE_INSTRUCTIONS.md").read_text()
    original = Path("/mnt/d/Codex/projects/OB-Claude-Web-Plan-20261002/FIRST_GROUP_CLAUDE_INSTRUCTIONS.md").read_text()
    assert instructions == original.replace('trace(bucket_id=H1, append="OBWEB-LS-APPEND-ONCE",', 'trace(bucket_id=H1, content="OBWEB-LS-APPEND-ONCE", append=True,')
    assert re.findall(r'operation_id="([^"]+)"', instructions) == re.findall(r'operation_id="([^"]+)"', original)
    passed("trace_parameters_only_operation_ids_preserved")
    prior = Path("/mnt/d/Codex/projects/OB-Claude-Synthetic-20261002-LSF-Local")
    manifests = json.loads((prior / "artifact-hashes.json").read_text())
    pins = {row["path"]: row["sha256"] for row in manifests}
    for row in json.loads((ROOT / "reuse-provenance.json").read_text()):
        digest = hashlib.sha256((prior / row["path"]).read_bytes()).hexdigest()
        assert digest == row["sha256"] == pins[row["path"]]
    for name in ("provider_stub.py", "observe.py", "seed.py"):
        assert (ROOT / name).read_bytes() == (prior / name).read_bytes()
    passed("accepted_artifact_hashes_and_exact_reuse")
    archive = Path("/mnt/d/Codex/projects/Remember-Me-0.1.0-release-artifacts/remember_me-0.1.0.tar.gz")
    from environment import RM_SHA
    assert hashlib.sha256(archive.read_bytes()).hexdigest() == RM_SHA
    assert "mcp==1.29.1" in (SOURCE / "constraints-py312-linux.txt").read_text()
    assert (SOURCE / ".python-version").read_text().strip() == "3.12.14"
    passed("official_rm_archive_hash_and_linux_pins")
    allowed = [line[1:] for line in (ROOT / "Dockerfile.dockerignore").read_text().splitlines() if line.startswith("!") and not line.endswith("/")]
    tracked = git("ls-files").splitlines()
    for path in allowed:
        assert (SOURCE / path).is_file()
        assert not any(part.startswith(".") for part in Path(path).parts)
        assert path in tracked or path.startswith("deploy/lsf-zeabur/")
        assert not any(part in ("tests", "buckets", "evidence") for part in Path(path).parts)
    assert "**" == (ROOT / "Dockerfile.dockerignore").read_text().splitlines()[0]
    passed("docker_context_allowlist_no_data_or_secrets")
    # Denial gates only, no new server processes or runtime data.
    import os
    from run import main as entry
    saved = os.environ.copy()
    for variables in ({}, {"OB_LSF_TEST_SERVICE": "true", "OMBRE_MCP_ALLOW_QUERY_TOKEN": "true"}):
        os.environ.clear()
        os.environ.update(variables)
        try:
            entry()
        except RuntimeError:
            pass
        else:
            raise AssertionError("startup_gate_missing")
    os.environ.clear()
    os.environ.update(saved)
    passed("missing_opt_in_and_token_fail_closed")
    report = {"baseline": BASELINE, "result": "PASS", "cases": cases, "archive_sha256": RM_SHA, "docker": "UNAVAILABLE_UNVERIFIED", "S5": "INCOMPLETE"}
    destination = ROOT / "evidence" / "bounded-audit.json"
    assert not destination.exists()
    destination.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(report))

if __name__ == "__main__":
    main()

"""Current L-SF audit entry. Read-only by default; --refresh rewrites hash inventories.

audit.py, c2_audit.py and c3_audit.py pin one-off delivery baselines (fixed HEAD,
no business diff, local paths). They stay as historical evidence, not as the entry
for ongoing changes. artifact-hashes.json is the C2 delivery inventory read only by
c2_audit.py; it is historical and neither checked nor refreshed here. --refresh only
updates values of already pinned files; adding or removing one stays a manual edit.
"""
import ast
import hashlib
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SOURCE = HERE.parents[1]
C2_BASELINE = "ab7a348b11e6c7d8725d8d0c4ad1f0a26e19cec6"
C3_BASELINE = "6be6e84d11f3942061aa6061ea9f431155b5d29b"
C3_REQUIRED = {"c3_run.py", "c3_provider.py", "c3_inputs.py", "test_c3.py", "Dockerfile",
               "Dockerfile.dockerignore"}
BANNED = ("synthetic-local-c3-token-with-no-external-permission",
          "synthetic-local-test-token-with-no-external-permission",
          "synthetic-no-external-permission")
CREDENTIAL = re.compile(r"(?i)(authorization:\s*bearer\s+\S+|[?&]token=[A-Za-z0-9_-]{20,}|sk-[A-Za-z0-9]{20,})")

def sha(path):
    # Checkout CRLF is not an implementation change; the image receives Git LF.
    return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()

def write_json(path, value):
    path.write_text(json.dumps(value, indent=2) + "\n")

def refresh():
    c2_file = HERE/"c2-source-hashes.json"
    c2 = json.loads(c2_file.read_text())
    c2["sha256"] = {name: sha(SOURCE/name) for name in sorted(c2["sha256"])}
    c2_file.write_text(json.dumps(c2, sort_keys=True, indent=2) + "\n")
    c3_file = HERE/"c3-artifact-hashes.json"
    c3 = json.loads(c3_file.read_text())
    for key in ("sha256", "delivery_sha256"):
        c3[key] = {name: sha(HERE/name) for name in c3[key]}
    write_json(c3_file, c3)

def problems():
    found, warnings = [], []
    def check(condition, message):
        if not condition:
            found.append(message)
    c2 = json.loads((HERE/"c2-source-hashes.json").read_text())
    check(c2.get("baseline") == C2_BASELINE, "c2-source-hashes.json: baseline")
    for name, expected in c2["sha256"].items():
        path = SOURCE/name
        check(path.resolve().is_relative_to(SOURCE) and path.is_file() and sha(path) == expected,
              f"c2-source-hashes.json: {name}")
    c3 = json.loads((HERE/"c3-artifact-hashes.json").read_text())
    check(c3.get("baseline") == C3_BASELINE and C3_REQUIRED <= set(c3["sha256"]),
          "c3-artifact-hashes.json: identity")
    for key in ("sha256", "delivery_sha256"):
        for name, expected in c3[key].items():
            path = HERE/name
            check(path.resolve().is_relative_to(HERE) and path.is_file() and sha(path) == expected,
                  f"c3-artifact-hashes.json: {name}")
    lines = (HERE/"Dockerfile.dockerignore").read_text().splitlines()
    allowed = [line[1:] for line in lines if line.startswith("!") and not line.endswith("/")]
    # A whitelisted file that no longer exists ships nothing; report, do not block.
    warnings += [f"Dockerfile.dockerignore: allows missing {p}" for p in allowed if not (SOURCE/p).is_file()]
    check(all("test_c2" not in p and "evidence/" not in p for p in allowed),
          "Dockerfile.dockerignore: test or evidence file shipped")
    for name in [*c3["sha256"], "c3-artifact-hashes.json", "c2_seed.py", "c2_vectors.py",
                 "c2-source-hashes.json"]:
        check("!deploy/lsf-zeabur/" + name in lines, f"Dockerfile.dockerignore: {name}")
    check('"/app/deploy/lsf-zeabur/c3_run.py"' in (HERE/"Dockerfile").read_text(), "Dockerfile: c3_run.py entry")
    for path in HERE.glob("*.py"):
        try:
            ast.parse(path.read_text(encoding="utf-8-sig"), filename=path.name)
        except SyntaxError:
            found.append(f"syntax: {path.name}")
    for path in (HERE/"evidence").glob("c3*"):
        text = path.read_text(encoding="utf-8-sig")
        check(not any(v in text for v in BANNED) and not CREDENTIAL.search(text), f"credential: {path.name}")
    sys.path.insert(0, str(HERE))
    from c2_seed import check_known_compatible
    try:
        check_known_compatible()
    except RuntimeError:
        found.append("c2_seed.py: KNOWN_COMPATIBLE")
    return found, warnings

def main():
    if "--refresh" in sys.argv:
        refresh()
    found, warnings = problems()
    print(json.dumps(dict(result="FAIL" if found else "PASS", problems=found, warnings=warnings),
                     ensure_ascii=False, indent=2))
    return 1 if found else 0

if __name__ == "__main__":
    sys.exit(main())

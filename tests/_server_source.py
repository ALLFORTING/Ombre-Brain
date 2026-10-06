"""Effective server source text for static source-inspection tests.

server.py executes its fragments (server_dashboard_auth.py, server_digest.py,
server_maintenance_checks.py, server_assets.py, server_dashboard_api.py) in its
own namespace at fixed include points. Tests
that inspect "the server source" read this text: server.py with every
fragment's code spliced back in, in include order, where it is executed, so
assertions see the same code, in the same order, as before the split.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HEADER_END = "# --- end of fragment header ---"
_INCLUDE = re.compile(r'^_exec_server_fragment\("([A-Za-z0-9_]+\.py)"\)$')
_INCLUDE_COMMENT = re.compile(r"^# --- Fragment ([A-Za-z0-9_]+\.py): ")


def fragment_code(filename: str) -> str:
    """Return a fragment's code without its explanatory header."""
    lines = (ROOT / filename).read_text(encoding="utf-8").splitlines(keepends=True)
    markers = [index for index, line in enumerate(lines) if line.rstrip("\n") == HEADER_END]
    assert len(markers) == 1, f"{filename}: fragment header end marker missing"
    index = markers[0] + 1
    while index < len(lines) and not lines[index].strip():
        index += 1
    return "".join(lines[index:])


def included_fragments() -> list[str]:
    """Fragment filenames in the order server.py executes them."""
    server = (ROOT / "server.py").read_text(encoding="utf-8").splitlines()
    return [match.group(1) for line in server if (match := _INCLUDE.match(line))]


def effective_server_source() -> str:
    lines = (ROOT / "server.py").read_text(encoding="utf-8").splitlines(keepends=True)
    output = []
    spliced = 0
    for line in lines:
        include = _INCLUDE.match(line.rstrip("\n"))
        if include:
            comment = _INCLUDE_COMMENT.match(output[-1]) if output else None
            if comment and comment.group(1) == include.group(1):
                output.pop()
            output.append(fragment_code(include.group(1)))
            spliced += 1
            continue
        output.append(line)
    assert spliced, "no server fragment include point found in server.py"
    return "".join(output)

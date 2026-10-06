"""Effective server source text for static source-inspection tests.

server.py executes server_assets.py in its own namespace at a fixed include
point. Tests that inspect "the server source" read this text: server.py with
the fragment's code spliced back in where it is executed, so assertions see
the same code, in the same order, as before the split.
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_INCLUDE_START = "# --- Image assets and Remember-Me: server_assets.py"
_INCLUDE_END = "del _server_assets_source\n"


def _fragment_code() -> str:
    lines = (ROOT / "server_assets.py").read_text(encoding="utf-8").splitlines(keepends=True)
    index = 0
    while index < len(lines) and lines[index].startswith("#"):
        index += 1
    while index < len(lines) and not lines[index].strip():
        index += 1
    return "".join(lines[index:])


def effective_server_source() -> str:
    server = (ROOT / "server.py").read_text(encoding="utf-8")
    start = server.find(_INCLUDE_START)
    end = server.find(_INCLUDE_END, start)
    assert start != -1 and end != -1, "server_assets.py include point not found in server.py"
    return server[:start] + _fragment_code() + server[end + len(_INCLUDE_END):]

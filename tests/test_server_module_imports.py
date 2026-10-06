# ============================================================
# Test: modules split out of server.py never import server
# 测试：从 server.py 拆出去的 server_*.py 模块禁止 import server
#
# Docker starts the service with `python server.py`, so the running module is
# __main__. A split module importing server would load a second copy with its
# own MCP instance and runtime state. server.py imports the split modules,
# never the other way round.
# ============================================================

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _split_modules():
    return sorted(ROOT.glob("server_*.py"))


def _server_imports(path):
    tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "server" or alias.name.startswith("server."):
                    found.append((node.lineno, f"import {alias.name}"))
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and (node.module == "server" or (node.module or "").startswith("server.")):
                found.append((node.lineno, f"from {node.module} import ..."))
        elif isinstance(node, ast.Call):
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            if name in {"import_module", "__import__"} and node.args:
                first = node.args[0]
                if isinstance(first, ast.Constant) and first.value == "server":
                    found.append((node.lineno, f"{name}('server')"))
    return found


def test_split_modules_exist():
    assert ROOT / "server_http_security.py" in _split_modules()


def test_split_modules_never_import_server():
    offenders = {
        path.name: hits
        for path in _split_modules()
        if (hits := _server_imports(path))
    }
    assert offenders == {}

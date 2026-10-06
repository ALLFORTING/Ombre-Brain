# ============================================================
# Test: server_assets.py is a fragment executed only by server.py
# 测试：server_assets.py 只能由 server.py 在固定位置读入执行
#
# server.py reads server_assets.py where the asset code used to live and
# executes it in its own namespace. Nothing may import it, the include must
# stay at that position (tool registration order), and a missing file must
# stop startup instead of being skipped.
# ============================================================

import ast
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EXCLUDED_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__", "build", "dist"}

DEFAULT_TOOL_ORDER = [
    "rm_asset_upload_link", "rm_asset_upload_status", "rm_asset_get",
    "rm_asset_update_metadata", "rm_asset_search", "rm_asset_reindex_embeddings",
    "rm_asset_download_link", "rm_asset_view", "rm_asset_inspect",
    "digest", "related_backfill", "breath", "hold", "grow",
    "get_letter", "list_notes", "get_note", "leave_note", "dismiss_note",
    "trace", "seal_letter", "archive_session", "todos", "boot",
    "refresh_tg_summary", "pulse", "dream",
]


def _python_files():
    for path in ROOT.rglob("*.py"):
        if any(part in EXCLUDED_DIRS for part in path.relative_to(ROOT).parts):
            continue
        yield path


def _is_include_statement(node):
    return isinstance(node, ast.With) and "_SERVER_ASSETS_PATH" in ast.unparse(node)


def test_nothing_imports_server_assets():
    offenders = []
    for path in _python_files():
        tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            elif isinstance(node, ast.Call) and node.args and isinstance(node.args[0], ast.Constant):
                func = node.func
                called = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
                names = [node.args[0].value] if called in {"import_module", "__import__"} else []
            else:
                continue
            if any(str(name).split(".")[0] == "server_assets" for name in names):
                offenders.append(f"{path.relative_to(ROOT)}:{node.lineno}")
    assert offenders == []


def test_server_reads_fragment_once_with_compile_and_real_path():
    tree = ast.parse((ROOT / "server.py").read_text(encoding="utf-8-sig"))
    includes = [node for node in tree.body if _is_include_statement(node)]
    assert len(includes) == 1
    source = ast.unparse(includes[0])
    assert "compile(" in source and "'exec'" in source and "globals()" in source
    path_assignments = [
        node for node in tree.body
        if isinstance(node, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "_SERVER_ASSETS_PATH" for t in node.targets)
    ]
    assert len(path_assignments) == 1
    assert "'server_assets.py'" in ast.unparse(path_assignments[0].value)


def test_include_sits_where_the_asset_block_used_to_be():
    tree = ast.parse((ROOT / "server.py").read_text(encoding="utf-8-sig"))
    body = tree.body
    index = next(i for i, node in enumerate(body) if _is_include_statement(node))
    previous_def = next(
        node.name for node in reversed(body[:index])
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    )
    next_def = next(
        node.name for node in body[index + 1:]
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    )
    assert previous_def == "_breath_impl"
    assert next_def == "digest"


def _list_tools(tmp_path, diagnostics):
    env = os.environ.copy()
    env["OMBRE_BUCKETS_DIR"] = str(tmp_path / "buckets")
    env.pop("OMBRE_API_KEY", None)
    if diagnostics:
        env["OMBRE_DIAG_TOOLS"] = "1"
    else:
        env.pop("OMBRE_DIAG_TOOLS", None)
    script = (
        "import asyncio, json, server\n"
        "from mcp.shared.memory import create_connected_server_and_client_session\n"
        "async def main():\n"
        "    async with create_connected_server_and_client_session(server.mcp) as client:\n"
        "        print(json.dumps([tool.name for tool in (await client.list_tools()).tools]))\n"
        "asyncio.run(main())\n"
    )
    completed = subprocess.run(
        [sys.executable, "-c", script], cwd=ROOT, env=env,
        check=True, capture_output=True, text=True,
    )
    return json.loads(completed.stdout)


def test_default_tool_registration_order_is_unchanged(tmp_path):
    assert _list_tools(tmp_path, diagnostics=False) == DEFAULT_TOOL_ORDER


def test_diagnostic_tools_register_before_digest(tmp_path):
    names = _list_tools(tmp_path, diagnostics=True)
    assert len(names) == 42
    digest_index = names.index("digest")
    diagnostic = [name for name in names if name.startswith("asset_")]
    assert len(diagnostic) == 15
    assert all(names.index(name) < digest_index for name in diagnostic)
    assert [name for name in names if not name.startswith("asset_")] == DEFAULT_TOOL_ORDER


def test_missing_fragment_stops_server_import(tmp_path):
    isolated = tmp_path / "isolated"
    isolated.mkdir()
    shutil.copy2(ROOT / "server.py", isolated / "server.py")
    env = os.environ.copy()
    env["OMBRE_BUCKETS_DIR"] = str(tmp_path / "buckets")
    env.pop("OMBRE_API_KEY", None)
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(ROOT), env.get("PYTHONPATH", "")]))
    completed = subprocess.run(
        [sys.executable, "-c", "import server"], cwd=isolated, env=env,
        capture_output=True, text=True,
    )
    assert completed.returncode != 0
    assert "FileNotFoundError" in completed.stderr
    assert "server_assets.py" in completed.stderr


def test_fragment_functions_report_their_own_file_and_lines(tmp_path):
    env = os.environ.copy()
    env["OMBRE_BUCKETS_DIR"] = str(tmp_path / "buckets")
    env.pop("OMBRE_API_KEY", None)
    script = (
        "import json, server\n"
        "code = server._selected_asset_backend.__code__\n"
        "print(json.dumps([code.co_filename, code.co_firstlineno, server._selected_asset_backend.__module__]))\n"
    )
    completed = subprocess.run(
        [sys.executable, "-c", script], cwd=ROOT, env=env,
        check=True, capture_output=True, text=True,
    )
    filename, first_line, module = json.loads(completed.stdout.strip().splitlines()[-1])
    assert Path(filename).name == "server_assets.py"
    lines = (ROOT / "server_assets.py").read_text(encoding="utf-8").splitlines()
    assert lines[first_line - 1].startswith("def _selected_asset_backend(")
    assert module == "server"

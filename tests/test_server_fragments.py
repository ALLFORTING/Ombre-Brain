# ============================================================
# Test: server fragments are executed only by server.py, in place
# 测试：服务片段只能由 server.py 在原位置读入执行
#
# server.py executes server_dashboard_auth.py, server_digest.py,
# server_maintenance_checks.py, server_breath.py, server_assets.py,
# server_breath_tool.py and server_dashboard_api.py in its own namespace
# where that code used to live.
# Nothing may import them, each include must stay at its position (tool and
# route registration order), and a missing fragment must stop startup.
# ============================================================

import ast
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from tests._server_source import HEADER_END, included_fragments

ROOT = Path(__file__).resolve().parent.parent
EXCLUDED_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__", "build", "dist"}

# fragment -> (last top-level def before the include, first top-level def after it,
#              a function defined in the fragment)
FRAGMENTS = {
    "server_dashboard_auth.py": ("_exec_server_fragment", "root_redirect", "_create_session"),
    "server_digest.py": ("_format_boot_delta", "_auto_link_related", "_run_digest"),
    "server_maintenance_checks.py": ("_auto_link_related", "digest", "_detect_conflict_warning"),
    "server_breath.py": ("_auto_link_related", "digest", "_breath_impl"),
    "server_assets.py": ("_auto_link_related", "digest", "_selected_asset_backend"),
    "server_breath_tool.py": ("related_backfill", "_format_hold_created", "breath"),
    "server_dashboard_api.py": ("dream", "add_backup_v2_status_entry_middleware", "api_system_status"),
}

DEFAULT_TOOL_ORDER = [
    "rm_asset_upload_link", "rm_asset_upload_status", "rm_asset_get",
    "rm_asset_update_metadata", "rm_asset_search", "rm_asset_reindex_embeddings",
    "rm_asset_download_link", "rm_asset_view", "rm_asset_inspect",
    "digest", "related_backfill", "breath", "hold", "grow",
    "get_letter", "list_notes", "get_note", "leave_note", "dismiss_note",
    "list_revisions", "restore_revision", "trace", "seal_letter", "archive_session", "todos", "boot",
    "refresh_tg_summary", "pulse", "dream",
]


def _python_files():
    for path in ROOT.rglob("*.py"):
        if any(part in EXCLUDED_DIRS for part in path.relative_to(ROOT).parts):
            continue
        yield path


def _server_tree():
    return ast.parse((ROOT / "server.py").read_text(encoding="utf-8-sig"))


def _include_calls(tree):
    calls = []
    for index, node in enumerate(tree.body):
        if (
            isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Call)
            and getattr(node.value.func, "id", "") == "_exec_server_fragment"
        ):
            calls.append((index, node.value.args[0].value))
    return calls


def _env(tmp_path, **extra):
    env = os.environ.copy()
    env["OMBRE_BUCKETS_DIR"] = str(tmp_path / "buckets")
    env.pop("OMBRE_API_KEY", None)
    env.pop("OMBRE_DIAG_TOOLS", None)
    env.update(extra)
    return env


def test_include_order_and_fragment_set():
    assert included_fragments() == list(FRAGMENTS)
    assert sorted(str(path.name) for path in ROOT.glob("server_*.py") if HEADER_END in path.read_text(encoding="utf-8")) == sorted(FRAGMENTS)


def test_nothing_imports_a_fragment():
    stems = {name[:-3] for name in FRAGMENTS}
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
            if any(str(name).split(".")[0] in stems for name in names):
                offenders.append(f"{path.relative_to(ROOT)}:{node.lineno}")
    assert offenders == []


def test_fragments_execute_once_each_through_compile_with_real_path():
    tree = _server_tree()
    assert [name for _, name in _include_calls(tree)] == list(FRAGMENTS)
    helper = next(
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "_exec_server_fragment"
    )
    source = ast.unparse(helper)
    assert "compile(source.read(), path, 'exec')" in source
    assert "globals()" in source
    assert "os.path.dirname(os.path.abspath(__file__))" in source
    assert "try" not in source and "except" not in source


@pytest.mark.parametrize("fragment", list(FRAGMENTS))
def test_each_include_sits_where_its_block_used_to_be(fragment):
    expected_previous, expected_next, _ = FRAGMENTS[fragment]
    tree = _server_tree()
    index = dict((name, i) for i, name in _include_calls(tree))[fragment]
    previous_def = next(
        node.name for node in reversed(tree.body[:index])
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    )
    next_def = next(
        (node.name for node in tree.body[index + 1:]
         if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))),
        None,
    )
    assert previous_def == expected_previous
    assert next_def == expected_next


def _list_tools(tmp_path, diagnostics):
    extra = {"OMBRE_DIAG_TOOLS": "1"} if diagnostics else {}
    script = (
        "import asyncio, json, server\n"
        "from mcp.shared.memory import create_connected_server_and_client_session\n"
        "async def main():\n"
        "    async with create_connected_server_and_client_session(server.mcp) as client:\n"
        "        print(json.dumps([tool.name for tool in (await client.list_tools()).tools]))\n"
        "asyncio.run(main())\n"
    )
    completed = subprocess.run(
        [sys.executable, "-c", script], cwd=ROOT, env=_env(tmp_path, **extra),
        check=True, capture_output=True, text=True,
    )
    return json.loads(completed.stdout.strip().splitlines()[-1])


def test_default_tool_registration_order_is_unchanged(tmp_path):
    assert _list_tools(tmp_path, diagnostics=False) == DEFAULT_TOOL_ORDER


def test_diagnostic_tools_register_before_digest(tmp_path):
    names = _list_tools(tmp_path, diagnostics=True)
    assert len(names) == 44
    digest_index = names.index("digest")
    diagnostic = [name for name in names if name.startswith("asset_")]
    assert len(diagnostic) == 15
    assert all(names.index(name) < digest_index for name in diagnostic)
    assert [name for name in names if not name.startswith("asset_")] == DEFAULT_TOOL_ORDER


@pytest.mark.parametrize("fragment", list(FRAGMENTS))
def test_missing_fragment_stops_server_import(tmp_path, fragment):
    isolated = tmp_path / "isolated"
    isolated.mkdir()
    shutil.copy2(ROOT / "server.py", isolated / "server.py")
    for other in FRAGMENTS:
        if other != fragment:
            shutil.copy2(ROOT / other, isolated / other)
    env = _env(tmp_path)
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(ROOT), env.get("PYTHONPATH", "")]))
    completed = subprocess.run(
        [sys.executable, "-c", "import server"], cwd=isolated, env=env,
        capture_output=True, text=True,
    )
    assert completed.returncode != 0
    assert "FileNotFoundError" in completed.stderr
    assert fragment in completed.stderr


def test_fragment_functions_report_their_own_file_and_lines(tmp_path):
    names = {fragment: function for fragment, (_, _, function) in FRAGMENTS.items()}
    script = (
        "import json, server\n"
        f"names = {names!r}\n"
        "out = {}\n"
        "for fragment, name in names.items():\n"
        "    function = getattr(server, name)\n"
        "    code = function.__code__\n"
        "    out[fragment] = [code.co_filename, code.co_firstlineno, function.__module__,\n"
        "                     function.__globals__ is vars(server)]\n"
        "print(json.dumps(out))\n"
    )
    completed = subprocess.run(
        [sys.executable, "-c", script], cwd=ROOT, env=_env(tmp_path),
        check=True, capture_output=True, text=True,
    )
    result = json.loads(completed.stdout.strip().splitlines()[-1])
    for fragment, function in names.items():
        filename, first_line, module, shares_globals = result[fragment]
        assert Path(filename).name == fragment
        lines = (ROOT / fragment).read_text(encoding="utf-8").splitlines()
        stripped = lines[first_line - 1].lstrip()
        assert stripped.startswith((f"def {function}(", f"async def {function}(", "@"))
        assert module == "server"
        assert shares_globals is True

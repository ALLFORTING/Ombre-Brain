"""Versioned AST audit for production persistence write primitives."""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


COVERAGE_SCHEMA_VERSION = 3

# Function-level registrations only. Startup entries execute before an
# unregistered capture controller can exist; transient entries are excluded
# capture staging/upload files and never formal bucket state.
REGISTERED_BOUNDARIES: dict[str, dict[str, str]] = {
    "add_timestamps.py": {"main": "standalone_maintenance_script"},
    "migrate_to_domains.py": {"migrate": "standalone_maintenance_script"},
    "reclassify_api.py": {"reclassify": "standalone_maintenance_script"},
    "reclassify_domains.py": {
        "update_domain_in_file": "standalone_maintenance_script",
        "reclassify": "standalone_maintenance_script",
    },
    "write_memory.py": {"write_memory": "standalone_maintenance_script"},
    "asset_dashboard.py": {
        "on_part_data": "guarded_caller_only",
        "parse_upload": "guarded_caller_only",
        "create_asset": "guarded_caller_only",
    },
    "offline_backup_bundle.py": {
        "prepare_backup_workspace": "isolated_workspace_factory",
        "_capture_source_into_bundle": "frozen_external_or_workspace_capture",
        "capture_external_source": "invalid_lease_bundle_cleanup",
        "verify_bundle": "isolated_temporary_verification",
        "restore_bundle": "isolated_restore_and_no_replace_publish",
        "write_test_private_key": "test_key_only",
        "write_test_public_key": "test_key_only",
        "_copy_regular_file_stable": "capture_staging",
        "_capture_source": "capture_staging",
        "_snapshot_sqlite": "capture_staging",
        "_build_archive": "capture_staging",
        "_encrypt_archive": "encrypted_bundle_staging_and_publish",
        "write_bounded": "capture_staging",
        "_decrypt_and_validate": "isolated_temporary_restore",
        "_validate_and_extract_archive": "isolated_temporary_restore",
        "_atomic_write_json": "workspace_manifest_factory",
        "_exclusive_key_write": "test_key_only",
        "_publish_file_no_replace": "formal_bundle_no_replace",
        "_exclusive_operation_lock": "workspace_restore_lock",
        "_publish_directory_no_replace": "formal_restore_no_replace",
        "_fsync_file": "formal_bundle_no_replace",
        "_safe_rmtree": "contained_temporary_cleanup",
        "_remove_file": "contained_temporary_cleanup",
    },
    "production_backup_capture.py": {
        "__init__": "permission_tightening_only",
        "acknowledge": "owned_encrypted_bundle_cleanup",
        "cleanup_stale": "owned_encrypted_bundle_cleanup",
        "_fail_and_cleanup": "owned_encrypted_bundle_cleanup",
        "body": "read_only_encrypted_stream",
        "receive_encrypted_bundle": "synthetic_transport_no_replace",
    },
    "bucket_manager.py": {
        "__init__": "startup_initialization",
        "_init_history_db": "startup_initialization",
        "record_history": "guarded_mutation",
        "record_letter": "guarded_mutation",
        "seal_letter": "guarded_mutation",
        "create": "guarded_async_mutation",
        "_move_bucket": "guarded_mutation",
        "update": "guarded_async_mutation",
        "delete": "guarded_async_mutation",
        "touch": "guarded_optional_async_mutation",
        "set_dormant": "guarded_async_mutation",
        "_time_ripple": "guarded_optional_async_mutation",
        "archive": "guarded_async_mutation",
        "clean_display_aliases": "guarded_async_mutation",
        "get_letters": "dynamic_sql_read_only",
    },
    "asset_store.py": {
        "__init__": "startup_initialization",
        "_init_db": "startup_initialization",
        "_create_temp_path_unchecked": "guarded_caller_only",
        "create_temp_path": "guarded_mutation",
        "_clean_image": "guarded_caller_only",
        "_prepare_candidate": "guarded_caller_only",
        "persist_upload": "guarded_mutation",
        "_persist_upload_unchecked": "guarded_mutation",
        "update_metadata": "guarded_mutation",
        "_update_metadata_unchecked": "guarded_mutation",
        "delete": "guarded_mutation",
        "_delete_unchecked": "guarded_mutation",
        "_tags_for_assets": "dynamic_sql_read_only",
        "search": "dynamic_sql_read_only",
    },
    "asset_embedding_index.py": {
        "_init_db": "startup_initialization",
        "delete": "guarded_mutation",
        "index_asset": "inline_writer_scope_after_network",
    },
    "embedding_engine.py": {
        "_init_db": "startup_initialization",
        "_store_embedding": "guarded_mutation_after_network",
        "delete_embedding": "guarded_mutation",
    },
    "dehydrator.py": {
        "_init_cache_db": "startup_initialization",
        "_set_cached_summary": "guarded_optional_mutation_after_network",
        "invalidate_cache": "guarded_mutation",
    },
    "asset_migration_state.py": {
        "__enter__": "manual_writer_scope",
        "__exit__": "manual_writer_scope",
        "_initialize": "startup_initialization",
        "_finalize_write": "manual_writer_scope",
        "acquire_freeze": "guarded_mutation",
        "renew_freeze": "guarded_mutation",
        "release_freeze": "guarded_mutation",
        "create_checkpoint": "guarded_mutation",
        "set_checkpoint_status": "guarded_mutation",
        "record_asset_success": "guarded_mutation",
        "_connect": "dynamic_sql_read_only",
    },
    "import_memory.py": {
        "save": "guarded_mutation",
    },
        "server.py": {
        "<module>": "startup_initialization",
        "_save_password_hash": "guarded_mutation",
        "_atomic_write_auth_payload": "guarded_caller_only",
        "_write_fd_bytes": "guarded_caller_only",
        "_publish_auth_payload_exclusive": "guarded_caller_only",
        "_create_auth_file_if_absent": "guarded_caller_only",
        "_record_emotion_snapshot": "guarded_mutation",
        "api_config_update": "guarded_http_mutation",
        "_write_env_var": "guarded_mutation",
        "_asset_begin_ingest_upload": "excluded_transient_upload",
        "_asset_ingest_chunk_data": "excluded_transient_upload",
        "_asset_finish_ingest_upload": "excluded_transient_upload",
        "_asset_abort_ingest_upload": "excluded_transient_upload",
        "_asset_cleanup_expired_ingest_uploads": "excluded_transient_upload",
        "_asset_cleanup_browser_uploads": "excluded_transient_upload",
        "_asset_stream_browser_upload": "excluded_transient_upload",
        "_rm_delete_upload_temp_path": "excluded_transient_upload",
        "rm_asset_upload_route": "excluded_transient_upload_then_guarded_store",
        "_rm_persist_remember_me_upload": "excluded_transient_upload_then_guarded_store",
        "api_import_upload": "excluded_transient_import_upload",
        "_run_import": "guarded_storage_components",
        "api_import_review": "excluded_transient_import_upload_cleanup",
    },
    "remember_me_import_adapter.py": {
        "__init__": "guarded_caller_only",
    },
    "remember_me_migration_rehearsal.py": {
        "prepare_rehearsal_workspace": "isolated_offline_workspace",
        "_atomic_write_json": "isolated_offline_workspace",
    },
    "scripts/backup_v2_key_tool.py": {
        "generate": "isolated_offline_key_workspace",
        "_exclusive_write": "offline_key_no_replace_publish",
    },
    "utils.py": {"load_config": "startup_initialization"},
}

GUARDED_CALLERS: dict[tuple[str, str], set[tuple[str, str]]] = {
    ("asset_dashboard.py", "on_part_data"): {("asset_dashboard.py", "parse_upload")},
    ("asset_dashboard.py", "parse_upload"): {("server.py", "api_assets")},
    ("asset_dashboard.py", "create_asset"): {("server.py", "api_assets")},
    ("asset_store.py", "_create_temp_path_unchecked"): {
        ("asset_store.py", "create_temp_path"),
        ("asset_store.py", "_clean_image"),
    },
    ("asset_store.py", "_clean_image"): {("asset_store.py", "_prepare_candidate")},
    ("asset_store.py", "_prepare_candidate"): {("asset_store.py", "_persist_upload_unchecked")},
    ("remember_me_import_adapter.py", "__init__"): {
        ("remember_me_migration_runner.py", "run_migration_batch"),
        ("remember_me_migration_rehearsal.py", "run_rehearsal"),
        ("remember_me_migration_runner.py", "__init__"),
        ("remember_me_migration_rehearsal.py", "__init__"),
    },
}

# Exact call-site exemptions for methods whose names overlap Path mutation
# primitives. Line anchoring makes nearby code movement or any new call fail
# until that individual call is audited again.
NON_PATH_CALL_ALLOWLIST: dict[tuple[str, str, int, str], str] = {
    ("asset_dashboard.py", "resolve_image", 440, "Image.open"): "pillow_image_read",
    ("asset_migration_state.py", "_now", 313, "value.replace"): "datetime_timezone",
    ("asset_migration_state.py", "inspect_existing_migration_state", 1107, "now.replace"): "datetime_timezone",
    ("asset_migration_state.py", "_parse_timestamp", 1177, "replace"): "datetime_timezone",
    ("asset_store.py", "_parse_iso8601", 250, "parsed.replace"): "datetime_timezone",
    ("asset_store.py", "_parse_iso8601", 257, "raw.replace"): "string_normalization",
    ("asset_store.py", "_parse_iso8601", 261, "parsed.replace"): "datetime_timezone",
    ("asset_store.py", "_row_datetime", 266, "replace"): "datetime_timezone",
    ("asset_store.py", "_row_datetime", 268, "parsed.replace"): "datetime_timezone",
    ("maintenance_write_gate.py", "freeze", 187, "reason.replace"): "string_validation",
    ("offline_backup_bundle.py", "_timestamp", 2245, "value.replace"): "datetime_timezone",
    ("production_backup_capture.py", "_now", 700, "value.replace"): "datetime_timezone",
    ("remember_me_core_adapter.py", "_normalize_timestamp", 504, "replace"): "datetime_timezone",
    ("remember_me_core_adapter.py", "_normalize_timestamp", 508, "parsed.replace"): "datetime_timezone",
    ("remember_me_migration_acceptance.py", "_timestamp", 1435, "value.replace"): "datetime_timezone",
    ("remember_me_mcp_presenter.py", "_verified_image", 386, "Image.open"): "pillow_image_read",
    ("remember_me_vector_provider.py", "_normalized_backend", 57, "replace"): "string_normalization",
    ("server.py", "breath_hook", 1006, "bucket_mgr.touch"): "incidental_bucket_activation",
    ("server.py", "dream_hook", 1054, "bucket_mgr.touch"): "incidental_bucket_activation",
    ("server.py", "_dream_summary_line", 1180, "replace"): "string_formatting",
    ("server.py", "_normalize_todos", 1308, "text.replace"): "string_normalization",
    ("server.py", "_days_since", 1619, "dt.replace"): "datetime_timezone",
    ("server.py", "_breath_impl", 2210, "bucket_mgr.touch"): "incidental_bucket_activation",
    ("server.py", "_breath_impl", 2244, "bucket_mgr.touch"): "incidental_bucket_activation",
    ("server.py", "_breath_impl", 2310, "bucket_mgr.touch"): "incidental_bucket_activation",
    ("server.py", "_breath_impl", 2451, "bucket_mgr.touch"): "incidental_bucket_activation",
    ("server.py", "_attachment_probe_scan", 2592, "replace"): "string_normalization",
    ("server.py", "_rm_verified_view_image", 3680, "Image.open"): "pillow_image_read",
    ("server.py", "hold", 4934, "replace"): "string_normalization",
    ("server.py", "boot", 5457, "replace"): "string_normalization",
    ("server.py", "dream", 5659, "bucket_mgr.touch"): "incidental_bucket_activation",
    ("server.py", "dream", 5685, "bucket_mgr.touch"): "incidental_bucket_activation",
    ("utils.py", "apply_display_aliases", 27, "text.replace"): "string_alias_replacement",
}

_CALL_NAMES = {
    "commit",
    "executemany",
    "mkdir",
    "makedirs",
    "rmtree",
    "unlink",
    "write",
    "writelines",
    "write_bytes",
    "write_text",
    "truncate",
}
_SQL_PREFIXES = ("INSERT", "UPDATE", "DELETE", "CREATE", "ALTER", "DROP", "REPLACE")
_READ_ONLY_OS_FLAGS = {"O_RDONLY", "O_BINARY", "O_CLOEXEC", "O_NOFOLLOW"}


@dataclass(frozen=True)
class WriteCoverageIssue:
    filename: str
    function: str
    line: int
    primitive: str


class _WriteVisitor(ast.NodeVisitor):
    def __init__(self, filename: str) -> None:
        self.filename = filename
        self.stack: list[str] = []
        self.hits: list[WriteCoverageIssue] = []
        self.functions: dict[str, ast.FunctionDef | ast.AsyncFunctionDef] = {}

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self.functions[node.name] = node
        self.stack.append(node.name)
        self.generic_visit(node)
        self.stack.pop()

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Call(self, node: ast.Call) -> None:
        primitive = self._primitive(node)
        function = self.stack[-1] if self.stack else "<module>"
        allowed = (
            self.filename,
            function,
            node.lineno,
            _call_name(node.func),
        ) in NON_PATH_CALL_ALLOWLIST
        if primitive and not allowed:
            self.hits.append(WriteCoverageIssue(
                self.filename,
                function,
                node.lineno,
                primitive,
            ))
        self.generic_visit(node)

    def _primitive(self, node: ast.Call) -> str | None:
        name = _call_name(node.func)
        if name == "os.open":
            flags = node.args[1] if len(node.args) > 1 else None
            return None if _os_flags_are_read_only(flags) else "os.open:write_or_dynamic"
        if name in {"open", "Path.open"} or name.endswith(".open"):
            mode = _open_mode(node, bound=name != "open")
            if mode is None:
                return "open:dynamic"
            return f"open:{mode}" if _mode_may_write(mode) else None
        if name.endswith((".execute", ".executemany", ".executescript")):
            statement = _constant_string(node.args[0]) if node.args else None
            if statement is None:
                return "sqlite_dynamic"
            if statement.lstrip().upper().startswith(_SQL_PREFIXES):
                return "sqlite_dml"
        if name in {
            "os.remove", "os.rename", "os.replace",
            "os.link", "os.symlink",
            "shutil.move", "shutil.copy", "shutil.copy2", "shutil.copyfile",
            "shutil.copytree", "shutil.rmtree",
        }:
            return name
        if isinstance(node.func, ast.Attribute) and node.func.attr in {
            "touch", "rename", "replace", "unlink", "symlink_to", "hardlink_to",
        }:
            return node.func.attr
        if name.endswith(("_path.rename", "_path.replace")):
            return name.rsplit(".", 1)[-1]
        leaf = name.rsplit(".", 1)[-1]
        return leaf if leaf in _CALL_NAMES else None


def scan_registered_write_coverage(root: str | Path) -> list[WriteCoverageIssue]:
    """Discover every production module, then validate each write boundary."""
    root_path = Path(root)
    issues: list[WriteCoverageIssue] = []
    visitors: dict[str, _WriteVisitor] = {}
    for path in _production_python_files(root_path):
        filename = path.relative_to(root_path).as_posix()
        visitor = _visitor(path.read_text(encoding="utf-8"), filename)
        visitors[filename] = visitor
        registrations = REGISTERED_BOUNDARIES.get(filename, {})
        for hit in visitor.hits:
            reason = registrations.get(hit.function)
            if reason is None:
                issues.append(hit)
                continue
            if not _boundary_shape_valid(visitor, hit.function, reason):
                issues.append(WriteCoverageIssue(
                    filename, hit.function, hit.line, f"guard_missing:{reason}"
                ))
    issues.extend(_validate_guarded_callers(visitors))
    return issues


def scan_unregistered_source(source: str, filename: str = "synthetic.py") -> list[WriteCoverageIssue]:
    return _scan(source, filename)


def scan_registered_source(source: str, filename: str) -> list[WriteCoverageIssue]:
    """Validate one registered production module, including guard structure."""
    visitor = _visitor(source, filename)
    registrations = REGISTERED_BOUNDARIES.get(filename, {})
    issues: list[WriteCoverageIssue] = []
    for hit in visitor.hits:
        reason = registrations.get(hit.function)
        if reason is None or not _boundary_shape_valid(
            visitor, hit.function, reason or ""
        ):
            issues.append(hit if reason is None else WriteCoverageIssue(
                filename, hit.function, hit.line, f"guard_missing:{reason}"
            ))
    return issues


def _scan(source: str, filename: str) -> list[WriteCoverageIssue]:
    return _visitor(source, filename).hits


def _visitor(source: str, filename: str) -> _WriteVisitor:
    visitor = _WriteVisitor(filename)
    visitor.visit(ast.parse(source, filename=filename))
    return visitor


def _production_python_files(root: Path) -> Iterable[Path]:
    excluded = {"tests", ".git", ".venv", "venv", "build", "dist", "__pycache__"}
    for path in sorted(root.rglob("*.py")):
        relative = path.relative_to(root)
        if any(part in excluded or part.startswith(".tmp") for part in relative.parts):
            continue
        yield path


def _boundary_shape_valid(
    visitor: _WriteVisitor,
    function: str,
    reason: str,
) -> bool:
    node = visitor.functions.get(function)
    if function == "<module>":
        return reason == "startup_initialization"
    if node is None:
        return False
    decorators = {_call_name(item.func if isinstance(item, ast.Call) else item).rsplit(".", 1)[-1]
                  for item in node.decorator_list}
    if reason in {"guarded_mutation", "guarded_mutation_after_network"}:
        return "guarded_mutation" in decorators
    if reason == "guarded_async_mutation":
        return "guarded_async_mutation" in decorators
    if reason == "guarded_optional_mutation_after_network":
        return "guarded_optional_mutation" in decorators
    if reason == "guarded_optional_async_mutation":
        return "guarded_optional_async_mutation" in decorators
    if reason == "guarded_http_mutation":
        return "guarded_http_mutation" in decorators
    if reason in {"manual_writer_scope", "inline_writer_scope_after_network"}:
        return _contains_writer_scope(node) or function in {"__exit__", "_finalize_write"}
    if reason == "startup_initialization":
        return function in {"__init__", "_initialize", "load_config"} or function.startswith("_init")
    if reason == "standalone_maintenance_script":
        return function in {"main", "migrate", "reclassify", "update_domain_in_file", "write_memory"}
    return reason in {
        "guarded_caller_only", "isolated_workspace_factory",
        "frozen_external_or_workspace_capture", "invalid_lease_bundle_cleanup",
        "isolated_temporary_verification", "isolated_restore_and_no_replace_publish",
        "test_key_only", "capture_staging", "encrypted_bundle_staging_and_publish",
        "isolated_temporary_restore", "workspace_manifest_factory",
        "formal_bundle_no_replace", "workspace_restore_lock", "formal_restore_no_replace",
        "contained_temporary_cleanup", "permission_tightening_only",
        "owned_encrypted_bundle_cleanup", "owned_encrypted_bundle_limit_cleanup",
        "read_only_encrypted_stream", "synthetic_transport_no_replace",
        "excluded_transient_upload", "excluded_transient_import_upload",
        "excluded_transient_import_upload_cleanup",
        "excluded_transient_upload_then_guarded_store", "guarded_storage_components",
        "isolated_offline_workspace",
        "isolated_offline_key_workspace", "offline_key_no_replace_publish",
        "dynamic_sql_read_only",
    }


def _contains_writer_scope(node: ast.AST) -> bool:
    for child in ast.walk(node):
        if isinstance(child, ast.Call) and _call_name(child.func).endswith(
            ("writer_scope", "async_writer_scope")
        ):
            return True
        if isinstance(child, (ast.With, ast.AsyncWith)):
            for item in child.items:
                call = item.context_expr
                if isinstance(call, ast.Call) and _call_name(call.func).endswith(
                    ("writer_scope", "async_writer_scope")
                ):
                    return True
    return False


def _validate_guarded_callers(
    visitors: dict[str, _WriteVisitor],
) -> list[WriteCoverageIssue]:
    issues: list[WriteCoverageIssue] = []
    for (target_file, target_function), allowed in GUARDED_CALLERS.items():
        found = False
        for filename, visitor in visitors.items():
            for function, node in visitor.functions.items():
                for child in ast.walk(node):
                    if not isinstance(child, ast.Call):
                        continue
                    call_name = _call_name(child.func)
                    if target_function == "__init__" and _is_super_init_call(child):
                        continue
                    if call_name.rsplit(".", 1)[-1] != target_function:
                        continue
                    found = True
                    if (filename, function) not in allowed:
                        issues.append(WriteCoverageIssue(
                            filename, function, child.lineno,
                            f"unguarded_caller:{target_file}:{target_function}",
                        ))
    return issues


def _call_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _call_name(node.value)
        return f"{prefix}.{node.attr}" if prefix else node.attr
    return ""


def _is_super_init_call(node: ast.Call) -> bool:
    if not isinstance(node.func, ast.Attribute) or node.func.attr != "__init__":
        return False
    receiver = node.func.value
    return (
        isinstance(receiver, ast.Call)
        and isinstance(receiver.func, ast.Name)
        and receiver.func.id == "super"
    )


def _open_mode(node: ast.Call, *, bound: bool) -> str | None:
    mode_index = 0 if bound else 1
    if len(node.args) > mode_index:
        return _constant_string(node.args[mode_index])
    for keyword in node.keywords:
        if keyword.arg == "mode":
            return _constant_string(keyword.value)
    return "r"


def _mode_may_write(mode: str) -> bool:
    return any(marker in mode.casefold() for marker in ("w", "a", "x", "+"))


def _os_flags_are_read_only(node: ast.AST | None) -> bool:
    if isinstance(node, ast.Constant) and isinstance(node.value, int):
        return node.value == 0
    if isinstance(node, ast.Attribute):
        return node.attr in _READ_ONLY_OS_FLAGS
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        return _os_flags_are_read_only(node.left) and _os_flags_are_read_only(node.right)
    return False


def _constant_string(node: ast.AST) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None

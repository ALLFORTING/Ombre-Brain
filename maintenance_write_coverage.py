"""Versioned AST audit for production persistence write primitives."""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


COVERAGE_SCHEMA_VERSION = 1

# Function-level registrations only. Startup entries execute before an
# unregistered capture controller can exist; transient entries are excluded
# capture staging/upload files and never formal bucket state.
REGISTERED_BOUNDARIES: dict[str, dict[str, str]] = {
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
        "_decrypt_and_validate": "isolated_temporary_restore",
        "_validate_and_extract_archive": "isolated_temporary_restore",
        "_atomic_write_json": "workspace_manifest_factory",
        "_exclusive_key_write": "test_key_only",
        "_publish_file_no_replace": "formal_bundle_no_replace",
        "_exclusive_operation_lock": "workspace_restore_lock",
        "_publish_directory_no_replace": "formal_restore_no_replace",
        "_safe_rmtree": "contained_temporary_cleanup",
        "_remove_file": "contained_temporary_cleanup",
    },
    "production_backup_capture.py": {
        "__init__": "permission_tightening_only",
        "acknowledge": "owned_encrypted_bundle_cleanup",
        "cleanup_stale": "owned_encrypted_bundle_cleanup",
        "_finish_bundle": "owned_encrypted_bundle_limit_cleanup",
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
        "touch": "guarded_async_mutation",
        "set_dormant": "guarded_async_mutation",
        "_time_ripple": "guarded_async_mutation",
        "archive": "guarded_async_mutation",
        "clean_display_aliases": "guarded_async_mutation",
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
        "_set_cached_summary": "guarded_mutation_after_network",
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
    },
    "import_memory.py": {
        "save": "guarded_mutation",
    },
    "server.py": {
        "<module>": "startup_initialization",
        "_save_password_hash": "guarded_mutation",
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
        "api_import_upload": "excluded_transient_import_upload",
        "_run_import": "guarded_storage_components",
        "api_import_review": "excluded_transient_import_upload_cleanup",
    },
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
}
_SQL_PREFIXES = ("INSERT", "UPDATE", "DELETE", "CREATE", "ALTER", "DROP", "REPLACE")
_WRITE_MODES = {"w", "wb", "wt", "a", "ab", "at", "x", "xb", "xt", "r+", "w+", "a+"}


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

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self.stack.append(node.name)
        self.generic_visit(node)
        self.stack.pop()

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Call(self, node: ast.Call) -> None:
        primitive = self._primitive(node)
        if primitive:
            self.hits.append(WriteCoverageIssue(
                self.filename,
                self.stack[-1] if self.stack else "<module>",
                node.lineno,
                primitive,
            ))
        self.generic_visit(node)

    def _primitive(self, node: ast.Call) -> str | None:
        name = _call_name(node.func)
        if name in {"open", "Path.open"} or name.endswith(".open"):
            mode = _open_mode(node)
            return f"open:{mode}" if mode in _WRITE_MODES else None
        if name.endswith(".execute") and node.args:
            statement = _constant_string(node.args[0])
            if statement and statement.lstrip().upper().startswith(_SQL_PREFIXES):
                return "sqlite_dml"
        if name in {"os.remove", "os.rename", "os.replace"}:
            return name
        if name.endswith(("_path.rename", "_path.replace")):
            return name.rsplit(".", 1)[-1]
        leaf = name.rsplit(".", 1)[-1]
        return leaf if leaf in _CALL_NAMES else None


def scan_registered_write_coverage(root: str | Path) -> list[WriteCoverageIssue]:
    root_path = Path(root)
    issues: list[WriteCoverageIssue] = []
    for filename, registrations in REGISTERED_BOUNDARIES.items():
        path = root_path / filename
        visitor = _scan(path.read_text(encoding="utf-8"), filename)
        issues.extend(hit for hit in visitor if hit.function not in registrations)
    return issues


def scan_unregistered_source(source: str, filename: str = "synthetic.py") -> list[WriteCoverageIssue]:
    return _scan(source, filename)


def _scan(source: str, filename: str) -> list[WriteCoverageIssue]:
    visitor = _WriteVisitor(filename)
    visitor.visit(ast.parse(source, filename=filename))
    return visitor.hits


def _call_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _call_name(node.value)
        return f"{prefix}.{node.attr}" if prefix else node.attr
    return ""


def _open_mode(node: ast.Call) -> str:
    if len(node.args) > 1:
        return _constant_string(node.args[1]) or ""
    for keyword in node.keywords:
        if keyword.arg == "mode":
            return _constant_string(keyword.value) or ""
    return "r"


def _constant_string(node: ast.AST) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None

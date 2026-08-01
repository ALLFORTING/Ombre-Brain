"""Explicit offline workspace runner for synthetic migration rehearsals."""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from importlib import metadata
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import sqlite3
import subprocess
import sys
import time
from typing import Any

from asset_migration_state import (
    HostMigrationState,
    HostMigrationStateError,
    canonical_path_identity,
    inspect_existing_migration_state,
)
from asset_store import AssetStore
from remember_me_import_adapter import (
    LegacyAssetImportAdapter,
    LegacyAssetImportAdapterError,
    _create_legacy_asset_import_offline_context,
)
from remember_me_migration_acceptance import (
    LocalMigrationAcceptanceCoordinator,
    LocalMigrationAcceptanceReport,
    LegacyRmReconciler,
    MigrationRecoveryDiagnostic,
    MigrationRecoveryDiagnostics,
)
from remember_me_migration_runner import MIGRATION_KEY


WORKSPACE_SCHEMA_VERSION = 1
REPORT_SCHEMA_VERSION = 1
MANIFEST_NAME = "rehearsal-manifest.json"
MARKER_NAME = ".ombre-stage8h-e-rehearsal"
REPORT_NAME = "rehearsal-report.json"
DEFAULT_BATCH_SIZE = 100
DEFAULT_MAX_BATCHES = 10_000
DEFAULT_LEASE_TTL_SECONDS = 60
MIN_FREE_SPACE_BYTES = 10 * 1024 * 1024
EXPECTED_REMEMBER_ME_VERSION = "0.1.0.dev7"
_WORKSPACE_ID_PATTERN = re.compile(r"[0-9a-f]{32}")
_NONCE_PATTERN = re.compile(r"[0-9a-f]{64}")
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")
_GIT_SHA_PATTERN = re.compile(r"[0-9a-f]{40}")
_FIXED_PATHS = {
    "source": "legacy",
    "target": "remember-me",
    "state": "state",
    "reports": "reports",
}
_EXIT_CODES = {
    "success": 0,
    "preflight_failed": 2,
    "migration_blocked": 3,
    "migration_failed": 4,
    "acceptance_failed": 5,
    "source_changed": 6,
    "lease_lost": 7,
    "workspace_invalid": 8,
    "internal_error": 9,
}


class RehearsalError(RuntimeError):
    """Stable public failure that never includes a filesystem path."""

    def __init__(self, status: str):
        self.status = status
        super().__init__(status)


@dataclass(frozen=True)
class RehearsalWorkspace:
    root: Path
    workspace_id: str
    nonce: str
    source_root: Path
    target_root: Path
    state_root: Path
    reports_root: Path

    @property
    def state_db(self) -> Path:
        return self.state_root / "migration.sqlite3"

    @property
    def report_path(self) -> Path:
        return self.reports_root / REPORT_NAME


@dataclass(frozen=True)
class RehearsalPreflight:
    status: str
    workspace_id: str
    source_identity: str
    target_identity: str
    legacy_asset_count: int
    legacy_stored_blob_bytes: int
    unsupported_asset_count: int
    corrupt_record_count: int
    free_space_bytes: int
    remember_me_version: str
    ob_commit_sha: str
    active_writer_detected: bool
    target_initially_empty: bool
    issue_codes: tuple[str, ...]

    @property
    def passed(self) -> bool:
        return self.status == "success"

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["issue_codes"] = list(self.issue_codes)
        return payload


def prepare_rehearsal_workspace(path: str | Path) -> RehearsalWorkspace:
    """Create only the fixed rehearsal directories and identity markers."""
    root = _validate_prepare_root(path)
    if root.exists():
        if not root.is_dir() or any(root.iterdir()):
            raise RehearsalError("workspace_invalid")
    root.mkdir(parents=True, exist_ok=True)
    for relative in _FIXED_PATHS.values():
        (root / relative).mkdir()
    workspace_id = secrets.token_hex(16)
    nonce = secrets.token_hex(32)
    manifest = {
        "schema_version": WORKSPACE_SCHEMA_VERSION,
        "workspace_id": workspace_id,
        "nonce": nonce,
        "paths": dict(_FIXED_PATHS),
        "created_at": _timestamp(),
    }
    marker = {"workspace_id": workspace_id, "nonce": nonce}
    _atomic_write_json(root / MANIFEST_NAME, manifest)
    _atomic_write_json(root / MARKER_NAME, marker)
    return load_rehearsal_workspace(root)


def load_rehearsal_workspace(path: str | Path) -> RehearsalWorkspace:
    """Validate marker, manifest, canonical paths, and containment."""
    try:
        root = Path(path)
        if not root.is_absolute():
            raise RehearsalError("workspace_invalid")
        if _path_contains_reparse_point(root):
            raise RehearsalError("workspace_invalid")
        root = root.resolve(strict=True)
        _validate_root_location(root)
        manifest = json.loads(
            (root / MANIFEST_NAME).read_text(encoding="utf-8")
        )
        marker = json.loads(
            (root / MARKER_NAME).read_text(encoding="utf-8")
        )
    except RehearsalError:
        raise
    except (OSError, ValueError, TypeError) as exc:
        raise RehearsalError("workspace_invalid") from exc
    if (
        not isinstance(manifest, dict)
        or manifest.get("schema_version") != WORKSPACE_SCHEMA_VERSION
        or _WORKSPACE_ID_PATTERN.fullmatch(
            str(manifest.get("workspace_id", ""))
        )
        is None
        or _NONCE_PATTERN.fullmatch(str(manifest.get("nonce", ""))) is None
        or manifest.get("paths") != _FIXED_PATHS
        or marker
        != {
            "workspace_id": manifest["workspace_id"],
            "nonce": manifest["nonce"],
        }
    ):
        raise RehearsalError("workspace_invalid")
    roots = {
        key: (root / relative).resolve(strict=True)
        for key, relative in _FIXED_PATHS.items()
    }
    _validate_workspace_paths(root, roots)
    return RehearsalWorkspace(
        root=root,
        workspace_id=manifest["workspace_id"],
        nonce=manifest["nonce"],
        source_root=roots["source"],
        target_root=roots["target"],
        state_root=roots["state"],
        reports_root=roots["reports"],
    )


def preflight_rehearsal(path: str | Path) -> RehearsalPreflight:
    """Inspect one prepared workspace without changing any file or lease."""
    workspace = load_rehearsal_workspace(path)
    issues: set[str] = set()
    asset_count = stored_bytes = unsupported = corrupt = 0
    source_db = workspace.source_root / "assets.sqlite3"
    source_files = tuple(workspace.source_root.iterdir())
    if source_db.is_file():
        try:
            with _read_only_sqlite(source_db) as connection:
                rows = connection.execute(
                    """
                    SELECT asset_id, stored_sha256, stored_relpath,
                           mime_type, kind, stored_bytes
                    FROM assets
                    ORDER BY asset_id
                    """
                ).fetchall()
            asset_count = len(rows)
            for row in rows:
                row_unsupported, row_corrupt, actual_size = (
                    _inspect_legacy_row(workspace, row)
                )
                unsupported += int(row_unsupported)
                corrupt += int(row_corrupt)
                stored_bytes += actual_size
        except (OSError, sqlite3.Error, ValueError, TypeError):
            issues.add("legacy_source_unreadable")
    elif source_files:
        issues.add("legacy_source_unreadable")
    if unsupported:
        issues.add("unsupported_legacy_assets")
    if corrupt:
        issues.add("corrupt_legacy_records")

    active_writer = False
    if workspace.state_db.exists():
        try:
            state = inspect_existing_migration_state(
                workspace.state_db,
                migration_key=MIGRATION_KEY,
            )
            active_writer = bool(
                state
                and (
                    state.write_uncertain
                    or state.lease_state == "active"
                )
            )
        except HostMigrationStateError:
            issues.add("migration_state_unreadable")
    if active_writer:
        issues.add("active_writer_detected")

    first_run = not workspace.state_db.exists()
    target_empty = not any(workspace.target_root.iterdir())
    if first_run and not target_empty:
        issues.add("target_not_empty")

    free_space = shutil.disk_usage(workspace.root).free
    if free_space < max(MIN_FREE_SPACE_BYTES, stored_bytes * 2):
        issues.add("insufficient_disk_space")
    try:
        rm_version = metadata.version("remember-me")
    except metadata.PackageNotFoundError:
        rm_version = "unavailable"
        issues.add("remember_me_unavailable")
    if rm_version != EXPECTED_REMEMBER_ME_VERSION:
        issues.add("remember_me_version_mismatch")
    commit = _ob_commit_sha()
    if not _GIT_SHA_PATTERN.fullmatch(commit):
        issues.add("ob_commit_unavailable")
    return RehearsalPreflight(
        status="success" if not issues else "preflight_failed",
        workspace_id=workspace.workspace_id,
        source_identity=canonical_path_identity(workspace.source_root),
        target_identity=canonical_path_identity(workspace.target_root),
        legacy_asset_count=asset_count,
        legacy_stored_blob_bytes=stored_bytes,
        unsupported_asset_count=unsupported,
        corrupt_record_count=corrupt,
        free_space_bytes=free_space,
        remember_me_version=rm_version,
        ob_commit_sha=commit,
        active_writer_detected=active_writer,
        target_initially_empty=target_empty,
        issue_codes=tuple(sorted(issues)),
    )


def run_rehearsal(
    path: str | Path,
    *,
    batch_size: int = DEFAULT_BATCH_SIZE,
    max_batches: int = DEFAULT_MAX_BATCHES,
    stop_after_batches: int | None = None,
    lease_ttl_seconds: int = DEFAULT_LEASE_TTL_SECONDS,
) -> dict[str, Any]:
    """Run migration and acceptance inside one validated offline workspace."""
    workspace = load_rehearsal_workspace(path)
    started_at = _timestamp()
    monotonic_started = time.monotonic()
    preflight = preflight_rehearsal(workspace.root)
    if not preflight.passed:
        report = _base_report(
            workspace,
            preflight,
            started_at,
            status="preflight_failed",
            completed_at=_timestamp(),
            elapsed_seconds=time.monotonic() - monotonic_started,
        )
        _atomic_write_json(workspace.report_path, report)
        return report

    context = None
    try:
        state = HostMigrationState(
            workspace.state_db,
            legacy_root=workspace.source_root,
            target_root=workspace.target_root,
        )
        store = AssetStore(workspace.source_root, write_gate=state)
        context = _create_legacy_asset_import_offline_context(
            workspace_root=workspace.root,
            workspace_id=workspace.workspace_id,
            nonce=workspace.nonce,
            legacy_root=workspace.source_root,
            rm_root=workspace.target_root,
        )
        context.bind_legacy_store(store)
        runtime = context.create_runtime()
        adapter = LegacyAssetImportAdapter(
            legacy_store=store,
            core=runtime.service,
            offline_context=context,
        )
        coordinator = LocalMigrationAcceptanceCoordinator(
            legacy_store=store,
            adapter=adapter,
            migration_state=state,
            source_identity=state.source_identity,
            target_identity=state.target_identity,
            batch_size=batch_size,
            max_batches=max_batches,
            stop_after_batches=stop_after_batches,
            lease_ttl_seconds=lease_ttl_seconds,
        )
        migration = coordinator.run()
        acceptance: LocalMigrationAcceptanceReport | None = None
        if migration.completed:
            acceptance = LegacyRmReconciler(
                legacy_store=store,
                adapter=adapter,
                migration_state=state,
                source_identity=state.source_identity,
                target_identity=state.target_identity,
                lease_ttl_seconds=lease_ttl_seconds,
            ).reconcile()
        diagnostic = MigrationRecoveryDiagnostics(
            state,
            source_identity=state.source_identity,
            target_identity=state.target_identity,
        ).inspect(acceptance_report=acceptance)
        status = _run_status(migration, acceptance)
        report = _build_report(
            workspace,
            preflight,
            started_at,
            time.monotonic() - monotonic_started,
            status,
            migration,
            acceptance,
            diagnostic,
        )
    except (RehearsalError, LegacyAssetImportAdapterError) as exc:
        report = _base_report(
            workspace,
            preflight,
            started_at,
            status=(
                exc.status
                if isinstance(exc, RehearsalError)
                else "workspace_invalid"
            ),
            completed_at=_timestamp(),
            elapsed_seconds=time.monotonic() - monotonic_started,
        )
    except HostMigrationStateError as exc:
        report = _base_report(
            workspace,
            preflight,
            started_at,
            status=_state_error_status(exc.code),
            completed_at=_timestamp(),
            elapsed_seconds=time.monotonic() - monotonic_started,
        )
    except Exception:
        report = _base_report(
            workspace,
            preflight,
            started_at,
            status="internal_error",
            completed_at=_timestamp(),
            elapsed_seconds=time.monotonic() - monotonic_started,
        )
    finally:
        if context is not None:
            context.close()
    _atomic_write_json(workspace.report_path, report)
    return report


def inspect_rehearsal(path: str | Path) -> dict[str, Any]:
    """Read checkpoint and report state without creating a lease or file."""
    workspace = load_rehearsal_workspace(path)
    report = _read_report(workspace.report_path)
    inspection = inspect_existing_migration_state(
        workspace.state_db,
        migration_key=MIGRATION_KEY,
    )
    checkpoint = inspection.checkpoint if inspection else None
    source_identity = canonical_path_identity(workspace.source_root)
    target_identity = canonical_path_identity(workspace.target_root)
    if report is not None:
        recovery_code = report.get("recovery_diagnostic_code", "no_checkpoint")
    else:
        recovery_code = MigrationRecoveryDiagnostics.from_existing(
            workspace.state_db,
            source_identity=source_identity,
            target_identity=target_identity,
        ).inspect().diagnostic_code
    safe_to_rerun = bool(
        inspection is None
        or (
            not inspection.write_uncertain
            and inspection.lease_state != "active"
            and (
                checkpoint is None
                or checkpoint.status in {
                    "ready",
                    "running",
                    "paused",
                    "completed",
                }
            )
        )
    )
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "rehearsal_workspace_id": workspace.workspace_id,
        "checkpoint_status": checkpoint.status if checkpoint else None,
        "recovery_diagnostic_code": recovery_code,
        "processed_count": checkpoint.processed_count if checkpoint else 0,
        "imported_count": checkpoint.imported_count if checkpoint else 0,
        "skipped_idempotent_count": (
            checkpoint.skipped_idempotent_count if checkpoint else 0
        ),
        "acceptance_overall_result": (
            report.get("acceptance_overall_result") if report else None
        ),
        "expected_asset_count": (
            report.get("expected_asset_count", 0) if report else 0
        ),
        "matched_asset_count": (
            report.get("matched_asset_count", 0) if report else 0
        ),
        "mismatched_asset_count": (
            report.get("mismatched_asset_count", 0) if report else 0
        ),
        "missing_target_count": (
            report.get("missing_target_count", 0) if report else 0
        ),
        "unexpected_target_count": (
            report.get("unexpected_target_count") if report else None
        ),
        "blob_verified_count": (
            report.get("blob_verified_count", 0) if report else 0
        ),
        "elapsed_seconds": report.get("elapsed_seconds") if report else None,
        "source_identity": source_identity,
        "target_identity": target_identity,
        "safe_to_rerun": safe_to_rerun,
        "reindex_ran": False,
        "production_access_occurred": False,
    }


def _validate_prepare_root(path: str | Path) -> Path:
    candidate = Path(path)
    if not candidate.is_absolute():
        raise RehearsalError("workspace_invalid")
    if _path_contains_reparse_point(candidate):
        raise RehearsalError("workspace_invalid")
    resolved = candidate.resolve(strict=False)
    _validate_root_location(resolved)
    return resolved


def _validate_root_location(root: Path) -> None:
    repository = Path(__file__).resolve().parent
    home = Path.home().resolve()
    if (
        root == root.parent
        or root == home
        or root == repository
        or _is_within(repository, root)
        or _is_within(root, repository)
    ):
        raise RehearsalError("workspace_invalid")


def _validate_workspace_paths(root: Path, roots: dict[str, Path]) -> None:
    values = tuple(roots.values())
    if len(set(values)) != len(values):
        raise RehearsalError("workspace_invalid")
    for candidate in values:
        if not _is_strict_within(root, candidate):
            raise RehearsalError("workspace_invalid")
        if _contains_reparse_point(root, candidate):
            raise RehearsalError("workspace_invalid")
    for index, left in enumerate(values):
        for right in values[index + 1 :]:
            if _is_within(left, right) or _is_within(right, left):
                raise RehearsalError("workspace_invalid")


def _contains_reparse_point(root: Path, candidate: Path) -> bool:
    current = root
    for part in candidate.relative_to(root).parts:
        current = current / part
        try:
            stat = current.lstat()
        except OSError:
            return True
        attributes = getattr(stat, "st_file_attributes", 0)
        if current.is_symlink() or attributes & 0x400:
            return True
    return False


def _path_contains_reparse_point(path: Path) -> bool:
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current = current / part
        if not current.exists():
            break
        try:
            stat = current.lstat()
        except OSError:
            return True
        attributes = getattr(stat, "st_file_attributes", 0)
        if current.is_symlink() or attributes & 0x400:
            return True
    return False


def _inspect_legacy_row(
    workspace: RehearsalWorkspace,
    row: sqlite3.Row,
) -> tuple[bool, bool, int]:
    unsupported = (
        row["kind"] != "image"
        or row["mime_type"] not in {"image/jpeg", "image/png"}
    )
    corrupt = False
    actual_size = 0
    try:
        asset_id = row["asset_id"]
        digest = row["stored_sha256"]
        relative = Path(row["stored_relpath"])
        stored_size = row["stored_bytes"]
        if (
            not isinstance(asset_id, str)
            or re.fullmatch(r"[0-9a-f]{32}", asset_id) is None
            or not isinstance(digest, str)
            or _SHA256_PATTERN.fullmatch(digest) is None
            or isinstance(stored_size, bool)
            or not isinstance(stored_size, int)
            or stored_size < 0
            or relative.is_absolute()
            or ".." in relative.parts
        ):
            return unsupported, True, 0
        blob = (workspace.source_root / relative).resolve(strict=True)
        assets_root = (workspace.source_root / "assets").resolve(strict=True)
        if not _is_within(assets_root, blob) or _contains_reparse_point(
            workspace.source_root,
            blob,
        ):
            return unsupported, True, 0
        content = blob.read_bytes()
        actual_size = len(content)
        corrupt = (
            actual_size != stored_size
            or hashlib.sha256(content).hexdigest() != digest
        )
    except (OSError, RuntimeError, TypeError, ValueError):
        corrupt = True
    return unsupported, corrupt, actual_size


def _read_only_sqlite(path: Path):
    connection = sqlite3.connect(
        "{}?mode=ro".format(path.as_uri()),
        uri=True,
        timeout=1,
    )
    connection.row_factory = sqlite3.Row
    return connection


def _build_report(
    workspace: RehearsalWorkspace,
    preflight: RehearsalPreflight,
    started_at: str,
    elapsed_seconds: float,
    status: str,
    migration,
    acceptance: LocalMigrationAcceptanceReport | None,
    diagnostic: MigrationRecoveryDiagnostic,
) -> dict[str, Any]:
    report = _base_report(
        workspace,
        preflight,
        started_at,
        status=status,
        completed_at=_timestamp(),
        elapsed_seconds=elapsed_seconds,
    )
    report.update(
        {
            "migration_checkpoint_status": migration.checkpoint_status,
            "processed_count": migration.cumulative_processed,
            "imported_count": migration.cumulative_imported,
            "skipped_idempotent_count": (
                migration.cumulative_skipped_idempotent
            ),
            "expected_asset_count": (
                acceptance.expected_asset_count if acceptance else 0
            ),
            "matched_asset_count": (
                acceptance.matched_asset_count if acceptance else 0
            ),
            "mismatched_asset_count": (
                acceptance.mismatched_asset_count if acceptance else 0
            ),
            "missing_target_count": (
                acceptance.missing_target_count if acceptance else 0
            ),
            "unexpected_target_count": (
                acceptance.unexpected_target_count if acceptance else None
            ),
            "blob_verified_count": (
                acceptance.blob_verified_count if acceptance else 0
            ),
            "acceptance_report_version": (
                acceptance.report_version if acceptance else None
            ),
            "acceptance_overall_result": (
                acceptance.overall_result if acceptance else "not_run"
            ),
            "stable_mismatch_summary": (
                dict(acceptance.mismatch_summary) if acceptance else {}
            ),
            "recovery_diagnostic_code": diagnostic.diagnostic_code,
            "safe_to_rerun": diagnostic.safe_to_resume,
        }
    )
    return report


def _base_report(
    workspace: RehearsalWorkspace,
    preflight: RehearsalPreflight,
    started_at: str,
    *,
    status: str,
    completed_at: str,
    elapsed_seconds: float,
) -> dict[str, Any]:
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "status": status,
        "rehearsal_workspace_id": workspace.workspace_id,
        "ob_commit_sha": preflight.ob_commit_sha,
        "remember_me_version": preflight.remember_me_version,
        "source_identity": preflight.source_identity,
        "target_identity": preflight.target_identity,
        "started_at": started_at,
        "completed_at": completed_at,
        "elapsed_seconds": round(max(0.0, elapsed_seconds), 6),
        "migration_checkpoint_status": None,
        "processed_count": 0,
        "imported_count": 0,
        "skipped_idempotent_count": 0,
        "expected_asset_count": preflight.legacy_asset_count,
        "matched_asset_count": 0,
        "mismatched_asset_count": 0,
        "missing_target_count": 0,
        "unexpected_target_count": None,
        "blob_verified_count": 0,
        "acceptance_report_version": None,
        "acceptance_overall_result": "not_run",
        "stable_mismatch_summary": {},
        "recovery_diagnostic_code": "not_run",
        "safe_to_rerun": False,
        "reindex_ran": False,
        "production_access_occurred": False,
    }


def _run_status(migration, acceptance) -> str:
    code = migration.error_code or migration.stopped_reason
    if not migration.completed:
        if code == "source_changed_since_checkpoint":
            return "source_changed"
        if code in {
            "migration_freeze_lost",
            "reconciliation_freeze_lost",
        }:
            return "lease_lost"
        return (
            "migration_failed"
            if migration.status == "failed"
            else "migration_blocked"
        )
    if acceptance is None or acceptance.overall_result != "passed":
        if acceptance and acceptance.error_code == "reconciliation_freeze_lost":
            return "lease_lost"
        if acceptance and acceptance.error_code == "source_changed_since_checkpoint":
            return "source_changed"
        return "acceptance_failed"
    return "success"


def _state_error_status(code: str) -> str:
    if code == "source_changed_since_checkpoint":
        return "source_changed"
    if code == "migration_freeze_lost":
        return "lease_lost"
    return "migration_failed"


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    temporary = path.with_name(
        ".{}.{}.tmp".format(path.name, secrets.token_hex(8))
    )
    try:
        temporary.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _read_report(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as exc:
        raise RehearsalError("workspace_invalid") from exc
    if (
        not isinstance(payload, dict)
        or payload.get("schema_version") != REPORT_SCHEMA_VERSION
    ):
        raise RehearsalError("workspace_invalid")
    return payload


def _ob_commit_sha() -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=Path(__file__).resolve().parent,
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return "unavailable"


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _is_within(root: Path, candidate: Path) -> bool:
    try:
        candidate.relative_to(root)
        return True
    except ValueError:
        return False


def _is_strict_within(root: Path, candidate: Path) -> bool:
    return root != candidate and _is_within(root, candidate)


def _print_result(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, sort_keys=True))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run a bounded offline Remember-Me migration rehearsal."
    )
    subparsers = parser.add_subparsers(dest="operation", required=True)
    for operation in ("prepare", "preflight", "inspect"):
        command = subparsers.add_parser(operation)
        command.add_argument("workspace", type=Path)
    run_command = subparsers.add_parser("run")
    run_command.add_argument("workspace", type=Path)
    run_command.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    run_command.add_argument(
        "--max-batches",
        type=int,
        default=DEFAULT_MAX_BATCHES,
    )
    run_command.add_argument("--stop-after-batches", type=int)
    arguments = parser.parse_args(argv)
    try:
        if arguments.operation == "prepare":
            workspace = prepare_rehearsal_workspace(arguments.workspace)
            payload = {
                "status": "success",
                "rehearsal_workspace_id": workspace.workspace_id,
            }
        elif arguments.operation == "preflight":
            payload = preflight_rehearsal(arguments.workspace).to_dict()
        elif arguments.operation == "inspect":
            payload = inspect_rehearsal(arguments.workspace)
            payload["status"] = "success"
        else:
            payload = run_rehearsal(
                arguments.workspace,
                batch_size=arguments.batch_size,
                max_batches=arguments.max_batches,
                stop_after_batches=arguments.stop_after_batches,
            )
    except RehearsalError as exc:
        payload = {"status": exc.status}
    _print_result(payload)
    return _EXIT_CODES.get(payload["status"], _EXIT_CODES["internal_error"])


if __name__ == "__main__":
    sys.exit(main())


__all__ = [
    "RehearsalError",
    "RehearsalPreflight",
    "RehearsalWorkspace",
    "inspect_rehearsal",
    "load_rehearsal_workspace",
    "main",
    "preflight_rehearsal",
    "prepare_rehearsal_workspace",
    "run_rehearsal",
]

"""Local-only acceptance, reconciliation, and recovery diagnostics."""

from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
import re
from typing import Callable

from asset_migration_state import (
    HostMigrationState,
    HostMigrationStateError,
    MigrationCheckpoint,
    inspect_existing_migration_state,
)
from asset_store import AssetStore
from remember_me_import_adapter import (
    LegacyAssetImportAdapter,
    LegacyAssetImportAdapterError,
    LegacyAssetTargetRecord,
)
from remember_me_migration_runner import (
    MIGRATION_KEY,
    MIGRATION_VERSION,
    HostMigrationRunnerError,
    MigrationBatchResult,
    run_migration_batch,
)


REPORT_VERSION = 1
MAX_COORDINATOR_BATCHES = 10_000
DEFAULT_MISMATCH_LIMIT = 100
_DEFAULT_UNSUPPORTED_TARGET_CHECKS = (
    "target_blob_bytes",
    "target_duplicate_asset_detection",
    "target_full_inventory",
    "target_snapshot_consistency",
    "target_tag_created_at",
    "target_unexpected_asset_detection",
)


@dataclass(frozen=True)
class MigrationAcceptanceRunResult:
    status: str
    batches_attempted: int
    batches_completed: int
    assets_processed_this_run: int
    cumulative_processed: int
    cumulative_imported: int
    cumulative_skipped_idempotent: int
    last_completed_asset_id: str | None
    upper_bound_asset_id: str | None
    checkpoint_status: str | None
    stopped_reason: str
    completed: bool
    blocked_asset_id: str | None
    error_code: str | None
    has_more: bool
    verification_status: str = "not_run"


@dataclass(frozen=True)
class MigrationMismatch:
    asset_id: str | None
    code: str
    field: str | None = None

    def to_dict(self) -> dict[str, str | None]:
        return asdict(self)


@dataclass(frozen=True)
class LocalMigrationAcceptanceReport:
    report_version: int
    migration_version: int
    source_identity: str
    target_identity: str
    checkpoint_status: str | None
    snapshot_generation: int | None
    current_generation: int | None
    upper_bound_asset_id: str | None
    expected_asset_count: int
    processed_count: int
    imported_count: int
    skipped_idempotent_count: int
    verified_asset_count: int
    matched_asset_count: int
    mismatched_asset_count: int
    missing_target_count: int
    unexpected_target_count: int | None
    blob_verified_count: int
    unsupported_checks: tuple[str, ...]
    mismatch_summary: tuple[tuple[str, int], ...]
    mismatches: tuple[MigrationMismatch, ...]
    truncated_mismatch_count: int
    reconciliation_started_at: str
    reconciliation_completed_at: str
    overall_result: str
    error_code: str | None = None

    def to_dict(self) -> dict:
        payload = asdict(self)
        payload["mismatch_summary"] = dict(self.mismatch_summary)
        payload["mismatches"] = [
            mismatch.to_dict() for mismatch in self.mismatches
        ]
        payload["unsupported_checks"] = list(self.unsupported_checks)
        return payload


@dataclass(frozen=True)
class MigrationRecoveryDiagnostic:
    diagnostic_code: str
    checkpoint_status: str | None
    safe_to_resume: bool
    safe_to_reconcile: bool
    requires_operator_investigation: bool
    recommended_action_code: str
    blocked_asset_id: str | None
    error_code: str | None
    current_generation: int | None
    snapshot_generation: int | None
    lease_state: str


class LocalMigrationAcceptanceCoordinator:
    """Call the bounded Stage 8G-C runner until a bounded stop condition."""

    def __init__(
        self,
        *,
        legacy_store: AssetStore,
        adapter: LegacyAssetImportAdapter,
        migration_state: HostMigrationState,
        source_identity: str,
        target_identity: str,
        batch_size: int,
        max_batches: int,
        stop_after_batches: int | None = None,
        lease_ttl_seconds: int = 60,
        batch_runner: Callable[..., MigrationBatchResult] = run_migration_batch,
    ) -> None:
        _validate_positive_int(batch_size, "migration_batch_size_invalid", 500)
        _validate_positive_int(
            max_batches,
            "migration_max_batches_invalid",
            MAX_COORDINATOR_BATCHES,
        )
        if stop_after_batches is not None:
            _validate_positive_int(
                stop_after_batches,
                "migration_stop_after_batches_invalid",
                max_batches,
            )
        if not callable(batch_runner):
            raise ValueError("migration_batch_runner_invalid")
        self._arguments = {
            "legacy_store": legacy_store,
            "adapter": adapter,
            "migration_state": migration_state,
            "source_identity": source_identity,
            "target_identity": target_identity,
            "batch_size": batch_size,
            "lease_ttl_seconds": lease_ttl_seconds,
        }
        self._max_batches = max_batches
        self._stop_after_batches = stop_after_batches
        self._batch_runner = batch_runner
        self._batch_size = batch_size

    def run(self) -> MigrationAcceptanceRunResult:
        attempted = completed_batches = 0
        baseline_processed: int | None = None
        previous: MigrationBatchResult | None = None
        limit = self._stop_after_batches or self._max_batches
        while attempted < limit:
            attempted += 1
            try:
                result = self._batch_runner(**self._arguments)
            except (HostMigrationRunnerError, HostMigrationStateError) as exc:
                return _coordinator_error(
                    attempted,
                    completed_batches,
                    _processed_delta(previous, baseline_processed),
                    previous,
                    exc.code,
                )
            except Exception:
                return _coordinator_error(
                    attempted,
                    completed_batches,
                    _processed_delta(previous, baseline_processed),
                    previous,
                    "migration_coordinator_internal_error",
                    status="failed",
                )
            if not isinstance(result, MigrationBatchResult):
                return _coordinator_error(
                    attempted,
                    completed_batches,
                    _processed_delta(previous, baseline_processed),
                    previous,
                    "migration_runner_result_invalid",
                    status="failed",
                )
            result_error = _validate_batch_result(
                result,
                previous=previous,
                batch_size=self._batch_size,
            )
            if result_error:
                return _coordinator_error(
                    attempted,
                    completed_batches,
                    _processed_delta(previous, baseline_processed),
                    previous,
                    result_error,
                    status="failed",
                )
            if baseline_processed is None:
                baseline_processed = (
                    result.processed_count - result.batch_processed_count
                )
            completed_batches += 1
            processed_this_run = result.processed_count - baseline_processed
            if result.completed:
                return _coordinator_result(
                    result, attempted, completed_batches, processed_this_run,
                    "completed",
                )
            if result.status in {"blocked", "failed"}:
                return _coordinator_result(
                    result, attempted, completed_batches, processed_this_run,
                    result.error_code or result.status,
                )
            if (
                result.batch_processed_count == 0
                or (
                    previous is not None
                    and result.processed_count == previous.processed_count
                    and result.last_completed_asset_id
                    == previous.last_completed_asset_id
                    and result.status == previous.status
                )
            ):
                return _coordinator_result(
                    result, attempted, completed_batches, processed_this_run,
                    "migration_no_progress",
                    status="incomplete",
                    error_code="migration_no_progress",
                )
            previous = result
        reason = (
            "stop_after_batches_reached"
            if self._stop_after_batches is not None
            else "max_batches_reached"
        )
        assert previous is not None
        return _coordinator_result(
            previous, attempted, completed_batches, processed_this_run,
            reason, status="incomplete",
        )


class LegacyRmReconciler:
    """Compare a completed legacy snapshot with the bound public RM view."""

    def __init__(
        self,
        *,
        legacy_store: AssetStore,
        adapter: LegacyAssetImportAdapter,
        migration_state: HostMigrationState,
        source_identity: str,
        target_identity: str,
        mismatch_limit: int = DEFAULT_MISMATCH_LIMIT,
        lease_ttl_seconds: int = 60,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        _validate_positive_int(mismatch_limit, "mismatch_limit_invalid", 10_000)
        self._store = legacy_store
        self._adapter = adapter
        self._state = migration_state
        self._source_identity = source_identity
        self._target_identity = target_identity
        self._mismatch_limit = mismatch_limit
        self._lease_ttl_seconds = lease_ttl_seconds
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def reconcile(self) -> LocalMigrationAcceptanceReport:
        started = _timestamp(self._clock)
        checkpoint: MigrationCheckpoint | None = None
        current_generation: int | None = None
        owner: str | None = None
        report: LocalMigrationAcceptanceReport | None = None
        try:
            binding_error = _binding_error(
                self._store,
                self._adapter,
                self._state,
                self._source_identity,
                self._target_identity,
            )
            if binding_error:
                report = self._empty_report(
                    started, "identity_mismatch", binding_error
                )
            else:
                owner = self._state.acquire_freeze(
                    ttl_seconds=self._lease_ttl_seconds
                )
                self._state.assert_freeze_owner(owner)
                checkpoint = self._state.get_checkpoint(MIGRATION_KEY)
                precondition = _checkpoint_precondition_error(
                    checkpoint,
                    self._source_identity,
                    self._target_identity,
                )
                if precondition:
                    report = self._empty_report(
                        started, "incomplete", precondition, checkpoint
                    )
                else:
                    current_generation = self._state.current_generation()
                    if current_generation != checkpoint.snapshot_generation:
                        report = self._empty_report(
                            started,
                            "source_changed",
                            "source_changed_since_checkpoint",
                            checkpoint,
                            current_generation,
                        )
                    else:
                        report = self._compare(
                            started,
                            checkpoint,
                            current_generation,
                            owner,
                        )
        except HostMigrationStateError as exc:
            overall = {
                "source_generation_uncertain": "source_uncertain",
                "source_changed_since_checkpoint": "source_changed",
                "migration_freeze_busy": "blocked",
                "migration_freeze_lost": "blocked",
            }.get(exc.code, "internal_error")
            error_code = (
                "reconciliation_freeze_lost"
                if exc.code == "migration_freeze_lost"
                else exc.code
            )
            report = self._empty_report(
                started, overall, error_code, checkpoint, current_generation
            )
        except Exception:
            report = self._empty_report(
                started,
                "internal_error",
                "migration_reconciliation_internal_error",
                checkpoint,
                current_generation,
            )
        if owner is not None:
            try:
                released = self._state.release_freeze(owner)
            except HostMigrationStateError:
                released = False
            if (
                not released
                and report is not None
                and report.error_code != "reconciliation_freeze_lost"
            ):
                report = replace(
                    report,
                    overall_result="internal_error",
                    error_code="reconciliation_freeze_cleanup_failed",
                )
        assert report is not None
        return report

    def _compare(
        self,
        started: str,
        checkpoint: MigrationCheckpoint,
        generation: int,
        owner: str,
    ) -> LocalMigrationAcceptanceReport:
        summary: Counter[str] = Counter()
        details: list[MigrationMismatch] = []
        verified = matched = missing = 0
        cursor: str | None = None
        source_count = 0
        unsupported = _normalized_unsupported_checks(
            self._adapter.target_reconciliation_unsupported_checks()
        )
        while True:
            self._state.renew_freeze(
                owner,
                ttl_seconds=self._lease_ttl_seconds,
            )
            asset_ids = self._store.list_asset_ids_for_migration(
                last_asset_id=cursor,
                upper_bound_asset_id=checkpoint.upper_bound_asset_id,
                batch_size=500,
            )
            if not asset_ids:
                break
            for asset_id in asset_ids:
                self._state.renew_freeze(
                    owner,
                    ttl_seconds=self._lease_ttl_seconds,
                )
                record = self._store.get_import_record(asset_id)
                if record is None:
                    self._state.assert_freeze_owner(owner)
                    source_count += 1
                    _add_mismatch(
                        summary, details, self._mismatch_limit,
                        MigrationMismatch(asset_id, "source_record_unavailable"),
                    )
                    continue
                self._state.renew_freeze(
                    owner,
                    ttl_seconds=self._lease_ttl_seconds,
                )
                try:
                    target = self._adapter.get_target_record(asset_id)
                except LegacyAssetImportAdapterError as exc:
                    self._state.assert_freeze_owner(owner)
                    source_count += 1
                    code = (
                        "target_record_invalid"
                        if exc.code == "rm_target_record_invalid"
                        else "target_record_unavailable"
                    )
                    target = None
                    _add_mismatch(
                        summary, details, self._mismatch_limit,
                        MigrationMismatch(asset_id, code),
                    )
                    continue
                self._state.assert_freeze_owner(owner)
                source_count += 1
                if target is None:
                    missing += 1
                    _add_mismatch(
                        summary, details, self._mismatch_limit,
                        MigrationMismatch(asset_id, "missing_target_asset"),
                    )
                    continue
                verified += 1
                before = sum(summary.values())
                _compare_record(
                    asset_id, record, target, summary, details,
                    self._mismatch_limit,
                )
                if sum(summary.values()) == before:
                    matched += 1
            cursor = asset_ids[-1]
        self._state.assert_freeze_owner(owner)
        if self._state.current_generation() != checkpoint.snapshot_generation:
            raise HostMigrationStateError("source_changed_since_checkpoint")
        if source_count != checkpoint.initial_asset_count:
            _add_mismatch(
                summary, details, self._mismatch_limit,
                MigrationMismatch(None, "source_inventory_count_mismatch"),
            )
        total_mismatches = sum(summary.values())
        unexpected_count = (
            None
            if "target_unexpected_asset_detection" in unsupported
            else 0
        )
        blob_verified = (
            0
            if "target_blob_bytes" in unsupported
            else verified
        )
        overall = (
            "failed"
            if total_mismatches
            else ("unsupported" if unsupported else "passed")
        )
        completed = _timestamp(self._clock)
        return LocalMigrationAcceptanceReport(
            REPORT_VERSION,
            MIGRATION_VERSION,
            self._source_identity,
            self._target_identity,
            checkpoint.status,
            checkpoint.snapshot_generation,
            generation,
            checkpoint.upper_bound_asset_id,
            checkpoint.initial_asset_count,
            checkpoint.processed_count,
            checkpoint.imported_count,
            checkpoint.skipped_idempotent_count,
            verified,
            matched,
            source_count - matched,
            missing,
            unexpected_count,
            blob_verified,
            unsupported,
            tuple(sorted(summary.items())),
            tuple(details),
            total_mismatches - len(details),
            started,
            completed,
            overall,
        )

    def _empty_report(
        self,
        started: str,
        overall: str,
        error: str,
        checkpoint: MigrationCheckpoint | None = None,
        generation: int | None = None,
    ) -> LocalMigrationAcceptanceReport:
        return LocalMigrationAcceptanceReport(
            REPORT_VERSION,
            MIGRATION_VERSION,
            self._source_identity,
            self._target_identity,
            checkpoint.status if checkpoint else None,
            checkpoint.snapshot_generation if checkpoint else None,
            generation,
            checkpoint.upper_bound_asset_id if checkpoint else None,
            checkpoint.initial_asset_count if checkpoint else 0,
            checkpoint.processed_count if checkpoint else 0,
            checkpoint.imported_count if checkpoint else 0,
            checkpoint.skipped_idempotent_count if checkpoint else 0,
            0, 0, 0, 0, None, 0,
            _DEFAULT_UNSUPPORTED_TARGET_CHECKS,
            (), (), 0, started, _timestamp(self._clock), overall, error,
        )


class MigrationRecoveryDiagnostics:
    """Explain persisted migration state without changing it."""

    def __init__(
        self,
        migration_state: HostMigrationState,
        *,
        source_identity: str,
        target_identity: str,
    ) -> None:
        self._state = migration_state
        self._source_identity = source_identity
        self._target_identity = target_identity

    @classmethod
    def from_existing(
        cls,
        db_path,
        *,
        source_identity: str,
        target_identity: str,
        clock: Callable[[], datetime] | None = None,
    ) -> "MigrationRecoveryDiagnostics":
        return cls(
            _ExistingMigrationStateReader(db_path, clock),
            source_identity=source_identity,
            target_identity=target_identity,
        )

    def inspect(
        self,
        *,
        acceptance_report: LocalMigrationAcceptanceReport | None = None,
    ) -> MigrationRecoveryDiagnostic:
        try:
            state = self._state.inspect(MIGRATION_KEY)
        except HostMigrationStateError as exc:
            code = (
                "schema_incompatible"
                if exc.code == "migration_schema_incompatible"
                else "failed_internal_error"
            )
            return _diagnostic(
                code, None, False, False, True,
                "incompatible_state_manual_review", exc.code, None, "unknown",
            )
        if state is None:
            return _diagnostic(
                "no_checkpoint", None, True, False, False,
                "start_migration", None, None, "none",
            )
        checkpoint = state.checkpoint
        if state.write_uncertain:
            return _diagnostic(
                "source_generation_uncertain",
                checkpoint,
                False, False, True,
                "investigate_uncertain_write",
                "source_generation_uncertain",
                state.generation,
                state.lease_state,
            )
        if checkpoint and (
            checkpoint.source_identity != self._source_identity
            or checkpoint.target_identity != self._target_identity
            or checkpoint.migration_version != MIGRATION_VERSION
        ):
            return _diagnostic(
                "identity_mismatch", checkpoint, False, False, True,
                "incompatible_state_manual_review", "migration_identity_mismatch",
                state.generation, state.lease_state,
            )
        if state.lease_state == "active":
            return _diagnostic(
                "active_freeze_owner", checkpoint, False, False, False,
                "wait_for_active_lease", None, state.generation, "active",
            )
        if state.lease_state == "expired":
            return _diagnostic(
                "expired_freeze_recoverable", checkpoint,
                checkpoint is None or checkpoint.status in {"ready", "running", "paused"},
                False, False,
                "start_migration" if checkpoint is None else "resume_migration",
                None, state.generation, "expired",
            )
        if checkpoint is None:
            return _diagnostic(
                "no_checkpoint", None, True, False, False,
                "start_migration", None, state.generation, "none",
            )
        if state.generation != checkpoint.snapshot_generation:
            return _diagnostic(
                "blocked_source_changed", checkpoint, False, False, True,
                "investigate_source_change", "source_changed_since_checkpoint",
                state.generation, "none",
            )
        if checkpoint.status == "blocked":
            source_changed = checkpoint.error_code == "source_changed_since_checkpoint"
            return _diagnostic(
                "blocked_source_changed" if source_changed else "blocked_adapter_rejection",
                checkpoint, False, False, True,
                "investigate_source_change" if source_changed else "investigate_blocked_asset",
                checkpoint.error_code, state.generation, "none",
            )
        if checkpoint.status == "failed":
            return _diagnostic(
                "failed_internal_error", checkpoint, False, False, True,
                "investigate_internal_failure", checkpoint.error_code,
                state.generation, "none",
            )
        if checkpoint.status == "completed":
            report_status = _validate_acceptance_report(
                acceptance_report,
                checkpoint=checkpoint,
                current_generation=state.generation,
                source_identity=self._source_identity,
                target_identity=self._target_identity,
            )
            return _diagnostic(
                (
                    "completed_verified"
                    if report_status == "passed"
                    else (
                        "completed_partially_verified"
                        if report_status == "unsupported"
                        else "completed_unverified"
                    )
                ),
                checkpoint,
                False,
                report_status != "passed",
                report_status not in {None, "passed", "unsupported"},
                (
                    "no_action_completed"
                    if report_status == "passed"
                    else (
                        "review_unsupported_checks"
                        if report_status == "unsupported"
                        else "run_reconciliation"
                    )
                ),
                (
                    None
                    if report_status in {None, "passed", "unsupported"}
                    else report_status
                ),
                state.generation,
                "none",
            )
        return _diagnostic(
            checkpoint.status, checkpoint, True, False, False,
            "resume_migration", None, state.generation, "none",
        )


class _ExistingMigrationStateReader:
    def __init__(self, db_path, clock):
        self._db_path = db_path
        self._clock = clock

    def inspect(self, migration_key):
        return inspect_existing_migration_state(
            self._db_path,
            migration_key=migration_key,
            clock=self._clock,
        )


def _compare_record(
    asset_id: str,
    source: dict,
    target: LegacyAssetTargetRecord,
    summary: Counter[str],
    details: list[MigrationMismatch],
    limit: int,
) -> None:
    comparisons = (
        ("asset_id", "target_record_unavailable"),
        ("source_sha256", "source_sha_mismatch"),
        ("stored_sha256", "stored_sha_mismatch"),
        ("original_filename", "filename_mismatch"),
        ("mime_type", "mime_mismatch"),
        ("kind", "kind_mismatch"),
        ("decoded_bytes", "decoded_bytes_mismatch"),
        ("stored_bytes", "stored_bytes_mismatch"),
        ("created_at", "created_at_mismatch"),
        ("updated_at", "updated_at_mismatch"),
        ("title", "title_mismatch"),
        ("description", "description_mismatch"),
    )
    for field, code in comparisons:
        source_value = source.get(field)
        target_value = getattr(target, field)
        if type(source_value) is not type(target_value) or source_value != target_value:
            _add_mismatch(
                summary, details, limit, MigrationMismatch(asset_id, code, field)
            )
    source_dimensions = (source.get("width"), source.get("height"))
    if (
        tuple(type(value) for value in source_dimensions) != (int, int)
        or source_dimensions != (target.width, target.height)
    ):
        _add_mismatch(
            summary, details, limit,
            MigrationMismatch(asset_id, "dimensions_mismatch", "width_height"),
        )
    source_tags = tuple(sorted(tag["value"] for tag in source.get("tags", ())))
    target_tags = tuple(sorted(target.tags))
    if source_tags != target_tags:
        _add_mismatch(
            summary, details, limit,
            MigrationMismatch(asset_id, "tags_mismatch", "tags"),
        )


def _binding_error(store, adapter, state, source_identity, target_identity):
    if not isinstance(store, AssetStore) or store.migration_write_gate is not state:
        return "migration_write_gate_mismatch"
    if not isinstance(adapter, LegacyAssetImportAdapter):
        return "migration_adapter_invalid"
    if not adapter.is_bound_to_legacy_store(store):
        return "migration_adapter_source_mismatch"
    if not adapter.is_bound_to_target_root(state.target_root):
        return "migration_adapter_target_mismatch"
    if (
        source_identity != state.source_identity
        or target_identity != state.target_identity
    ):
        return "migration_identity_mismatch"
    return None


def _checkpoint_precondition_error(checkpoint, source_identity, target_identity):
    if checkpoint is None:
        return "migration_checkpoint_missing"
    if checkpoint.migration_version != MIGRATION_VERSION:
        return "migration_version_mismatch"
    if (
        checkpoint.source_identity != source_identity
        or checkpoint.target_identity != target_identity
    ):
        return "migration_identity_mismatch"
    if checkpoint.status != "completed":
        return "migration_checkpoint_not_completed"
    return None


def _add_mismatch(summary, details, limit, mismatch):
    summary[mismatch.code] += 1
    details.append(mismatch)
    details.sort(
        key=lambda item: (
            item.asset_id or "",
            item.code,
            item.field or "",
        )
    )
    if len(details) > limit:
        details.pop()


def _normalized_unsupported_checks(values) -> tuple[str, ...]:
    if (
        not isinstance(values, tuple)
        or any(
            not isinstance(value, str)
            or re.fullmatch(r"[a-z0-9_]{1,128}", value) is None
            for value in values
        )
    ):
        raise LegacyAssetImportAdapterError(
            "rm_target_capabilities_invalid"
        )
    return tuple(sorted(set(values)))


def _validate_batch_result(
    result: MigrationBatchResult,
    *,
    previous: MigrationBatchResult | None,
    batch_size: int,
) -> str | None:
    integer_values = (
        result.batch_processed_count,
        result.processed_count,
        result.imported_count,
        result.skipped_idempotent_count,
    )
    if any(type(value) is not int or value < 0 for value in integer_values):
        return "migration_runner_result_invalid"
    if (
        result.status not in {"paused", "blocked", "failed", "completed"}
        or not isinstance(result.has_more, bool)
        or not isinstance(result.completed, bool)
        or result.batch_processed_count > batch_size
        or result.processed_count
        != result.imported_count + result.skipped_idempotent_count
        or (result.completed != (result.status == "completed"))
        or (result.completed and result.has_more)
        or (result.status == "paused" and not result.has_more)
        or (
            result.status in {"paused", "completed"}
            and result.error_code is not None
        )
        or (
            result.status in {"blocked", "failed"}
            and result.error_code is None
        )
    ):
        return "migration_runner_result_invalid"
    for asset_id in (
        result.last_completed_asset_id,
        result.upper_bound_asset_id,
        result.blocked_asset_id,
    ):
        if (
            asset_id is not None
            and (
                not isinstance(asset_id, str)
                or re.fullmatch(r"[0-9a-f]{32}", asset_id) is None
            )
        ):
            return "migration_runner_result_invalid"
    if result.error_code is not None and (
        not isinstance(result.error_code, str)
        or re.fullmatch(r"[a-z0-9_]{1,128}", result.error_code) is None
    ):
        return "migration_runner_result_invalid"
    if (
        result.last_completed_asset_id is not None
        and result.upper_bound_asset_id is not None
        and result.last_completed_asset_id > result.upper_bound_asset_id
    ):
        return "migration_runner_result_invalid"
    if previous is None:
        if result.processed_count < result.batch_processed_count:
            return "migration_runner_result_invalid"
        return None
    if (
        result.processed_count < previous.processed_count
        or result.imported_count < previous.imported_count
        or result.skipped_idempotent_count
        < previous.skipped_idempotent_count
        or result.batch_processed_count
        != result.processed_count - previous.processed_count
        or result.batch_processed_count
        != (
            result.imported_count
            - previous.imported_count
            + result.skipped_idempotent_count
            - previous.skipped_idempotent_count
        )
        or result.upper_bound_asset_id != previous.upper_bound_asset_id
        or (
            previous.last_completed_asset_id is not None
            and (
                result.last_completed_asset_id is None
                or result.last_completed_asset_id
                < previous.last_completed_asset_id
            )
        )
    ):
        return "migration_runner_result_invalid"
    return None


def _processed_delta(
    previous: MigrationBatchResult | None,
    baseline: int | None,
) -> int:
    if previous is None or baseline is None:
        return 0
    return previous.processed_count - baseline


def _validate_acceptance_report(
    report,
    *,
    checkpoint: MigrationCheckpoint,
    current_generation: int,
    source_identity: str,
    target_identity: str,
) -> str | None:
    if report is None:
        return None
    if type(report) is not LocalMigrationAcceptanceReport:
        return "acceptance_report_invalid"
    count_values = (
        report.expected_asset_count,
        report.processed_count,
        report.imported_count,
        report.skipped_idempotent_count,
        report.verified_asset_count,
        report.matched_asset_count,
        report.mismatched_asset_count,
        report.missing_target_count,
        report.blob_verified_count,
        report.truncated_mismatch_count,
    )
    if (
        type(report.report_version) is not int
        or report.report_version != REPORT_VERSION
        or type(report.migration_version) is not int
        or report.migration_version != MIGRATION_VERSION
        or any(type(value) is not int or value < 0 for value in count_values)
        or report.source_identity != source_identity
        or report.target_identity != target_identity
        or report.checkpoint_status != "completed"
        or report.snapshot_generation != checkpoint.snapshot_generation
        or report.current_generation != current_generation
        or report.upper_bound_asset_id != checkpoint.upper_bound_asset_id
        or report.expected_asset_count != checkpoint.initial_asset_count
        or report.processed_count != checkpoint.processed_count
        or report.imported_count != checkpoint.imported_count
        or report.skipped_idempotent_count
        != checkpoint.skipped_idempotent_count
    ):
        return "acceptance_report_incompatible"
    try:
        report_completed = datetime.fromisoformat(
            report.reconciliation_completed_at
        )
        report_started = datetime.fromisoformat(
            report.reconciliation_started_at
        )
        checkpoint_completed = datetime.fromisoformat(
            checkpoint.completed_at or ""
        )
    except (TypeError, ValueError):
        return "acceptance_report_incompatible"
    if (
        report_completed.tzinfo is None
        or report_started.tzinfo is None
        or checkpoint_completed.tzinfo is None
        or report_completed < report_started
        or report_completed < checkpoint_completed
    ):
        return "acceptance_report_incompatible"
    if (
        not isinstance(report.unsupported_checks, tuple)
        or report.unsupported_checks
        != tuple(sorted(set(report.unsupported_checks)))
        or any(
            not isinstance(value, str)
            for value in report.unsupported_checks
        )
        or not isinstance(report.mismatch_summary, tuple)
        or not isinstance(report.mismatches, tuple)
        or any(
            type(item) is not MigrationMismatch
            for item in report.mismatches
        )
        or (
            report.unexpected_target_count is not None
            and (
                type(report.unexpected_target_count) is not int
                or report.unexpected_target_count < 0
            )
        )
        or any(
            not isinstance(item.code, str)
            or re.fullmatch(r"[a-z0-9_]{1,128}", item.code) is None
            or (
                item.asset_id is not None
                and (
                    not isinstance(item.asset_id, str)
                    or re.fullmatch(r"[0-9a-f]{32}", item.asset_id) is None
                )
            )
            or (
                item.field is not None
                and (
                    not isinstance(item.field, str)
                    or re.fullmatch(
                        r"[a-z0-9_]{1,128}", item.field
                    ) is None
                )
            )
            for item in report.mismatches
        )
    ):
        return "acceptance_report_incompatible"
    try:
        mismatch_count = sum(
            count for code, count in report.mismatch_summary
            if (
                isinstance(code, str)
                and re.fullmatch(r"[a-z0-9_]{1,128}", code) is not None
                and type(count) is int
                and count >= 0
            )
        )
    except (TypeError, ValueError):
        return "acceptance_report_incompatible"
    if len(report.mismatch_summary) != len(
        {
            code
            for code, count in report.mismatch_summary
            if (
                isinstance(code, str)
                and re.fullmatch(r"[a-z0-9_]{1,128}", code) is not None
                and type(count) is int
                and count >= 0
            )
        }
    ):
        return "acceptance_report_incompatible"
    if report.overall_result == "passed":
        if (
            report.unsupported_checks
            or mismatch_count
            or report.mismatched_asset_count
            or report.missing_target_count
            or report.unexpected_target_count not in {0}
            or report.processed_count != report.expected_asset_count
            or report.verified_asset_count != report.expected_asset_count
            or report.matched_asset_count != report.expected_asset_count
            or report.blob_verified_count != report.expected_asset_count
        ):
            return "acceptance_report_incompatible"
        return "passed"
    if report.overall_result == "unsupported":
        if (
            not report.unsupported_checks
            or mismatch_count
            or report.mismatched_asset_count
        ):
            return "acceptance_report_incompatible"
        return "unsupported"
    return "acceptance_report_not_passing"


def _validate_positive_int(value, code, maximum):
    if type(value) is not int or not 1 <= value <= maximum:
        raise ValueError(code)


def _timestamp(clock):
    value = clock()
    if not isinstance(value, datetime):
        raise RuntimeError("migration_acceptance_clock_unavailable")
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _coordinator_error(
    attempted,
    completed,
    processed_this_run,
    previous,
    code,
    *,
    status="blocked",
):
    return MigrationAcceptanceRunResult(
        status,
        attempted,
        completed,
        processed_this_run,
        previous.processed_count if previous else 0,
        previous.imported_count if previous else 0,
        previous.skipped_idempotent_count if previous else 0,
        previous.last_completed_asset_id if previous else None,
        previous.upper_bound_asset_id if previous else None,
        previous.status if previous else None,
        code,
        False,
        previous.blocked_asset_id if previous else None,
        code,
        previous.has_more if previous else True,
    )


def _coordinator_result(
    result, attempted, batches, processed, reason, *, status=None, error_code=None
):
    return MigrationAcceptanceRunResult(
        status or result.status,
        attempted,
        batches,
        processed,
        result.processed_count,
        result.imported_count,
        result.skipped_idempotent_count,
        result.last_completed_asset_id,
        result.upper_bound_asset_id,
        result.status,
        reason,
        result.completed,
        result.blocked_asset_id,
        error_code if error_code is not None else result.error_code,
        result.has_more,
    )


def _diagnostic(
    code,
    checkpoint,
    safe_resume,
    safe_reconcile,
    investigate,
    action,
    error,
    generation,
    lease_state,
):
    return MigrationRecoveryDiagnostic(
        code,
        checkpoint.status if checkpoint else None,
        safe_resume,
        safe_reconcile,
        investigate,
        action,
        checkpoint.blocked_asset_id if checkpoint else None,
        error,
        generation,
        checkpoint.snapshot_generation if checkpoint else None,
        lease_state,
    )


__all__ = [
    "LegacyRmReconciler",
    "LocalMigrationAcceptanceCoordinator",
    "LocalMigrationAcceptanceReport",
    "MigrationAcceptanceRunResult",
    "MigrationMismatch",
    "MigrationRecoveryDiagnostic",
    "MigrationRecoveryDiagnostics",
]

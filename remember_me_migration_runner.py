"""Bounded local Host runner for legacy asset migration."""

from __future__ import annotations

from dataclasses import dataclass, replace

from asset_migration_state import (
    HostMigrationState,
    HostMigrationStateError,
    MigrationCheckpoint,
)
from asset_store import AssetStore
from remember_me_import_adapter import (
    LegacyAssetImportAdapter,
    LegacyAssetImportRequest,
)


MIGRATION_KEY = "ombre-stage8g-c-assets"
MIGRATION_VERSION = 1


class HostMigrationRunnerError(RuntimeError):
    """Stable setup or state error that prevents a batch from starting."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class MigrationBatchResult:
    status: str
    batch_processed_count: int
    processed_count: int
    imported_count: int
    skipped_idempotent_count: int
    last_completed_asset_id: str | None
    upper_bound_asset_id: str | None
    blocked_asset_id: str | None
    error_code: str | None
    has_more: bool
    completed: bool


def run_migration_batch(
    *,
    legacy_store: AssetStore,
    adapter: LegacyAssetImportAdapter,
    migration_state: HostMigrationState,
    source_identity: str,
    target_identity: str,
    batch_size: int,
    lease_ttl_seconds: int = 60,
    owner_token: str | None = None,
    migration_key: str = MIGRATION_KEY,
    migration_version: int = MIGRATION_VERSION,
) -> MigrationBatchResult:
    """Run at most one deterministic migration page and persist each success."""
    _validate_inputs(
        legacy_store=legacy_store,
        adapter=adapter,
        migration_state=migration_state,
        source_identity=source_identity,
        target_identity=target_identity,
        batch_size=batch_size,
        migration_key=migration_key,
        migration_version=migration_version,
    )
    owner: str | None = None
    checkpoint: MigrationCheckpoint | None = None
    batch_processed = 0
    try:
        owner = migration_state.acquire_freeze(
            ttl_seconds=lease_ttl_seconds,
            owner_token=owner_token,
        )
        checkpoint = migration_state.get_checkpoint(migration_key)
        if checkpoint is None:
            generation = migration_state.current_generation()
            upper_bound, initial_count = (
                legacy_store.get_migration_snapshot_bounds()
            )
            checkpoint = migration_state.create_checkpoint(
                owner_token=owner,
                migration_key=migration_key,
                migration_version=migration_version,
                source_identity=source_identity,
                target_identity=target_identity,
                snapshot_generation=generation,
                upper_bound_asset_id=upper_bound,
                initial_asset_count=initial_count,
            )
        else:
            _validate_checkpoint(
                checkpoint=checkpoint,
                migration_version=migration_version,
                source_identity=source_identity,
                target_identity=target_identity,
            )

        if checkpoint.status == "completed":
            return _result(checkpoint, batch_processed, has_more=False)
        if checkpoint.status in {"blocked", "failed"}:
            return _result(
                checkpoint,
                batch_processed,
                has_more=checkpoint.status != "completed",
            )

        if (
            migration_state.current_generation()
            != checkpoint.snapshot_generation
        ):
            checkpoint = migration_state.set_checkpoint_status(
                owner_token=owner,
                migration_key=migration_key,
                status="blocked",
                error_code="source_changed_since_checkpoint",
            )
            return _result(checkpoint, batch_processed, has_more=True)

        checkpoint = migration_state.set_checkpoint_status(
            owner_token=owner,
            migration_key=migration_key,
            status="running",
            require_generation=checkpoint.snapshot_generation,
        )
        asset_ids = legacy_store.list_asset_ids_for_migration(
            last_asset_id=checkpoint.last_completed_asset_id,
            upper_bound_asset_id=checkpoint.upper_bound_asset_id,
            batch_size=batch_size,
        )
        for asset_id in asset_ids:
            migration_state.renew_freeze(
                owner,
                ttl_seconds=lease_ttl_seconds,
            )
            if (
                migration_state.current_generation()
                != checkpoint.snapshot_generation
            ):
                checkpoint = migration_state.set_checkpoint_status(
                    owner_token=owner,
                    migration_key=migration_key,
                    status="blocked",
                    blocked_asset_id=asset_id,
                    error_code="source_changed_since_checkpoint",
                )
                return _result(checkpoint, batch_processed, has_more=True)

            try:
                imported = adapter.import_asset(
                    LegacyAssetImportRequest(
                        asset_id=asset_id,
                        dry_run=False,
                    )
                )
            except Exception:
                checkpoint = migration_state.set_checkpoint_status(
                    owner_token=owner,
                    migration_key=migration_key,
                    status="failed",
                    blocked_asset_id=asset_id,
                    error_code="migration_adapter_failure",
                    require_generation=checkpoint.snapshot_generation,
                )
                return _result(checkpoint, batch_processed, has_more=True)

            disposition = getattr(imported.disposition, "value", "")
            if disposition == "imported":
                checkpoint = migration_state.record_asset_success(
                    owner_token=owner,
                    migration_key=migration_key,
                    snapshot_generation=checkpoint.snapshot_generation,
                    asset_id=asset_id,
                    imported=True,
                )
                batch_processed += 1
            elif disposition == "skipped_idempotent":
                checkpoint = migration_state.record_asset_success(
                    owner_token=owner,
                    migration_key=migration_key,
                    snapshot_generation=checkpoint.snapshot_generation,
                    asset_id=asset_id,
                    imported=False,
                )
                batch_processed += 1
            elif disposition == "rejected":
                raw_error = getattr(imported, "error_code", None)
                error_code = getattr(raw_error, "value", None)
                checkpoint = migration_state.set_checkpoint_status(
                    owner_token=owner,
                    migration_key=migration_key,
                    status="blocked",
                    blocked_asset_id=asset_id,
                    error_code=error_code or "migration_asset_rejected",
                    require_generation=checkpoint.snapshot_generation,
                )
                return _result(checkpoint, batch_processed, has_more=True)
            else:
                checkpoint = migration_state.set_checkpoint_status(
                    owner_token=owner,
                    migration_key=migration_key,
                    status="failed",
                    blocked_asset_id=asset_id,
                    error_code="migration_unexpected_disposition",
                    require_generation=checkpoint.snapshot_generation,
                )
                return _result(checkpoint, batch_processed, has_more=True)

        migration_state.assert_freeze_owner(owner)
        if (
            migration_state.current_generation()
            != checkpoint.snapshot_generation
        ):
            checkpoint = migration_state.set_checkpoint_status(
                owner_token=owner,
                migration_key=migration_key,
                status="blocked",
                error_code="source_changed_since_checkpoint",
            )
            return _result(checkpoint, batch_processed, has_more=True)
        remaining = legacy_store.list_asset_ids_for_migration(
            last_asset_id=checkpoint.last_completed_asset_id,
            upper_bound_asset_id=checkpoint.upper_bound_asset_id,
            batch_size=1,
        )
        has_more = bool(remaining)
        checkpoint = migration_state.set_checkpoint_status(
            owner_token=owner,
            migration_key=migration_key,
            status="paused" if has_more else "completed",
            require_generation=checkpoint.snapshot_generation,
        )
        return _result(checkpoint, batch_processed, has_more=has_more)
    except HostMigrationRunnerError:
        raise
    except HostMigrationStateError as exc:
        if exc.code == "source_changed_since_checkpoint" and checkpoint is not None:
            try:
                checkpoint = migration_state.set_checkpoint_status(
                    owner_token=owner or "",
                    migration_key=migration_key,
                    status="blocked",
                    error_code="source_changed_since_checkpoint",
                )
                return _result(checkpoint, batch_processed, has_more=True)
            except HostMigrationStateError:
                pass
        if exc.code == "migration_freeze_lost" and checkpoint is not None:
            return _result(
                replace(
                    checkpoint,
                    status="failed",
                    error_code="migration_freeze_lost",
                ),
                batch_processed,
                has_more=True,
            )
        raise HostMigrationRunnerError(exc.code) from exc
    except Exception as exc:
        if checkpoint is not None and owner is not None:
            try:
                checkpoint = migration_state.set_checkpoint_status(
                    owner_token=owner,
                    migration_key=migration_key,
                    status="failed",
                    error_code="migration_internal_failure",
                )
                return _result(checkpoint, batch_processed, has_more=True)
            except HostMigrationStateError:
                pass
        raise HostMigrationRunnerError(
            "migration_internal_failure"
        ) from exc
    finally:
        if owner is not None:
            try:
                migration_state.release_freeze(owner)
            except HostMigrationStateError:
                pass


def _validate_inputs(
    *,
    legacy_store: AssetStore,
    adapter: LegacyAssetImportAdapter,
    migration_state: HostMigrationState,
    source_identity: str,
    target_identity: str,
    batch_size: int,
    migration_key: str,
    migration_version: int,
) -> None:
    if not isinstance(legacy_store, AssetStore):
        raise HostMigrationRunnerError("migration_legacy_store_invalid")
    if not isinstance(adapter, LegacyAssetImportAdapter):
        raise HostMigrationRunnerError("migration_adapter_invalid")
    if not isinstance(migration_state, HostMigrationState):
        raise HostMigrationRunnerError("migration_state_invalid")
    if legacy_store.migration_write_gate is not migration_state:
        raise HostMigrationRunnerError("migration_write_gate_mismatch")
    if not adapter.is_bound_to_legacy_store(legacy_store):
        raise HostMigrationRunnerError("migration_adapter_source_mismatch")
    try:
        target_matches = adapter.is_bound_to_target_root(
            migration_state.target_root
        )
    except Exception as exc:
        raise HostMigrationRunnerError(
            "migration_adapter_invalid"
        ) from exc
    if not target_matches:
        raise HostMigrationRunnerError("migration_adapter_target_mismatch")
    if (
        source_identity != migration_state.source_identity
        or target_identity != migration_state.target_identity
        or source_identity == target_identity
    ):
        raise HostMigrationRunnerError("migration_identity_mismatch")
    if (
        isinstance(batch_size, bool)
        or not isinstance(batch_size, int)
        or not 1 <= batch_size <= 500
    ):
        raise HostMigrationRunnerError("migration_batch_size_invalid")
    if (
        not isinstance(migration_key, str)
        or not migration_key
        or len(migration_key) > 128
        or isinstance(migration_version, bool)
        or not isinstance(migration_version, int)
        or migration_version < 1
    ):
        raise HostMigrationRunnerError("migration_configuration_invalid")


def _validate_checkpoint(
    *,
    checkpoint: MigrationCheckpoint,
    migration_version: int,
    source_identity: str,
    target_identity: str,
) -> None:
    if checkpoint.migration_version != migration_version:
        raise HostMigrationRunnerError("migration_version_mismatch")
    if (
        checkpoint.source_identity != source_identity
        or checkpoint.target_identity != target_identity
    ):
        raise HostMigrationRunnerError("migration_identity_mismatch")


def _result(
    checkpoint: MigrationCheckpoint,
    batch_processed_count: int,
    *,
    has_more: bool,
) -> MigrationBatchResult:
    return MigrationBatchResult(
        status=checkpoint.status,
        batch_processed_count=batch_processed_count,
        processed_count=checkpoint.processed_count,
        imported_count=checkpoint.imported_count,
        skipped_idempotent_count=checkpoint.skipped_idempotent_count,
        last_completed_asset_id=checkpoint.last_completed_asset_id,
        upper_bound_asset_id=checkpoint.upper_bound_asset_id,
        blocked_asset_id=checkpoint.blocked_asset_id,
        error_code=checkpoint.error_code,
        has_more=has_more,
        completed=checkpoint.status == "completed",
    )


__all__ = [
    "HostMigrationRunnerError",
    "MIGRATION_KEY",
    "MIGRATION_VERSION",
    "MigrationBatchResult",
    "run_migration_batch",
]

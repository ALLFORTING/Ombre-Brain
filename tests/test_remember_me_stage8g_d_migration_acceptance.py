import hashlib
import io
import json
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
import threading

import pytest
from PIL import Image

from asset_migration_state import (
    HostMigrationState,
    HostMigrationStateError,
    MigrationCheckpoint,
    MigrationStateInspection,
)
from asset_store import AssetStore
from remember_me_import_adapter import (
    LegacyAssetImportAdapter,
    LegacyAssetImportAdapterError,
    LegacyAssetTargetRecord,
    create_legacy_asset_import_fixture_context,
)
from remember_me_migration_acceptance import (
    LocalMigrationAcceptanceReport,
    LegacyRmReconciler,
    LocalMigrationAcceptanceCoordinator,
    MigrationRecoveryDiagnostics,
)
from remember_me_migration_runner import (
    HostMigrationRunnerError,
    MIGRATION_KEY,
    MigrationBatchResult,
)


ROOT = Path(__file__).resolve().parent.parent


class MutableClock:
    def __init__(self):
        self.value = datetime(2026, 7, 30, tzinfo=timezone.utc)

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += timedelta(seconds=seconds)


def _image_bytes(color, image_format="PNG"):
    output = io.BytesIO()
    with Image.new("RGB", (7, 5), color) as image:
        image.save(output, format=image_format)
    return output.getvalue()


def _fixture(tmp_path, colors=(), image_formats=None):
    fixture = create_legacy_asset_import_fixture_context(
        tmp_path,
        legacy_root=tmp_path / "legacy",
        rm_root=tmp_path / "rm",
    )
    state = HostMigrationState(
        tmp_path / "migration.sqlite3",
        legacy_root=fixture.legacy_root,
        target_root=fixture.rm_root,
    )
    store = AssetStore(fixture.legacy_root, write_gate=state)
    fixture.bind_legacy_store(store)
    runtime = fixture.create_runtime()
    adapter = LegacyAssetImportAdapter(
        legacy_store=store,
        core=runtime.service,
        fixture_context=fixture,
    )
    assets = []
    formats = image_formats or ("PNG",) * len(colors)
    for color, image_format in zip(colors, formats, strict=True):
        extension = ".png" if image_format == "PNG" else ".jpg"
        mime_type = "image/png" if image_format == "PNG" else "image/jpeg"
        payload = _image_bytes(color, image_format)
        source = store.create_temp_path(extension)
        source.write_bytes(payload)
        asset = store.persist_upload(
            source,
            hashlib.sha256(payload).hexdigest(),
            len(payload),
            f"{color}{extension}",
            mime_type,
            require_image=True,
        )
        store.update_metadata(
            asset["asset_id"],
            title=f"title-{color}",
            description=f"description-{color}",
            tags=[color, "fixture"],
        )
        assets.append(store.get_import_record(asset["asset_id"]))
    return fixture, state, store, adapter, runtime, assets


def _reconciler(state, store, adapter, **overrides):
    arguments = {
        "legacy_store": store,
        "adapter": adapter,
        "migration_state": state,
        "source_identity": state.source_identity,
        "target_identity": state.target_identity,
    }
    arguments.update(overrides)
    return LegacyRmReconciler(**arguments)


def _coordinator(state, store, adapter, **overrides):
    arguments = {
        "legacy_store": store,
        "adapter": adapter,
        "migration_state": state,
        "source_identity": state.source_identity,
        "target_identity": state.target_identity,
        "batch_size": 1,
        "max_batches": 10,
    }
    arguments.update(overrides)
    return LocalMigrationAcceptanceCoordinator(**arguments)


@pytest.mark.parametrize("colors", [(), ("red",), ("red", "green", "blue")])
def test_coordinator_runs_bounded_batches_to_completion(tmp_path, colors):
    fixture, state, store, adapter, _, assets = _fixture(tmp_path, colors)
    result = _coordinator(state, store, adapter).run()

    assert result.completed is True
    assert result.status == "completed"
    assert result.assets_processed_this_run == len(assets)
    assert result.cumulative_processed == len(assets)
    assert result.batches_attempted <= max(1, len(assets) + 1)
    fixture.close()


def test_coordinator_stops_and_new_instance_resumes_checkpoint(
    tmp_path, monkeypatch
):
    fixture, state, store, adapter, _, assets = _fixture(
        tmp_path, ("red", "green", "blue")
    )
    first = _coordinator(
        state, store, adapter, stop_after_batches=1
    ).run()
    assert first.status == "incomplete"
    assert first.stopped_reason == "stop_after_batches_reached"
    assert first.cumulative_processed == 1

    second = _coordinator(state, store, adapter).run()
    assert second.completed is True
    assert second.cumulative_processed == len(assets)

    calls = []
    monkeypatch.setattr(
        adapter,
        "import_asset",
        lambda request: calls.append(request),
    )
    third = _coordinator(state, store, adapter).run()
    assert third.completed is True
    assert calls == []
    fixture.close()


def test_coordinator_detects_no_progress_without_retrying(tmp_path):
    fixture, state, store, adapter, _, _ = _fixture(tmp_path)
    calls = []
    paused = MigrationBatchResult(
        status="paused",
        batch_processed_count=0,
        processed_count=0,
        imported_count=0,
        skipped_idempotent_count=0,
        last_completed_asset_id=None,
        upper_bound_asset_id="f" * 32,
        blocked_asset_id=None,
        error_code=None,
        has_more=True,
        completed=False,
    )

    result = _coordinator(
        state,
        store,
        adapter,
        batch_runner=lambda **kwargs: calls.append(kwargs) or paused,
    ).run()
    assert result.stopped_reason == "migration_no_progress"
    assert result.error_code == "migration_no_progress"
    assert len(calls) == 1
    assert state.get_checkpoint(MIGRATION_KEY) is None
    fixture.close()


@pytest.mark.parametrize(
    "mutations",
    [
        {"processed_count": 0, "imported_count": 1},
        {"processed_count": 1, "batch_processed_count": 2},
        {"completed": True, "status": "completed", "has_more": True},
        {"last_completed_asset_id": "0" * 32},
    ],
)
def test_coordinator_rejects_inconsistent_runner_results(
    tmp_path, mutations
):
    fixture, state, store, adapter, _, _ = _fixture(tmp_path)
    first = MigrationBatchResult(
        status="paused",
        batch_processed_count=1,
        processed_count=1,
        imported_count=1,
        skipped_idempotent_count=0,
        last_completed_asset_id="1" * 32,
        upper_bound_asset_id="f" * 32,
        blocked_asset_id=None,
        error_code=None,
        has_more=True,
        completed=False,
    )
    candidate = replace(first, **mutations)
    results = iter((first, candidate))
    result = _coordinator(
        state,
        store,
        adapter,
        batch_runner=lambda **kwargs: next(results),
    ).run()
    assert result.status == "failed"
    assert result.error_code == "migration_runner_result_invalid"
    assert result.assets_processed_this_run == 1
    fixture.close()


def test_coordinator_preserves_first_batch_delta_when_second_fails(tmp_path):
    fixture, state, store, adapter, _, _ = _fixture(tmp_path)
    first = MigrationBatchResult(
        "paused", 1, 6, 4, 2, "6" * 32, "f" * 32,
        None, None, True, False,
    )
    calls = 0

    def runner(**kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return first
        raise HostMigrationRunnerError("migration_freeze_lost")

    result = _coordinator(
        state, store, adapter, batch_runner=runner
    ).run()
    assert result.assets_processed_this_run == 1
    assert result.cumulative_processed == 6
    assert result.error_code == "migration_freeze_lost"
    fixture.close()


def test_coordinator_strictly_validates_bounds(tmp_path):
    fixture, state, store, adapter, _, _ = _fixture(tmp_path)
    for field, value, code in (
        ("batch_size", True, "migration_batch_size_invalid"),
        ("max_batches", 0, "migration_max_batches_invalid"),
        ("max_batches", 10_001, "migration_max_batches_invalid"),
        ("stop_after_batches", True, "migration_stop_after_batches_invalid"),
        ("stop_after_batches", 3, "migration_stop_after_batches_invalid"),
    ):
        values = {"max_batches": 2}
        values[field] = value
        with pytest.raises(ValueError, match=f"^{code}$"):
            _coordinator(state, store, adapter, **values)
    fixture.close()


def test_real_fixture_reconciliation_matches_supported_fields(tmp_path):
    fixture, state, store, adapter, _, assets = _fixture(
        tmp_path,
        ("red", "green"),
        image_formats=("PNG", "JPEG"),
    )
    assert _coordinator(state, store, adapter).run().completed

    report = LegacyRmReconciler(
        legacy_store=store,
        adapter=adapter,
        migration_state=state,
        source_identity=state.source_identity,
        target_identity=state.target_identity,
    ).reconcile()

    assert report.overall_result == "passed"
    assert report.verified_asset_count == len(assets)
    assert report.matched_asset_count == len(assets)
    assert report.mismatched_asset_count == 0
    assert report.mismatch_summary == ()
    assert report.unsupported_checks == ()
    assert report.unsupported_checks == tuple(sorted(report.unsupported_checks))
    assert report.unexpected_target_count == 0
    assert report.blob_verified_count == len(assets)
    json.dumps(report.to_dict())
    serialized = repr(asdict(report))
    assert "PNG" not in serialized
    assert str(tmp_path) not in serialized
    fixture.close()


def test_reconciliation_detects_stable_mismatch_codes_and_truncates(
    tmp_path, monkeypatch
):
    fixture, state, store, adapter, _, assets = _fixture(tmp_path, ("red",))
    assert _coordinator(state, store, adapter).run().completed
    target = adapter.get_target_record(assets[0]["asset_id"])
    assert target is not None
    damaged = replace(
        target,
        stored_sha256="0" * 64,
        title="changed",
        tags=("different",),
        stored_bytes=target.stored_bytes + 1,
    )
    monkeypatch.setattr(adapter, "get_target_record", lambda asset_id: damaged)

    report = LegacyRmReconciler(
        legacy_store=store,
        adapter=adapter,
        migration_state=state,
        source_identity=state.source_identity,
        target_identity=state.target_identity,
        mismatch_limit=2,
    ).reconcile()

    codes = dict(report.mismatch_summary)
    assert report.overall_result == "failed"
    assert codes["stored_sha_mismatch"] == 1
    assert codes["stored_bytes_mismatch"] == 1
    assert codes["title_mismatch"] == 1
    assert codes["tags_mismatch"] == 1
    assert report.unsupported_checks == ()
    assert len(report.mismatches) == 2
    assert report.truncated_mismatch_count == 2
    assert tuple(report.mismatches) == tuple(
        sorted(
            report.mismatches,
            key=lambda item: (
                item.asset_id or "",
                item.code,
                item.field or "",
            ),
        )
    )
    assert state.inspect(MIGRATION_KEY).lease_state == "none"
    fixture.close()


def test_reconciliation_detects_missing_target(tmp_path, monkeypatch):
    fixture, state, store, adapter, _, _ = _fixture(tmp_path, ("red",))
    assert _coordinator(state, store, adapter).run().completed
    monkeypatch.setattr(adapter, "get_target_record", lambda asset_id: None)

    report = LegacyRmReconciler(
        legacy_store=store,
        adapter=adapter,
        migration_state=state,
        source_identity=state.source_identity,
        target_identity=state.target_identity,
    ).reconcile()
    assert report.overall_result == "failed"
    assert report.missing_target_count == 1
    assert dict(report.mismatch_summary) == {"missing_target_asset": 1}
    fixture.close()


@pytest.mark.parametrize(
    ("adapter_code", "mismatch_code"),
    [
        ("rm_target_read_unavailable", "target_record_unavailable"),
        ("rm_target_record_invalid", "target_record_invalid"),
    ],
)
def test_reconciliation_classifies_target_read_failures(
    tmp_path, monkeypatch, adapter_code, mismatch_code
):
    fixture, state, store, adapter, _, _ = _fixture(tmp_path, ("red",))
    assert _coordinator(state, store, adapter).run().completed

    def fail(asset_id):
        raise LegacyAssetImportAdapterError(adapter_code)

    monkeypatch.setattr(adapter, "get_target_record", fail)
    report = _reconciler(state, store, adapter).reconcile()
    assert report.overall_result == "failed"
    assert dict(report.mismatch_summary) == {mismatch_code: 1}
    assert report.missing_target_count == 0
    fixture.close()


def test_empty_unsupported_declaration_produces_verified_pass(
    tmp_path, monkeypatch
):
    fixture, state, store, adapter, _, assets = _fixture(tmp_path, ("red",))
    assert _coordinator(state, store, adapter).run().completed
    monkeypatch.setattr(
        adapter,
        "target_reconciliation_unsupported_checks",
        lambda: (),
    )
    report = _reconciler(state, store, adapter).reconcile()
    assert report.overall_result == "passed"
    assert report.unsupported_checks == ()
    assert report.unexpected_target_count == 0
    assert report.blob_verified_count == len(assets)
    fixture.close()


def test_adapter_subclass_uses_public_verification_evidence(tmp_path):
    fixture, state, store, adapter, runtime, _ = _fixture(tmp_path, ("red",))
    assert _coordinator(state, store, adapter).run().completed

    class EmptyDeclarationAdapter(LegacyAssetImportAdapter):
        def target_reconciliation_unsupported_checks(self):
            return ()

    subclass_adapter = EmptyDeclarationAdapter(
        legacy_store=store,
        core=runtime.service,
        fixture_context=fixture,
    )
    report = _reconciler(state, store, subclass_adapter).reconcile()
    assert report.overall_result == "passed"
    assert report.unsupported_checks == ()
    assert report.unexpected_target_count == 0
    assert report.blob_verified_count == 1
    fixture.close()


def test_adapter_declaration_can_only_add_unsupported_checks(
    tmp_path, monkeypatch
):
    fixture, state, store, adapter, _, _ = _fixture(tmp_path, ("red",))
    assert _coordinator(state, store, adapter).run().completed
    monkeypatch.setattr(
        adapter,
        "target_reconciliation_unsupported_checks",
        lambda: ("future_public_check", "target_blob_bytes"),
    )

    report = _reconciler(state, store, adapter).reconcile()
    assert report.overall_result == "unsupported"
    assert report.unsupported_checks == tuple(sorted(report.unsupported_checks))
    assert set(report.unsupported_checks) == {
        "future_public_check",
        "target_blob_bytes",
    }
    fixture.close()


def test_adapter_target_projection_revalidates_capability_and_shape(
    tmp_path, monkeypatch
):
    fixture, state, store, adapter, _, assets = _fixture(tmp_path, ("red",))
    assert _coordinator(state, store, adapter).run().completed
    asset_id = assets[0]["asset_id"]
    valid = adapter.get_target_record(asset_id)
    assert valid is not None
    malformed = replace(valid, stored_bytes=True)
    monkeypatch.setattr(adapter._core, "get_asset", lambda request: malformed)
    with pytest.raises(
        LegacyAssetImportAdapterError,
        match="^rm_target_record_invalid$",
    ):
        adapter.get_target_record(asset_id)
    fixture.close()
    with pytest.raises(
        LegacyAssetImportAdapterError,
        match="^fixture_root_violation$",
    ):
        adapter.get_target_record(asset_id)


def test_reconciliation_fails_closed_for_incomplete_and_changed_source(tmp_path):
    fixture, state, store, adapter, _, _ = _fixture(
        tmp_path, ("red", "green")
    )
    assert _coordinator(
        state, store, adapter, stop_after_batches=1
    ).run().status == "incomplete"
    reconciler = LegacyRmReconciler(
        legacy_store=store,
        adapter=adapter,
        migration_state=state,
        source_identity=state.source_identity,
        target_identity=state.target_identity,
    )
    incomplete = reconciler.reconcile()
    assert incomplete.overall_result == "incomplete"
    assert incomplete.error_code == "migration_checkpoint_not_completed"

    assert _coordinator(state, store, adapter).run().completed
    store.update_metadata(
        store.get_migration_snapshot_bounds()[0],
        title="changed-after-completion",
    )
    changed = reconciler.reconcile()
    assert changed.overall_result == "source_changed"
    assert changed.error_code == "source_changed_since_checkpoint"
    fixture.close()


def test_reconciliation_rejects_active_lease_and_binding_mismatch(tmp_path):
    fixture, state, store, adapter, _, _ = _fixture(tmp_path, ("red",))
    assert _coordinator(state, store, adapter).run().completed
    owner = state.acquire_freeze(ttl_seconds=60)
    try:
        busy = LegacyRmReconciler(
            legacy_store=store,
            adapter=adapter,
            migration_state=state,
            source_identity=state.source_identity,
            target_identity=state.target_identity,
        ).reconcile()
        assert busy.overall_result == "blocked"
        assert busy.error_code == "migration_freeze_busy"
    finally:
        state.release_freeze(owner)

    mismatch = LegacyRmReconciler(
        legacy_store=store,
        adapter=adapter,
        migration_state=state,
        source_identity="sha256:" + "0" * 64,
        target_identity=state.target_identity,
    ).reconcile()
    assert mismatch.overall_result == "identity_mismatch"
    fixture.close()


def test_reconciliation_release_failure_downgrades_success(
    tmp_path, monkeypatch
):
    fixture, state, store, adapter, _, _ = _fixture(tmp_path, ("red",))
    assert _coordinator(state, store, adapter).run().completed
    monkeypatch.setattr(state, "release_freeze", lambda owner: False)
    report = _reconciler(state, store, adapter).reconcile()
    assert report.overall_result == "internal_error"
    assert report.error_code == "reconciliation_freeze_cleanup_failed"
    fixture.close()


def test_reconciliation_reads_checkpoint_and_generation_only_after_freeze(
    tmp_path, monkeypatch
):
    fixture, state, store, adapter, _, assets = _fixture(tmp_path, ("red",))
    assert _coordinator(state, store, adapter).run().completed
    entered_acquire = threading.Event()
    allow_acquire = threading.Event()
    original_acquire = state.acquire_freeze
    target_reads = []

    def paused_acquire(**kwargs):
        entered_acquire.set()
        assert allow_acquire.wait(5)
        return original_acquire(**kwargs)

    monkeypatch.setattr(state, "acquire_freeze", paused_acquire)
    original_target_read = adapter.get_target_record
    monkeypatch.setattr(
        adapter,
        "get_target_record",
        lambda asset_id: (
            target_reads.append(asset_id) or original_target_read(asset_id)
        ),
    )
    reports = []
    thread = threading.Thread(
        target=lambda: reports.append(_reconciler(state, store, adapter).reconcile())
    )
    thread.start()
    assert entered_acquire.wait(5)
    store.update_metadata(assets[0]["asset_id"], title="changed-before-freeze")
    allow_acquire.set()
    thread.join(5)

    assert not thread.is_alive()
    assert reports[0].overall_result == "source_changed"
    assert reports[0].error_code == "source_changed_since_checkpoint"
    assert target_reads == []
    fixture.close()


def test_reconciliation_lease_takeover_invalidates_old_scan(
    tmp_path, monkeypatch
):
    clock = MutableClock()
    fixture = create_legacy_asset_import_fixture_context(
        tmp_path,
        legacy_root=tmp_path / "legacy",
        rm_root=tmp_path / "rm",
    )
    state = HostMigrationState(
        tmp_path / "migration.sqlite3",
        legacy_root=fixture.legacy_root,
        target_root=fixture.rm_root,
        clock=clock,
    )
    store = AssetStore(fixture.legacy_root, write_gate=state)
    fixture.bind_legacy_store(store)
    runtime = fixture.create_runtime()
    adapter = LegacyAssetImportAdapter(
        legacy_store=store,
        core=runtime.service,
        fixture_context=fixture,
    )
    payload = _image_bytes("red")
    source = store.create_temp_path(".png")
    source.write_bytes(payload)
    store.persist_upload(
        source, hashlib.sha256(payload).hexdigest(), len(payload),
        "red.png", "image/png", require_image=True,
    )
    assert _coordinator(state, store, adapter).run().completed
    original_read = adapter.get_target_record
    takeover = {}

    def target_read_with_takeover(asset_id):
        record = original_read(asset_id)
        clock.advance(11)
        takeover["owner"] = state.acquire_freeze(ttl_seconds=10)
        return record

    monkeypatch.setattr(adapter, "get_target_record", target_read_with_takeover)
    report = _reconciler(
        state, store, adapter, lease_ttl_seconds=10
    ).reconcile()

    assert report.overall_result == "blocked"
    assert report.error_code == "reconciliation_freeze_lost"
    assert report.matched_asset_count == 0
    state.assert_freeze_owner(takeover["owner"])
    state.release_freeze(takeover["owner"])
    monkeypatch.setattr(adapter, "get_target_record", original_read)
    assert _reconciler(state, store, adapter).reconcile().overall_result == "passed"
    fixture.close()


def test_recovery_diagnostics_are_read_only_for_core_states(tmp_path):
    fixture, state, store, adapter, _, assets = _fixture(tmp_path, ("red",))
    diagnostics = MigrationRecoveryDiagnostics(
        state,
        source_identity=state.source_identity,
        target_identity=state.target_identity,
    )
    before = state.db_path.read_bytes()
    empty = diagnostics.inspect()
    after = state.db_path.read_bytes()
    assert empty.diagnostic_code == "no_checkpoint"
    assert empty.recommended_action_code == "start_migration"
    assert before == after

    assert _coordinator(
        state, store, adapter, stop_after_batches=1
    ).run().completed
    completed = diagnostics.inspect()
    assert completed.diagnostic_code == "completed_unverified"
    assert completed.safe_to_reconcile is True
    report = _reconciler(state, store, adapter).reconcile()
    verified = diagnostics.inspect(acceptance_report=report)
    assert verified.diagnostic_code == "completed_verified"
    assert verified.recommended_action_code == "review_verified_migration"
    assert verified.safe_to_reconcile is False

    passed_report = replace(
        report,
        overall_result="passed",
        unsupported_checks=(),
        unexpected_target_count=0,
        blob_verified_count=report.expected_asset_count,
    )
    verified_copy = diagnostics.inspect(acceptance_report=passed_report)
    assert verified_copy.diagnostic_code == "completed_verified"
    assert verified_copy.error_code is None

    directly_constructed = LocalMigrationAcceptanceReport(
        **asdict(passed_report)
    )
    direct = diagnostics.inspect(acceptance_report=directly_constructed)
    assert direct.diagnostic_code == "completed_verified"
    assert direct.error_code is None

    forged = diagnostics.inspect(acceptance_report=True)
    assert forged.diagnostic_code == "completed_unverified"
    assert diagnostics.inspect(
        acceptance_report=passed_report.to_dict()
    ).diagnostic_code == "completed_unverified"

    @dataclass(frozen=True)
    class LookalikeReport:
        overall_result: str = "passed"

    assert diagnostics.inspect(
        acceptance_report=LookalikeReport()
    ).diagnostic_code == "completed_unverified"

    failed_report = replace(
        report,
        overall_result="failed",
        error_code="target_record_unavailable",
    )
    failed = diagnostics.inspect(acceptance_report=failed_report)
    assert failed.diagnostic_code == "completed_unverified"
    assert failed.requires_operator_investigation is True

    incompatible = diagnostics.inspect(
        acceptance_report=replace(
            passed_report,
            source_identity="sha256:" + "0" * 64,
        )
    )
    assert incompatible.diagnostic_code == "completed_unverified"
    assert incompatible.error_code == "acceptance_report_incompatible"

    store.update_metadata(
        assets[0]["asset_id"],
        title="changed-after-report",
    )
    changed = diagnostics.inspect(acceptance_report=report)
    assert changed.diagnostic_code == "blocked_source_changed"
    assert changed.error_code == "source_changed_since_checkpoint"
    fixture.close()


def test_recovery_diagnostics_existing_reader_never_creates_state(tmp_path):
    missing = tmp_path / "missing" / "migration.sqlite3"
    diagnostic = MigrationRecoveryDiagnostics.from_existing(
        missing,
        source_identity="sha256:" + "1" * 64,
        target_identity="sha256:" + "2" * 64,
    ).inspect()
    assert diagnostic.diagnostic_code == "no_checkpoint"
    assert not missing.exists()


def test_recovery_diagnostics_preserve_database_bytes_and_rows(tmp_path):
    fixture, state, store, adapter, _, _ = _fixture(tmp_path, ("red",))
    assert _coordinator(state, store, adapter).run().completed
    before_bytes = state.db_path.read_bytes()
    before_stat = state.db_path.stat()
    with sqlite3.connect(state.db_path) as connection:
        before_rows = {
            table: connection.execute(
                f"SELECT * FROM {table} ORDER BY 1"
            ).fetchall()
            for table in (
                "migration_schema",
                "migration_meta",
                "freeze_lease",
                "migration_checkpoints",
            )
        }
        before_version = connection.execute(
            "PRAGMA data_version"
        ).fetchone()[0]

    diagnostic = MigrationRecoveryDiagnostics.from_existing(
        state.db_path,
        source_identity=state.source_identity,
        target_identity=state.target_identity,
    ).inspect()

    with sqlite3.connect(state.db_path) as connection:
        after_rows = {
            table: connection.execute(
                f"SELECT * FROM {table} ORDER BY 1"
            ).fetchall()
            for table in before_rows
        }
        after_version = connection.execute(
            "PRAGMA data_version"
        ).fetchone()[0]
    after_stat = state.db_path.stat()
    assert diagnostic.diagnostic_code == "completed_unverified"
    assert state.db_path.read_bytes() == before_bytes
    assert after_rows == before_rows
    assert after_version == before_version
    assert after_stat.st_size == before_stat.st_size
    assert after_stat.st_mtime_ns == before_stat.st_mtime_ns
    fixture.close()


def test_recovery_diagnostics_report_active_expired_and_uncertain(tmp_path):
    clock = MutableClock()
    fixture = create_legacy_asset_import_fixture_context(
        tmp_path,
        legacy_root=tmp_path / "legacy",
        rm_root=tmp_path / "rm",
    )
    state = HostMigrationState(
        tmp_path / "migration.sqlite3",
        legacy_root=fixture.legacy_root,
        target_root=fixture.rm_root,
        clock=clock,
    )
    diagnostics = MigrationRecoveryDiagnostics(
        state,
        source_identity=state.source_identity,
        target_identity=state.target_identity,
    )
    owner = state.acquire_freeze(ttl_seconds=10)
    inspection = state.inspect(MIGRATION_KEY)
    serialized = (
        repr(inspection)
        + str(inspection)
        + repr(asdict(inspection))
    )
    assert owner not in serialized
    assert diagnostics.inspect().diagnostic_code == "active_freeze_owner"
    clock.advance(11)
    assert diagnostics.inspect().diagnostic_code == "expired_freeze_recoverable"
    with sqlite3.connect(state.db_path) as connection:
        lease_before = connection.execute(
            "SELECT * FROM freeze_lease"
        ).fetchall()
    diagnostics.inspect()
    with sqlite3.connect(state.db_path) as connection:
        lease_after = connection.execute(
            "SELECT * FROM freeze_lease"
        ).fetchall()
    assert lease_after == lease_before

    with sqlite3.connect(state.db_path) as connection:
        connection.execute(
            """
            UPDATE migration_meta
            SET write_uncertain = 1,
                write_owner_token = ?,
                write_started_at = ?
            WHERE singleton = 1
            """,
            ("f" * 64, clock().isoformat()),
        )
    uncertain = diagnostics.inspect()
    assert uncertain.diagnostic_code == "source_generation_uncertain"
    assert uncertain.recommended_action_code == "investigate_uncertain_write"
    with sqlite3.connect(state.db_path) as connection:
        assert connection.execute(
            "SELECT write_uncertain FROM migration_meta"
        ).fetchone()[0] == 1
    fixture.close()


@pytest.mark.parametrize(
    ("status", "error_code", "diagnostic_code", "action"),
    [
        (
            "blocked",
            "rm_import_validation_failure",
            "blocked_adapter_rejection",
            "investigate_blocked_asset",
        ),
        (
            "blocked",
            "source_changed_since_checkpoint",
            "blocked_source_changed",
            "investigate_source_change",
        ),
        (
            "failed",
            "migration_internal_error",
            "failed_internal_error",
            "investigate_internal_failure",
        ),
    ],
)
def test_recovery_diagnostics_explain_terminal_failures(
    tmp_path, status, error_code, diagnostic_code, action
):
    case_root = tmp_path / status / error_code
    case_root.mkdir(parents=True)
    fixture, state, _, _, _, _ = _fixture(case_root)
    owner = state.acquire_freeze(ttl_seconds=60)
    try:
        checkpoint = state.create_checkpoint(
            owner_token=owner,
            migration_key=MIGRATION_KEY,
            migration_version=1,
            source_identity=state.source_identity,
            target_identity=state.target_identity,
            snapshot_generation=state.current_generation(),
            upper_bound_asset_id=None,
            initial_asset_count=0,
        )
        state.set_checkpoint_status(
            owner_token=owner,
            migration_key=MIGRATION_KEY,
            status=status,
            error_code=error_code,
        )
    finally:
        state.release_freeze(owner)
    diagnostic = MigrationRecoveryDiagnostics(
        state,
        source_identity=state.source_identity,
        target_identity=state.target_identity,
    ).inspect()
    assert checkpoint.status == "ready"
    assert diagnostic.diagnostic_code == diagnostic_code
    assert diagnostic.recommended_action_code == action
    assert diagnostic.requires_operator_investigation is True
    fixture.close()


@pytest.mark.parametrize(
    ("status", "expected_code", "expected_action"),
    [
        ("ready", "ready", "resume_migration"),
        ("running", "running", "resume_migration"),
        ("paused", "paused", "resume_migration"),
    ],
)
def test_recovery_diagnostics_explain_resumable_states(
    status, expected_code, expected_action
):
    checkpoint = MigrationCheckpoint(
        migration_key=MIGRATION_KEY,
        migration_version=1,
        source_identity="sha256:" + "1" * 64,
        target_identity="sha256:" + "2" * 64,
        snapshot_generation=3,
        upper_bound_asset_id="f" * 32,
        last_completed_asset_id=None,
        status=status,
        initial_asset_count=1,
        processed_count=0,
        imported_count=0,
        skipped_idempotent_count=0,
        blocked_asset_id=None,
        error_code=None,
        created_at="2026-07-30T00:00:00+00:00",
        updated_at="2026-07-30T00:00:00+00:00",
        completed_at=None,
    )

    class ReadOnlyState:
        def inspect(self, migration_key):
            assert migration_key == MIGRATION_KEY
            return MigrationStateInspection(3, False, "none", checkpoint)

    diagnostic = MigrationRecoveryDiagnostics(
        ReadOnlyState(),
        source_identity=checkpoint.source_identity,
        target_identity=checkpoint.target_identity,
    ).inspect()
    assert diagnostic.diagnostic_code == expected_code
    assert diagnostic.recommended_action_code == expected_action
    assert diagnostic.safe_to_resume is True


def test_recovery_diagnostics_fail_closed_for_schema_and_identity():
    class IncompatibleState:
        def inspect(self, migration_key):
            raise HostMigrationStateError("migration_schema_incompatible")

    schema = MigrationRecoveryDiagnostics(
        IncompatibleState(),
        source_identity="sha256:" + "1" * 64,
        target_identity="sha256:" + "2" * 64,
    ).inspect()
    assert schema.diagnostic_code == "schema_incompatible"
    assert schema.requires_operator_investigation is True

    checkpoint = MigrationCheckpoint(
        MIGRATION_KEY, 1, "sha256:" + "3" * 64, "sha256:" + "4" * 64,
        0, None, None, "ready", 0, 0, 0, 0, None, None,
        "2026-07-30T00:00:00+00:00",
        "2026-07-30T00:00:00+00:00",
        None,
    )

    class IdentityState:
        def inspect(self, migration_key):
            return MigrationStateInspection(0, False, "none", checkpoint)

    identity = MigrationRecoveryDiagnostics(
        IdentityState(),
        source_identity="sha256:" + "1" * 64,
        target_identity="sha256:" + "2" * 64,
    ).inspect()
    assert identity.diagnostic_code == "identity_mismatch"
    assert identity.safe_to_resume is False


def test_stage8gd_static_architecture_boundaries():
    source = (ROOT / "remember_me_migration_acceptance.py").read_text(
        encoding="utf-8"
    )
    forbidden = (
        "RememberMeCore.import_asset",
        "core.import_asset",
        "remember_me.repository",
        "remember_me.storage",
        ".repository",
        "OMBR" + "E_RM_RUNTIME_ENABLED",
        "dual-" + "write",
        "shadow-" + "write",
        "reset_checkpoint",
        "delete_checkpoint",
        "force_clear",
    )
    for token in forbidden:
        assert token not in source
    assert "run_migration_batch" in source
    assert "adapter.import_asset" not in source
    assert "sqlite3" not in source
    assert '"completed_verified"' in source
    assert "begin_target_verification" in source
    assert "list_target_verification_page" in source
    assert "verify_target_blob" in source
    assert "complete_target_verification" in source

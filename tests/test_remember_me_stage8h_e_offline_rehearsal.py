import hashlib
import io
import json
import os
from pathlib import Path
import socket
import sys
from dataclasses import replace

import pytest
from PIL import Image

from asset_migration_state import HostMigrationState, HostMigrationStateError
from asset_store import AssetStore
from remember_me_adapter import RememberMeAdapter
from remember_me_import_adapter import (
    LegacyAssetImportAdapter,
    LegacyAssetImportAdapterError,
    LegacyAssetImportDisposition,
    LegacyAssetImportOfflineContext,
    LegacyAssetImportResult,
    _create_legacy_asset_import_offline_context,
    create_legacy_asset_import_fixture_context,
)
from remember_me_migration_acceptance import (
    LocalMigrationAcceptanceCoordinator,
)
from remember_me_migration_rehearsal import (
    MARKER_NAME,
    REPORT_NAME,
    RehearsalError,
    _atomic_write_json,
    inspect_rehearsal,
    load_rehearsal_workspace,
    main,
    preflight_rehearsal,
    prepare_rehearsal_workspace,
    run_rehearsal,
)
from remember_me_migration_runner import MIGRATION_KEY, MIGRATION_VERSION


def _make_state(workspace):
    return HostMigrationState(
        workspace.state_db,
        legacy_root=workspace.source_root,
        target_root=workspace.target_root,
    )


def _write_report(workspace, **updates):
    state = _make_state(workspace)
    payload = {
        "schema_version": 1,
        "status": "success",
        "rehearsal_workspace_id": workspace.workspace_id,
        "source_identity": state.source_identity,
        "target_identity": state.target_identity,
        "processed_count": 0,
        "imported_count": 0,
        "skipped_idempotent_count": 0,
        "expected_asset_count": 0,
        "matched_asset_count": 0,
        "mismatched_asset_count": 0,
        "missing_target_count": 0,
        "blob_verified_count": 0,
        "reindex_ran": False,
        "production_access_occurred": False,
        "recovery_diagnostic_code": "stale_report",
    }
    payload.update(updates)
    workspace.report_path.write_text(
        json.dumps(payload),
        encoding="utf-8",
    )


def _make_nonempty_target_without_state(workspace):
    (workspace.target_root / "orphan").write_bytes(b"synthetic")


def _png_bytes(index):
    output = io.BytesIO()
    color = (
        index & 0xFF,
        (index >> 8) & 0xFF,
        (index >> 16) & 0xFF,
    )
    with Image.new("RGB", (2, 2), color) as image:
        image.save(output, format="PNG")
    return output.getvalue()


def _populate(workspace, count):
    store = AssetStore(workspace.source_root)
    for index in range(count):
        payload = _png_bytes(index)
        source = store.create_temp_path(".png")
        source.write_bytes(payload)
        result = store.persist_upload(
            source,
            hashlib.sha256(payload).hexdigest(),
            len(payload),
            f"synthetic-{index}.png",
            "image/png",
            require_image=True,
        )
        store.update_metadata(
            result["asset_id"],
            title=f"title-{index}",
            description=f"description-{index}",
            tags=["synthetic", f"batch-{index // 100}"],
        )
    return store


def _snapshot(root):
    return {
        path.relative_to(root).as_posix(): (
            path.stat().st_size,
            path.stat().st_mtime_ns,
            hashlib.sha256(path.read_bytes()).hexdigest(),
        )
        for path in root.rglob("*")
        if path.is_file()
    }


def _workspace(tmp_path, count=0):
    workspace = prepare_rehearsal_workspace(tmp_path / "rehearsal")
    if count:
        _populate(workspace, count)
    return workspace


def test_prepare_creates_only_fixed_workspace_and_preflight_is_read_only(
    tmp_path,
):
    workspace = _workspace(tmp_path)
    assert {item.name for item in workspace.root.iterdir()} == {
        "rehearsal-manifest.json",
        MARKER_NAME,
        "legacy",
        "remember-me",
        "state",
        "reports",
    }
    before = _snapshot(workspace.root)
    preflight = preflight_rehearsal(workspace.root)
    after = _snapshot(workspace.root)

    assert preflight.passed is True
    assert preflight.legacy_asset_count == 0
    assert preflight.remember_me_version == "0.1.0.dev7"
    assert before == after


@pytest.mark.parametrize(
    "mutation",
    ["not_workspace", "overlap", "marker_mismatch", "target_nonempty"],
)
def test_workspace_fail_closed_boundaries(tmp_path, mutation):
    if mutation == "not_workspace":
        candidate = tmp_path / "plain"
        candidate.mkdir()
        with pytest.raises(RehearsalError, match="^workspace_invalid$"):
            load_rehearsal_workspace(candidate)
        return

    workspace = _workspace(tmp_path)
    if mutation == "overlap":
        manifest_path = workspace.root / "rehearsal-manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["paths"]["target"] = "legacy"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        with pytest.raises(RehearsalError, match="^workspace_invalid$"):
            load_rehearsal_workspace(workspace.root)
    elif mutation == "marker_mismatch":
        marker_path = workspace.root / MARKER_NAME
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
        marker["nonce"] = "0" * 64
        marker_path.write_text(json.dumps(marker), encoding="utf-8")
        with pytest.raises(RehearsalError, match="^workspace_invalid$"):
            load_rehearsal_workspace(workspace.root)
    else:
        (workspace.target_root / "unexpected").write_text(
            "synthetic",
            encoding="utf-8",
        )
        preflight = preflight_rehearsal(workspace.root)
        assert preflight.status == "preflight_failed"
        assert "target_not_empty" in preflight.issue_codes


@pytest.mark.parametrize(
    "setup",
    [
        "missing",
        "empty",
        "no_checkpoint",
        "source_mismatch",
        "target_mismatch",
        "version_mismatch",
    ],
)
def test_nonempty_target_requires_current_bound_checkpoint(tmp_path, setup):
    workspace = _workspace(tmp_path / setup)
    _make_nonempty_target_without_state(workspace)
    if setup == "missing":
        preflight = preflight_rehearsal(workspace.root)
        assert "target_state_unbound" in preflight.issue_codes
        return
    if setup == "empty":
        workspace.state_db.write_bytes(b"")
    else:
        state = _make_state(workspace)
        if setup == "no_checkpoint":
            state.release_freeze(
                state.acquire_freeze(ttl_seconds=60)
            )
        else:
            owner = state.acquire_freeze(ttl_seconds=60)
            state.create_checkpoint(
                owner_token=owner,
                migration_key=MIGRATION_KEY,
                migration_version=(
                    MIGRATION_VERSION + 1
                    if setup == "version_mismatch"
                    else MIGRATION_VERSION
                ),
                source_identity=(
                    "path-sha256:" + "0" * 64
                    if setup == "source_mismatch"
                    else state.source_identity
                ),
                target_identity=(
                    "path-sha256:" + "1" * 64
                    if setup == "target_mismatch"
                    else state.target_identity
                ),
                snapshot_generation=state.current_generation(),
                upper_bound_asset_id=None,
                initial_asset_count=0,
            )
            state.release_freeze(owner)
    before = _snapshot(workspace.root)
    preflight = preflight_rehearsal(workspace.root)
    after = _snapshot(workspace.root)
    assert preflight.status == "preflight_failed"
    assert set(preflight.issue_codes) & {
        "target_state_unbound",
        "target_state_identity_mismatch",
        "target_state_version_mismatch",
    }
    assert before == after


def test_completed_and_paused_bound_target_allows_rerun(tmp_path):
    workspace = _workspace(tmp_path / "completed", 1)
    first = run_rehearsal(workspace.root)
    assert first["status"] == "success"
    assert preflight_rehearsal(workspace.root).passed is True
    assert run_rehearsal(workspace.root)["status"] == "success"

    paused_workspace = _workspace(tmp_path / "paused", 2)
    paused = run_rehearsal(
        paused_workspace.root,
        stop_after_batches=1,
        batch_size=1,
    )
    assert paused["status"] == "migration_blocked"
    assert preflight_rehearsal(paused_workspace.root).passed is True


def test_symlink_or_junction_escape_is_rejected(tmp_path):
    workspace = _workspace(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    workspace.target_root.rmdir()
    try:
        workspace.target_root.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation is unavailable on this Windows host")
    with pytest.raises(RehearsalError, match="^workspace_invalid$"):
        load_rehearsal_workspace(workspace.root)


def test_preflight_reports_unsupported_corrupt_and_active_writer(
    tmp_path,
):
    workspace = _workspace(tmp_path, 1)
    store = AssetStore(workspace.source_root)
    asset_id = store.get_migration_snapshot_bounds()[0]
    blob = store.resolve_file(asset_id)[1]
    blob.write_bytes(blob.read_bytes() + b"corrupt")
    with store._connect() as connection:
        connection.execute(
            """
            UPDATE assets
            SET kind = 'video', mime_type = 'application/octet-stream'
            WHERE asset_id = ?
            """,
            (asset_id,),
        )
    state = HostMigrationState(
        workspace.state_db,
        legacy_root=workspace.source_root,
        target_root=workspace.target_root,
    )
    owner = state.acquire_freeze(ttl_seconds=60)
    try:
        preflight = preflight_rehearsal(workspace.root)
    finally:
        state.release_freeze(owner)

    assert preflight.status == "preflight_failed"
    assert preflight.unsupported_asset_count == 1
    assert preflight.corrupt_record_count == 1
    assert preflight.active_writer_detected is True
    assert set(preflight.issue_codes) >= {
        "unsupported_legacy_assets",
        "corrupt_legacy_records",
        "active_writer_detected",
    }


@pytest.mark.parametrize("count,batch_size", [(0, 100), (1, 100), (7, 2)])
def test_real_offline_rehearsal_success_and_idempotent_rerun(
    tmp_path,
    count,
    batch_size,
    monkeypatch,
):
    workspace = _workspace(tmp_path, count)
    monkeypatch.setattr(
        socket.socket,
        "connect",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("network must not be used")
        ),
    )
    first = run_rehearsal(workspace.root, batch_size=batch_size)
    second = run_rehearsal(workspace.root, batch_size=batch_size)

    assert first["status"] == "success"
    assert first["processed_count"] == count
    assert first["imported_count"] == count
    assert first["matched_asset_count"] == count
    assert first["mismatched_asset_count"] == 0
    assert first["blob_verified_count"] == count
    assert first["acceptance_overall_result"] == "passed"
    assert first["reindex_ran"] is False
    assert first["production_access_occurred"] is False
    assert second["status"] == "success"
    assert second["skipped_idempotent_count"] == 0


@pytest.mark.parametrize("count", [500, 501])
def test_500_and_501_asset_batch_boundaries(tmp_path, count):
    workspace = _workspace(tmp_path, count)
    report = run_rehearsal(
        workspace.root,
        batch_size=500,
        max_batches=3,
    )
    assert report["status"] == "success"
    assert report["processed_count"] == count
    assert report["blob_verified_count"] == count


def test_pause_inspect_and_safe_resume_is_read_only(tmp_path):
    workspace = _workspace(tmp_path, 3)
    paused = run_rehearsal(
        workspace.root,
        batch_size=1,
        stop_after_batches=1,
    )
    before = _snapshot(workspace.root)
    inspected = inspect_rehearsal(workspace.root)
    after = _snapshot(workspace.root)
    resumed = run_rehearsal(workspace.root, batch_size=1)

    assert paused["status"] == "migration_blocked"
    assert inspected["checkpoint_status"] == "paused"
    assert inspected["safe_to_rerun"] is True
    assert inspected["mismatched_asset_count"] == 0
    assert before == after
    assert resumed["status"] == "success"
    assert resumed["processed_count"] == 3


def test_source_generation_change_and_lease_loss_fail_closed(
    tmp_path,
    monkeypatch,
):
    workspace = _workspace(tmp_path, 2)
    paused = run_rehearsal(
        workspace.root,
        batch_size=1,
        stop_after_batches=1,
    )
    assert paused["status"] == "migration_blocked"
    state = HostMigrationState(
        workspace.state_db,
        legacy_root=workspace.source_root,
        target_root=workspace.target_root,
    )
    store = AssetStore(workspace.source_root, write_gate=state)
    payload = _png_bytes(900)
    source = store.create_temp_path(".png")
    source.write_bytes(payload)
    store.persist_upload(
        source,
        hashlib.sha256(payload).hexdigest(),
        len(payload),
        "source-changed.png",
        "image/png",
        require_image=True,
    )
    assert run_rehearsal(workspace.root)["status"] == "source_changed"

    other = _workspace(tmp_path / "lease", 1)
    original = HostMigrationState.renew_freeze
    calls = {"count": 0}

    def lose_lease(self, owner_token, *, ttl_seconds):
        calls["count"] += 1
        if calls["count"] == 1:
            raise __import__(
                "asset_migration_state"
            ).HostMigrationStateError("migration_freeze_lost")
        return original(self, owner_token, ttl_seconds=ttl_seconds)

    monkeypatch.setattr(HostMigrationState, "renew_freeze", lose_lease)
    assert run_rehearsal(other.root)["status"] == "lease_lost"


def test_adapter_rejection_is_not_success(
    tmp_path,
    monkeypatch,
):
    workspace = _workspace(tmp_path / "adapter", 1)

    def reject(self, request):
        return LegacyAssetImportResult(
            request.asset_id,
            LegacyAssetImportDisposition.REJECTED,
        )

    monkeypatch.setattr(LegacyAssetImportAdapter, "import_asset", reject)
    rejected = run_rehearsal(workspace.root)
    assert rejected["status"] == "migration_blocked"
    assert rejected["acceptance_overall_result"] == "not_run"


@pytest.mark.parametrize(
    "field",
    [
        "matches_expected_sha256",
        "matches_expected_size",
        "matches_expected_bytes",
    ],
)
def test_blob_checksum_size_and_exact_bytes_fail_closed(
    tmp_path,
    monkeypatch,
    field,
):
    mismatch = _workspace(tmp_path / field, 1)
    original_verify = LegacyAssetImportAdapter.verify_target_blob

    def bad_blob(self, **kwargs):
        result = original_verify(self, **kwargs)
        return replace(result, **{field: False})

    monkeypatch.setattr(
        LegacyAssetImportAdapter,
        "verify_target_blob",
        bad_blob,
    )
    failed = run_rehearsal(mismatch.root)
    assert failed["status"] == "acceptance_failed"
    assert failed["acceptance_overall_result"] == "failed"


def test_completion_failure_and_same_service_instance(tmp_path, monkeypatch):
    workspace = _workspace(tmp_path / "complete", 2)
    core_ids = []
    original_begin = LegacyAssetImportAdapter.begin_target_verification
    original_page = LegacyAssetImportAdapter.list_target_verification_page
    original_blob = LegacyAssetImportAdapter.verify_target_blob
    original_complete = LegacyAssetImportAdapter.complete_target_verification

    def track(method):
        def wrapped(self, *args, **kwargs):
            core_ids.append(id(self._core))
            return method(self, *args, **kwargs)

        return wrapped

    monkeypatch.setattr(
        LegacyAssetImportAdapter,
        "begin_target_verification",
        track(original_begin),
    )
    monkeypatch.setattr(
        LegacyAssetImportAdapter,
        "list_target_verification_page",
        track(original_page),
    )
    monkeypatch.setattr(
        LegacyAssetImportAdapter,
        "verify_target_blob",
        track(original_blob),
    )
    monkeypatch.setattr(
        LegacyAssetImportAdapter,
        "complete_target_verification",
        track(original_complete),
    )
    assert run_rehearsal(workspace.root)["status"] == "success"
    assert len(set(core_ids)) == 1

    monkeypatch.undo()
    failed_workspace = _workspace(tmp_path / "rescan", 1)

    def fail_completion(self, **kwargs):
        raise LegacyAssetImportAdapterError(
            "rm_target_verification_snapshot_changed"
        )

    monkeypatch.setattr(
        LegacyAssetImportAdapter,
        "complete_target_verification",
        fail_completion,
    )
    assert (
        run_rehearsal(failed_workspace.root)["status"]
        == "acceptance_failed"
    )


def test_offline_capability_is_separate_unforgeable_and_expires(tmp_path):
    workspace = _workspace(tmp_path)
    store = AssetStore(workspace.source_root)
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
    fixture = create_legacy_asset_import_fixture_context()

    assert not isinstance(context, type(fixture))
    assert not hasattr(RememberMeAdapter(), "offline_capability")
    with pytest.raises(
        LegacyAssetImportAdapterError,
        match="^invalid_offline_capability$",
    ):
        LegacyAssetImportOfflineContext(
            _token=object(),
            workspace_root=workspace.root,
            workspace_id=workspace.workspace_id,
            nonce=workspace.nonce,
            legacy_root=workspace.source_root,
            rm_root=workspace.target_root,
        )
    with pytest.raises(
        LegacyAssetImportAdapterError,
        match="^invalid_fixture_capability$",
    ):
        LegacyAssetImportAdapter(
            legacy_store=store,
            core=runtime.service,
            fixture_context=fixture,
            offline_context=context,
        )
    context.close()
    with pytest.raises(
        LegacyAssetImportAdapterError,
        match="^offline_capability_expired$",
    ):
        adapter.begin_target_verification()
    fixture.close()


def test_report_is_atomic_and_redacted(tmp_path, monkeypatch):
    workspace = _workspace(tmp_path, 1)
    replacements = []
    original_replace = os.replace

    def recording_replace(source, destination):
        replacements.append((Path(source), Path(destination)))
        return original_replace(source, destination)

    monkeypatch.setattr(os, "replace", recording_replace)
    report = run_rehearsal(workspace.root)
    serialized = json.dumps(report, sort_keys=True)
    marker = json.loads(
        (workspace.root / MARKER_NAME).read_text(encoding="utf-8")
    )

    assert replacements
    assert replacements[-1][1] == workspace.reports_root / REPORT_NAME
    assert replacements[-1][0].parent == workspace.reports_root
    assert not replacements[-1][0].exists()
    assert str(workspace.root) not in serialized
    assert marker["nonce"] not in serialized
    for forbidden in (
        "snapshot_id",
        "cursor",
        "blob_locator",
        "sqlite",
        "image_bytes",
    ):
        assert forbidden not in serialized.casefold()


@pytest.mark.parametrize(
    "updates",
    [
        {"rehearsal_workspace_id": "0" * 32},
        {"source_identity": "path-sha256:" + "0" * 64},
        {"target_identity": "path-sha256:" + "1" * 64},
        {"status": "unknown"},
        {"processed_count": "broken"},
        {"matched_asset_count": True},
        {"unexpected_target_count": -1},
        {"reindex_ran": True},
        {"production_access_occurred": True},
    ],
)
def test_inspect_rejects_unbound_or_malformed_report(tmp_path, updates):
    workspace = _workspace(tmp_path)
    _write_report(workspace, **updates)
    with pytest.raises(RehearsalError, match="^workspace_invalid$"):
        inspect_rehearsal(workspace.root)


def test_inspect_separates_current_state_from_last_report(tmp_path):
    workspace = _workspace(tmp_path)
    _write_report(
        workspace,
        recovery_diagnostic_code="completed_verified",
    )
    before = _snapshot(workspace.root)
    result = inspect_rehearsal(workspace.root)
    after = _snapshot(workspace.root)

    assert result["recovery_diagnostic_code"] == "no_checkpoint"
    assert result["current_recovery_diagnostic_code"] == "no_checkpoint"
    assert (
        result["last_report_recovery_diagnostic_code"]
        == "completed_verified"
    )
    assert result["last_report_status"] == "success"
    assert before == after


def test_inspect_without_state_does_not_create_it(tmp_path):
    workspace = _workspace(tmp_path)
    assert not workspace.state_db.exists()
    result = inspect_rehearsal(workspace.root)
    assert result["current_recovery_diagnostic_code"] == "no_checkpoint"
    assert result["safe_to_rerun"] is True
    assert not workspace.state_db.exists()


def test_cli_maps_ordinary_and_state_errors_without_leaks(
    tmp_path,
    monkeypatch,
    capsys,
):
    secret_path = str(tmp_path / "private-workspace")

    def fail_prepare(path):
        raise OSError(f"sqlite failed at {secret_path}")

    monkeypatch.setattr(
        "remember_me_migration_rehearsal.prepare_rehearsal_workspace",
        fail_prepare,
    )
    assert main(["prepare", secret_path]) == 9
    captured = capsys.readouterr()
    assert captured.out == '{"status": "internal_error"}\n'
    assert captured.err == ""
    assert secret_path not in captured.out
    assert "sqlite" not in captured.out.casefold()
    assert "traceback" not in captured.err.casefold()

    def fail_inspect(path):
        raise HostMigrationStateError("migration_state_unavailable")

    monkeypatch.setattr(
        "remember_me_migration_rehearsal.inspect_rehearsal",
        fail_inspect,
    )
    assert main(["inspect", secret_path]) == 4
    captured = capsys.readouterr()
    assert captured.out == '{"status": "migration_failed"}\n'
    assert captured.err == ""


def test_cli_report_replace_failure_preserves_existing_report(
    tmp_path,
    monkeypatch,
    capsys,
):
    workspace = _workspace(tmp_path)
    _write_report(workspace)
    original = workspace.report_path.read_bytes()

    def fail_replace(source, destination):
        raise OSError(f"replace failed for {workspace.root}")

    def fail_run(path, **kwargs):
        _atomic_write_json(
            workspace.report_path,
            {"schema_version": 999},
        )

    monkeypatch.setattr(os, "replace", fail_replace)
    monkeypatch.setattr(
        "remember_me_migration_rehearsal.run_rehearsal",
        fail_run,
    )
    assert main(["run", str(workspace.root)]) == 9
    captured = capsys.readouterr()
    assert captured.out == '{"status": "internal_error"}\n'
    assert captured.err == ""
    assert str(workspace.root) not in captured.out
    assert workspace.report_path.read_bytes() == original
    assert not any(
        path.name.endswith(".tmp")
        for path in workspace.reports_root.iterdir()
    )


@pytest.mark.parametrize("failure", [KeyboardInterrupt(), SystemExit(7)])
def test_cli_base_exceptions_propagate(tmp_path, monkeypatch, failure):
    def fail(path):
        raise failure

    monkeypatch.setattr(
        "remember_me_migration_rehearsal.inspect_rehearsal",
        fail,
    )
    with pytest.raises(type(failure)):
        main(["inspect", str(tmp_path)])


def test_corrupt_state_cli_is_stable_and_read_only(
    tmp_path,
    capsys,
):
    workspace = _workspace(tmp_path)
    workspace.state_db.write_bytes(b"not sqlite")
    before = _snapshot(workspace.root)
    assert main(["inspect", str(workspace.root)]) == 8
    captured = capsys.readouterr()
    after = _snapshot(workspace.root)
    assert captured.out == '{"status": "workspace_invalid"}\n'
    assert captured.err == ""
    assert "sqlite" not in captured.out.casefold()
    assert str(workspace.root) not in captured.out
    assert before == after


def test_run_never_uses_search_reindex_or_server_startup(
    tmp_path,
    monkeypatch,
):
    from remember_me.core.service import RememberMeService

    workspace = _workspace(tmp_path, 1)
    forbidden_imports = {
        "server",
        "asset_dashboard",
        "embedding_engine",
        "backfill_embeddings",
    }
    original_import = __import__

    def guarded_import(name, *args, **kwargs):
        if name in forbidden_imports:
            raise AssertionError(f"{name} must not be imported")
        return original_import(name, *args, **kwargs)

    async def forbidden(*args, **kwargs):
        raise AssertionError("Search and Reindex must not run")

    for name in forbidden_imports:
        sys.modules.pop(name, None)
    monkeypatch.setattr("builtins.__import__", guarded_import)
    monkeypatch.setattr(RememberMeService, "search_assets", forbidden)
    monkeypatch.setattr(RememberMeService, "reindex_embeddings", forbidden)

    report = run_rehearsal(workspace.root)
    assert report["status"] == "success"
    assert all(name not in sys.modules for name in forbidden_imports)


@pytest.mark.parametrize(
    "failure",
    [KeyboardInterrupt(), SystemExit(2)],
)
def test_base_exceptions_propagate(tmp_path, monkeypatch, failure):
    workspace = _workspace(tmp_path)

    def interrupt(self):
        raise failure

    monkeypatch.setattr(
        LocalMigrationAcceptanceCoordinator,
        "run",
        interrupt,
    )
    with pytest.raises(type(failure)):
        run_rehearsal(workspace.root)


def test_atomic_writer_uses_replace_and_leaves_no_temporary_file(
    tmp_path,
    monkeypatch,
):
    target = tmp_path / "report.json"
    calls = []
    original = os.replace

    def replace(source, destination):
        calls.append((Path(source), Path(destination)))
        original(source, destination)

    monkeypatch.setattr(os, "replace", replace)
    _atomic_write_json(target, {"schema_version": 1})
    assert json.loads(target.read_text(encoding="utf-8")) == {
        "schema_version": 1
    }
    assert calls == [(calls[0][0], target)]
    assert not calls[0][0].exists()

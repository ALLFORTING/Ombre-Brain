from __future__ import annotations

import asyncio
import base64
from contextlib import suppress
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
from types import SimpleNamespace
import sqlite3
import threading
import time
import uuid

import pytest
from cryptography.hazmat.primitives import serialization
from starlette.applications import Starlette
from starlette.testclient import TestClient

from asset_migration_state import HostMigrationState
from asset_store import AssetStore, AssetStoreError
from bucket_manager import BucketManager
from dehydrator import Dehydrator
from maintenance_write_gate import (
    DEFAULT_WRITE_COORDINATOR,
    FreezeLease,
    MaintenanceWriteCoordinator,
    MaintenanceWriteError,
    guarded_http_mutation,
)
from maintenance_write_coverage import (
    COVERAGE_SCHEMA_VERSION,
    scan_registered_write_coverage,
    scan_registered_source,
    scan_unregistered_source,
)
from offline_backup_bundle import (
    BackupBundleError,
    CaptureAbortSignal,
    CaptureResult,
    _EXIT_CODES,
    capture_external_source,
    generate_test_keypair,
    inspect_bundle,
    prepare_backup_workspace,
    restore_bundle,
    verify_bundle,
)
from production_backup_capture import (
    CaptureChannelError,
    CaptureJob,
    CaptureLimits,
    ProductionBackupCaptureController,
    StrictBackupV2OidcPolicy,
    V2_AUDIENCE,
    V2_REF,
    V2_REPOSITORY,
    V2_WORKFLOW,
    build_backup_v2_routes,
    public_key_fingerprint,
    receive_encrypted_bundle,
)


COMMIT = "1" * 40


def _claims(run_id: str = "123", run_attempt: str = "1") -> dict[str, str]:
    return {
        "repository": V2_REPOSITORY,
        "repository_owner": "ALLFORTING",
        "repository_id": "99",
        "repository_owner_id": "88",
        "ref": V2_REF,
        "event_name": "workflow_dispatch",
        "aud": V2_AUDIENCE,
        "workflow_ref": f"{V2_REPOSITORY}/{V2_WORKFLOW}@{V2_REF}",
        "run_id": run_id,
        "run_attempt": run_attempt,
    }


def _policy() -> StrictBackupV2OidcPolicy:
    return StrictBackupV2OidcPolicy(
        expected_repository_id="99",
        expected_repository_owner_id="88",
    )


async def _terminal(controller, request_id, claims=None):
    return await controller.wait_for_terminal(
        request_id,
        claims or _claims(),
        timeout=30,
    )


def _config(tmp_path: Path) -> dict:
    root = tmp_path / "buckets"
    return {
        "buckets_dir": str(root),
        "matching": {},
        "wikilink": {},
        "scoring_weights": {},
    }


def _controller(
    tmp_path: Path,
    *,
    coordinator=None,
    limits=None,
    worker_count=1,
    disk_usage=shutil.disk_usage,
):
    source = tmp_path / "external-source"
    source.mkdir(parents=True)
    workspace = prepare_backup_workspace(tmp_path / "capture-workspace")
    private_key, public_key = generate_test_keypair()
    coordinator = coordinator or MaintenanceWriteCoordinator()
    limits = limits or CaptureLimits(
        freeze_timeout_seconds=2,
        max_freeze_seconds=30,
        max_source_bytes=16 * 1024 * 1024,
        max_bundle_bytes=32 * 1024 * 1024,
        minimum_free_bytes=1,
        ready_ttl_seconds=60,
    )
    controller = ProductionBackupCaptureController(
        enabled=True,
        worker_count=worker_count,
        coordinator=coordinator,
        source_root=source,
        workspace_root=workspace.root,
        recipient_public_key=public_key,
        recipient_fingerprint=public_key_fingerprint(public_key),
        runtime_commit=COMMIT,
        limits=limits,
        oidc_policy=_policy(),
        disk_usage=disk_usage,
    )
    return controller, source, workspace, private_key, public_key


def test_writer_scope_normal_nested_and_generation():
    coordinator = MaintenanceWriteCoordinator()
    assert coordinator.status().generation == 0
    with coordinator.writer_scope("outer"):
        assert coordinator.status().active_writers == 1
        with coordinator.writer_scope("nested"):
            assert coordinator.status().active_writers == 1
    assert coordinator.status().state == "open"
    assert coordinator.status().generation == 1


def test_capture_abort_statuses_are_stable_and_have_distinct_exit_codes():
    cancelled = CaptureAbortSignal()
    cancelled.abort("capture_cancelled")
    with pytest.raises(BackupBundleError) as cancelled_error:
        cancelled.raise_if_aborted()
    assert cancelled_error.value.status == "capture_cancelled"

    now = [10.0]
    expired = CaptureAbortSignal(deadline=10.0, monotonic=lambda: now[0])
    with pytest.raises(BackupBundleError) as expired_error:
        expired.raise_if_aborted()
    assert expired_error.value.status == "freeze_lease_expired"
    assert _EXIT_CODES["capture_cancelled"] != _EXIT_CODES["freeze_lease_expired"]
    assert "internal_error" not in {
        cancelled_error.value.status,
        expired_error.value.status,
    }


@pytest.mark.asyncio
async def test_optional_writer_scope_skips_only_during_maintenance():
    coordinator = MaintenanceWriteCoordinator()
    with coordinator.optional_writer_scope("incidental") as entered:
        assert entered is True
    assert coordinator.status().generation == 1
    async with coordinator.freeze(
        reason="synthetic_capture",
        drain_timeout_seconds=1,
        max_freeze_seconds=5,
    ):
        with coordinator.optional_writer_scope("incidental") as entered:
            assert entered is False
        async with coordinator.optional_async_writer_scope("incidental") as entered:
            assert entered is False
        assert coordinator.status().generation == 1


@pytest.mark.asyncio
async def test_frozen_reads_skip_touch_ripple_and_dehydration_cache(tmp_path, monkeypatch):
    coordinator = MaintenanceWriteCoordinator()
    config = _config(tmp_path)
    manager = BucketManager(config, write_coordinator=coordinator)
    first_id = await manager.create("first synthetic memory")
    second_id = await manager.create("second synthetic memory")
    first_path = Path(manager._find_bucket_file(first_id))
    second_path = Path(manager._find_bucket_file(second_id))

    dehydrator = Dehydrator(config, write_coordinator=coordinator)
    cached_content = "cached synthetic content " * 80
    uncached_content = "uncached synthetic content " * 80
    dehydrator._set_cached_summary(cached_content, "cached result")
    dehydrator.api_available = True

    async def synthetic_dehydrate(content):
        assert content == uncached_content
        return "uncached result"

    monkeypatch.setattr(dehydrator, "_api_dehydrate", synthetic_dehydrate)
    bucket_before = (first_path.read_bytes(), second_path.read_bytes())
    cache_before = Path(dehydrator.cache_db_path).read_bytes()
    generation_before = coordinator.status().generation

    async with coordinator.freeze(
        reason="synthetic_capture",
        drain_timeout_seconds=1,
        max_freeze_seconds=10,
    ):
        assert (await manager.get(first_id))["content"] == "first synthetic memory"
        breath_like = await manager.get(first_id)
        await manager.touch(breath_like["id"])
        dream_like = (await manager.list_all(include_archive=False))[0]
        await manager.touch(dream_like["id"])
        assert "cached result" in await dehydrator.dehydrate(cached_content)
        assert "uncached result" in await dehydrator.dehydrate(uncached_content)
        with pytest.raises(MaintenanceWriteError, match="maintenance_in_progress"):
            dehydrator.invalidate_cache(cached_content)
        with pytest.raises(MaintenanceWriteError, match="maintenance_in_progress"):
            await manager.update(first_id, content="blocked")
        with pytest.raises(MaintenanceWriteError, match="maintenance_in_progress"):
            await manager.create("blocked")
        assert (first_path.read_bytes(), second_path.read_bytes()) == bucket_before
        assert Path(dehydrator.cache_db_path).read_bytes() == cache_before
        assert coordinator.status().generation == generation_before

    await manager.touch(first_id)
    assert first_path.read_bytes() != bucket_before[0]
    await dehydrator.dehydrate(uncached_content)
    assert dehydrator._get_cached_summary(uncached_content) == "uncached result"


@pytest.mark.asyncio
async def test_async_writer_scope_normal_and_generation():
    coordinator = MaintenanceWriteCoordinator()
    async with coordinator.async_writer_scope("write"):
        assert coordinator.status().active_writers == 1
    assert coordinator.status().generation == 1


@pytest.mark.asyncio
async def test_freeze_drains_existing_writer_and_rejects_new_writers():
    coordinator = MaintenanceWriteCoordinator()
    entered = asyncio.Event()
    release = asyncio.Event()

    async def slow_writer():
        async with coordinator.async_writer_scope("slow"):
            entered.set()
            await release.wait()

    writer = asyncio.create_task(slow_writer())
    await entered.wait()
    frozen = asyncio.Event()

    async def freezer():
        async with coordinator.freeze(
            reason="synthetic_capture",
            drain_timeout_seconds=2,
            max_freeze_seconds=5,
        ) as lease:
            coordinator.validate_lease(lease)
            frozen.set()
            with pytest.raises(MaintenanceWriteError, match="maintenance_in_progress"):
                with coordinator.writer_scope("blocked"):
                    pass

    task = asyncio.create_task(freezer())
    await asyncio.sleep(0.05)
    assert coordinator.status().state == "draining"
    with pytest.raises(MaintenanceWriteError, match="maintenance_in_progress"):
        with coordinator.writer_scope("blocked_drain"):
            pass
    release.set()
    await frozen.wait()
    await task
    await writer
    assert coordinator.status().state == "open"


@pytest.mark.asyncio
async def test_drain_timeout_restores_open():
    coordinator = MaintenanceWriteCoordinator()
    entered = asyncio.Event()
    release = asyncio.Event()

    async def slow():
        async with coordinator.async_writer_scope():
            entered.set()
            await release.wait()

    writer = asyncio.create_task(slow())
    await entered.wait()
    with pytest.raises(MaintenanceWriteError, match="freeze_drain_timeout"):
        async with coordinator.freeze(
            reason="synthetic_capture",
            drain_timeout_seconds=0.02,
            max_freeze_seconds=1,
        ):
            pass
    assert coordinator.status().state == "open"
    release.set()
    await writer


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [RuntimeError("x"), KeyboardInterrupt(), SystemExit()])
async def test_freeze_exception_and_baseexception_restore_open(error):
    coordinator = MaintenanceWriteCoordinator()
    with pytest.raises(type(error)):
        async with coordinator.freeze(
            reason="synthetic_capture",
            drain_timeout_seconds=1,
            max_freeze_seconds=2,
        ):
            raise error
    assert coordinator.status().state == "open"


@pytest.mark.asyncio
async def test_freeze_cancellation_restores_open():
    coordinator = MaintenanceWriteCoordinator()
    entered = asyncio.Event()

    async def freeze_forever():
        async with coordinator.freeze(
            reason="synthetic_capture",
            drain_timeout_seconds=1,
            max_freeze_seconds=30,
        ):
            entered.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(freeze_forever())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert coordinator.status().state == "open"


@pytest.mark.asyncio
async def test_concurrent_freeze_and_foreign_released_lease_rejected():
    first = MaintenanceWriteCoordinator()
    second = MaintenanceWriteCoordinator()
    captured = None
    async with first.freeze(
        reason="synthetic_capture",
        drain_timeout_seconds=1,
        max_freeze_seconds=5,
    ) as lease:
        captured = lease
        with pytest.raises(MaintenanceWriteError, match="freeze_unavailable"):
            async with first.freeze(
                reason="synthetic_capture",
                drain_timeout_seconds=1,
                max_freeze_seconds=5,
            ):
                pass
        with pytest.raises(MaintenanceWriteError, match="freeze_lease_invalid"):
            second.validate_lease(lease)
    with pytest.raises(MaintenanceWriteError, match="freeze_lease_invalid"):
        first.validate_lease(captured)


@pytest.mark.asyncio
async def test_lease_generation_stable_while_frozen():
    coordinator = MaintenanceWriteCoordinator()
    async with coordinator.freeze(
        reason="synthetic_capture",
        drain_timeout_seconds=1,
        max_freeze_seconds=5,
    ) as lease:
        assert coordinator.status().generation == lease.generation
        coordinator.validate_lease(lease)


@pytest.mark.asyncio
async def test_bucket_and_asset_writes_fail_closed_then_thaw(tmp_path):
    coordinator = MaintenanceWriteCoordinator()
    manager = BucketManager(_config(tmp_path), write_coordinator=coordinator)
    asset_root = tmp_path / "asset-root"
    store = AssetStore(str(asset_root), write_coordinator=coordinator)
    before = sorted(path.relative_to(tmp_path) for path in tmp_path.rglob("*"))
    async with coordinator.freeze(
        reason="synthetic_capture",
        drain_timeout_seconds=1,
        max_freeze_seconds=5,
    ):
        with pytest.raises(MaintenanceWriteError, match="maintenance_in_progress"):
            await manager.create("blocked")
        with pytest.raises(MaintenanceWriteError, match="maintenance_in_progress"):
            store.create_temp_path()
    after = sorted(path.relative_to(tmp_path) for path in tmp_path.rglob("*"))
    assert after == before
    bucket_id = await manager.create("allowed")
    assert await manager.get(bucket_id)
    assert store.create_temp_path().exists()


@pytest.mark.asyncio
async def test_migration_state_write_is_blocked(tmp_path):
    coordinator = MaintenanceWriteCoordinator()
    legacy = tmp_path / "legacy"
    target = tmp_path / "target"
    legacy.mkdir()
    target.mkdir()
    state = HostMigrationState(
        tmp_path / "state" / "migration.sqlite3",
        legacy_root=legacy,
        target_root=target,
        write_coordinator=coordinator,
    )
    before = state.db_path.read_bytes()
    async with coordinator.freeze(
        reason="synthetic_capture",
        drain_timeout_seconds=1,
        max_freeze_seconds=5,
    ):
        with pytest.raises(MaintenanceWriteError, match="maintenance_in_progress"):
            state.acquire_freeze(ttl_seconds=30)
    assert state.db_path.read_bytes() == before


@pytest.mark.asyncio
async def test_external_capture_requires_current_lease_and_exact_source(tmp_path):
    coordinator = MaintenanceWriteCoordinator()
    source = tmp_path / "source"
    source.mkdir()
    (source / "bucket.md").write_bytes(b"synthetic")
    workspace = prepare_backup_workspace(tmp_path / "workspace")
    private_key, public_key = generate_test_keypair()
    with pytest.raises(MaintenanceWriteError, match="freeze_lease_invalid"):
        capture_external_source(
            workspace.root,
            source,
            source,
            public_key,
            coordinator=coordinator,
            freeze_lease=object(),
            ob_commit_sha=COMMIT,
        )
    async with coordinator.freeze(
        reason="synthetic_capture",
        drain_timeout_seconds=1,
        max_freeze_seconds=20,
    ) as lease:
        with pytest.raises(BackupBundleError, match="workspace_invalid"):
            capture_external_source(
                workspace.root,
                source,
                tmp_path / "different",
                public_key,
                coordinator=coordinator,
                freeze_lease=lease,
                ob_commit_sha=COMMIT,
            )
        captured = capture_external_source(
            workspace.root,
            source,
            source,
            public_key,
            coordinator=coordinator,
            freeze_lease=lease,
            ob_commit_sha=COMMIT,
        )
    assert inspect_bundle(workspace.root, captured.bundle_name)["authenticated"] is False
    assert verify_bundle(workspace.root, captured.bundle_name, private_key)["authenticated"] is True


@pytest.mark.asyncio
async def test_external_capture_rejects_workspace_overlap(tmp_path):
    coordinator = MaintenanceWriteCoordinator()
    workspace = prepare_backup_workspace(tmp_path / "workspace")
    _, public_key = generate_test_keypair()
    async with coordinator.freeze(
        reason="synthetic_capture",
        drain_timeout_seconds=1,
        max_freeze_seconds=5,
    ) as lease:
        with pytest.raises(BackupBundleError, match="workspace_invalid"):
            capture_external_source(
                workspace.root,
                workspace.source_root,
                workspace.source_root,
                public_key,
                coordinator=coordinator,
                freeze_lease=lease,
                ob_commit_sha=COMMIT,
            )


@pytest.mark.asyncio
async def test_controller_capture_transport_cross_workspace_restore_and_ack(tmp_path):
    controller, source, workspace, private_key, _ = _controller(tmp_path)
    (source / "bucket.md").write_bytes(b"synthetic bucket bytes")
    db = source / "assets.sqlite3"
    with sqlite3.connect(db) as connection:
        connection.execute("CREATE TABLE assets (id INTEGER PRIMARY KEY, value TEXT)")
        connection.execute("INSERT INTO assets(value) VALUES ('synthetic')")
    request_id = str(uuid.uuid4())
    result = await controller.create_capture(
        request_id=request_id,
        expected_runtime_commit=COMMIT,
        expected_recipient_fingerprint=controller.recipient_fingerprint,
        claims=_claims(),
    )
    assert result["state"] == "accepted"
    result = await _terminal(controller, request_id)
    assert result["state"] == "ready"
    assert controller.coordinator.status().state == "open"
    restore_workspace = prepare_backup_workspace(tmp_path / "restore-workspace")
    async with controller.delivery(request_id, _claims()) as delivery:
        async def chunks():
            while block := delivery.handle.read(13):
                yield block

        received = await receive_encrypted_bundle(
            chunks(),
            restore_workspace.bundles_root / f"{delivery.bundle_id}.obbackup",
            expected_size=delivery.encrypted_size,
            expected_sha256=delivery.encrypted_sha256,
            maximum_bytes=controller.limits.max_bundle_bytes,
        )
    assert inspect_bundle(restore_workspace.root, received.name)["authenticated"] is False
    assert verify_bundle(restore_workspace.root, received.name, private_key)["authenticated"] is True
    restored = restore_bundle(restore_workspace.root, received.name, private_key)
    restored_root = restore_workspace.restored_root / restored["restore_name"]
    assert (restored_root / "bucket.md").read_bytes() == b"synthetic bucket bytes"
    with sqlite3.connect(restored_root / "assets.sqlite3") as connection:
        assert connection.execute("SELECT value FROM assets").fetchone()[0] == "synthetic"
    assert not list(restored_root.rglob("*-wal"))
    server_bundle = next(controller.workspace.bundles_root.glob("*.obbackup"))
    acknowledged = await controller.acknowledge(request_id, _claims())
    assert acknowledged["state"] == "consumed"
    assert not server_bundle.exists()
    assert not any(controller.workspace.temp_root.iterdir())


@pytest.mark.asyncio
async def test_controller_drains_then_blocks_real_storage_components(tmp_path, monkeypatch):
    import production_backup_capture as capture_module

    coordinator = MaintenanceWriteCoordinator()
    controller, source, _, _, _ = _controller(tmp_path, coordinator=coordinator)
    manager = BucketManager({
        "buckets_dir": str(source),
        "matching": {},
        "wikilink": {},
        "scoring_weights": {},
    }, write_coordinator=coordinator)
    store = AssetStore(str(source), write_coordinator=coordinator)
    target = tmp_path / "migration-target"
    target.mkdir()
    state = HostMigrationState(
        source / "state" / "migration.sqlite3",
        legacy_root=source,
        target_root=target,
        write_coordinator=coordinator,
    )
    writer_entered = asyncio.Event()
    writer_release = asyncio.Event()

    async def slow_writer():
        async with coordinator.async_writer_scope("synthetic_slow_mutation"):
            writer_entered.set()
            await writer_release.wait()

    slow = asyncio.create_task(slow_writer())
    await writer_entered.wait()
    capture_entered = threading.Event()
    capture_release = threading.Event()
    original_capture = capture_module.capture_external_source

    def paused_capture(*args, **kwargs):
        capture_entered.set()
        if not capture_release.wait(5):
            raise RuntimeError("synthetic capture wait timed out")
        return original_capture(*args, **kwargs)

    monkeypatch.setattr(capture_module, "capture_external_source", paused_capture)
    request_id = str(uuid.uuid4())
    accepted = await controller.create_capture(
        request_id=request_id,
        expected_runtime_commit=COMMIT,
        expected_recipient_fingerprint=controller.recipient_fingerprint,
        claims=_claims(),
    )
    assert accepted["state"] == "accepted"
    for _ in range(100):
        if coordinator.status().state == "draining":
            break
        await asyncio.sleep(0.01)
    assert coordinator.status().state == "draining"
    writer_release.set()
    await asyncio.to_thread(capture_entered.wait, 5)
    assert coordinator.status().state == "frozen"
    with pytest.raises(MaintenanceWriteError, match="maintenance_in_progress"):
        await manager.create("blocked bucket")
    with pytest.raises(MaintenanceWriteError, match="maintenance_in_progress"):
        store.create_temp_path()
    with pytest.raises(MaintenanceWriteError, match="maintenance_in_progress"):
        state.acquire_freeze(ttl_seconds=30)
    capture_release.set()
    result = await _terminal(controller, request_id)
    await slow
    assert result["state"] == "ready"
    assert coordinator.status().state == "open"
    assert await manager.get(await manager.create("allowed after thaw"))


@pytest.mark.asyncio
async def test_controller_idempotency_conflict_and_single_active_job(tmp_path):
    controller, source, _, _, _ = _controller(tmp_path)
    (source / "one.txt").write_text("synthetic", encoding="utf-8")
    request_id = str(uuid.uuid4())
    first = await controller.create_capture(
        request_id=request_id,
        expected_runtime_commit=COMMIT,
        expected_recipient_fingerprint=controller.recipient_fingerprint,
        claims=_claims(),
    )
    second = await controller.create_capture(
        request_id=request_id,
        expected_runtime_commit=COMMIT,
        expected_recipient_fingerprint=controller.recipient_fingerprint,
        claims=_claims(),
    )
    assert second == first
    with pytest.raises(CaptureChannelError, match="capture_request_conflict"):
        await controller.create_capture(
            request_id=request_id,
            expected_runtime_commit=COMMIT,
            expected_recipient_fingerprint=controller.recipient_fingerprint,
            claims=_claims("124"),
        )


@pytest.mark.parametrize(
    "change",
    [
        {"repository": "fork/repo"},
        {"ref": "refs/heads/feature"},
        {"event_name": "schedule"},
        {"event_name": "pull_request"},
        {"aud": "ombre-brain-backup"},
        {"workflow_ref": f"{V2_REPOSITORY}/.github/workflows/backup.yml@{V2_REF}"},
    ],
)
def test_oidc_policy_rejects_non_v2_identity(change):
    claims = _claims()
    claims.update(change)
    with pytest.raises(CaptureChannelError, match="oidc_denied"):
        _policy().verify(claims)


def test_oidc_policy_accepts_exact_v2_claims():
    assert _policy().verify(_claims()) == {
        "run_id": "123",
        "run_attempt": "1",
    }


def test_controller_disabled_multiworker_and_private_key_config_rejected(tmp_path):
    private_key, public_key = generate_test_keypair()
    raw = public_key.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    common = {
        "coordinator": MaintenanceWriteCoordinator(),
        "source_root": tmp_path / "source",
        "workspace_root": tmp_path / "workspace",
        "oidc_policy": _policy(),
    }
    with pytest.raises(CaptureChannelError, match="capture_disabled"):
        ProductionBackupCaptureController.from_config({"enabled": False}, **common)
    config = {
        "enabled": True,
        "recipient_public_key_b64": base64.b64encode(raw).decode("ascii"),
        "recipient_fingerprint": public_key_fingerprint(public_key),
        "runtime_commit": COMMIT,
        "worker_count": 1,
        "freeze_timeout_seconds": 1,
        "max_freeze_seconds": 2,
        "max_source_bytes": 10,
        "max_bundle_bytes": 100,
        "private_key": "forbidden",
    }
    with pytest.raises(CaptureChannelError, match="capture_key_invalid"):
        ProductionBackupCaptureController.from_config(config, **common)
    controller, _, _, _, _ = _controller(tmp_path / "multi", worker_count=1)
    with pytest.raises(CaptureChannelError, match="capture_multi_worker_unsupported"):
        ProductionBackupCaptureController(
            enabled=True,
            worker_count=2,
            coordinator=controller.coordinator,
            source_root=controller.source_root,
            workspace_root=controller.workspace.root,
            recipient_public_key=controller.recipient_public_key,
            recipient_fingerprint=controller.recipient_fingerprint,
            runtime_commit=COMMIT,
            limits=controller.limits,
            oidc_policy=_policy(),
        )


@pytest.mark.asyncio
async def test_preflight_limits_fail_before_freeze(tmp_path):
    limits = CaptureLimits(1, 5, 1, 1024 * 1024, minimum_free_bytes=1)
    controller, source, _, _, _ = _controller(tmp_path, limits=limits)
    (source / "large.bin").write_bytes(b"12")
    request_id = str(uuid.uuid4())
    await controller.create_capture(
            request_id=request_id,
            expected_runtime_commit=COMMIT,
            expected_recipient_fingerprint=controller.recipient_fingerprint,
            claims=_claims(),
        )
    result = await _terminal(controller, request_id)
    assert result["state"] == "failed"
    assert result["failure_code"] == "capture_source_too_large"
    assert controller.coordinator.status().state == "open"


@pytest.mark.asyncio
async def test_space_preflight_fails_before_freeze(tmp_path):
    controller, source, _, _, _ = _controller(tmp_path)
    (source / "one.bin").write_bytes(b"x")
    controller._disk_usage = lambda path: SimpleNamespace(free=0)
    request_id = str(uuid.uuid4())
    await controller.create_capture(
            request_id=request_id,
            expected_runtime_commit=COMMIT,
            expected_recipient_fingerprint=controller.recipient_fingerprint,
            claims=_claims(),
        )
    result = await _terminal(controller, request_id)
    assert result["failure_code"] == "capture_space_insufficient"
    assert controller.coordinator.status().state == "open"


@pytest.mark.asyncio
async def test_bundle_limit_removes_formal_bundle_and_thaws(tmp_path):
    limits = CaptureLimits(1, 5, 1024 * 1024, 1, minimum_free_bytes=1)
    controller, source, workspace, _, _ = _controller(tmp_path, limits=limits)
    (source / "one.bin").write_bytes(b"x")
    request_id = str(uuid.uuid4())
    await controller.create_capture(
            request_id=request_id,
            expected_runtime_commit=COMMIT,
            expected_recipient_fingerprint=controller.recipient_fingerprint,
            claims=_claims(),
        )
    result = await _terminal(controller, request_id)
    assert result["failure_code"] == "capture_bundle_too_large"
    assert controller.coordinator.status().state == "open"
    assert not list(workspace.bundles_root.glob("*.obbackup"))


@pytest.mark.asyncio
async def test_ready_bundle_survives_until_stale_cleanup(tmp_path):
    now = datetime(2026, 8, 1, tzinfo=timezone.utc)
    controller, source, workspace, _, _ = _controller(tmp_path)
    controller._clock = lambda: now
    (source / "one.bin").write_bytes(b"x")
    request_id = str(uuid.uuid4())
    await controller.create_capture(
        request_id=request_id,
        expected_runtime_commit=COMMIT,
        expected_recipient_fingerprint=controller.recipient_fingerprint,
        claims=_claims(),
    )
    await _terminal(controller, request_id)
    assert len(list(workspace.bundles_root.glob("*.obbackup"))) == 1
    assert await controller.cleanup_stale() == 0
    now += timedelta(seconds=controller.limits.ready_ttl_seconds + 1)
    assert await controller.cleanup_stale() == 1
    assert not list(workspace.bundles_root.glob("*.obbackup"))
    assert controller.get_job(request_id, _claims())["state"] == "stale"


@pytest.mark.asyncio
async def test_external_capture_final_lease_failure_removes_bundle(tmp_path, monkeypatch):
    coordinator = MaintenanceWriteCoordinator()
    source = tmp_path / "source"
    source.mkdir()
    (source / "one.bin").write_bytes(b"x")
    workspace = prepare_backup_workspace(tmp_path / "workspace")
    _, public_key = generate_test_keypair()
    original = coordinator.validate_lease
    calls = 0

    def fail_final(lease):
        nonlocal calls
        calls += 1
        if calls >= 2:
            raise MaintenanceWriteError("freeze_lease_expired")
        original(lease)

    monkeypatch.setattr(coordinator, "validate_lease", fail_final)
    async with coordinator.freeze(
        reason="synthetic_capture",
        drain_timeout_seconds=1,
        max_freeze_seconds=5,
    ) as lease:
        with pytest.raises(MaintenanceWriteError, match="freeze_lease_expired"):
            capture_external_source(
                workspace.root,
                source,
                source,
                public_key,
                coordinator=coordinator,
                freeze_lease=lease,
                ob_commit_sha=COMMIT,
            )
    assert not list(workspace.bundles_root.glob("*.obbackup"))


@pytest.mark.asyncio
async def test_reads_remain_available_while_frozen(tmp_path):
    coordinator = MaintenanceWriteCoordinator()
    manager = BucketManager(_config(tmp_path), write_coordinator=coordinator)
    bucket_id = await manager.create("readable")
    generation = coordinator.status().generation
    async with coordinator.freeze(
        reason="synthetic_capture",
        drain_timeout_seconds=1,
        max_freeze_seconds=5,
    ):
        assert (await manager.get(bucket_id))["content"] == "readable"
        assert coordinator.status().generation == generation


@pytest.mark.asyncio
async def test_transport_hash_size_limit_and_no_overwrite(tmp_path):
    async def chunks():
        yield b"abc"

    target = tmp_path / "bundle.obbackup"
    with pytest.raises(CaptureChannelError, match="transport_integrity_failed"):
        await receive_encrypted_bundle(
            chunks(), target, expected_size=4, expected_sha256="0" * 64, maximum_bytes=5
        )
    assert not target.exists() and not list(tmp_path.glob("*.part"))
    target.write_bytes(b"existing")
    with pytest.raises(CaptureChannelError, match="transport_target_invalid"):
        await receive_encrypted_bundle(
            chunks(), target, expected_size=3,
            expected_sha256=hashlib.sha256(b"abc").hexdigest(), maximum_bytes=5
        )
    assert target.read_bytes() == b"existing"


def test_route_factory_is_unregistered_and_path_body_is_not_accepted(tmp_path):
    controller, _, _, _, _ = _controller(tmp_path)
    routes = build_backup_v2_routes(controller, lambda request: _claims())
    assert len(routes) == 4
    assert all(route.path.startswith("/api/backup/v2/") for route in routes)
    server_source = (Path(__file__).parents[1] / "server.py").read_text(encoding="utf-8")
    backup_entry_source = (Path(__file__).parents[1] / "backup_entry.py").read_text(encoding="utf-8")
    assert "production_backup_capture" not in server_source
    assert "production_backup_capture" not in backup_entry_source


def test_route_factory_strict_body_oidc_download_headers_and_ack(tmp_path):
    controller, source, _, _, _ = _controller(tmp_path)
    (source / "one.bin").write_bytes(b"synthetic")
    app = Starlette(routes=build_backup_v2_routes(controller, lambda request: _claims()))
    request_id = str(uuid.uuid4())
    with TestClient(app) as client:
        rejected = client.post("/api/backup/v2/captures", json={
            "request_id": request_id,
            "expected_runtime_commit": COMMIT,
            "expected_recipient_fingerprint": controller.recipient_fingerprint,
            "source_path": "forbidden",
        })
        assert rejected.status_code == 400
        assert rejected.json() == {"status": "request_invalid"}
        assert rejected.headers["cache-control"] == "no-store"
        created = client.post("/api/backup/v2/captures", json={
            "request_id": request_id,
            "expected_runtime_commit": COMMIT,
            "expected_recipient_fingerprint": controller.recipient_fingerprint,
        })
        assert created.status_code == 202
        assert created.json()["state"] == "accepted"
        status = None
        for _ in range(300):
            status = client.get(f"/api/backup/v2/captures/{request_id}")
            if status.json()["state"] in {"ready", "failed"}:
                break
            time.sleep(0.01)
        assert status is not None and status.json()["state"] == "ready"
        assert controller.coordinator.status().state == "open"
        downloaded = client.get(f"/api/backup/v2/captures/{request_id}/bundle")
        assert downloaded.status_code == 200
        assert downloaded.headers["content-type"] == "application/octet-stream"
        assert downloaded.headers["cache-control"] == "no-store"
        assert downloaded.headers["x-backup-sha256"] == hashlib.sha256(downloaded.content).hexdigest()
        assert downloaded.headers["x-backup-recipient-fingerprint"] == controller.recipient_fingerprint
        acked = client.post(f"/api/backup/v2/captures/{request_id}/ack")
        assert acked.json()["state"] == "consumed"


def test_route_factory_rejects_wrong_oidc_for_status(tmp_path):
    controller, _, _, _, _ = _controller(tmp_path)
    claims = _claims()
    claims["event_name"] = "schedule"
    app = Starlette(routes=build_backup_v2_routes(controller, lambda request: claims))
    with TestClient(app) as client:
        response = client.get(f"/api/backup/v2/captures/{uuid.uuid4()}")
    assert response.status_code == 400
    assert response.json() == {"status": "oidc_denied"}


@pytest.mark.asyncio
async def test_http_mutation_maps_freeze_to_stable_503_and_allows_reads():
    @guarded_http_mutation("synthetic_http_write", methods=("POST",))
    async def endpoint(request):
        return JSONResponse({"ok": True})

    from starlette.responses import JSONResponse

    class FakeRequest:
        def __init__(self, method):
            self.method = method

    async with DEFAULT_WRITE_COORDINATOR.freeze(
        reason="synthetic_capture",
        drain_timeout_seconds=1,
        max_freeze_seconds=5,
    ):
        read = await endpoint(FakeRequest("GET"))
        write = await endpoint(FakeRequest("POST"))
    assert read.status_code == 200
    assert write.status_code == 503
    assert json.loads(write.body) == {"error": "maintenance_in_progress"}
    assert write.headers["cache-control"] == "no-store"


def test_job_public_metadata_contains_no_paths_tokens_or_keys(tmp_path):
    controller, _, _, _, _ = _controller(tmp_path)
    job = controller._jobs
    assert job == {}
    serialized = json.dumps({
        "runtime_commit": controller.runtime_commit,
        "recipient_fingerprint": controller.recipient_fingerprint,
    })
    assert str(controller.source_root) not in serialized
    assert "private" not in serialized.casefold()


def test_invalid_request_ids_rejected(tmp_path):
    controller, _, _, _, _ = _controller(tmp_path)
    with pytest.raises(CaptureChannelError, match="request_invalid"):
        controller.get_job("../escape", _claims())


def test_registered_production_write_coverage_is_complete():
    root = Path(__file__).parents[1]
    assert COVERAGE_SCHEMA_VERSION == 3
    assert scan_registered_write_coverage(root) == []


def test_new_bare_write_primitive_is_detected():
    issues = scan_unregistered_source(
        "def newly_added(path):\n    path.write_bytes(b'x')\n"
    )
    assert [(issue.function, issue.primitive) for issue in issues] == [
        ("newly_added", "write_bytes")
    ]


@pytest.mark.asyncio
async def test_cancelled_capture_worker_exits_before_thaw(tmp_path, monkeypatch):
    import production_backup_capture as capture_module

    controller, source, workspace, _, _ = _controller(tmp_path)
    (source / "one.bin").write_bytes(b"synthetic")
    entered = threading.Event()
    exited = threading.Event()

    def cooperative_capture(*args, abort_signal=None, **kwargs):
        entered.set()
        try:
            while True:
                abort_signal.raise_if_aborted()
                time.sleep(0.005)
        finally:
            exited.set()

    monkeypatch.setattr(capture_module, "capture_external_source", cooperative_capture)
    request_id = str(uuid.uuid4())
    await controller.create_capture(
        request_id=request_id,
        expected_runtime_commit=COMMIT,
        expected_recipient_fingerprint=controller.recipient_fingerprint,
        claims=_claims(),
    )
    assert await asyncio.to_thread(entered.wait, 5)
    task = controller._tasks[request_id]
    task.cancel()
    await asyncio.wait_for(asyncio.shield(task), timeout=5)
    assert exited.is_set()
    assert controller._active_workers == 0
    assert controller.coordinator.status().state == "open"
    assert controller.get_job(request_id, _claims())["failure_code"] == "capture_cancelled"
    assert not list(workspace.bundles_root.glob("*.obbackup"))
    assert not list(workspace.temp_root.iterdir())


@pytest.mark.asyncio
async def test_cancelled_preflight_reader_exits_before_next_capture(tmp_path, monkeypatch):
    controller, source, _, _, _ = _controller(tmp_path)
    (source / "one.bin").write_bytes(b"synthetic")
    entered = threading.Event()
    exited = threading.Event()

    def paused_preflight(abort_signal):
        entered.set()
        try:
            while True:
                abort_signal.raise_if_aborted()
                time.sleep(0.005)
        finally:
            exited.set()

    monkeypatch.setattr(controller, "_preflight", paused_preflight)
    request_id = str(uuid.uuid4())
    await controller.create_capture(
        request_id=request_id,
        expected_runtime_commit=COMMIT,
        expected_recipient_fingerprint=controller.recipient_fingerprint,
        claims=_claims(),
    )
    assert await asyncio.to_thread(entered.wait, 5)
    controller._tasks[request_id].cancel()
    await asyncio.wait_for(asyncio.shield(controller._tasks[request_id]), 5)
    assert exited.is_set()
    assert controller._active_workers == 0
    assert controller.coordinator.status().state == "open"
    assert controller._active_request_id is None


@pytest.mark.asyncio
async def test_max_freeze_deadline_aborts_worker_before_open(tmp_path, monkeypatch):
    import production_backup_capture as capture_module

    limits = CaptureLimits(1, 0.05, 1024 * 1024, 1024 * 1024, minimum_free_bytes=1)
    controller, source, workspace, _, _ = _controller(tmp_path, limits=limits)
    (source / "one.bin").write_bytes(b"synthetic")
    exited = threading.Event()

    def deadline_capture(*args, abort_signal=None, **kwargs):
        try:
            while True:
                abort_signal.raise_if_aborted()
                time.sleep(0.005)
        finally:
            exited.set()

    monkeypatch.setattr(capture_module, "capture_external_source", deadline_capture)
    request_id = str(uuid.uuid4())
    await controller.create_capture(
        request_id=request_id,
        expected_runtime_commit=COMMIT,
        expected_recipient_fingerprint=controller.recipient_fingerprint,
        claims=_claims(),
    )
    result = await _terminal(controller, request_id)
    assert result["state"] == "failed"
    assert result["failure_code"] == "freeze_lease_expired"
    assert exited.is_set() and controller._active_workers == 0
    assert controller.coordinator.status().state == "open"
    assert not list(workspace.bundles_root.glob("*.obbackup"))
    assert not list(workspace.temp_root.iterdir())


@pytest.mark.asyncio
async def test_delivery_lease_blocks_ack_cleanup_and_parallel_delivery(tmp_path):
    now = datetime(2026, 8, 1, tzinfo=timezone.utc)
    controller, source, workspace, _, _ = _controller(tmp_path)
    controller._clock = lambda: now
    (source / "one.bin").write_bytes(b"synthetic")
    request_id = str(uuid.uuid4())
    await controller.create_capture(
        request_id=request_id,
        expected_runtime_commit=COMMIT,
        expected_recipient_fingerprint=controller.recipient_fingerprint,
        claims=_claims(),
    )
    assert (await _terminal(controller, request_id))["state"] == "ready"
    async with controller.delivery(request_id, _claims()) as delivery:
        with pytest.raises(CaptureChannelError, match="capture_delivery_active"):
            await controller.acknowledge(request_id, _claims())
        with pytest.raises(CaptureChannelError, match="capture_delivery_active"):
            async with controller.delivery(request_id, _claims()):
                pass
        now += timedelta(seconds=controller.limits.ready_ttl_seconds + 1)
        assert await controller.cleanup_stale() == 0
        assert delivery.handle.read(1)
    assert next(workspace.bundles_root.glob("*.obbackup")).exists()
    assert (await controller.acknowledge(request_id, _claims()))["state"] == "consumed"


@pytest.mark.asyncio
async def test_run_attempt_is_part_of_job_ownership(tmp_path):
    controller, source, _, _, _ = _controller(tmp_path)
    (source / "one.bin").write_bytes(b"synthetic")
    request_id = str(uuid.uuid4())
    await controller.create_capture(
        request_id=request_id,
        expected_runtime_commit=COMMIT,
        expected_recipient_fingerprint=controller.recipient_fingerprint,
        claims=_claims("123", "1"),
    )
    await _terminal(controller, request_id, _claims("123", "1"))
    with pytest.raises(CaptureChannelError, match="capture_not_found"):
        controller.get_job(request_id, _claims("123", "2"))
    with pytest.raises(CaptureChannelError, match="capture_not_found"):
        async with controller.delivery(request_id, _claims("123", "2")):
            pass
    with pytest.raises(CaptureChannelError, match="capture_not_found"):
        await controller.acknowledge(request_id, _claims("123", "2"))


def test_oidc_requires_exact_repository_and_owner_ids():
    for field, value in (
        ("repository_id", "100"),
        ("repository_owner_id", "89"),
        ("repository_id", ""),
        ("repository_owner_id", ""),
    ):
        claims = _claims()
        claims[field] = value
        with pytest.raises(CaptureChannelError, match="oidc_denied"):
            _policy().verify(claims)


def test_async_claim_verifier_and_redacted_failure(tmp_path):
    controller, _, _, _, _ = _controller(tmp_path)

    async def verifier(request):
        del request
        return _claims()

    app = Starlette(routes=build_backup_v2_routes(controller, verifier))
    with TestClient(app) as client:
        response = client.get(f"/api/backup/v2/captures/{uuid.uuid4()}")
    assert response.status_code == 400
    assert response.json() == {"status": "capture_not_found"}

    async def failing(request):
        del request
        raise RuntimeError("private synthetic detail")

    app = Starlette(routes=build_backup_v2_routes(controller, failing))
    with TestClient(app) as client:
        response = client.get(f"/api/backup/v2/captures/{uuid.uuid4()}")
    assert response.json() == {"status": "internal_error"}
    assert "private synthetic detail" not in response.text


def test_registered_decorator_and_manual_scope_are_structurally_verified():
    root = Path(__file__).parents[1]
    bucket_source = (root / "bucket_manager.py").read_text(encoding="utf-8")
    assert scan_registered_source(bucket_source, "bucket_manager.py") == []
    broken = bucket_source.replace(
        '    @guarded_async_mutation("bucket_create")\n', "", 1
    )
    assert any(
        issue.primitive.startswith("guard_missing")
        for issue in scan_registered_source(broken, "bucket_manager.py")
    )
    embedding_source = (root / "asset_embedding_index.py").read_text(encoding="utf-8")
    broken = embedding_source.replace(
        'with self.write_coordinator.writer_scope("asset_embedding_store"):',
        'if True:',
        1,
    )
    assert any(
        issue.primitive.startswith("guard_missing")
        for issue in scan_registered_source(broken, "asset_embedding_index.py")
    )


def test_dynamic_sql_and_new_production_module_fail_closed(tmp_path):
    issues = scan_unregistered_source(
        "def mutate(connection, statement):\n    connection.execute(statement)\n",
        "new_production.py",
    )
    assert issues[0].primitive == "sqlite_dynamic"
    module = tmp_path / "new_production.py"
    module.write_text("def mutate(path):\n    path.write_bytes(b'x')\n", encoding="utf-8")
    assert scan_registered_write_coverage(tmp_path)


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("def mutate(name, mode):\n    open(name, mode)\n", "open:dynamic"),
        ("def mutate(p, mode):\n    p.open(mode)\n", "open:dynamic"),
        ("def mutate(name, flags):\n    os.open(name, flags)\n", "os.open:write_or_dynamic"),
        ("def mutate(p):\n    p.touch()\n", "touch"),
        ("def mutate(p):\n    p.unlink()\n", "unlink"),
        ("def mutate(a, b):\n    shutil.copy2(a, b)\n", "shutil.copy2"),
        ("def mutate(a, b):\n    shutil.copytree(a, b)\n", "shutil.copytree"),
        ("def mutate(a, b):\n    os.link(a, b)\n", "os.link"),
    ],
)
def test_ambiguous_and_extended_write_primitives_fail_closed(source, expected):
    issues = scan_unregistered_source(source, "new_production.py")
    assert expected in {issue.primitive for issue in issues}


def test_new_module_with_all_extended_write_classes_fails(tmp_path):
    module = tmp_path / "new_production.py"
    module.write_text(
        "import os, shutil\n"
        "def mutate(name, mode, flags, p, a, b, connection, sql):\n"
        "    open(name, mode)\n"
        "    p.open(mode)\n"
        "    os.open(name, flags)\n"
        "    p.touch()\n"
        "    p.unlink()\n"
        "    shutil.copy2(a, b)\n"
        "    shutil.copytree(a, b)\n"
        "    os.link(a, b)\n"
        "    connection.execute(sql)\n",
        encoding="utf-8",
    )
    primitives = {
        issue.primitive for issue in scan_registered_write_coverage(tmp_path)
    }
    assert {
        "open:dynamic", "os.open:write_or_dynamic", "touch", "unlink",
        "shutil.copy2", "shutil.copytree", "os.link", "sqlite_dynamic",
    } <= primitives


@pytest.mark.asyncio
async def test_post_capture_hash_failure_cleans_owned_bundle(tmp_path, monkeypatch):
    import production_backup_capture as capture_module

    controller, source, workspace, _, _ = _controller(tmp_path)
    (source / "one.bin").write_bytes(b"synthetic")
    original_hash = capture_module._hash_file

    def fail_hash(path):
        if path.suffix == ".obbackup":
            raise CaptureChannelError("bundle_invalid")
        return original_hash(path)

    monkeypatch.setattr(capture_module, "_hash_file", fail_hash)
    request_id = str(uuid.uuid4())
    await controller.create_capture(
        request_id=request_id,
        expected_runtime_commit=COMMIT,
        expected_recipient_fingerprint=controller.recipient_fingerprint,
        claims=_claims(),
    )
    result = await _terminal(controller, request_id)
    assert result["state"] == "failed"
    assert result["orphan_present"] is False
    assert not list(workspace.bundles_root.glob("*.obbackup"))


@pytest.mark.asyncio
async def test_ready_metadata_failure_cleans_owned_bundle(tmp_path, monkeypatch):
    controller, source, workspace, _, _ = _controller(tmp_path)
    (source / "one.bin").write_bytes(b"synthetic")
    original_set_state = controller._set_state

    def fail_ready(job, state):
        if state == "ready":
            raise RuntimeError("synthetic metadata failure")
        return original_set_state(job, state)

    monkeypatch.setattr(controller, "_set_state", fail_ready)
    request_id = str(uuid.uuid4())
    await controller.create_capture(
        request_id=request_id,
        expected_runtime_commit=COMMIT,
        expected_recipient_fingerprint=controller.recipient_fingerprint,
        claims=_claims(),
    )
    result = await _terminal(controller, request_id)
    assert result["state"] == "failed"
    assert result["failure_code"] == "internal_error"
    assert result["orphan_present"] is False
    assert not list(workspace.bundles_root.glob("*.obbackup"))


@pytest.mark.parametrize("phase", ["hash", "sqlite", "encryption"])
@pytest.mark.asyncio
async def test_deadline_is_observed_inside_capture_phases(tmp_path, monkeypatch, phase):
    import offline_backup_bundle as bundle_module

    limits = CaptureLimits(1, 0.5, 4 * 1024 * 1024, 8 * 1024 * 1024, minimum_free_bytes=1)
    controller, source, workspace, _, _ = _controller(tmp_path, limits=limits)
    if phase == "sqlite":
        with sqlite3.connect(source / "data.sqlite3") as connection:
            connection.execute("CREATE TABLE values_table(value TEXT)")
            connection.execute("INSERT INTO values_table VALUES ('synthetic')")
    else:
        (source / "one.bin").write_bytes(b"synthetic")
    target_name = {
        "hash": "_hash_file",
        "sqlite": "_snapshot_sqlite",
        "encryption": "_encrypt_archive",
    }[phase]
    original = getattr(bundle_module, target_name)
    observed = threading.Event()
    controller._preflight = lambda abort_signal: abort_signal.raise_if_aborted()

    def delayed(*args, abort_signal=None, **kwargs):
        if abort_signal is None:
            return original(*args, abort_signal=abort_signal, **kwargs)
        observed.set()
        while True:
            abort_signal.raise_if_aborted()
            time.sleep(0.005)

    monkeypatch.setattr(bundle_module, target_name, delayed)
    request_id = str(uuid.uuid4())
    await controller.create_capture(
        request_id=request_id,
        expected_runtime_commit=COMMIT,
        expected_recipient_fingerprint=controller.recipient_fingerprint,
        claims=_claims(),
    )
    result = await _terminal(controller, request_id)
    assert observed.is_set()
    assert result["failure_code"] == "freeze_lease_expired"
    assert controller._active_workers == 0
    assert controller.coordinator.status().state == "open"
    assert not list(workspace.bundles_root.glob("*.obbackup"))
    monkeypatch.setattr(bundle_module, target_name, original)


def test_create_route_returns_before_controller_owned_job_finishes(tmp_path, monkeypatch):
    import production_backup_capture as capture_module

    controller, source, _, _, _ = _controller(tmp_path)
    (source / "one.bin").write_bytes(b"synthetic")
    entered = threading.Event()
    release = threading.Event()
    original = capture_module.capture_external_source

    def paused(*args, **kwargs):
        entered.set()
        if not release.wait(5):
            raise RuntimeError("synthetic pause timeout")
        return original(*args, **kwargs)

    monkeypatch.setattr(capture_module, "capture_external_source", paused)

    async def verifier(request):
        del request
        return _claims()

    app = Starlette(routes=build_backup_v2_routes(controller, verifier))
    request_id = str(uuid.uuid4())
    with TestClient(app) as client:
        created = client.post("/api/backup/v2/captures", json={
            "request_id": request_id,
            "expected_runtime_commit": COMMIT,
            "expected_recipient_fingerprint": controller.recipient_fingerprint,
        })
        assert created.status_code == 202
        assert created.json()["state"] == "accepted"
        assert entered.wait(5)
        status = client.get(f"/api/backup/v2/captures/{request_id}")
        assert status.json()["state"] == "capturing"
        assert controller.coordinator.status().state == "frozen"
        release.set()
        for _ in range(300):
            status = client.get(f"/api/backup/v2/captures/{request_id}")
            if status.json()["state"] in {"ready", "failed"}:
                break
            time.sleep(0.01)
        assert status.json()["state"] == "ready"
    assert controller.coordinator.status().state == "open"


@pytest.mark.asyncio
async def test_frozen_source_limit_rechecks_after_existing_writer_drains(tmp_path, monkeypatch):
    import offline_backup_bundle as bundle_module

    coordinator = MaintenanceWriteCoordinator()
    limits = CaptureLimits(2, 10, 64, 1024 * 1024, minimum_free_bytes=1)
    controller, source, workspace, _, _ = _controller(
        tmp_path, coordinator=coordinator, limits=limits
    )
    source_file = source / "growing.bin"
    source_file.write_bytes(b"x")
    writer_entered = asyncio.Event()
    enlarge = asyncio.Event()
    staging_started = threading.Event()
    original_capture_source = bundle_module._capture_source

    def observed_capture_source(*args, **kwargs):
        staging_started.set()
        return original_capture_source(*args, **kwargs)

    monkeypatch.setattr(bundle_module, "_capture_source", observed_capture_source)

    async def existing_writer():
        async with coordinator.async_writer_scope("synthetic_growth"):
            writer_entered.set()
            await enlarge.wait()
            source_file.write_bytes(b"x" * 128)

    writer = asyncio.create_task(existing_writer())
    await writer_entered.wait()
    request_id = str(uuid.uuid4())
    await controller.create_capture(
        request_id=request_id,
        expected_runtime_commit=COMMIT,
        expected_recipient_fingerprint=controller.recipient_fingerprint,
        claims=_claims(),
    )
    for _ in range(300):
        if controller.get_job(request_id, _claims())["state"] == "draining":
            break
        await asyncio.sleep(0.01)
    assert controller.get_job(request_id, _claims())["state"] == "draining"
    enlarge.set()
    await writer
    result = await _terminal(controller, request_id)
    assert result["failure_code"] == "capture_source_too_large"
    assert not staging_started.is_set()
    assert controller.coordinator.status().state == "open"
    assert controller._active_workers == 0
    assert not list(workspace.bundles_root.glob("*.obbackup"))
    assert not any(workspace.temp_root.iterdir())


@pytest.mark.asyncio
async def test_frozen_space_recheck_happens_before_plaintext_staging(tmp_path, monkeypatch):
    import offline_backup_bundle as bundle_module

    calls = 0

    def changing_disk_usage(path):
        nonlocal calls
        del path
        calls += 1
        return SimpleNamespace(free=10**9 if calls == 1 else 0)

    controller, source, workspace, _, _ = _controller(
        tmp_path, disk_usage=changing_disk_usage
    )
    (source / "one.bin").write_bytes(b"synthetic")
    staging_started = threading.Event()
    original_capture_source = bundle_module._capture_source

    def observed_capture_source(*args, **kwargs):
        staging_started.set()
        return original_capture_source(*args, **kwargs)

    monkeypatch.setattr(bundle_module, "_capture_source", observed_capture_source)
    request_id = str(uuid.uuid4())
    await controller.create_capture(
        request_id=request_id,
        expected_runtime_commit=COMMIT,
        expected_recipient_fingerprint=controller.recipient_fingerprint,
        claims=_claims(),
    )
    result = await _terminal(controller, request_id)
    assert result["failure_code"] == "capture_space_insufficient"
    assert calls >= 2
    assert not staging_started.is_set()
    assert not list(workspace.bundles_root.glob("*.obbackup"))
    assert not any(workspace.temp_root.iterdir())


@pytest.mark.asyncio
async def test_encrypted_output_limit_stops_before_formal_publication(tmp_path):
    limits = CaptureLimits(2, 10, 1024 * 1024, 128, minimum_free_bytes=1)
    controller, source, workspace, _, _ = _controller(tmp_path, limits=limits)
    (source / "one.bin").write_bytes(b"synthetic payload")
    request_id = str(uuid.uuid4())
    await controller.create_capture(
        request_id=request_id,
        expected_runtime_commit=COMMIT,
        expected_recipient_fingerprint=controller.recipient_fingerprint,
        claims=_claims(),
    )
    result = await _terminal(controller, request_id)
    assert result["failure_code"] == "capture_bundle_too_large"
    assert controller.coordinator.status().state == "open"
    assert controller._active_workers == 0
    assert not list(workspace.bundles_root.glob("*.obbackup"))
    assert not any(workspace.temp_root.iterdir())


async def _ready_capture(controller, source):
    (source / f"{uuid.uuid4()}.bin").write_bytes(b"synthetic delivery")
    request_id = str(uuid.uuid4())
    await controller.create_capture(
        request_id=request_id,
        expected_runtime_commit=COMMIT,
        expected_recipient_fingerprint=controller.recipient_fingerprint,
        claims=_claims(),
    )
    assert (await _terminal(controller, request_id))["state"] == "ready"
    return request_id


def _download_request(request_id):
    from starlette.requests import Request

    return Request({
        "type": "http",
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": f"/api/backup/v2/captures/{request_id}/bundle",
        "raw_path": b"/api/backup/v2/captures/bundle",
        "query_string": b"",
        "headers": [],
        "client": ("test", 1),
        "server": ("test", 80),
        "path_params": {"request_id": request_id},
    })


@pytest.mark.asyncio
async def test_route_cancel_during_delivery_prehash_joins_worker(tmp_path, monkeypatch):
    import production_backup_capture as capture_module

    controller, source, _, _, _ = _controller(tmp_path)
    request_id = await _ready_capture(controller, source)
    original_hash = capture_module._hash_handle
    entered = threading.Event()
    release = threading.Event()

    def paused_hash(handle):
        entered.set()
        if not release.wait(5):
            raise RuntimeError("synthetic hash pause")
        return original_hash(handle)

    monkeypatch.setattr(capture_module, "_hash_handle", paused_hash)
    routes = build_backup_v2_routes(controller, lambda request: _claims())
    endpoint = next(route.endpoint for route in routes if route.path.endswith("/bundle"))
    task = asyncio.create_task(endpoint(_download_request(request_id)))
    assert await asyncio.to_thread(entered.wait, 5)
    task.cancel()
    await asyncio.sleep(0)
    assert request_id in controller._active_deliveries
    with pytest.raises(CaptureChannelError, match="capture_delivery_active"):
        await controller.acknowledge(request_id, _claims())
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert request_id not in controller._active_deliveries
    assert controller._active_workers == 0
    assert controller.get_job(request_id, _claims())["state"] == "ready"
    async with controller.delivery(request_id, _claims()) as retry:
        assert retry.handle.read(1)
    assert (await controller.acknowledge(request_id, _claims()))["state"] == "consumed"


@pytest.mark.asyncio
async def test_asgi_midstream_cancel_releases_delivery_and_allows_retry(tmp_path):
    controller, source, _, _, _ = _controller(tmp_path)
    request_id = await _ready_capture(controller, source)
    routes = build_backup_v2_routes(controller, lambda request: _claims())
    endpoint = next(route.endpoint for route in routes if route.path.endswith("/bundle"))
    response = await endpoint(_download_request(request_id))
    first_body = asyncio.Event()

    async def receive():
        await asyncio.Event().wait()

    async def send(message):
        if message["type"] == "http.response.body" and message.get("more_body"):
            first_body.set()
            await asyncio.Event().wait()

    response_task = asyncio.create_task(response(_download_request(request_id).scope, receive, send))
    await asyncio.wait_for(first_body.wait(), 5)
    assert request_id in controller._active_deliveries
    response_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await response_task
    assert request_id not in controller._active_deliveries
    assert controller.get_job(request_id, _claims())["state"] == "ready"
    async with controller.delivery(request_id, _claims()) as retry:
        assert retry.handle.read(1)
    assert (await controller.acknowledge(request_id, _claims()))["state"] == "consumed"


@pytest.mark.asyncio
async def test_asgi_stream_exception_releases_delivery_and_preserves_bundle(tmp_path):
    controller, source, _, _, _ = _controller(tmp_path)
    request_id = await _ready_capture(controller, source)
    routes = build_backup_v2_routes(controller, lambda request: _claims())
    endpoint = next(route.endpoint for route in routes if route.path.endswith("/bundle"))
    response = await endpoint(_download_request(request_id))

    async def broken_body():
        yield b"synthetic-prefix"
        raise RuntimeError("synthetic stream failure")

    response.body_iterator = broken_body()

    async def receive():
        await asyncio.Event().wait()

    async def send(message):
        del message

    with pytest.raises(RuntimeError, match="synthetic stream failure"):
        await response(_download_request(request_id).scope, receive, send)
    assert request_id not in controller._active_deliveries
    assert controller.get_job(request_id, _claims())["state"] == "ready"
    async with controller.delivery(request_id, _claims()) as retry:
        assert retry.handle.read(1)
    assert (await controller.acknowledge(request_id, _claims()))["state"] == "consumed"


@pytest.mark.asyncio
async def test_response_construction_failure_releases_delivery(tmp_path, monkeypatch):
    import production_backup_capture as capture_module

    controller, source, _, _, _ = _controller(tmp_path)
    request_id = await _ready_capture(controller, source)

    class FailingResponse:
        def __init__(self, *args, **kwargs):
            raise RuntimeError("synthetic response construction")

    monkeypatch.setattr(capture_module, "_ManagedStreamingResponse", FailingResponse)
    routes = build_backup_v2_routes(controller, lambda request: _claims())
    endpoint = next(route.endpoint for route in routes if route.path.endswith("/bundle"))
    response = await endpoint(_download_request(request_id))
    assert response.status_code == 400
    assert json.loads(response.body)["status"] == "internal_error"
    assert request_id not in controller._active_deliveries
    assert controller.get_job(request_id, _claims())["state"] == "ready"


@pytest.mark.asyncio
async def test_sequential_terminal_jobs_release_task_registry(tmp_path):
    controller, source, _, _, _ = _controller(tmp_path)
    completed = []
    for _ in range(3):
        request_id = await _ready_capture(controller, source)
        await asyncio.sleep(0)
        assert controller._tasks == {}
        assert controller.get_job(request_id, _claims())["state"] == "ready"
        completed.append(request_id)
        await controller.acknowledge(request_id, _claims())
    assert all(
        controller.get_job(request_id, _claims())["state"] == "consumed"
        for request_id in completed
    )


def _synthetic_capture_job(controller, request_id):
    now = controller._timestamp()
    return CaptureJob(
        request_id=request_id,
        oidc_run_id="123",
        oidc_run_attempt="1",
        runtime_commit=COMMIT,
        recipient_fingerprint=controller.recipient_fingerprint,
        state="capturing",
        created_at=now,
        updated_at=now,
    )


def _synthetic_capture_result(bundle_name):
    return CaptureResult(
        status="success",
        bundle_id=bundle_name[:32],
        bundle_name=bundle_name,
        manifest_sha256="a" * 64,
        entry_count=1,
        ordinary_file_count=1,
        sqlite_snapshot_count=0,
        total_plaintext_bytes=1,
        exclusion_count=0,
    )


@pytest.mark.asyncio
async def test_successful_worker_result_is_owned_before_cancellation_propagates(tmp_path):
    controller, _, workspace, _, _ = _controller(tmp_path)
    request_id = str(uuid.uuid4())
    job = _synthetic_capture_job(controller, request_id)
    bundle_name = "a" * 32 + ".obbackup"
    (workspace.bundles_root / bundle_name).write_bytes(b"synthetic bundle")
    worker = asyncio.create_task(
        asyncio.sleep(0, result=_synthetic_capture_result(bundle_name))
    )
    signal = CaptureAbortSignal(
        deadline=controller.coordinator.monotonic() + 10,
        monotonic=controller.coordinator.monotonic,
    )
    asyncio.get_running_loop().call_soon(asyncio.current_task().cancel)
    with pytest.raises(asyncio.CancelledError):
        await controller._await_capture_worker(
            worker,
            job,
            signal,
            controller.coordinator.monotonic() + 10,
        )
    assert job.bundle_name == bundle_name
    await controller._fail_and_cleanup(job, "capture_cancelled")
    assert job.failure_code == "capture_cancelled"
    assert job.orphan_present is False
    assert not (workspace.bundles_root / bundle_name).exists()
    assert controller.coordinator.status().state == "open"
    assert controller._active_workers == 0


@pytest.mark.asyncio
async def test_deadline_worker_result_is_owned_before_expiry_propagates(tmp_path):
    controller, _, workspace, _, _ = _controller(tmp_path)
    request_id = str(uuid.uuid4())
    job = _synthetic_capture_job(controller, request_id)
    bundle_name = "b" * 32 + ".obbackup"
    (workspace.bundles_root / bundle_name).write_bytes(b"synthetic bundle")

    async def delayed_success():
        await asyncio.sleep(0.02)
        return _synthetic_capture_result(bundle_name)

    worker = asyncio.create_task(delayed_success())
    signal = CaptureAbortSignal(
        deadline=controller.coordinator.monotonic() + 0.001,
        monotonic=controller.coordinator.monotonic,
    )
    with pytest.raises(CaptureChannelError, match="freeze_lease_expired"):
        await controller._await_capture_worker(
            worker,
            job,
            signal,
            controller.coordinator.monotonic() + 0.001,
        )
    assert job.bundle_name == bundle_name
    await controller._fail_and_cleanup(job, "freeze_lease_expired")
    assert job.failure_code == "freeze_lease_expired"
    assert job.orphan_present is False
    assert not (workspace.bundles_root / bundle_name).exists()
    assert controller.coordinator.status().state == "open"


@pytest.mark.asyncio
async def test_owned_bundle_cleanup_failure_is_attributable_and_contained(tmp_path, monkeypatch):
    import production_backup_capture as capture_module

    controller, _, workspace, _, _ = _controller(tmp_path)
    request_id = str(uuid.uuid4())
    job = _synthetic_capture_job(controller, request_id)
    bundle_name = "c" * 32 + ".obbackup"
    bundle = workspace.bundles_root / bundle_name
    unrelated = workspace.bundles_root / ("d" * 32 + ".obbackup")
    bundle.write_bytes(b"owned")
    unrelated.write_bytes(b"unrelated")
    original_unlink = capture_module.Path.unlink

    def fail_owned_unlink(path, *args, **kwargs):
        if path == bundle:
            raise OSError("synthetic cleanup failure")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(capture_module.Path, "unlink", fail_owned_unlink)
    job.bundle_name = bundle_name
    await controller._fail_and_cleanup(job, "capture_cancelled")
    assert job.failure_code == "internal_error"
    assert job.orphan_present is True
    assert job.bundle_name == bundle_name
    assert bundle.read_bytes() == b"owned"
    assert unrelated.read_bytes() == b"unrelated"


@pytest.mark.asyncio
async def test_repeated_asgi_cancellation_waits_for_delivery_release(tmp_path):
    controller, source, _, _, _ = _controller(tmp_path)
    request_id = await _ready_capture(controller, source)
    routes = build_backup_v2_routes(controller, lambda request: _claims())
    endpoint = next(route.endpoint for route in routes if route.path.endswith("/bundle"))
    response = await endpoint(_download_request(request_id))
    first_body = asyncio.Event()
    body_release = asyncio.Event()

    async def receive():
        await asyncio.Event().wait()

    async def send(message):
        if message["type"] == "http.response.body" and message.get("more_body"):
            first_body.set()
            await body_release.wait()

    await controller._job_lock.acquire()
    response_task = asyncio.create_task(
        response(_download_request(request_id).scope, receive, send)
    )
    await asyncio.wait_for(first_body.wait(), 5)
    response_task.cancel()
    await asyncio.sleep(0)
    response_task.cancel()
    await asyncio.sleep(0)
    assert not response_task.done()
    assert request_id in controller._active_deliveries
    controller._job_lock.release()
    body_release.set()
    with pytest.raises(asyncio.CancelledError):
        await response_task
    assert request_id not in controller._active_deliveries
    assert controller._active_workers == 0
    assert controller.get_job(request_id, _claims())["state"] == "ready"
    async with controller.delivery(request_id, _claims()) as retry:
        assert retry.handle.read(1)
    assert (await controller.acknowledge(request_id, _claims()))["state"] == "consumed"


@pytest.mark.asyncio
async def test_repeated_cancellation_during_response_failure_releases_delivery(
    tmp_path, monkeypatch
):
    import production_backup_capture as capture_module

    controller, source, _, _, _ = _controller(tmp_path)
    request_id = await _ready_capture(controller, source)

    class FailingResponse:
        def __init__(self, *args, **kwargs):
            current = asyncio.current_task()
            asyncio.get_running_loop().call_soon(current.cancel)
            asyncio.get_running_loop().call_soon(current.cancel)
            raise RuntimeError("synthetic response construction")

    monkeypatch.setattr(capture_module, "_ManagedStreamingResponse", FailingResponse)
    routes = build_backup_v2_routes(controller, lambda request: _claims())
    endpoint = next(route.endpoint for route in routes if route.path.endswith("/bundle"))
    with pytest.raises(asyncio.CancelledError):
        await endpoint(_download_request(request_id))
    assert request_id not in controller._active_deliveries
    assert controller._active_workers == 0
    assert controller.get_job(request_id, _claims())["state"] == "ready"
    async with controller.delivery(request_id, _claims()) as retry:
        assert retry.handle.read(1)
    assert (await controller.acknowledge(request_id, _claims()))["state"] == "consumed"

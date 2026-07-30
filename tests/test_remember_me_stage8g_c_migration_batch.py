import hashlib
import io
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import sqlite3
import threading
import time

import pytest
from PIL import Image

from asset_migration_state import (
    HostMigrationState,
    HostMigrationStateError,
    canonical_path_identity,
)
from asset_store import AssetStore, AssetStoreError
from remember_me_import_adapter import (
    LegacyAssetImportAdapter,
    LegacyAssetImportDisposition,
    LegacyAssetImportErrorCode,
    LegacyAssetImportRequest,
    LegacyAssetImportResult,
    create_legacy_asset_import_fixture_context,
)
from remember_me_migration_runner import (
    HostMigrationRunnerError,
    MIGRATION_KEY,
    MigrationBatchResult,
    run_migration_batch,
)


ROOT = Path(__file__).resolve().parent.parent
OWNER_ONE = "1" * 64
OWNER_TWO = "2" * 64


class MutableClock:
    def __init__(self):
        self.value = datetime(2026, 7, 30, tzinfo=timezone.utc)

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += timedelta(seconds=seconds)


def _state(tmp_path, *, clock=None, busy_timeout_ms=5_000):
    legacy_root = tmp_path / "legacy"
    target_root = tmp_path / "rm"
    legacy_root.mkdir(exist_ok=True)
    target_root.mkdir(exist_ok=True)
    return HostMigrationState(
        tmp_path / "migration.sqlite3",
        legacy_root=legacy_root,
        target_root=target_root,
        clock=clock,
        busy_timeout_ms=busy_timeout_ms,
    )


def _png_bytes(color):
    output = io.BytesIO()
    image = Image.new("RGB", (7, 5), color)
    image.save(output, format="PNG")
    image.close()
    return output.getvalue()


def _persist_image(store, color):
    payload = _png_bytes(color)
    source = store.create_temp_path(".png")
    source.write_bytes(payload)
    return store.persist_upload(
        source,
        hashlib.sha256(payload).hexdigest(),
        len(payload),
        "{}.png".format(color),
        "image/png",
        require_image=True,
    )


def _runner_fixture(tmp_path, colors=("red",)):
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
    assets = [_persist_image(store, color) for color in colors]
    return fixture, state, store, adapter, runtime, assets


def _run(state, store, adapter, batch_size=100, **kwargs):
    return run_migration_batch(
        legacy_store=store,
        adapter=adapter,
        migration_state=state,
        source_identity=state.source_identity,
        target_identity=state.target_identity,
        batch_size=batch_size,
        **kwargs,
    )


def _insert_asset_ids(store, asset_ids):
    now = "2026-07-30T00:00:00+00:00"
    with store._connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        for index, asset_id in enumerate(asset_ids):
            digest = "{:064x}".format(index + 1)
            connection.execute(
                """
                INSERT INTO assets (
                    asset_id, source_sha256, stored_sha256, stored_relpath,
                    original_filename, mime_type, kind, decoded_bytes,
                    stored_bytes, width, height, created_at, title,
                    description, updated_at
                ) VALUES (?, ?, ?, ?, ?, 'image/png', 'image', 1, 1,
                          1, 1, ?, '', '', ?)
                """,
                (
                    asset_id,
                    digest,
                    digest,
                    "assets/{}/{}.png".format(digest[:2], digest),
                    "{}.png".format(asset_id),
                    now,
                    now,
                ),
            )


def test_default_asset_store_is_compatible_and_creates_no_migration_db(tmp_path):
    root = tmp_path / "legacy"
    store = AssetStore(root)
    asset = _persist_image(store, "red")

    assert store.migration_write_gate is None
    assert store.get(asset["asset_id"]) is not None
    assert not (root / "migration.sqlite3").exists()
    assert not (tmp_path / "migration.sqlite3").exists()


def test_freeze_blocks_all_public_writes_but_not_reads(tmp_path):
    state = _state(tmp_path)
    store = AssetStore(tmp_path / "legacy", write_gate=state)
    asset = _persist_image(store, "red")
    before = store.get_import_record(asset["asset_id"])
    blob = store.resolve_file(asset["asset_id"])[1]
    external_source = tmp_path / "caller-owned.png"
    payload = _png_bytes("blue")
    external_source.write_bytes(payload)
    owner = state.acquire_freeze(ttl_seconds=60, owner_token=OWNER_ONE)

    with pytest.raises(AssetStoreError, match="^asset_write_frozen$"):
        store.create_temp_path()
    with pytest.raises(AssetStoreError, match="^asset_write_frozen$"):
        store.persist_upload(
            external_source,
            hashlib.sha256(payload).hexdigest(),
            len(payload),
            "blue.png",
            "image/png",
            require_image=True,
        )
    assert external_source.read_bytes() == payload
    with pytest.raises(AssetStoreError, match="^asset_write_frozen$"):
        store.update_metadata(asset["asset_id"], title="changed")
    with pytest.raises(AssetStoreError, match="^asset_write_frozen$"):
        store.delete(asset["asset_id"])

    assert store.get(asset["asset_id"]) is not None
    assert store.get_import_record(asset["asset_id"]) == before
    assert store.resolve_file(asset["asset_id"])[1] == blob
    assert blob.is_file()
    assert store.search()["total"] == 1
    assert len(store.list_for_embedding()) == 1
    upper, count = store.get_migration_snapshot_bounds()
    assert count == 1
    assert store.list_asset_ids_for_migration(
        last_asset_id=None,
        upper_bound_asset_id=upper,
        batch_size=1,
    ) == [asset["asset_id"]]
    assert state.release_freeze(owner)


def test_gate_database_failure_is_fail_closed(tmp_path):
    state = _state(tmp_path)
    store = AssetStore(tmp_path / "legacy", write_gate=state)
    with sqlite3.connect(state.db_path) as connection:
        connection.execute("DROP TABLE freeze_lease")

    with pytest.raises(
        AssetStoreError,
        match="^asset_write_gate_unavailable$",
    ):
        store.create_temp_path()
    assert list(store.temp_dir.iterdir()) == []


def test_lease_acquire_renew_release_expiry_and_ownership(tmp_path):
    clock = MutableClock()
    state = _state(tmp_path, clock=clock)
    assert state.acquire_freeze(
        ttl_seconds=10,
        owner_token=OWNER_ONE,
    ) == OWNER_ONE
    with pytest.raises(
        HostMigrationStateError,
        match="^migration_freeze_busy$",
    ):
        state.acquire_freeze(ttl_seconds=10, owner_token=OWNER_TWO)
    with pytest.raises(
        HostMigrationStateError,
        match="^migration_freeze_lost$",
    ):
        state.renew_freeze(OWNER_TWO, ttl_seconds=10)
    assert state.release_freeze(OWNER_TWO) is False

    state.renew_freeze(OWNER_ONE, ttl_seconds=20)
    clock.advance(21)
    assert state.acquire_freeze(
        ttl_seconds=10,
        owner_token=OWNER_TWO,
    ) == OWNER_TWO
    with pytest.raises(
        HostMigrationStateError,
        match="^migration_freeze_lost$",
    ):
        state.assert_freeze_owner(OWNER_ONE)
    assert state.release_freeze(OWNER_ONE) is False
    assert state.release_freeze(OWNER_TWO) is True


def test_unknown_schema_version_fails_closed(tmp_path):
    state = _state(tmp_path)
    with sqlite3.connect(state.db_path) as connection:
        connection.execute(
            "UPDATE migration_schema SET schema_version = 999"
        )
    with pytest.raises(
        HostMigrationStateError,
        match="^migration_schema_incompatible$",
    ):
        _state(tmp_path)


def test_writer_and_freeze_have_no_check_then_act_window(
    tmp_path,
    monkeypatch,
):
    state = _state(tmp_path, busy_timeout_ms=5_000)
    store = AssetStore(tmp_path / "legacy", write_gate=state)
    writer_entered = threading.Event()
    writer_may_finish = threading.Event()
    writer_done = threading.Event()
    freeze_done = threading.Event()
    freeze_entered_coordination = threading.Event()
    order = []
    original_create = store._create_temp_path_unchecked
    original_connect = state._connect

    def held_create(suffix=".upload"):
        writer_entered.set()
        assert writer_may_finish.wait(5)
        path = original_create(suffix)
        order.append("writer")
        return path

    monkeypatch.setattr(store, "_create_temp_path_unchecked", held_create)

    class SignalingConnection:
        def __init__(self, connection):
            self._connection = connection

        def execute(self, sql, *args):
            if sql.strip().upper() == "BEGIN IMMEDIATE":
                freeze_entered_coordination.set()
            return self._connection.execute(sql, *args)

        def __getattr__(self, name):
            return getattr(self._connection, name)

    def signaling_connect():
        connection = original_connect()
        if threading.current_thread().name == "stage8gc-freezer":
            return SignalingConnection(connection)
        return connection

    monkeypatch.setattr(state, "_connect", signaling_connect)

    def writer():
        path = store.create_temp_path()
        path.unlink()
        writer_done.set()

    def freezer():
        state.acquire_freeze(
            ttl_seconds=60,
            owner_token=OWNER_ONE,
        )
        order.append("freeze")
        freeze_done.set()

    writer_thread = threading.Thread(target=writer)
    freeze_thread = threading.Thread(
        target=freezer,
        name="stage8gc-freezer",
    )
    writer_thread.start()
    assert writer_entered.wait(5)
    freeze_thread.start()
    assert freeze_entered_coordination.wait(5)
    assert not freeze_done.is_set()
    writer_may_finish.set()
    assert writer_done.wait(5)
    assert freeze_done.wait(5)
    writer_thread.join()
    freeze_thread.join()

    assert order == ["writer", "freeze"]
    assert state.current_generation() == 0
    with pytest.raises(AssetStoreError, match="^asset_write_frozen$"):
        store.create_temp_path()


def test_generation_advances_only_for_persistent_legacy_changes(tmp_path):
    state = _state(tmp_path)
    store = AssetStore(tmp_path / "legacy", write_gate=state)
    assert state.current_generation() == 0
    temporary = store.create_temp_path()
    assert state.current_generation() == 0
    temporary.unlink()

    asset = _persist_image(store, "red")
    assert state.current_generation() == 1
    duplicate = _persist_image(store, "red")
    assert duplicate["asset_id"] == asset["asset_id"]
    assert duplicate["deduplicated"] is True
    assert state.current_generation() == 1
    store.update_metadata(asset["asset_id"], title="new")
    assert state.current_generation() == 2
    store.update_metadata(asset["asset_id"], title="new")
    assert state.current_generation() == 2
    store.delete(asset["asset_id"])
    assert state.current_generation() == 3

    owner = state.acquire_freeze(ttl_seconds=60, owner_token=OWNER_ONE)
    assert state.current_generation() == 3
    state.create_checkpoint(
        owner_token=owner,
        migration_key="generation-test",
        migration_version=1,
        source_identity=state.source_identity,
        target_identity=state.target_identity,
        snapshot_generation=3,
        upper_bound_asset_id=None,
        initial_asset_count=0,
    )
    assert state.current_generation() == 3
    assert state.release_freeze(owner)
    assert state.current_generation() == 3


def test_delete_cleanup_failure_still_advances_generation(
    tmp_path,
    monkeypatch,
):
    state = _state(tmp_path)
    store = AssetStore(tmp_path / "legacy", write_gate=state)
    asset = _persist_image(store, "red")
    original_unlink = Path.unlink

    def fail_quarantine_cleanup(path, *args, **kwargs):
        if path.name.startswith("delete-"):
            raise OSError("synthetic cleanup failure")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_quarantine_cleanup)
    result = store.delete(asset["asset_id"])
    assert result["deleted"] is True
    assert result["cleanup_pending"] is True
    assert state.current_generation() == 2
    assert store.get(asset["asset_id"]) is None


def test_generation_finalize_failure_leaves_source_uncertain_and_blocks_resume(
    tmp_path,
    monkeypatch,
):
    fixture, state, store, adapter, _, assets = _runner_fixture(
        tmp_path,
        colors=("red", "blue"),
    )
    first = _run(state, store, adapter, batch_size=1)
    assert first.status == "paused"
    checkpoint_before = state.get_checkpoint(MIGRATION_KEY)
    original_finalize = state._finalize_write

    def fail_finalize(connection, owner_token, outcome):
        raise sqlite3.OperationalError("synthetic finalize failure")

    monkeypatch.setattr(state, "_finalize_write", fail_finalize)
    with pytest.raises(
        AssetStoreError,
        match="^asset_write_gate_unavailable$",
    ):
        store.update_metadata(assets[0]["asset_id"], title="committed change")
    assert store.get(assets[0]["asset_id"])["title"] == "committed change"

    monkeypatch.setattr(state, "_finalize_write", original_finalize)
    calls = []
    monkeypatch.setattr(
        adapter,
        "import_asset",
        lambda request: calls.append(request),
    )
    with pytest.raises(HostMigrationRunnerError) as captured:
        _run(state, store, adapter, batch_size=1)
    assert captured.value.code == "source_generation_uncertain"
    assert calls == []
    assert state.get_checkpoint(MIGRATION_KEY) == checkpoint_before
    fixture.close()


def test_keyset_pagination_is_strict_validated_and_deterministic(tmp_path):
    store = AssetStore(tmp_path / "legacy")
    ids = ["f" * 32, "1" * 32, "a" * 32, "5" * 32]
    _insert_asset_ids(store, ids)
    upper, count = store.get_migration_snapshot_bounds()

    first = store.list_asset_ids_for_migration(
        last_asset_id=None,
        upper_bound_asset_id=upper,
        batch_size=2,
    )
    second = store.list_asset_ids_for_migration(
        last_asset_id=first[-1],
        upper_bound_asset_id=upper,
        batch_size=2,
    )
    assert count == 4
    assert first + second == sorted(ids)
    assert len(set(first + second)) == 4
    assert store.list_asset_ids_for_migration(
        last_asset_id="a" * 32,
        upper_bound_asset_id="a" * 32,
        batch_size=1,
    ) == []

    for bad in ("A" * 32, "bad", ""):
        with pytest.raises(AssetStoreError, match="invalid_migration_cursor"):
            store.list_asset_ids_for_migration(
                last_asset_id=bad,
                upper_bound_asset_id=upper,
                batch_size=1,
            )
    for bad_upper in (
        "A" * 32,
        "a" * 31,
        "a" * 33,
        "g" * 32,
        "",
        True,
        1,
    ):
        with pytest.raises(
            AssetStoreError,
            match="invalid_migration_upper_bound",
        ):
            store.list_asset_ids_for_migration(
                last_asset_id=None,
                upper_bound_asset_id=bad_upper,
                batch_size=1,
            )
    for bad_limit in (True, 0, 501, "1"):
        with pytest.raises(
            AssetStoreError,
            match="invalid_migration_batch_size",
        ):
            store.list_asset_ids_for_migration(
                last_asset_id=None,
                upper_bound_asset_id=upper,
                batch_size=bad_limit,
            )
    assert "OFFSET" not in (
        ROOT / "asset_store.py"
    ).read_text(encoding="utf-8").split(
        "def list_asset_ids_for_migration", 1
    )[1].split("def list_for_embedding", 1)[0].upper()


def test_empty_store_completes_without_adapter_call(tmp_path, monkeypatch):
    fixture, state, store, adapter, _, _ = _runner_fixture(
        tmp_path,
        colors=(),
    )
    calls = []
    monkeypatch.setattr(
        adapter,
        "import_asset",
        lambda request: calls.append(request),
    )
    result = _run(state, store, adapter, batch_size=3)

    assert result.status == "completed"
    assert result.completed
    assert result.processed_count == 0
    assert result.upper_bound_asset_id is None
    assert calls == []
    fixture.close()


def test_runner_pauses_resumes_and_completed_rerun_is_idempotent(
    tmp_path,
    monkeypatch,
):
    fixture, state, store, adapter, runtime, assets = _runner_fixture(
        tmp_path,
        colors=("red", "green", "blue"),
    )
    requests = []
    original_import = adapter.import_asset

    def recording_import(request):
        requests.append(request)
        return original_import(request)

    monkeypatch.setattr(adapter, "import_asset", recording_import)
    first = _run(state, store, adapter, batch_size=2)
    assert first.status == "paused"
    assert first.batch_processed_count == 2
    assert first.processed_count == 2
    assert all(
        isinstance(request, LegacyAssetImportRequest)
        and request.dry_run is False
        for request in requests
    )

    state = HostMigrationState(
        state.db_path,
        legacy_root=fixture.legacy_root,
        target_root=fixture.rm_root,
    )
    store = AssetStore(fixture.legacy_root, write_gate=state)
    fixture.bind_legacy_store(store)
    fixture.bind_core(runtime.service)
    adapter = LegacyAssetImportAdapter(
        legacy_store=store,
        core=runtime.service,
        fixture_context=fixture,
    )
    original_import = adapter.import_asset
    monkeypatch.setattr(adapter, "import_asset", recording_import)
    second = _run(state, store, adapter, batch_size=2)
    assert second.status == "completed"
    assert second.processed_count == 3
    assert second.imported_count == 3
    assert second.last_completed_asset_id == max(
        asset["asset_id"] for asset in assets
    )
    count_after_completion = len(requests)
    third = _run(state, store, adapter, batch_size=2)
    assert third == MigrationBatchResult(
        status="completed",
        batch_processed_count=0,
        processed_count=3,
        imported_count=3,
        skipped_idempotent_count=0,
        last_completed_asset_id=second.last_completed_asset_id,
        upper_bound_asset_id=second.upper_bound_asset_id,
        blocked_asset_id=None,
        error_code=None,
        has_more=False,
        completed=True,
    )
    assert len(requests) == count_after_completion
    fixture.close()


@pytest.mark.parametrize("mutation", ["persist", "update", "delete"])
def test_source_change_between_batches_blocks_before_adapter(
    tmp_path,
    monkeypatch,
    mutation,
):
    fixture, state, store, adapter, _, assets = _runner_fixture(
        tmp_path,
        colors=("red", "green"),
    )
    first = _run(state, store, adapter, batch_size=1)
    assert first.status == "paused"
    if mutation == "persist":
        _persist_image(store, "blue")
    elif mutation == "update":
        store.update_metadata(assets[0]["asset_id"], title="changed later")
    else:
        store.delete(assets[0]["asset_id"])
    calls = []
    monkeypatch.setattr(
        adapter,
        "import_asset",
        lambda request: calls.append(request),
    )

    second = _run(state, store, adapter, batch_size=1)
    assert second.status == "blocked"
    assert second.error_code == "source_changed_since_checkpoint"
    assert second.last_completed_asset_id == first.last_completed_asset_id
    assert second.upper_bound_asset_id == first.upper_bound_asset_id
    assert calls == []
    fixture.close()


def test_rejected_and_unexpected_dispositions_do_not_advance_cursor(
    tmp_path,
    monkeypatch,
):
    fixture, state, store, adapter, _, assets = _runner_fixture(
        tmp_path,
        colors=("red",),
    )
    asset_id = assets[0]["asset_id"]
    monkeypatch.setattr(
        adapter,
        "import_asset",
        lambda request: LegacyAssetImportResult(
            asset_id=request.asset_id,
            disposition=LegacyAssetImportDisposition.REJECTED,
            error_code=LegacyAssetImportErrorCode.LEGACY_BLOB_MISSING,
        ),
    )
    blocked = _run(state, store, adapter)
    assert blocked.status == "blocked"
    assert blocked.blocked_asset_id == asset_id
    assert blocked.error_code == "legacy_blob_missing"
    assert blocked.processed_count == 0
    assert blocked.last_completed_asset_id is None
    blocked_checkpoint = state.get_checkpoint(MIGRATION_KEY)
    blocked_calls = []
    monkeypatch.setattr(
        adapter,
        "import_asset",
        lambda request: blocked_calls.append(request),
    )
    blocked_again = _run(state, store, adapter)
    assert blocked_again == blocked
    assert blocked_calls == []
    assert state.get_checkpoint(MIGRATION_KEY) == blocked_checkpoint
    fixture.close()

    other = tmp_path / "other"
    other.mkdir()
    fixture, state, store, adapter, _, assets = _runner_fixture(
        other,
        colors=("blue",),
    )
    monkeypatch.setattr(
        adapter,
        "import_asset",
        lambda request: LegacyAssetImportResult(
            asset_id=request.asset_id,
            disposition=LegacyAssetImportDisposition.DRY_RUN_VALID,
        ),
    )
    failed = _run(state, store, adapter)
    assert failed.status == "failed"
    assert failed.error_code == "migration_unexpected_disposition"
    assert failed.last_completed_asset_id is None
    failed_checkpoint = state.get_checkpoint(MIGRATION_KEY)
    failed_calls = []
    monkeypatch.setattr(
        adapter,
        "import_asset",
        lambda request: failed_calls.append(request),
    )
    failed_again = _run(state, store, adapter)
    assert failed_again == failed
    assert failed_calls == []
    assert state.get_checkpoint(MIGRATION_KEY) == failed_checkpoint
    fixture.close()


def test_unexpected_exception_preserves_prior_per_asset_checkpoint(
    tmp_path,
    monkeypatch,
):
    fixture, state, store, adapter, _, assets = _runner_fixture(
        tmp_path,
        colors=("red", "blue"),
    )
    sorted_ids = sorted(asset["asset_id"] for asset in assets)
    calls = []

    def import_then_fail(request):
        calls.append(request.asset_id)
        if len(calls) == 2:
            raise RuntimeError("synthetic")
        return LegacyAssetImportResult(
            asset_id=request.asset_id,
            disposition=LegacyAssetImportDisposition.IMPORTED,
        )

    monkeypatch.setattr(adapter, "import_asset", import_then_fail)
    result = _run(state, store, adapter, batch_size=2)
    assert result.status == "failed"
    assert result.error_code == "migration_adapter_failure"
    assert result.processed_count == 1
    assert result.imported_count == 1
    assert result.last_completed_asset_id == sorted_ids[0]
    assert result.blocked_asset_id == sorted_ids[1]
    fixture.close()


def test_idempotent_skip_advances_and_crash_like_retry_is_safe(
    tmp_path,
    monkeypatch,
):
    fixture, state, store, adapter, _, assets = _runner_fixture(
        tmp_path,
        colors=("red",),
    )
    original_record = state.record_asset_success
    first_record = True

    def crash_before_checkpoint(**kwargs):
        nonlocal first_record
        if first_record:
            first_record = False
            raise SystemExit("synthetic crash")
        return original_record(**kwargs)

    monkeypatch.setattr(state, "record_asset_success", crash_before_checkpoint)
    with pytest.raises(SystemExit, match="synthetic crash"):
        _run(state, store, adapter)
    checkpoint = state.get_checkpoint(MIGRATION_KEY)
    assert checkpoint.last_completed_asset_id is None
    assert checkpoint.processed_count == 0

    result = _run(state, store, adapter)
    assert result.status == "completed"
    assert result.processed_count == 1
    assert result.imported_count == 0
    assert result.skipped_idempotent_count == 1
    assert result.last_completed_asset_id == assets[0]["asset_id"]
    fixture.close()


def test_lease_loss_stops_without_advancing_current_asset(tmp_path, monkeypatch):
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
    _persist_image(store, "red")

    def expire_during_import(request):
        clock.advance(2)
        return LegacyAssetImportResult(
            asset_id=request.asset_id,
            disposition=LegacyAssetImportDisposition.IMPORTED,
        )

    monkeypatch.setattr(adapter, "import_asset", expire_during_import)
    result = _run(
        state,
        store,
        adapter,
        lease_ttl_seconds=1,
    )
    assert result.status == "failed"
    assert result.error_code == "migration_freeze_lost"
    assert result.processed_count == 0
    assert result.last_completed_asset_id is None
    fixture.close()


def test_expired_lease_takeover_during_import_uses_idempotent_recovery(
    tmp_path,
    monkeypatch,
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
    asset = _persist_image(store, "red")
    original_import = adapter.import_asset
    takeover_done = False

    def import_then_lose_lease(request):
        nonlocal takeover_done
        result = original_import(request)
        if not takeover_done:
            takeover_done = True
            clock.advance(2)
            state.acquire_freeze(
                ttl_seconds=30,
                owner_token=OWNER_TWO,
            )
        return result

    monkeypatch.setattr(adapter, "import_asset", import_then_lose_lease)
    first = _run(
        state,
        store,
        adapter,
        lease_ttl_seconds=1,
        owner_token=OWNER_ONE,
    )
    assert first.status == "failed"
    assert first.error_code == "migration_freeze_lost"
    assert first.processed_count == 0
    assert first.last_completed_asset_id is None
    state.assert_freeze_owner(OWNER_TWO)
    assert state.release_freeze(OWNER_TWO)

    monkeypatch.setattr(adapter, "import_asset", original_import)
    second = _run(state, store, adapter, lease_ttl_seconds=30)
    assert second.status == "completed"
    assert second.processed_count == 1
    assert second.imported_count == 0
    assert second.skipped_idempotent_count == 1
    assert second.last_completed_asset_id == asset["asset_id"]
    fixture.close()


def test_runner_rejects_gate_adapter_and_identity_mismatch(tmp_path):
    fixture, state, store, adapter, _, _ = _runner_fixture(
        tmp_path,
        colors=(),
    )
    unbound = AssetStore(tmp_path / "unbound")
    with pytest.raises(
        HostMigrationRunnerError,
        match="^migration_write_gate_mismatch$",
    ):
        run_migration_batch(
            legacy_store=unbound,
            adapter=adapter,
            migration_state=state,
            source_identity=state.source_identity,
            target_identity=state.target_identity,
            batch_size=1,
        )
    with pytest.raises(
        HostMigrationRunnerError,
        match="^migration_identity_mismatch$",
    ):
        run_migration_batch(
            legacy_store=store,
            adapter=adapter,
            migration_state=state,
            source_identity="wrong",
            target_identity=state.target_identity,
            batch_size=1,
        )
    completed = _run(state, store, adapter, batch_size=1)
    assert completed.status == "completed"
    checkpoint = state.get_checkpoint(MIGRATION_KEY)
    with pytest.raises(
        HostMigrationRunnerError,
        match="^migration_identity_mismatch$",
    ):
        run_migration_batch(
            legacy_store=store,
            adapter=adapter,
            migration_state=state,
            source_identity=state.source_identity,
            target_identity="wrong",
            batch_size=1,
        )
    assert state.get_checkpoint(MIGRATION_KEY) == checkpoint
    with pytest.raises(
        HostMigrationRunnerError,
        match="^migration_version_mismatch$",
    ):
        _run(
            state,
            store,
            adapter,
            batch_size=1,
            migration_version=2,
        )
    assert state.get_checkpoint(MIGRATION_KEY) == checkpoint
    fixture.close()


def test_runner_rejects_adapter_target_root_mismatch(tmp_path):
    fixture = create_legacy_asset_import_fixture_context(
        tmp_path,
        legacy_root=tmp_path / "legacy",
        rm_root=tmp_path / "actual-rm",
    )
    claimed_target = tmp_path / "claimed-rm"
    claimed_target.mkdir()
    state = HostMigrationState(
        tmp_path / "migration.sqlite3",
        legacy_root=fixture.legacy_root,
        target_root=claimed_target,
    )
    store = AssetStore(fixture.legacy_root, write_gate=state)
    fixture.bind_legacy_store(store)
    runtime = fixture.create_runtime()
    adapter = LegacyAssetImportAdapter(
        legacy_store=store,
        core=runtime.service,
        fixture_context=fixture,
    )

    with pytest.raises(
        HostMigrationRunnerError,
        match="^migration_adapter_target_mismatch$",
    ):
        _run(state, store, adapter, batch_size=1)
    assert state.get_checkpoint(MIGRATION_KEY) is None
    fixture.close()


def test_runner_rejects_untrusted_rebound_core_target(tmp_path):
    source_fixture = create_legacy_asset_import_fixture_context(
        tmp_path / "source-fixture",
    )
    source_runtime = source_fixture.create_runtime()
    migration_fixture = create_legacy_asset_import_fixture_context(
        tmp_path / "migration-fixture",
    )
    state = HostMigrationState(
        migration_fixture.fixture_root / "migration.sqlite3",
        legacy_root=migration_fixture.legacy_root,
        target_root=migration_fixture.rm_root,
    )
    store = AssetStore(migration_fixture.legacy_root, write_gate=state)
    migration_fixture.bind_legacy_store(store)
    migration_fixture.bind_core(source_runtime.service)
    adapter = LegacyAssetImportAdapter(
        legacy_store=store,
        core=source_runtime.service,
        fixture_context=migration_fixture,
    )

    with pytest.raises(
        HostMigrationRunnerError,
        match="^migration_adapter_target_mismatch$",
    ):
        _run(state, store, adapter, batch_size=1)
    assert state.get_checkpoint(MIGRATION_KEY) is None
    migration_fixture.close()
    source_fixture.close()


def test_canonical_path_identity_normalizes_windows_and_resolved_paths(
    tmp_path,
    monkeypatch,
):
    root = tmp_path / "LegacyRoot"
    root.mkdir()
    (root / "child").mkdir()
    monkeypatch.chdir(tmp_path)
    expected = canonical_path_identity(root)

    assert canonical_path_identity(Path("LegacyRoot")) == expected
    assert canonical_path_identity(root / "child" / "..") == expected
    assert canonical_path_identity(str(root).replace("\\", "/")) == expected
    if os.name == "nt":
        assert canonical_path_identity(str(root).upper()) == expected

    link = tmp_path / "legacy-link"
    try:
        link.symlink_to(root, target_is_directory=True)
    except OSError:
        pass
    else:
        assert canonical_path_identity(link) == expected


def test_runner_static_architecture_and_no_production_wiring():
    runner = (ROOT / "remember_me_migration_runner.py").read_text(
        encoding="utf-8"
    )
    server = (ROOT / "server.py").read_text(encoding="utf-8")
    lower = runner.casefold()

    assert "LegacyAssetImportAdapter" in runner
    assert "LegacyAssetImportRequest" in runner
    assert "RememberMeCore" not in runner
    assert "remember_me.repository" not in runner
    assert "remember_me.storage" not in runner
    assert ".repository" not in runner
    assert "core.import_asset" not in runner
    assert "reindex" not in lower
    assert "dual-write" not in lower
    assert "shadow-write" not in lower
    assert "remember_me_migration_runner" not in server
    assert "OMBRE_RM_RUNTIME_ENABLED" not in runner


def test_migration_state_path_and_roots_are_contained_safely(tmp_path):
    legacy = tmp_path / "legacy"
    target = tmp_path / "rm"
    legacy.mkdir()
    target.mkdir()
    with pytest.raises(
        HostMigrationStateError,
        match="^migration_state_path_unsafe$",
    ):
        HostMigrationState(
            legacy / "assets" / "migration.sqlite3",
            legacy_root=legacy,
            target_root=target,
        )
    with pytest.raises(
        HostMigrationStateError,
        match="^migration_roots_overlap$",
    ):
        HostMigrationState(
            tmp_path / "migration.sqlite3",
            legacy_root=legacy,
            target_root=legacy / "nested",
        )

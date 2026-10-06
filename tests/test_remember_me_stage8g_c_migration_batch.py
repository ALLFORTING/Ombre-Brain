import hashlib
import io
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import sqlite3
import threading

import pytest
from PIL import Image

from asset_migration_state import (
    HostMigrationState,
    HostMigrationStateError,
    canonical_path_identity,
)
from asset_store import AssetStore, AssetStoreError


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

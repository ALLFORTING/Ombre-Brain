from __future__ import annotations

from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import sqlite3

import pytest

from asset_authority import (
    AssetAuthority,
    AssetAuthorityConfigError,
    load_asset_authority,
    parse_asset_authority,
)
from asset_cutover_state import (
    CutoverState,
    CutoverStateError,
    CutoverStateStore,
    MigrationIdentity,
    validate_cutover_boot,
)
from asset_mutation_gate import AssetMutationGate
from asset_storage_layout import (
    AssetStorageLayoutError,
    validate_asset_storage_layout,
)


class MutableClock:
    def __init__(self):
        self.value = datetime(2026, 8, 14, tzinfo=timezone.utc)

    def __call__(self):
        return self.value

    def advance(self, seconds: int):
        self.value += timedelta(seconds=seconds)


def _store(tmp_path: Path, *, clock=None) -> CutoverStateStore:
    return CutoverStateStore(tmp_path / "state" / "migration.sqlite3", clock=clock)


def _identity(key="forward") -> MigrationIdentity:
    return MigrationIdentity(
        migration_key=key,
        migration_version=1,
        source_identity="path-sha256:source",
        source_generation=7,
        target_identity="path-sha256:target",
    )


def _ready_store(tmp_path: Path) -> CutoverStateStore:
    store = _store(tmp_path)
    store.set_rm_available(True)
    store.transition(CutoverState.LEGACY_AUTHORITY_RM_READY)
    return store


def _open_rm_store(tmp_path: Path) -> CutoverStateStore:
    store = _ready_store(tmp_path)
    lease = store.acquire_freeze(
        expected_state=CutoverState.LEGACY_AUTHORITY_RM_READY,
        frozen_state=CutoverState.FROZEN_LEGACY_MIGRATION,
        ttl_seconds=60,
        migration_identity=_identity(),
    )
    store.transition(CutoverState.FROZEN_READY_FOR_RM_SWITCH, lease=lease)
    store.transition(CutoverState.FROZEN_RM_ACCEPTANCE, lease=lease)
    store.release_freeze(lease, target_state=CutoverState.RM_AUTHORITY_OPEN)
    return store


def test_authority_configuration_is_strict_and_legacy_by_default():
    assert parse_asset_authority(None) is AssetAuthority.LEGACY
    assert parse_asset_authority("") is AssetAuthority.LEGACY
    assert parse_asset_authority(" legacy ") is AssetAuthority.LEGACY
    assert parse_asset_authority("rm") is AssetAuthority.RM
    assert load_asset_authority({}).authority is AssetAuthority.LEGACY
    assert load_asset_authority({"OMBRE_ASSET_AUTHORITY": "rm"}).source == "env"

    with pytest.raises(AssetAuthorityConfigError, match="^asset_authority_invalid$"):
        parse_asset_authority("shadow")


def test_default_authority_parser_has_no_state_or_routing_side_effect(tmp_path):
    result = load_asset_authority({})
    assert result.authority is AssetAuthority.LEGACY
    assert not list(tmp_path.iterdir())


def test_design_a_owned_subpaths_are_accepted(tmp_path):
    legacy = tmp_path / "buckets"
    layout = validate_asset_storage_layout(
        legacy,
        legacy / "remember-me",
        legacy / "state",
    )
    assert layout.legacy_root == legacy.resolve()
    assert layout.rm_root == (legacy / "remember-me").resolve()
    assert layout.state_db_path == (legacy / "state" / "migration.sqlite3").resolve()


@pytest.mark.parametrize(
    "rm_relative",
    [
        Path("assets"),
        Path("assets") / "nested",
        Path("permanent"),
        Path("dynamic"),
        Path("archive"),
        Path("feel"),
        Path("state"),
    ],
)
def test_rm_owned_path_cannot_collide_with_legacy_namespaces(tmp_path, rm_relative):
    legacy = tmp_path / "buckets"
    with pytest.raises(AssetStorageLayoutError):
        validate_asset_storage_layout(
            legacy,
            legacy / rm_relative,
            legacy / "state",
        )


def test_exact_legacy_root_and_ancestor_or_state_overlap_are_rejected(tmp_path):
    legacy = tmp_path / "buckets"
    with pytest.raises(AssetStorageLayoutError):
        validate_asset_storage_layout(legacy, legacy, legacy / "state")
    with pytest.raises(AssetStorageLayoutError):
        validate_asset_storage_layout(legacy, tmp_path, legacy / "state")
    with pytest.raises(AssetStorageLayoutError):
        validate_asset_storage_layout(
            legacy,
            legacy / "remember-me",
            legacy / "remember-me" / "state",
        )


def test_unrelated_absolute_roots_are_allowed(tmp_path):
    layout = validate_asset_storage_layout(
        tmp_path / "legacy",
        tmp_path / "rm",
        tmp_path / "state",
    )
    assert layout.rm_root == (tmp_path / "rm").resolve()


def test_symlink_collision_is_rejected_when_supported(tmp_path):
    legacy = tmp_path / "buckets"
    legacy.mkdir()
    (legacy / "assets").mkdir()
    link = legacy / "remember-me"
    try:
        link.symlink_to(legacy / "assets", target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation is not supported by this test environment")
    with pytest.raises(AssetStorageLayoutError, match="^rm_root_symlink_unsupported$"):
        validate_asset_storage_layout(legacy, link, legacy / "state")


def test_state_initializes_to_legacy_unavailable_and_survives_reopen(tmp_path):
    state_path = tmp_path / "state" / "migration.sqlite3"
    store = CutoverStateStore(state_path)
    snapshot = store.get_snapshot()
    assert snapshot.state is CutoverState.LEGACY_UNAVAILABLE_RM
    assert snapshot.authority is AssetAuthority.LEGACY
    assert snapshot.freeze_status == "open"
    assert snapshot.lease_id is None

    reopened = CutoverStateStore(state_path)
    assert reopened.get_snapshot() == snapshot


def test_valid_state_machine_transitions_and_identity_binding(tmp_path):
    store = _ready_store(tmp_path)
    assert store.get_snapshot().state is CutoverState.LEGACY_AUTHORITY_RM_READY

    forward = _identity()
    lease = store.acquire_freeze(
        expected_state=CutoverState.LEGACY_AUTHORITY_RM_READY,
        frozen_state=CutoverState.FROZEN_LEGACY_MIGRATION,
        ttl_seconds=60,
        migration_identity=forward,
    )
    assert store.get_snapshot().freeze_status == "active"
    store.transition(CutoverState.FROZEN_READY_FOR_RM_SWITCH, lease=lease)
    store.transition(CutoverState.FROZEN_RM_ACCEPTANCE, lease=lease)
    opened = store.release_freeze(
        lease,
        target_state=CutoverState.RM_AUTHORITY_OPEN,
    )
    assert opened.state is CutoverState.RM_AUTHORITY_OPEN
    assert opened.authority is AssetAuthority.RM
    assert opened.freeze_status == "open"
    assert opened.migration_identity == forward

    reverse = _identity("reverse")
    reverse_lease = store.acquire_freeze(
        expected_state=CutoverState.RM_AUTHORITY_OPEN,
        frozen_state=CutoverState.FROZEN_RM_ROLLBACK,
        ttl_seconds=60,
        migration_identity=reverse,
    )
    store.transition(CutoverState.FROZEN_LEGACY_ACCEPTANCE, lease=reverse_lease)
    restored = store.release_freeze(
        reverse_lease,
        target_state=CutoverState.LEGACY_AUTHORITY_RM_READY,
    )
    assert restored.state is CutoverState.LEGACY_AUTHORITY_RM_READY
    assert restored.authority is AssetAuthority.LEGACY
    assert restored.migration_identity is None


def test_invalid_state_transitions_and_authority_combinations_fail_closed(tmp_path):
    store = _ready_store(tmp_path)
    for target in (
        CutoverState.FROZEN_RM_ACCEPTANCE,
        CutoverState.RM_AUTHORITY_OPEN,
    ):
        with pytest.raises(CutoverStateError, match="^state_transition_invalid$"):
            store.transition(target)

    lease = store.acquire_freeze(
        expected_state=CutoverState.LEGACY_AUTHORITY_RM_READY,
        frozen_state=CutoverState.FROZEN_LEGACY_MIGRATION,
        ttl_seconds=60,
        migration_identity=_identity(),
    )
    store.transition(CutoverState.FROZEN_READY_FOR_RM_SWITCH, lease=lease)
    store.transition(CutoverState.FROZEN_RM_ACCEPTANCE, lease=lease)
    with pytest.raises(CutoverStateError, match="^state_transition_invalid$"):
        store.release_freeze(
            lease,
            target_state=CutoverState.LEGACY_UNAVAILABLE_RM,
        )
    with pytest.raises(CutoverStateError, match="^state_transition_invalid$"):
        store.release_freeze(
            lease,
            target_state=CutoverState.LEGACY_AUTHORITY_RM_READY,
        )
    store.transition(
        CutoverState.FROZEN_RM_ROLLBACK,
        lease=lease,
        migration_identity=_identity(),
    )
    store.transition(
        CutoverState.FROZEN_LEGACY_ACCEPTANCE,
        lease=lease,
        migration_identity=_identity(),
    )
    store.release_freeze(
        lease,
        target_state=CutoverState.LEGACY_AUTHORITY_RM_READY,
    )

    store.set_rm_available(False)
    store.transition(CutoverState.LEGACY_UNAVAILABLE_RM)
    with pytest.raises(CutoverStateError, match="^state_transition_invalid$"):
        store.transition(CutoverState.RM_AUTHORITY_OPEN)

    with sqlite3.connect(store.db_path) as connection:
        connection.execute(
            "UPDATE cutover_state SET authority = 'rm' WHERE singleton = 1"
        )
    with pytest.raises(CutoverStateError, match="^state_authority_ambiguous$"):
        CutoverStateStore(store.db_path)


def test_freeze_lease_acquire_renew_release_and_wrong_holder_rejection(tmp_path):
    store = _ready_store(tmp_path)
    lease = store.acquire_freeze(
        expected_state=CutoverState.LEGACY_AUTHORITY_RM_READY,
        frozen_state=CutoverState.FROZEN_LEGACY_MIGRATION,
        ttl_seconds=10,
        migration_identity=_identity(),
    )
    with pytest.raises(CutoverStateError, match="^freeze_lease_busy$"):
        store.acquire_freeze(
            expected_state=CutoverState.FROZEN_LEGACY_MIGRATION,
            frozen_state=CutoverState.FROZEN_LEGACY_MIGRATION,
            ttl_seconds=10,
            migration_identity=_identity(),
        )

    wrong = type(lease)(
        lease_id=lease.lease_id,
        token="wrong-holder",
        generation=lease.generation,
        acquired_at=lease.acquired_at,
        expires_at=lease.expires_at,
    )
    with pytest.raises(CutoverStateError, match="^freeze_lease_invalid$"):
        store.renew_freeze(wrong, ttl_seconds=10)
    with pytest.raises(CutoverStateError, match="^freeze_lease_invalid$"):
        store.release_freeze(wrong, target_state=CutoverState.LEGACY_AUTHORITY_RM_READY)

    renewed = store.renew_freeze(lease, ttl_seconds=20)
    reopened = CutoverStateStore(store.db_path)
    assert reopened.get_snapshot().freeze_status == "active"
    reopened.release_freeze(
        renewed,
        target_state=CutoverState.LEGACY_AUTHORITY_RM_READY,
    )
    assert reopened.get_snapshot().freeze_status == "open"


def test_expired_freeze_remains_blocked_until_explicit_recovery(tmp_path):
    clock = MutableClock()
    store = _ready_store(tmp_path)
    store._clock = clock
    lease = store.acquire_freeze(
        expected_state=CutoverState.LEGACY_AUTHORITY_RM_READY,
        frozen_state=CutoverState.FROZEN_LEGACY_MIGRATION,
        ttl_seconds=10,
        migration_identity=_identity(),
    )
    clock.advance(11)
    assert store.get_snapshot().freeze_status == "expired"
    with pytest.raises(CutoverStateError, match="^asset_mutation_frozen$"):
        store.assert_public_mutation_allowed()
    with pytest.raises(CutoverStateError, match="^freeze_lease_expired$"):
        store.renew_freeze(lease, ttl_seconds=10)
    with pytest.raises(CutoverStateError, match="^freeze_lease_stale$"):
        store.acquire_freeze(
            expected_state=CutoverState.FROZEN_LEGACY_MIGRATION,
            frozen_state=CutoverState.FROZEN_LEGACY_MIGRATION,
            ttl_seconds=10,
            migration_identity=_identity(),
        )

    recovered = store.recover_expired_freeze(
        expected_lease_id=lease.lease_id,
        target_state=CutoverState.LEGACY_AUTHORITY_RM_READY,
    )
    assert recovered.state is CutoverState.LEGACY_AUTHORITY_RM_READY
    store.assert_public_mutation_allowed()


def test_expired_recovery_cannot_directly_reopen_rm_or_rollback_state(tmp_path):
    clock = MutableClock()
    store = _ready_store(tmp_path)
    store._clock = clock
    lease = store.acquire_freeze(
        expected_state=CutoverState.LEGACY_AUTHORITY_RM_READY,
        frozen_state=CutoverState.FROZEN_LEGACY_MIGRATION,
        ttl_seconds=1,
        migration_identity=_identity(),
    )
    clock.advance(2)
    with pytest.raises(CutoverStateError, match="^recovery_target_invalid$"):
        store.recover_expired_freeze(
            expected_lease_id=lease.lease_id,
            target_state=CutoverState.RM_AUTHORITY_OPEN,
        )


def test_privileged_capability_is_ephemeral_and_bound_to_active_lease(tmp_path):
    store = _ready_store(tmp_path)
    lease = store.acquire_freeze(
        expected_state=CutoverState.LEGACY_AUTHORITY_RM_READY,
        frozen_state=CutoverState.FROZEN_LEGACY_MIGRATION,
        ttl_seconds=60,
        migration_identity=_identity(),
    )
    capability = store.issue_privileged_capability(
        lease,
        purpose="rm_import",
    )
    store.assert_privileged_capability(capability, purpose="rm_import")
    assert capability.token not in store.db_path.read_text(encoding="utf-8", errors="ignore")
    with pytest.raises(CutoverStateError, match="^capability_invalid$"):
        store.assert_privileged_capability(capability, purpose="rm_reindex")

    gate = AssetMutationGate(store)
    assert not gate.public_mutations_allowed()
    store.release_freeze(lease, target_state=CutoverState.LEGACY_AUTHORITY_RM_READY)
    assert gate.public_mutations_allowed()


def test_corrupt_or_unsupported_state_fails_closed(tmp_path):
    store = _store(tmp_path)
    with sqlite3.connect(store.db_path) as connection:
        connection.execute(
            "UPDATE cutover_schema SET schema_version = 999 WHERE singleton = 1"
        )
    with pytest.raises(CutoverStateError, match="^state_schema_incompatible$"):
        CutoverStateStore(store.db_path)

    damaged = tmp_path / "damaged.sqlite3"
    damaged_store = CutoverStateStore(damaged)
    with sqlite3.connect(damaged_store.db_path) as connection:
        connection.execute("DROP TABLE cutover_state")
    with pytest.raises(CutoverStateError, match="^state_db_corrupt$"):
        CutoverStateStore(damaged)


def test_boot_validation_detects_state_authority_and_freeze_residue(tmp_path):
    store = _ready_store(tmp_path)
    snapshot = store.get_snapshot()
    valid = validate_cutover_boot(
        AssetAuthority.LEGACY,
        snapshot,
        rm_available=True,
    )
    assert valid.writes_allowed is True

    with pytest.raises(CutoverStateError, match="^state_authority_ambiguous$"):
        validate_cutover_boot(AssetAuthority.RM, snapshot, rm_available=True)

    lease = store.acquire_freeze(
        expected_state=CutoverState.LEGACY_AUTHORITY_RM_READY,
        frozen_state=CutoverState.FROZEN_LEGACY_MIGRATION,
        ttl_seconds=60,
        migration_identity=_identity(),
    )
    frozen = store.get_snapshot()
    result = validate_cutover_boot(
        AssetAuthority.LEGACY,
        frozen,
        rm_available=True,
    )
    assert result.writes_allowed is False
    assert result.frozen is True

    with sqlite3.connect(store.db_path) as connection:
        connection.execute("DELETE FROM cutover_freeze WHERE singleton = 1")
    missing = store.get_snapshot()
    assert missing.freeze_status == "missing"
    with pytest.raises(CutoverStateError, match="^state_freeze_ambiguous$"):
        validate_cutover_boot(AssetAuthority.LEGACY, missing, rm_available=True)

    # Keep the lease referenced so the test documents that the missing row is
    # an intentional residue, not a normal release path.
    assert lease.lease_id


def test_boot_without_state_accepts_only_legacy(tmp_path):
    result = validate_cutover_boot(
        AssetAuthority.LEGACY,
        None,
        rm_available=False,
    )
    assert result.writes_allowed is True
    with pytest.raises(CutoverStateError, match="^rm_authority_without_state$"):
        validate_cutover_boot(AssetAuthority.RM, None, rm_available=True)

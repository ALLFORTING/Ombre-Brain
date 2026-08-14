"""Test-only helpers for explicit RM authority routing fixtures."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from asset_authority import AssetAuthority
from asset_backend import AssetBackendError, RememberMeAssetBackend, RuntimeMutationGate
from asset_cutover_state import CutoverState, CutoverStateStore, MigrationIdentity


def open_rm_authority_state(legacy_root: Path) -> CutoverStateStore:
    store = CutoverStateStore(legacy_root / "state" / "migration.sqlite3")
    store.set_rm_available(True)
    store.transition(CutoverState.LEGACY_AUTHORITY_RM_READY)
    lease = store.acquire_freeze(
        expected_state=CutoverState.LEGACY_AUTHORITY_RM_READY,
        frozen_state=CutoverState.FROZEN_LEGACY_MIGRATION,
        ttl_seconds=60,
        migration_identity=MigrationIdentity(
            migration_key="test-rm-routing",
            migration_version=1,
            source_identity="legacy-test",
            source_generation=1,
            target_identity="rm-test",
        ),
    )
    store.transition(CutoverState.FROZEN_READY_FOR_RM_SWITCH, lease=lease)
    store.transition(CutoverState.FROZEN_RM_ACCEPTANCE, lease=lease)
    store.release_freeze(lease, target_state=CutoverState.RM_AUTHORITY_OPEN)
    return store


def configure_rm_authority(tmp_path: Path, monkeypatch) -> None:
    open_rm_authority_state(tmp_path / "buckets")
    monkeypatch.setenv("OMBRE_ASSET_AUTHORITY", "rm")
    monkeypatch.setenv("OMBRE_RM_RUNTIME_ENABLED", "true")
    monkeypatch.setenv("OMBRE_RM_DATA_ROOT", str(tmp_path / "remember-me-runtime"))


def install_fake_rm_backend(server, bundle) -> None:
    server.remember_me_host_bundle = bundle
    backend = RememberMeAssetBackend(
        lambda: server.remember_me_host_bundle,
        RuntimeMutationGate(None),
    )

    def selected_backend():
        if server.remember_me_host_bundle is None:
            raise AssetBackendError("rm_authority_unavailable")
        return backend

    server.asset_backend_registry = SimpleNamespace(
        authority=AssetAuthority.RM,
        selected_backend=selected_backend,
        snapshot=None,
    )

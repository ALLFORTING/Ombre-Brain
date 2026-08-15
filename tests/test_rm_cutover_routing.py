from __future__ import annotations

import hashlib
import io
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from asset_authority import AssetAuthority
from asset_backend import (
    AssetBackendError,
    LegacyAssetBackend,
    RememberMeAssetBackend,
    RuntimeAssetBackendRegistry,
    RuntimeMutationGate,
)
from asset_cutover_state import (
    CutoverState,
    CutoverStateStore,
    FreezeLease,
    MigrationIdentity,
)
from asset_dashboard import AssetDashboardService, AssetUpload
from asset_store import AssetStore


def _png_bytes(size=(8, 6), color="purple") -> bytes:
    image = Image.new("RGB", size, color)
    output = io.BytesIO()
    image.save(output, format="PNG", optimize=True)
    image.close()
    return output.getvalue()


def _asset(asset_id: str, content: bytes, filename: str = "asset.png") -> dict:
    digest = hashlib.sha256(content).hexdigest()
    return {
        "asset_id": asset_id,
        "source_sha256": digest,
        "stored_sha256": digest,
        "decoded_bytes": len(content),
        "stored_bytes": len(content),
        "original_filename": filename,
        "filename": filename,
        "mime_type": "image/png",
        "kind": "image",
        "width": 8,
        "height": 6,
        "created_at": "2026-08-14T00:00:00+00:00",
        "updated_at": "2026-08-14T00:00:00+00:00",
        "title": "title",
        "description": "description",
        "tags": ["tag"],
    }


class FakeCore:
    def __init__(self, content: bytes):
        self.content = content
        self.asset = _asset("a" * 32, content)
        self.calls: list[str] = []
        self.fail_get = False

    def ingest_image(self, content, expected_bytes, filename, mime_type, **kwargs):
        self.calls.append("ingest_image")
        assert len(content) == expected_bytes
        self.asset = _asset(self.asset["asset_id"], content, filename)
        self.asset.update({key: value for key, value in kwargs.items() if key in {"title", "description", "tags"}})
        return dict(self.asset, deduplicated=False)

    def get(self, asset_id):
        self.calls.append("get")
        if self.fail_get or asset_id != self.asset["asset_id"]:
            return None
        return dict(self.asset)

    def update_metadata(self, asset_id, **kwargs):
        self.calls.append("update_metadata")
        if self.get(asset_id) is None:
            raise RuntimeError("asset_unavailable")
        self.asset.update({key: value for key, value in kwargs.items() if value is not None})
        return dict(self.asset)

    def delete(self, asset_id):
        self.calls.append("delete")
        if self.get(asset_id) is None:
            raise RuntimeError("asset_unavailable")
        return {"asset_id": asset_id, "deleted": True, "cleanup_pending": False}

    async def search(self, **kwargs):
        self.calls.append("search")
        item = dict(self.asset)
        item.pop("original_filename", None)
        item["match_reasons"] = ["filename"]
        return {
            "total": 1,
            "offset": kwargs.get("offset", 0),
            "limit": kwargs.get("limit", 20),
            "results": [item],
        }

    def resolve_blob(self, asset_id):
        self.calls.append("resolve_blob")
        if self.get(asset_id) is None:
            raise RuntimeError("asset_unavailable")
        return dict(self.asset), self.content

    async def reindex_embeddings(self, **kwargs):
        self.calls.append("reindex_embeddings")
        return SimpleNamespace(scanned=1, indexed=1, skipped=0, failed=0)


class FakePresenter:
    def __init__(self):
        self.calls: list[str] = []

    def rm_asset_get(self, asset_id):
        self.calls.append("get")
        return "rm-get"

    def rm_asset_update_metadata(self, asset_id, **kwargs):
        self.calls.append("update")
        return "rm-update"

    async def rm_asset_search(self, **kwargs):
        self.calls.append("search")
        return "rm-search"

    async def rm_asset_reindex_embeddings(self, **kwargs):
        self.calls.append("reindex")
        return "rm-reindex"

    def rm_asset_download_link(self, asset_id):
        self.calls.append("download")
        return "rm-download"

    def rm_asset_view(self, asset_id):
        self.calls.append("view")
        return "rm-view"

    def rm_asset_inspect(self, asset_id):
        self.calls.append("inspect")
        return "rm-inspect"


def _bundle(content: bytes):
    return SimpleNamespace(
        core_adapter=FakeCore(content),
        presenter=FakePresenter(),
    )


def _open_rm_state(root: Path) -> CutoverStateStore:
    store = CutoverStateStore(root / "state" / "migration.sqlite3")
    store.set_rm_available(True)
    store.transition(CutoverState.LEGACY_AUTHORITY_RM_READY)
    lease = store.acquire_freeze(
        expected_state=CutoverState.LEGACY_AUTHORITY_RM_READY,
        frozen_state=CutoverState.FROZEN_LEGACY_MIGRATION,
        ttl_seconds=60,
        migration_identity=MigrationIdentity(
            migration_key="routing-test",
            migration_version=1,
            source_identity="source",
            source_generation=1,
            target_identity="target",
        ),
    )
    store.transition(CutoverState.FROZEN_READY_FOR_RM_SWITCH, lease=lease)
    store.transition(CutoverState.FROZEN_RM_ACCEPTANCE, lease=lease)
    store.release_freeze(lease, target_state=CutoverState.RM_AUTHORITY_OPEN)
    return store


def test_backend_selection_uses_authority_not_rm_presence(tmp_path):
    legacy = AssetStore(tmp_path / "legacy")
    bundle = _bundle(_png_bytes())

    default_registry = RuntimeAssetBackendRegistry.from_runtime(
        legacy_store=legacy,
        bundle_provider=lambda: bundle,
        authority_environ={},
    )
    assert default_registry.authority is AssetAuthority.LEGACY
    assert default_registry.selected_backend().name == "legacy"

    _open_rm_state(legacy.data_root)
    rm_registry = RuntimeAssetBackendRegistry.from_runtime(
        legacy_store=legacy,
        bundle_provider=lambda: bundle,
        authority_environ={
            "OMBRE_ASSET_AUTHORITY": "rm",
            "OMBRE_RM_DATA_ROOT": str(legacy.data_root / "remember-me"),
        },
    )
    assert rm_registry.authority is AssetAuthority.RM
    assert rm_registry.selected_backend().name == "rm"


def test_rm_authority_without_available_runtime_fails_closed(tmp_path):
    legacy = AssetStore(tmp_path / "legacy")
    _open_rm_state(legacy.data_root)
    with pytest.raises(AssetBackendError, match="^asset_authority_unavailable$"):
        RuntimeAssetBackendRegistry.from_runtime(
            legacy_store=legacy,
            bundle_provider=lambda: None,
            authority_environ={
                "OMBRE_ASSET_AUTHORITY": "rm",
                "OMBRE_RM_DATA_ROOT": str(legacy.data_root / "remember-me"),
            },
        )


def test_rm_failure_never_falls_back_to_legacy(tmp_path):
    legacy = AssetStore(tmp_path / "legacy")
    bundle = _bundle(_png_bytes())
    _open_rm_state(legacy.data_root)
    registry = RuntimeAssetBackendRegistry.from_runtime(
        legacy_store=legacy,
        bundle_provider=lambda: bundle,
        authority_environ={
            "OMBRE_ASSET_AUTHORITY": "rm",
            "OMBRE_RM_DATA_ROOT": str(legacy.data_root / "remember-me"),
        },
    )
    bundle.core_adapter.fail_get = True
    assert registry.selected_backend().get("a" * 32) is None
    assert legacy.search(kind="image")["total"] == 0


def test_rm_dashboard_adapter_preserves_common_asset_operations(tmp_path):
    content = _png_bytes()
    bundle = _bundle(content)
    backend = RememberMeAssetBackend(lambda: bundle, RuntimeMutationGate(None))
    path = backend.create_temp_path()
    path.write_bytes(content)
    asset = backend.persist_upload(
        path,
        hashlib.sha256(content).hexdigest(),
        len(content),
        "dashboard.png",
        "image/png",
        require_image=True,
        title="title",
        description="description",
        tags=["tag"],
    )
    assert asset["asset_id"] == "a" * 32
    assert not path.exists()

    service = AssetDashboardService(
        backend_provider=lambda: backend,
        max_asset_bytes=10_000,
    )
    result = service.list_assets(limit=20, offset=0)
    assert result["results"][0]["filename"] == "dashboard.png"
    updated = service.update_asset(asset["asset_id"], {"title": "changed"})
    assert updated["title"] == "changed"
    resolved = service.resolve_image(asset["asset_id"])
    assert resolved.path is None
    assert resolved.content == content
    assert service.delete_asset(asset["asset_id"])["deleted"] is True


def test_persistent_freeze_blocks_both_backend_public_mutations(tmp_path):
    legacy_store = AssetStore(tmp_path / "legacy")
    state = CutoverStateStore(legacy_store.data_root / "state" / "migration.sqlite3")
    state.set_rm_available(True)
    state.transition(CutoverState.LEGACY_AUTHORITY_RM_READY)
    lease = state.acquire_freeze(
        expected_state=CutoverState.LEGACY_AUTHORITY_RM_READY,
        frozen_state=CutoverState.FROZEN_LEGACY_MIGRATION,
        ttl_seconds=60,
        migration_identity=MigrationIdentity("freeze", 1, "source", 1, "target"),
    )
    gate = RuntimeMutationGate(state)
    legacy_backend = LegacyAssetBackend(legacy_store, gate)
    with pytest.raises(AssetBackendError, match="^asset_write_frozen$"):
        legacy_backend.create_temp_path()

    rm_backend = RememberMeAssetBackend(
        lambda: _bundle(_png_bytes()),
        gate,
    )
    with pytest.raises(AssetBackendError, match="^asset_write_frozen$"):
        rm_backend.update_metadata("a" * 32, title="blocked")

    reopened = CutoverStateStore(state.db_path)
    assert reopened.get_snapshot().freeze_status == "active"
    assert reopened.get_snapshot().state is CutoverState.FROZEN_LEGACY_MIGRATION
    assert lease.lease_id


def test_rm_authority_is_persistent_across_registry_reopen(tmp_path):
    legacy = AssetStore(tmp_path / "legacy")
    _open_rm_state(legacy.data_root)
    bundle = _bundle(_png_bytes())
    first = RuntimeAssetBackendRegistry.from_runtime(
        legacy_store=legacy,
        bundle_provider=lambda: bundle,
        authority_environ={
            "OMBRE_ASSET_AUTHORITY": "rm",
            "OMBRE_RM_DATA_ROOT": str(legacy.data_root / "remember-me"),
        },
    )
    assert first.snapshot.state is CutoverState.RM_AUTHORITY_OPEN
    second = RuntimeAssetBackendRegistry.from_runtime(
        legacy_store=legacy,
        bundle_provider=lambda: bundle,
        authority_environ={
            "OMBRE_ASSET_AUTHORITY": "rm",
            "OMBRE_RM_DATA_ROOT": str(legacy.data_root / "remember-me"),
        },
    )
    assert second.selected_backend().name == "rm"

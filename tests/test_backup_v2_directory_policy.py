"""Synthetic /app topology only; no deployment data or production keys."""
import base64
import shutil
import sqlite3
import uuid

import pytest
from cryptography.hazmat.primitives import serialization

import backup_v2_runtime as runtime
import offline_backup_bundle as bundle
from tests.test_stage8h_g1d_backup_v2_runtime import _server, _key_env, COMMIT
from tests.test_stage8h_g1c_quiesced_capture import _claims


def layout(tmp_path, monkeypatch):
    app = tmp_path / "app"
    app.mkdir()
    monkeypatch.setattr(bundle, "__file__", str(app / "offline_backup_bundle.py"))
    source = app / "buckets"
    source.mkdir()
    return app, source, app / "backup-v2-workspace"


@pytest.mark.asyncio
async def test_runtime_app_prepare_load_capture_and_encrypted_verification(tmp_path, monkeypatch):
    app, source, workspace = layout(tmp_path, monkeypatch)
    with pytest.raises(bundle.BackupBundleError, match="workspace_invalid"):
        bundle.prepare_backup_workspace(workspace)
    # The default external-source rule independently rejects /app/buckets.
    offline = bundle.prepare_backup_workspace(tmp_path / "offline")
    with pytest.raises(bundle.BackupBundleError, match="workspace_invalid"):
        bundle._validate_external_source(offline, source, source)
    server = _server(tmp_path)
    server.config["buckets_dir"] = str(source)
    (source / "bucket.md").write_text("synthetic")
    with sqlite3.connect(source / "assets.sqlite3") as db:
        db.execute("CREATE TABLE assets (value TEXT)")
        db.execute("INSERT INTO assets VALUES ('synthetic')")
    private, public = bundle.generate_test_keypair()
    env = _key_env(tmp_path)
    env["OMBRE_BACKUP_V2_WORKSPACE_ROOT"] = str(workspace)
    env["OMBRE_BACKUP_V2_PUBLIC_KEY_B64"] = base64.b64encode(
        public.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    ).decode("ascii")
    from production_backup_capture import public_key_fingerprint
    env["OMBRE_BACKUP_V2_RECIPIENT_FINGERPRINT"] = public_key_fingerprint(public)
    assert runtime.register_backup_v2_if_enabled(server, "streamable-http", environ=env).registered
    controller = server._backup_v2_controller
    policy = controller.directory_policy
    assert bundle.load_backup_workspace(workspace, directory_policy=policy).root == workspace
    # Fresh registration exercises load rather than prepare.
    from tests.test_stage8h_g1d_backup_v2_runtime import FakeMcp
    server.mcp = FakeMcp()
    runtime.register_backup_v2_if_enabled(server, "streamable-http", environ=env)
    controller = server._backup_v2_controller
    request_id = str(uuid.uuid4())
    await controller.create_capture(
        request_id=request_id, expected_runtime_commit=COMMIT,
        expected_recipient_fingerprint=controller.recipient_fingerprint, claims=_claims(),
    )
    result = await controller.wait_for_terminal(request_id, _claims(), timeout=30)
    assert result["state"] == "ready"
    assert controller.coordinator.status().state == "open"
    encrypted = next(workspace.joinpath("bundles").glob("*.obbackup"))
    shutil.copyfile(encrypted, offline.bundles_root / encrypted.name)
    verified = bundle.verify_bundle(offline.root, encrypted.name, private)
    assert verified["authenticated"] and verified["entry_count"] >= 2
    with pytest.raises(bundle.BackupBundleError):
        bundle.load_backup_workspace(workspace)


@pytest.mark.parametrize("kind", ["same", "source-parent", "workspace-parent", "repository-source", "repository-workspace"])
def test_policy_rejects_overlap_and_repository(tmp_path, monkeypatch, kind):
    app, source, workspace = layout(tmp_path, monkeypatch)
    pairs = {
        "same": (source, source),
        "source-parent": (source, source / "workspace"),
        "workspace-parent": (source, app),
        "repository-source": (app, workspace),
        "repository-workspace": (source, app),
    }
    with pytest.raises(bundle.BackupBundleError):
        bundle.ProductionDirectoryPolicy(*pairs[kind])


@pytest.mark.parametrize("root", ["source", "workspace"])
@pytest.mark.parametrize("change", ["replacement", "symlink"])
def test_bound_root_replacement_and_symlink_rejected(tmp_path, monkeypatch, root, change):
    app, source, workspace = layout(tmp_path, monkeypatch)
    policy = bundle.ProductionDirectoryPolicy(source, workspace)
    bundle.prepare_backup_workspace(workspace, directory_policy=policy)
    target = source if root == "source" else workspace
    saved = target.with_name(target.name + "-saved")
    target.rename(saved)
    if change == "symlink":
        target.symlink_to(saved, target_is_directory=True)
    else:
        target.mkdir()
    with pytest.raises(bundle.BackupBundleError):
        bundle.load_backup_workspace(workspace, directory_policy=policy)


@pytest.mark.parametrize("root", ["source", "workspace"])
def test_initial_symlink_rejected(tmp_path, monkeypatch, root):
    app, source, workspace = layout(tmp_path, monkeypatch)
    alias = app / "alias"
    alias.symlink_to(source, target_is_directory=True)
    with pytest.raises(bundle.BackupBundleError):
        bundle.ProductionDirectoryPolicy(alias if root == "source" else source,
                                         alias if root == "workspace" else workspace)


def test_workspace_internal_escape_and_wrong_exact_pair(tmp_path, monkeypatch):
    app, source, workspace = layout(tmp_path, monkeypatch)
    policy = bundle.ProductionDirectoryPolicy(source, workspace)
    prepared = bundle.prepare_backup_workspace(workspace, directory_policy=policy)
    other = app / "other"
    other.mkdir()
    with pytest.raises(bundle.BackupBundleError):
        policy.validate(other, source=True)
    with pytest.raises(bundle.BackupBundleError):
        bundle.prepare_backup_workspace(other, directory_policy=policy)
    prepared.temp_root.rmdir()
    prepared.temp_root.symlink_to(other, target_is_directory=True)
    with pytest.raises(bundle.BackupBundleError):
        bundle.load_backup_workspace(workspace, directory_policy=policy)


def test_runtime_symlink_config_is_not_resolved_away(tmp_path, monkeypatch):
    app, source, workspace = layout(tmp_path, monkeypatch)
    server = _server(tmp_path)
    alias = app / "alias"
    alias.symlink_to(source, target_is_directory=True)
    server.config["buckets_dir"] = str(alias)
    with pytest.raises(runtime.BackupV2RuntimeConfigError):
        runtime.register_backup_v2_if_enabled(server, "streamable-http", environ=_key_env(tmp_path))

def test_dangling_workspace_link_rejected(tmp_path, monkeypatch):
    app, source, workspace = layout(tmp_path, monkeypatch)
    workspace.symlink_to(app / "missing", target_is_directory=True)
    with pytest.raises(bundle.BackupBundleError):
        bundle.ProductionDirectoryPolicy(source, workspace)


@pytest.mark.parametrize("relative", ["temp", "workspace-manifest.json", ".workspace-marker.json"])
def test_workspace_internal_link_even_when_contained_is_rejected(tmp_path, monkeypatch, relative):
    app, source, workspace = layout(tmp_path, monkeypatch)
    policy = bundle.ProductionDirectoryPolicy(source, workspace)
    prepared = bundle.prepare_backup_workspace(workspace, directory_policy=policy)
    # Use actual constants for the two metadata files.
    target = (prepared.temp_root if relative == "temp" else
              workspace / (bundle.WORKSPACE_MANIFEST if relative == "workspace-manifest.json"
                           else bundle.WORKSPACE_MARKER))
    saved = target.with_name(target.name + "-saved")
    target.rename(saved)
    target.symlink_to(saved, target_is_directory=saved.is_dir())
    with pytest.raises(bundle.BackupBundleError):
        bundle.load_backup_workspace(workspace, directory_policy=policy)

@pytest.mark.parametrize("root", ["source", "workspace", "internal"])
def test_windows_reparse_attribute_is_rejected(tmp_path, monkeypatch, root):
    from pathlib import Path
    from types import SimpleNamespace
    import stat
    app, source, workspace = layout(tmp_path, monkeypatch)
    policy = bundle.ProductionDirectoryPolicy(source, workspace)
    prepared = bundle.prepare_backup_workspace(workspace, directory_policy=policy)
    target = {"source": source, "workspace": workspace, "internal": prepared.temp_root}[root]
    original = Path.lstat

    def reparse_lstat(path, *args, **kwargs):
        if path == target:
            return SimpleNamespace(st_file_attributes=0x400, st_mode=stat.S_IFDIR)
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "lstat", reparse_lstat)
    with pytest.raises(bundle.BackupBundleError):
        bundle.load_backup_workspace(workspace, directory_policy=policy)


@pytest.mark.asyncio
async def test_replaced_source_cannot_enter_capture(tmp_path, monkeypatch):
    from maintenance_write_gate import MaintenanceWriteCoordinator
    app, source, workspace = layout(tmp_path, monkeypatch)
    policy = bundle.ProductionDirectoryPolicy(source, workspace)
    bundle.prepare_backup_workspace(workspace, directory_policy=policy)
    source.rename(app / "old-buckets")
    source.mkdir()
    _, public = bundle.generate_test_keypair()
    coordinator = MaintenanceWriteCoordinator()
    async with coordinator.freeze(reason="synthetic", drain_timeout_seconds=2,
                                  max_freeze_seconds=30) as lease:
        with pytest.raises(bundle.BackupBundleError):
            bundle.capture_external_source(
                workspace, source, source, public, coordinator=coordinator,
                freeze_lease=lease, ob_commit_sha=COMMIT, directory_policy=policy,
            )
    assert coordinator.status().state == "open"
    assert not list(workspace.rglob("*.obbackup"))


def test_fixed_workspace_directory_replacement_rejected(tmp_path, monkeypatch):
    app, source, workspace = layout(tmp_path, monkeypatch)
    policy = bundle.ProductionDirectoryPolicy(source, workspace)
    prepared = bundle.prepare_backup_workspace(workspace, directory_policy=policy)
    prepared.temp_root.rename(workspace / "old-temp")
    prepared.temp_root.mkdir()
    with pytest.raises(bundle.BackupBundleError):
        bundle.load_backup_workspace(workspace, directory_policy=policy)

"""Bounded closeout checks: reporting, binding, no overwrite and failure receipts."""
import hashlib
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace
import uuid

import pytest
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
import offline_backup_bundle as core
from scripts import backup_v2_recovery as recovery
from scripts.backup_closeout import local_prepare as closeout

COMMIT = "1" * 40


def plan():
    return {"final_deployment_sha": COMMIT, "ob_source_commit": COMMIT,
            "client_commit": closeout.CLIENT_SHA, "session_name": "closeout-" + "2" * 32}


def bundle(tmp_path):
    workspace = core.prepare_backup_workspace(tmp_path / "workspace")
    (workspace.source_root / "permanent").mkdir()
    (workspace.source_root / "permanent" / "fixture.md").write_text("synthetic only")
    (workspace.source_root / "remember-me").mkdir()
    with sqlite3.connect(workspace.source_root / "remember-me" / "fixture.db") as db:
        db.execute("CREATE TABLE fixture (value TEXT)")
        db.execute("INSERT INTO fixture VALUES ('synthetic')")
    (workspace.source_root / "discard.tmp").write_text("excluded")
    key = X25519PrivateKey.generate()
    captured = core.capture_bundle(workspace.root, key.public_key(), ob_commit_sha=COMMIT)
    binding = {"request_id": str(uuid.uuid4()), "run_id": "42", "run_attempt": "1", "artifact_id": "99",
               "artifact_digest": "sha256:" + "a" * 64, "workflow_commit": closeout.CLIENT_SHA,
               "runtime_commit": COMMIT, "bundle_id": captured.bundle_id,
               "encrypted_size": (workspace.bundles_root / captured.bundle_name).stat().st_size,
               "encrypted_sha256": hashlib.sha256((workspace.bundles_root / captured.bundle_name).read_bytes()).hexdigest(), "independent_copy_verified": True}
    return workspace, key, captured, binding


def test_report_keeps_authenticated_manifest_actual_file_database_checks_and_exclusions(tmp_path):
    workspace, key, captured, binding = bundle(tmp_path)
    result = core.restore_bundle(workspace.root, captured.bundle_name, key,
                                 report_name=captured.bundle_id + ".restore", association=binding)
    report = Path(result["report_path"])
    manifest = closeout.read_json(report / "manifest.json")
    assert core._validate_manifest((report / "manifest.json").read_bytes()) == manifest
    validation = closeout.read_json(report / "verification.json")
    association = closeout.read_json(report / "association.json")
    assert result["authenticated"] and manifest["ob_commit_sha"] == COMMIT
    assert len(validation["file_checks"]) == manifest["entry_count"] == 2
    for check in validation["file_checks"]:
        restored = workspace.restored_root / captured.bundle_id / check["relative_path"]
        assert check["sha256"] == hashlib.sha256(restored.read_bytes()).hexdigest()
        assert check["size_bytes"] == restored.stat().st_size and check["status"] == "passed"
    database = validation["database_checks"][0]["database"]
    assert database["quick_check"] == "ok"
    entry = next(item for item in manifest["entries"] if item["entry_type"] == "sqlite_snapshot")
    assert {k: database[k] for k in ("page_size", "page_count", "user_version", "schema_sha256")} == {
        k: entry[k] for k in ("page_size", "page_count", "user_version", "schema_sha256")}
    assert validation["coverage"]["categories"]["remember_me"] == ["remember-me/fixture.db"]
    assert validation["coverage"]["exclusions"] == manifest["exclusions"]
    assert any(item["relative_path"] == "discard.tmp" for item in manifest["exclusions"])
    assert association["request_id"] == binding["request_id"]
    assert association["original_job_lease_release"] == "unknown"
    assert validation["complete_acceptance"] is False  # Core cannot certify external custody.
    assert not list(workspace.temp_root.iterdir())


def test_corrupt_bundle_never_publishes_restore_or_report(tmp_path):
    workspace, key, captured, binding = bundle(tmp_path)
    path = workspace.bundles_root / captured.bundle_name
    data = bytearray(path.read_bytes())
    data[-1] ^= 1
    path.write_bytes(data)
    with pytest.raises(core.BackupBundleError, match="authentication_failed"):
        core.restore_bundle(workspace.root, captured.bundle_name, key,
                            report_name=captured.bundle_id + ".restore", association=binding)
    assert not list(workspace.restored_root.iterdir()) and not list(workspace.reports_root.iterdir())


def test_report_write_failure_never_publishes_restore(tmp_path, monkeypatch):
    workspace, key, captured, binding = bundle(tmp_path)
    original = core._atomic_write_json
    def fail(path, payload):
        if path.name == "verification.json":
            raise OSError("synthetic report persistence failure")
        return original(path, payload)
    monkeypatch.setattr(core, "_atomic_write_json", fail)
    with pytest.raises(core.BackupBundleError):
        core.restore_bundle(workspace.root, captured.bundle_name, key,
                            report_name=captured.bundle_id + ".restore", association=binding)
    assert not list(workspace.restored_root.iterdir()) and not list(workspace.reports_root.iterdir())
    assert not list(workspace.temp_root.iterdir())


def test_report_publication_failure_keeps_restored_target_but_fails(tmp_path, monkeypatch):
    workspace, key, captured, binding = bundle(tmp_path)
    original = core._publish_directory_no_replace
    def fail(source, target):
        if target.parent == workspace.reports_root:
            raise OSError("synthetic report publication failure")
        return original(source, target)
    monkeypatch.setattr(core, "_publish_directory_no_replace", fail)
    with pytest.raises(core.BackupBundleError):
        core.restore_bundle(workspace.root, captured.bundle_name, key,
                            report_name=captured.bundle_id + ".restore", association=binding)
    assert (workspace.restored_root / captured.bundle_id / "permanent" / "fixture.md").exists()
    assert not list(workspace.reports_root.iterdir())


@pytest.mark.parametrize("target", ["report", "restored"])
def test_existing_targets_are_rejected_before_decryption(tmp_path, monkeypatch, target):
    workspace, key, captured, binding = bundle(tmp_path)
    existing = (workspace.reports_root / (captured.bundle_id + ".restore") if target == "report"
                else workspace.restored_root / captured.bundle_id)
    existing.mkdir()
    (existing / "keep").write_text("existing")
    def forbidden(*args, **kwargs):
        raise AssertionError("existing target must fail before decrypt")
    monkeypatch.setattr(core, "_decrypt_and_validate", forbidden)
    with pytest.raises(core.BackupBundleError):
        core.restore_bundle(workspace.root, captured.bundle_name, key,
                            report_name=captured.bundle_id + ".restore", association=binding)
    assert (existing / "keep").read_text() == "existing"


def test_runtime_receipt_mismatch_prevents_publication(tmp_path):
    workspace, key, captured, binding = bundle(tmp_path)
    binding["runtime_commit"] = "9" * 40
    with pytest.raises(core.BackupBundleError, match="manifest_invalid"):
        core.verify_bundle(workspace.root, captured.bundle_name, key,
                           report_name=captured.bundle_id + ".verify", association=binding)
    assert not list(workspace.reports_root.iterdir())


def test_pending_final_commit_stops_before_git_keys_or_writes(monkeypatch):
    pending = plan()
    pending["final_deployment_sha"] = closeout.PENDING_DEPLOYMENT
    monkeypatch.setattr(closeout.subprocess, "check_output", lambda *args, **kw: pytest.fail("git called"))
    with pytest.raises(RuntimeError, match="pending"):
        closeout.guard(pending)


def test_binding_uses_exact_new_run_attempt_and_client_revision():
    binding = {"repository": "ALLFORTING/ob-backup", "workflow_commit": closeout.CLIENT_SHA,
               "run_id": "42", "run_attempt": 1, "artifact_id": 99, "artifact_digest": "sha256:" + "a" * 64,
               "bundle_id": "b" * 32, "encrypted_size": 123, "encrypted_sha256": "c" * 64}
    request = {"request_id": str(uuid.uuid4()), "oidc_run_id": "42", "oidc_run_attempt": "1"}
    assert closeout.validate_binding(binding, request, plan())["runtime_commit"] == COMMIT
    wrong = dict(request, oidc_run_attempt="2")
    with pytest.raises(RuntimeError, match="attempt_mismatch"):
        closeout.validate_binding(binding, wrong, plan())
    with pytest.raises(RuntimeError, match="revision_mismatch"):
        closeout.validate_binding(dict(binding, workflow_commit="7" * 40), request, plan())


def test_complete_receipt_requires_report_and_saved_receipt(tmp_path, monkeypatch):
    workspace, key, captured, binding = bundle(tmp_path)
    session = tmp_path / plan()["session_name"]
    session.mkdir()
    # Isolate the already-validated synthetic workspace beneath this new session.
    workspace.root.rename(session / "recovery")
    workspace = core.load_backup_workspace(session / "recovery")
    closeout.write_new_json(session / "recovery-binding.json", {
        "workspace": str(workspace.root), "target": str(workspace.restored_root / captured.bundle_id),
        "bundle": captured.bundle_name, "association": binding})
    monkeypatch.setattr(recovery, "load_encrypted_key", lambda path: key)
    original = closeout.write_new_json
    def fail_receipt(path, payload):
        if path.name == "restore-receipt.json":
            raise OSError("synthetic receipt persistence failure")
        return original(path, payload)
    monkeypatch.setattr(closeout, "write_new_json", fail_receipt)
    with pytest.raises(OSError):
        closeout.recover("restore", plan(), tmp_path, recovery)
    assert not (session / "restore-receipt.json").exists()
    assert (workspace.restored_root / captured.bundle_id).is_dir()
    assert (workspace.reports_root / (captured.bundle_id + ".restore") / "manifest.json").exists()


def test_exclusive_receipt_write_preserves_existing(tmp_path):
    receipt = tmp_path / "receipt.json"
    closeout.write_new_json(receipt, {"original": True})
    with pytest.raises(FileExistsError):
        closeout.write_new_json(receipt, {"replacement": True})
    assert closeout.read_json(receipt) == {"original": True}


def test_new_session_independent_stage_verify_restore_and_complete_receipt(tmp_path, monkeypatch):
    import shutil
    workspace, key, captured, association = bundle(tmp_path)
    selected_plan = plan()
    session = tmp_path / selected_plan["session_name"]
    download = session / "download"
    download.mkdir(parents=True)
    source = workspace.bundles_root / captured.bundle_name
    shutil.copyfile(source, download / captured.bundle_name)
    binding = {"repository": "ALLFORTING/ob-backup", **{
        field: association[field] for field in ("workflow_commit", "run_id", "run_attempt", "artifact_id",
                                               "artifact_digest", "bundle_id", "encrypted_size", "encrypted_sha256")}}
    closeout.write_new_json(download / "run-binding.json", binding)
    closeout.write_new_json(session / "download-selection.json", {"download_directory": str(download)})
    closeout.write_new_json(session / "request.json", {"request_id": association["request_id"],
        "oidc_run_id": "42", "oidc_run_attempt": "1"})
    with pytest.raises(RuntimeError, match="independent_bundle_path_pending"):
        closeout.stage_recovery(selected_plan, tmp_path, core)
    assert not (session / "recovery").exists()
    independent = tmp_path / "synthetic-independent-medium" / captured.bundle_name
    independent.parent.mkdir()
    shutil.copyfile(source, independent)
    selected_plan.update(independent_bundle_path=str(independent), independent_medium_confirmed=True)
    closeout.stage_recovery(selected_plan, tmp_path, core)
    monkeypatch.setattr(recovery, "load_encrypted_key", lambda path: key)
    verified = closeout.recover("verify", selected_plan, tmp_path, recovery)
    assert verified["complete_acceptance"] is False
    restored = closeout.recover("restore", selected_plan, tmp_path, recovery)
    assert restored["complete_acceptance"] is True
    assert closeout.read_json(session / "restore-receipt.json") == restored
    assert closeout.read_json(session / "independent-save-receipt.json")["verified"] is True
    saved_report = Path(restored["result"]["report_path"])
    assert closeout.read_json(saved_report / "association.json")["request_id"] == association["request_id"]
    assert restored["original_job_lease_release"] == "unknown"
    with pytest.raises(RuntimeError, match="no_overwrite"):
        closeout.stage_recovery(selected_plan, tmp_path, core)


def test_receipt_fsync_failure_does_not_publish_complete_marker(tmp_path, monkeypatch):
    receipt = tmp_path / "restore-receipt.json"
    def fail(descriptor):
        raise OSError("synthetic receipt fsync failure")
    monkeypatch.setattr(closeout.os, "fsync", fail)
    with pytest.raises(OSError):
        closeout.write_new_json(receipt, {"complete_acceptance": True})
    assert not receipt.exists() and not receipt.with_name(receipt.name + ".part").exists()

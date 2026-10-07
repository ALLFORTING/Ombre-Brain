"""Purpose-limited local backup closeout. Importing never reads keys or writes."""
from __future__ import annotations
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import uuid

ROOT = Path("D:/Codex/projects/OB-Backup-Recovery-20261004")
PENDING_DEPLOYMENT = "PENDING_FINAL_DEPLOYMENT_SHA"
CLIENT_SHA = "a78711f63609552fddb18f2d7128194476a9aff8"


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def write_new_json(path, value):
    path = Path(path)
    temporary = path.with_name(path.name + ".part")
    created = False
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as handle:
            created = True
            json.dump(value, handle, sort_keys=True, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        # Final receipts never expose unsynced/partial JSON, and never replace.
        os.link(temporary, path)
    finally:
        if created:
            temporary.unlink()

def validated_plan(plan):
    expected = plan.get("final_deployment_sha")
    if not isinstance(expected, str) or re.fullmatch("[0-9a-f]{40}", expected) is None:
        raise RuntimeError("final_deployment_sha_pending")
    if expected != plan.get("ob_source_commit"):
        raise RuntimeError("source_and_final_deployment_mismatch")
    if plan.get("client_commit") != CLIENT_SHA:
        raise RuntimeError("client_revision_mismatch")
    if re.fullmatch(r"closeout-[0-9a-f]{32}", str(plan.get("session_name", ""))) is None:
        raise RuntimeError("session_name_invalid")
    return plan


def bounded_ancestors(path):
    boundary = Path("D:/Codex") if path.drive else Path("/tmp")
    return [candidate for candidate in (path, *path.parents)
            if candidate == boundary or boundary in candidate.parents]


def guard(plan):
    validated_plan(plan)
    for key, commit in (("ob_source_worktree", plan["ob_source_commit"]),
                        ("client_worktree", CLIENT_SHA)):
        repo = Path(plan[key])
        if (not repo.is_absolute() or ".." in repo.parts
                or not repo.as_posix().startswith("D:/Codex/projects/")):
            raise RuntimeError("worktree_boundary_invalid")
        for path in bounded_ancestors(repo):
            if path.is_symlink() or path.is_junction():
                raise RuntimeError("worktree_link_denied")
        head = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
        dirty = subprocess.check_output(["git", "-C", str(repo), "status", "--porcelain"], text=True).strip()
        if head != commit or dirty:
            raise RuntimeError("worktree_revision_or_clean_mismatch")


def session_root(plan, root=ROOT):
    validated_plan(plan)
    for path in bounded_ancestors(root):
        if path.is_symlink() or path.is_junction():
            raise RuntimeError("preparation_link_denied")
    return root / plan["session_name"]


def validate_binding(binding, request, plan):
    validated_plan(plan)
    if binding.get("repository") != "ALLFORTING/ob-backup" or binding.get("workflow_commit") != CLIENT_SHA:
        raise RuntimeError("download_revision_mismatch")
    for field in ("run_id", "run_attempt", "artifact_id"):
        if re.fullmatch(r"[1-9][0-9]{0,19}", str(binding.get(field, ""))) is None:
            raise RuntimeError("download_identity_invalid")
    if (request.get("oidc_run_id") != str(binding["run_id"])
            or request.get("oidc_run_attempt") != str(binding["run_attempt"])):
        raise RuntimeError("request_attempt_mismatch")
    if str(uuid.UUID(request["request_id"])) != request["request_id"]:
        raise RuntimeError("request_id_invalid")
    if (re.fullmatch(r"[0-9a-f]{32}", str(binding.get("bundle_id", ""))) is None
            or re.fullmatch(r"[0-9a-f]{64}", str(binding.get("encrypted_sha256", ""))) is None
            or re.fullmatch(r"sha256:[0-9a-f]{64}", str(binding.get("artifact_digest", ""))) is None
            or type(binding.get("encrypted_size")) is not int or binding["encrypted_size"] <= 0):
        raise RuntimeError("download_metadata_invalid")
    return {"request_id": request["request_id"],
            **{field: binding[field] for field in ("run_id", "run_attempt", "artifact_id", "artifact_digest",
                                                  "workflow_commit", "bundle_id", "encrypted_size", "encrypted_sha256")},
            "runtime_commit": plan["final_deployment_sha"], "independent_copy_verified": True}


def check_bundle(path, association):
    path = Path(path)
    if path.is_symlink() or path.is_junction() or not path.is_file():
        raise RuntimeError("bundle_path_invalid")
    for ancestor in bounded_ancestors(path.parent):
        if ancestor.is_symlink() or ancestor.is_junction():
            raise RuntimeError("bundle_ancestor_link_denied")
    with path.open("rb") as handle:
        digest = hashlib.file_digest(handle, "sha256").hexdigest()
    if path.stat().st_size != association["encrypted_size"] or digest != association["encrypted_sha256"]:
        raise RuntimeError("bundle_integrity_mismatch")


def stage_recovery(plan, root=ROOT, core=None):
    session = session_root(plan, root)
    selection = read_json(session / "download-selection.json")
    directory = Path(selection["download_directory"])
    if directory.parent != session or directory.is_symlink() or directory.is_junction():
        raise RuntimeError("download_path_invalid")
    binding = read_json(directory / "run-binding.json")
    request = read_json(session / "request.json")
    association = validate_binding(binding, request, plan)
    check_bundle(directory / (association["bundle_id"] + ".obbackup"), association)
    independent = plan.get("independent_bundle_path")
    if not independent or independent == "PENDING_INDEPENDENT_BUNDLE_PATH":
        raise RuntimeError("independent_bundle_path_pending")
    independent = Path(independent)
    if plan.get("independent_medium_confirmed") is not True:
        raise RuntimeError("independent_medium_not_confirmed")
    if independent.drive and independent.drive.casefold() == root.drive.casefold():
        raise RuntimeError("independent_copy_on_same_drive")
    if not independent.is_absolute():
        raise RuntimeError("independent_bundle_path_invalid")
    # An exact path is supplied only in the later approved independent-save phase.
    check_bundle(independent, association)
    if independent.absolute() == (directory / (association["bundle_id"] + ".obbackup")).absolute():
        raise RuntimeError("independent_copy_is_download_source")
    workspace_path = session / "recovery"
    if workspace_path.exists() or (session / "recovery-binding.json").exists():
        raise RuntimeError("existing_recovery_material_no_overwrite")
    if core is None:
        import offline_backup_bundle as core
    workspace = core.prepare_backup_workspace(workspace_path)
    target_bundle = workspace.bundles_root / (association["bundle_id"] + ".obbackup")
    with independent.open("rb") as reader, target_bundle.open("xb") as writer:
        shutil.copyfileobj(reader, writer)
        writer.flush()
        os.fsync(writer.fileno())
    check_bundle(target_bundle, association)
    write_new_json(session / "independent-save-receipt.json", {
        "path": str(independent), "verified": True,
        "encrypted_size": association["encrypted_size"], "encrypted_sha256": association["encrypted_sha256"]})
    write_new_json(session / "recovery-binding.json", {
        "workspace": str(workspace.root), "target": str(workspace.restored_root / association["bundle_id"]),
        "bundle": target_bundle.name, "association": association})


def recover(operation, plan, root=ROOT, tool=None):
    session = session_root(plan, root)
    record = read_json(session / "recovery-binding.json")
    association = record["association"]
    if association["runtime_commit"] != plan["final_deployment_sha"]:
        raise RuntimeError("recovery_runtime_mismatch")
    bundle_id = association["bundle_id"]
    workspace = session / "recovery"
    target = workspace / "restored" / bundle_id
    if (Path(record["workspace"]) != workspace or Path(record["target"]) != target
            or record["bundle"] != bundle_id + ".obbackup"):
        raise RuntimeError("recovery_binding_invalid")
    check_bundle(workspace / "bundles" / record["bundle"], association)
    if tool is None:
        from scripts import backup_v2_recovery as tool
    result = tool.recover(operation, workspace, record["bundle"], root / "keys" / "recipient-private-key.pem",
                          target if operation == "restore" else None,
                          report_name=bundle_id + "." + operation, association=association)
    # No complete-success receipt exists if verification, publication or writing fails.
    if not result.get("authenticated") or not result.get("report_path"):
        raise RuntimeError("authenticated_report_missing")
    report = Path(result["report_path"])
    verification = read_json(report / "verification.json")
    manifest = read_json(report / "manifest.json")
    saved_association = read_json(report / "association.json")
    if (verification["status"] != "validated" or manifest["ob_commit_sha"] != plan["final_deployment_sha"]
            or saved_association["request_id"] != association["request_id"]):
        raise RuntimeError("saved_report_binding_mismatch")
    receipt = {"schema_version": 1, "operation": operation, "result": result,
               "association": association, "original_job_lease_release": "unknown",
               "complete_acceptance": operation == "restore"}
    write_new_json(session / (operation + "-receipt.json"), receipt)
    return receipt


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=["stage-recovery", "verify", "restore"])
    args = parser.parse_args(argv)
    plan = read_json(ROOT / "backup-closeout-plan.json")
    guard(plan)
    import sys
    sys.path.insert(0, plan["ob_source_worktree"])
    if args.operation == "stage-recovery":
        stage_recovery(plan)
        print("encrypted bundle staged from verified independent copy")
    else:
        receipt = recover(args.operation, plan)
        print(json.dumps(receipt, sort_keys=True))


if __name__ == "__main__":
    main()

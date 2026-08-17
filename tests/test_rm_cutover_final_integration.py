from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
import stat
import subprocess
import sys

from PIL import Image
import pytest

from asset_cutover_state import CutoverState, CutoverStateError, CutoverStateStore
from asset_mutation_gate import AssetMutationGate
from asset_store import AssetStore
from cutover_lease_capability import read_capability
from remember_me_adapter import EXPECTED_PACKAGE_VERSION
from remember_me_cutover_operations import acceptance_check_spec
from remember_me_cutover_transition import CutoverTransitionController, LEGACY_ACCEPTANCE_NAMES, RM_SOURCE_COMMIT
from tests._rm_acceptance_artifact import valid_rm_acceptance_artifact


ROOT = Path(__file__).resolve().parents[1]
MIGRATION_IDENTITY = "ombre-rm-production-cutover"


def _image_bytes() -> bytes:
    stream = io.BytesIO()
    image = Image.new("RGB", (4, 3), "green")
    image.save(stream, format="PNG")
    image.close()
    return stream.getvalue()


def _workspace(tmp_path: Path) -> dict[str, Path]:
    legacy = tmp_path / "buckets"
    store = AssetStore(str(legacy))
    payload = _image_bytes()
    temporary = store.create_temp_path(".png")
    temporary.write_bytes(payload)
    store.persist_upload(
        temporary,
        hashlib.sha256(payload).hexdigest(),
        len(payload),
        "fixture.png",
        "image/png",
        require_image=True,
    )
    rm = legacy / "remember-me"
    state = legacy / "state"
    assert not rm.exists()
    assert not state.exists()
    return {
        "root": tmp_path,
        "legacy": legacy,
        "rm": rm,
        "state": state,
        "state_db": state / "migration.sqlite3",
        "cap": state / "operator" / "lease-token.json",
        "backup": tmp_path / "backup",
        "reports": tmp_path / "reports",
    }


def _write_json(path: Path, value: object) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")
    return path


def _run(
    module: str,
    args: list[str],
    *,
    expected: int = 0,
    output: list[str] | None = None,
) -> str:
    result = subprocess.run(
        [sys.executable, "-m", module, *args],
        cwd=ROOT,
        env={
            **os.environ,
            "PYTHONUNBUFFERED": "1",
            "RENDER_INSTANCE_ID": "test-instance",
            "RENDER_GIT_COMMIT": "test-commit",
            "RENDER_SERVICE_ID": "test-service",
        },
        capture_output=True,
        text=True,
        check=False,
    )
    combined = result.stdout + result.stderr
    if output is not None:
        output.append(combined)
    assert result.returncode == expected, f"{module} exited {result.returncode}"
    return combined


def _run_fresh_server_boot(
    ws: dict[str, Path], *, check_health: bool = False
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    pythonpath = env.get("PYTHONPATH")
    env["PYTHONPATH"] = os.pathsep.join(
        [str(ROOT), *(pythonpath.split(os.pathsep) if pythonpath else [])]
    )
    for key in (
        "OMBRE_API_KEY",
        "OMBRE_ASSET_AUTHORITY",
        "OMBRE_RM_RUNTIME_ENABLED",
        "OMBRE_RM_DATA_ROOT",
    ):
        env.pop(key, None)
    env.update(
        {
            "OMBRE_BUCKETS_DIR": str(ws["legacy"]),
            "OMBRE_ASSET_AUTHORITY": "rm",
            "OMBRE_RM_RUNTIME_ENABLED": "true",
            "OMBRE_RM_DATA_ROOT": str(ws["rm"]),
            "PYTHONUNBUFFERED": "1",
        }
    )
    return subprocess.run(
        [
            sys.executable,
            "-c",
            f"""
import json
import server
from asset_backend import AssetBackendError

registry = server.asset_backend_registry
validation = registry._validate_boot()
mutation_error = None
try:
    registry.selected_backend().create_temp_path()
except AssetBackendError as exc:
    mutation_error = exc.code
health_status = None
if {check_health!r}:
    import asyncio

    health_status = asyncio.run(server.health_check(None)).status_code
payload = {{
    "boot": "ok",
    "boot_mode": validation.boot_mode,
    "selected_backend": registry.selected_backend().name,
    "authority": registry.authority.value,
    "recovery_required": validation.requires_recovery,
    "coordination_pending": validation.coordination_pending,
    "writes_allowed": validation.writes_allowed,
    "legacy_fallback_allowed": validation.legacy_fallback_allowed,
    "mutation_error": mutation_error,
    "state": registry.snapshot.state.value,
    "durable_authority": registry.snapshot.authority.value,
}}
if health_status is not None:
    payload["health_status"] = health_status
print(json.dumps(payload))
""",
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def _migration_args(ws: dict[str, Path], report_name: str) -> list[str]:
    return [
        "--legacy-root",
        str(ws["legacy"]),
        "--rm-root",
        str(ws["rm"]),
        "--state-db",
        str(ws["state_db"]),
        "--report",
        str(ws["reports"] / report_name),
        "--migration-identity",
        MIGRATION_IDENTITY,
        "--migration-version",
        "1",
    ]


def _bootstrap_and_acquire(ws: dict[str, Path], output: list[str]) -> None:
    _run(
        "remember_me_cutover_migration",
        [
            "preflight-local",
            *_migration_args(ws, "preflight-local.json"),
        ],
        output=output,
    )
    assert not ws["rm"].exists()
    assert not ws["state"].exists()

    _run(
        "remember_me_cutover_operations",
        [
            "backup",
            "--profile",
            "legacy-authoritative",
            "--legacy-root",
            str(ws["legacy"]),
            "--rm-root",
            str(ws["rm"]),
            "--state-db",
            str(ws["state_db"]),
            "--destination",
            str(ws["backup"]),
            "--report",
            str(ws["reports"] / "backup.json"),
        ],
        output=output,
    )
    _run(
        "remember_me_cutover_operations",
        ["verify-backup", "--backup", str(ws["backup"])],
        output=output,
    )

    restored = ws["root"] / "isolated-restore"
    _run(
        "remember_me_cutover_operations",
        [
            "restore",
            "--backup",
            str(ws["backup"]),
            "--legacy-root",
            str(restored / "legacy"),
            "--rm-root",
            str(restored / "legacy" / "remember-me"),
            "--state-root",
            str(restored / "legacy" / "state"),
            "--report",
            str(ws["reports"] / "restore.json"),
        ],
        output=output,
    )
    assert not (restored / "legacy" / "remember-me").exists()
    assert not (restored / "legacy" / "state").exists()

    _run(
        "remember_me_cutover_migration",
        [
            "initialize-cutover",
            "--legacy-root",
            str(ws["legacy"]),
            "--rm-root",
            str(ws["rm"]),
            "--state-db",
            str(ws["state_db"]),
            "--report",
            str(ws["reports"] / "initialize.json"),
        ],
        output=output,
    )
    assert ws["rm"].is_dir()
    assert ws["state_db"].is_file()

    _run(
        "remember_me_cutover_migration",
        [
            "acquire-freeze",
            *_migration_args(ws, "acquire-freeze.json"),
            "--lease-ttl-seconds",
            "300",
            "--lease-capability-file",
            str(ws["cap"]),
        ],
        output=output,
    )
    assert ws["cap"].is_file()
    if os.name != "nt":
        assert stat.S_IMODE(ws["cap"].stat().st_mode) == 0o600


def _run_to_ready(
    ws: dict[str, Path], output: list[str], *, enter_rm_acceptance: bool = True
) -> None:
    _bootstrap_and_acquire(ws, output)
    for command, report in (
        ("migrate", "migrate.json"),
        ("reconcile", "reconcile.json"),
        ("verify", "verify.json"),
    ):
        extra = []
        if command == "migrate":
            extra = ["--batch-size", "1"]
        _run(
            "remember_me_cutover_migration",
            [
                command,
                *_migration_args(ws, report),
                *extra,
                "--lease-ttl-seconds",
                "300",
                "--lease-capability-file",
                str(ws["cap"]),
            ],
            output=output,
        )

    _run(
        "remember_me_cutover_operations",
        [
            "preflight",
            "--legacy-root",
            str(ws["legacy"]),
            "--rm-root",
            str(ws["rm"]),
            "--state-db",
            str(ws["state_db"]),
            "--report",
            str(ws["reports"] / "d1-preflight.json"),
            "--backup-root",
            str(ws["backup"]),
            "--embedding-enabled",
            "false",
            "--estimated-vector-bytes",
            "0",
            "--headroom-bytes",
            "0",
            "--worker-count",
            "1",
            "--multiprocess",
            "false",
            "--shared-state",
            "true",
            "--service-instances",
            "1",
        ],
        output=output,
    )
    _run(
        "remember_me_cutover_migration",
        [
            "reindex",
            *_migration_args(ws, "reindex.json"),
            "--lease-ttl-seconds",
            "300",
            "--max-new-index-work",
            "0",
            "--lease-capability-file",
            str(ws["cap"]),
        ],
        output=output,
    )

    readiness = {
        "transition_identity": "local-transition-1",
        "readiness_evidence_id": "local-readiness-evidence",
        "dependency_exact": True,
        "storage_layout": True,
        "freeze_held": True,
        "legacy_authority_active": True,
        "reconciliation_exact": True,
        "verification_passed": True,
        "vector_profile": True,
        "backup_verified": True,
        "disk_acceptable": True,
        "topology_safe": True,
        "stale_authority_clear": True,
        "dependency": {
            "version": EXPECTED_PACKAGE_VERSION,
            "source_commit": RM_SOURCE_COMMIT,
        },
        "rm_runtime_healthy": True,
        "rm_data_root_healthy": True,
        "state_healthy": True,
        "migration_complete": True,
        "reconciliation_pass": True,
        "verification_pass": True,
        "vector_readiness_pass": True,
        "backup_evidence_present": True,
        "backup_evidence_id": "local-backup-evidence",
        "storage_root_validation_pass": True,
        "disk_readiness_pass": True,
        "topology_readiness_pass": True,
        "no_stale_authority": True,
    }
    readiness_path = _write_json(ws["reports"] / "readiness-evidence.json", readiness)
    _run(
        "remember_me_cutover_operations",
        [
            "readiness-gate",
            "--evidence",
            str(readiness_path),
            "--report",
            str(ws["reports"] / "readiness-gate.json"),
        ],
        output=output,
    )
    if not enter_rm_acceptance:
        return
    _run(
        "remember_me_cutover_transition",
        [
            "prepare-rm",
            "--state-db",
            str(ws["state_db"]),
            "--configured-authority",
            "legacy",
            "--lease-capability-file",
            str(ws["cap"]),
            "--evidence",
            str(readiness_path),
        ],
        output=output,
    )
    _run_rm_switch(ws, output)


def _run_rm_switch(ws: dict[str, Path], output: list[str]) -> None:
    _run(
        "remember_me_cutover_transition",
        [
            "switch-to-rm",
            "--state-db",
            str(ws["state_db"]),
            "--configured-authority",
            "rm",
            "--lease-capability-file",
            str(ws["cap"]),
            "--restart-validated",
            "--rm-available",
            "true",
        ],
        output=output,
    )


def _rerun_to_ready_after_abort(ws: dict[str, Path], output: list[str]) -> None:
    """Repeat the gated D1/D2 preparation on an existing aborted workspace."""
    _run(
        "remember_me_cutover_migration",
        [
            "acquire-freeze",
            *_migration_args(ws, "reacquire-freeze.json"),
            "--lease-ttl-seconds",
            "300",
            "--lease-capability-file",
            str(ws["cap"]),
        ],
        output=output,
    )
    for command, report in (
        ("migrate", "remigrate.json"),
        ("reconcile", "rereconcile.json"),
        ("verify", "reverify.json"),
    ):
        extra = ["--batch-size", "1"] if command == "migrate" else []
        _run(
            "remember_me_cutover_migration",
            [
                command,
                *_migration_args(ws, report),
                *extra,
                "--lease-ttl-seconds",
                "300",
                "--lease-capability-file",
                str(ws["cap"]),
            ],
            output=output,
        )

    _run(
        "remember_me_cutover_operations",
        [
            "preflight",
            "--legacy-root",
            str(ws["legacy"]),
            "--rm-root",
            str(ws["rm"]),
            "--state-db",
            str(ws["state_db"]),
            "--report",
            str(ws["reports"] / "re-d1-preflight.json"),
            "--backup-root",
            str(ws["backup"]),
            "--embedding-enabled",
            "false",
            "--estimated-vector-bytes",
            "0",
            "--headroom-bytes",
            "0",
            "--worker-count",
            "1",
            "--multiprocess",
            "false",
            "--shared-state",
            "true",
            "--service-instances",
            "1",
        ],
        output=output,
    )
    _run(
        "remember_me_cutover_migration",
        [
            "reindex",
            *_migration_args(ws, "reindex-again.json"),
            "--lease-ttl-seconds",
            "300",
            "--max-new-index-work",
            "0",
            "--lease-capability-file",
            str(ws["cap"]),
        ],
        output=output,
    )
    readiness = {
        "transition_identity": "local-transition-2",
        "readiness_evidence_id": "local-readiness-evidence-2",
        "dependency_exact": True,
        "storage_layout": True,
        "freeze_held": True,
        "legacy_authority_active": True,
        "reconciliation_exact": True,
        "verification_passed": True,
        "vector_profile": True,
        "backup_verified": True,
        "disk_acceptable": True,
        "topology_safe": True,
        "stale_authority_clear": True,
        "dependency": {
            "version": EXPECTED_PACKAGE_VERSION,
            "source_commit": RM_SOURCE_COMMIT,
        },
        "rm_runtime_healthy": True,
        "rm_data_root_healthy": True,
        "state_healthy": True,
        "migration_complete": True,
        "reconciliation_pass": True,
        "verification_pass": True,
        "vector_readiness_pass": True,
        "backup_evidence_present": True,
        "backup_evidence_id": "local-backup-evidence-2",
        "storage_root_validation_pass": True,
        "disk_readiness_pass": True,
        "topology_readiness_pass": True,
        "no_stale_authority": True,
    }
    readiness_path = _write_json(ws["reports"] / "readiness-evidence-2.json", readiness)
    _run(
        "remember_me_cutover_operations",
        [
            "readiness-gate",
            "--evidence",
            str(readiness_path),
            "--report",
            str(ws["reports"] / "readiness-gate-2.json"),
        ],
        output=output,
    )
    _run(
        "remember_me_cutover_transition",
        [
            "prepare-rm",
            "--state-db",
            str(ws["state_db"]),
            "--configured-authority",
            "legacy",
            "--lease-capability-file",
            str(ws["cap"]),
            "--evidence",
            str(readiness_path),
        ],
        output=output,
    )
def _run_rm_acceptance(ws: dict[str, Path], output: list[str], *, passed: bool) -> None:
    controller = CutoverTransitionController(ws["state_db"], capability_file=ws["cap"])
    checks, evidence = valid_rm_acceptance_artifact(
        CutoverStateStore(ws["state_db"]),
        controller,
        passed=passed,
    )
    checks_path = _write_json(ws["reports"] / "rm-checks.json", checks)
    evidence_path = _write_json(ws["reports"] / "rm-acceptance-evidence.json", evidence)
    _run(
        "remember_me_cutover_operations",
        [
            "acceptance-checks",
            "--evidence",
            str(evidence_path),
            "--report",
            str(ws["reports"] / "rm-acceptance.json"),
        ],
        expected=0 if passed else 2,
        output=output,
    )
    _run(
        "remember_me_cutover_transition",
        [
            "accept-rm",
            "--state-db",
            str(ws["state_db"]),
            "--configured-authority",
            "rm",
            "--lease-capability-file",
            str(ws["cap"]),
            "--checks",
            str(checks_path),
        ],
        output=output,
    )


def _assert_secret_containment(ws: dict[str, Path], output: list[str]) -> str:
    token = read_capability(ws["cap"], state_root=ws["state"]).token
    secret = token.encode("utf-8")
    assert all(secret not in item.encode("utf-8") for item in output)
    for path in ws["root"].rglob("*"):
        if path.is_file() and path != ws["cap"]:
            assert secret not in path.read_bytes()
    return token


def _assert_no_secret_after_cleanup(ws: dict[str, Path], output: list[str], token: str) -> None:
    assert not ws["cap"].exists()
    secret = token.encode("utf-8")
    for path in ws["root"].rglob("*"):
        if path.is_file():
            assert secret not in path.read_bytes()
    assert all(token not in item for item in output)


def _assert_rm_open_read_and_write_gate(ws: dict[str, Path]) -> None:
    status = CutoverStateStore(ws["state_db"]).get_snapshot()
    assert status.state is CutoverState.RM_AUTHORITY_OPEN
    assert AssetMutationGate(CutoverStateStore(ws["state_db"])).public_mutations_allowed()
    rm_db = ws["rm"] / "assets.sqlite3"
    with sqlite3.connect(f"file:{rm_db.as_posix()}?mode=ro", uri=True) as connection:
        assert connection.execute("SELECT COUNT(*) FROM assets").fetchone()[0] == 1


def test_true_start_cli_rehearsal_reaches_rm_open(tmp_path):
    ws = _workspace(tmp_path)
    output: list[str] = []
    _run_to_ready(ws, output)
    success_token = _assert_secret_containment(ws, output)
    _run_rm_acceptance(ws, output, passed=True)
    _run(
        "remember_me_cutover_transition",
        [
            "release-freeze-to-rm",
            "--state-db",
            str(ws["state_db"]),
            "--configured-authority",
            "rm",
            "--lease-capability-file",
            str(ws["cap"]),
        ],
        output=output,
    )
    _run(
        "remember_me_cutover_transition",
        [
            "status",
            "--state-db",
            str(ws["state_db"]),
            "--configured-authority",
            "rm",
            "--rm-available",
            "true",
        ],
        output=output,
    )
    _assert_rm_open_read_and_write_gate(ws)
    _assert_no_secret_after_cleanup(ws, output, success_token)


def test_true_server_restart_boots_rm_prepared_then_switches(tmp_path):
    ws = _workspace(tmp_path)
    output: list[str] = []
    _run_to_ready(ws, output, enter_rm_acceptance=False)
    _run(
        "remember_me_cutover_transition",
        [
            "prepare-rm",
            "--state-db",
            str(ws["state_db"]),
            "--configured-authority",
            "legacy",
            "--lease-capability-file",
            str(ws["cap"]),
            "--evidence",
            str(ws["reports"] / "readiness-evidence.json"),
        ],
        output=output,
    )

    before = CutoverStateStore(ws["state_db"]).get_snapshot()
    assert before.state is CutoverState.FROZEN_READY_FOR_RM_SWITCH
    assert before.authority.value == "legacy"

    completed = _run_fresh_server_boot(ws)
    combined = completed.stdout + completed.stderr
    assert completed.returncode == 0, combined
    assert "state_authority_ambiguous" not in combined
    payload = json.loads(completed.stdout)
    assert payload == {
        "authority": "rm",
        "boot": "ok",
        "boot_mode": "COORDINATION_PENDING",
        "coordination_pending": True,
        "durable_authority": "legacy",
        "legacy_fallback_allowed": False,
        "mutation_error": "asset_write_frozen",
        "recovery_required": False,
        "selected_backend": "rm",
        "state": "frozen_ready_for_rm_switch",
        "writes_allowed": False,
    }

    after_boot = CutoverStateStore(ws["state_db"]).get_snapshot()
    assert after_boot.state is CutoverState.FROZEN_READY_FOR_RM_SWITCH
    assert after_boot.authority.value == "legacy"
    _run_rm_switch(ws, output)
    switched = CutoverStateStore(ws["state_db"]).get_snapshot()
    assert switched.state is CutoverState.FROZEN_RM_ACCEPTANCE
    assert switched.authority.value == "rm"


def test_abort_invalidates_stale_d2_record_and_next_cycle_refreshes_it(tmp_path):
    ws = _workspace(tmp_path)
    output: list[str] = []
    _run_to_ready(ws, output, enter_rm_acceptance=False)
    _run(
        "remember_me_cutover_transition",
        [
            "prepare-rm",
            "--state-db",
            str(ws["state_db"]),
            "--configured-authority",
            "legacy",
            "--lease-capability-file",
            str(ws["cap"]),
            "--evidence",
            str(ws["reports"] / "readiness-evidence.json"),
        ],
        output=output,
    )
    prepared = json.loads(
        _run(
            "remember_me_cutover_transition",
            [
                "status",
                "--state-db",
                str(ws["state_db"]),
                "--configured-authority",
                "legacy",
                "--rm-available",
                "true",
            ],
            output=output,
        )
    )
    assert prepared["phase"] == "RM_PREPARED"
    old_lease_id = prepared["freeze_lease_id"]
    old_transition_identity = prepared["transition_identity"]

    _run(
        "remember_me_cutover_migration",
        [
            "abort",
            *_migration_args(ws, "abort-stale-d2.json"),
            "--reason",
            "local stale d2 rehearsal",
            "--lease-capability-file",
            str(ws["cap"]),
        ],
        output=output,
    )
    aborted = json.loads(
        _run(
            "remember_me_cutover_transition",
            [
                "status",
                "--state-db",
                str(ws["state_db"]),
                "--configured-authority",
                "legacy",
                "--rm-available",
                "true",
            ],
            output=output,
        )
    )
    assert aborted["cutover_state"] == CutoverState.LEGACY_AUTHORITY_RM_READY.value
    assert aborted["phase"] == "NONE"
    assert aborted["transition_identity"] is None
    assert aborted["freeze_active"] is False
    assert aborted["next_legal_operator_actions"] == [
        "prepare RM switch only after D1 readiness is PASS"
    ]

    _rerun_to_ready_after_abort(ws, output)
    refreshed = json.loads(
        _run(
            "remember_me_cutover_transition",
            [
                "status",
                "--state-db",
                str(ws["state_db"]),
                "--configured-authority",
                "legacy",
                "--rm-available",
                "true",
            ],
            output=output,
        )
    )
    assert refreshed["phase"] == "RM_PREPARED"
    assert refreshed["transition_identity"] == "local-transition-2"
    assert refreshed["freeze_lease_id"] != old_lease_id
    assert refreshed["transition_identity"] != old_transition_identity


def test_cli_pre_d2_abort_from_both_frozen_states(tmp_path):
    first = _workspace(tmp_path / "migration")
    first_output: list[str] = []
    _bootstrap_and_acquire(first, first_output)
    first_token = _assert_secret_containment(first, first_output)
    _run(
        "remember_me_cutover_migration",
        [
            "abort",
            *_migration_args(first, "abort.json"),
            "--reason",
            "local pre-d2 rehearsal",
            "--lease-capability-file",
            str(first["cap"]),
        ],
        output=first_output,
    )
    assert CutoverStateStore(first["state_db"]).get_snapshot().state is CutoverState.LEGACY_AUTHORITY_RM_READY
    _assert_no_secret_after_cleanup(first, first_output, first_token)

    second = _workspace(tmp_path / "ready")
    second_output: list[str] = []
    _run_to_ready(second, second_output, enter_rm_acceptance=False)
    assert CutoverStateStore(second["state_db"]).get_snapshot().state is CutoverState.FROZEN_READY_FOR_RM_SWITCH
    second_token = _assert_secret_containment(second, second_output)
    _run(
        "remember_me_cutover_migration",
        [
            "abort",
            *_migration_args(second, "abort.json"),
            "--reason",
            "local ready-state rehearsal",
            "--lease-capability-file",
            str(second["cap"]),
        ],
        output=second_output,
    )
    assert CutoverStateStore(second["state_db"]).get_snapshot().state is CutoverState.LEGACY_AUTHORITY_RM_READY
    assert (second["rm"] / "assets.sqlite3").is_file()
    _assert_no_secret_after_cleanup(second, second_output, second_token)


def test_cli_d2_class_a_rollback_preserves_rm_target(tmp_path):
    ws = _workspace(tmp_path)
    output: list[str] = []
    _run_to_ready(ws, output)
    rollback_token = read_capability(ws["cap"], state_root=ws["state"]).token
    _run_rm_acceptance(ws, output, passed=False)
    assert CutoverStateStore(ws["state_db"]).get_snapshot().state is CutoverState.FROZEN_RM_ACCEPTANCE
    _run(
        "remember_me_cutover_transition",
        [
            "class-a-rollback",
            "--state-db",
            str(ws["state_db"]),
            "--configured-authority",
            "rm",
            "--lease-capability-file",
            str(ws["cap"]),
            "--reason",
            "local frozen acceptance failure",
            "--mode",
            "prepare",
            "--rm-available",
            "true",
        ],
        output=output,
    )
    _run(
        "remember_me_cutover_transition",
        [
            "class-a-rollback",
            "--state-db",
            str(ws["state_db"]),
            "--configured-authority",
            "legacy",
            "--lease-capability-file",
            str(ws["cap"]),
            "--reason",
            "local frozen acceptance failure",
            "--mode",
            "finalize",
            "--restart-validated",
            "--rm-available",
            "true",
        ],
        output=output,
    )
    legacy_checks = _write_json(
        ws["reports"] / "legacy-checks.json",
        {name: True for name in LEGACY_ACCEPTANCE_NAMES},
    )
    _run(
        "remember_me_cutover_transition",
        [
            "accept-legacy",
            "--state-db",
            str(ws["state_db"]),
            "--configured-authority",
            "legacy",
            "--lease-capability-file",
            str(ws["cap"]),
            "--checks",
            str(legacy_checks),
        ],
        output=output,
    )
    _run(
        "remember_me_cutover_transition",
        [
            "release-freeze-to-legacy",
            "--state-db",
            str(ws["state_db"]),
            "--configured-authority",
            "legacy",
            "--lease-capability-file",
            str(ws["cap"]),
        ],
        output=output,
    )
    assert CutoverStateStore(ws["state_db"]).get_snapshot().state is CutoverState.LEGACY_AUTHORITY_RM_READY
    assert (ws["rm"] / "assets.sqlite3").is_file()
    _assert_no_secret_after_cleanup(ws, output, rollback_token)


def test_expired_d2_recovery_rotates_lease_and_survives_fresh_rm_boot(tmp_path):
    ws = _workspace(tmp_path)
    output: list[str] = []
    _run_to_ready(ws, output, enter_rm_acceptance=False)
    _run(
        "remember_me_cutover_transition",
        [
            "prepare-rm",
            "--state-db",
            str(ws["state_db"]),
            "--configured-authority",
            "legacy",
            "--lease-capability-file",
            str(ws["cap"]),
            "--evidence",
            str(ws["reports"] / "readiness-evidence.json"),
        ],
        output=output,
    )
    coordination_boot = _run_fresh_server_boot(ws)
    assert coordination_boot.returncode == 0, coordination_boot.stdout + coordination_boot.stderr
    _run_rm_switch(ws, output)

    old_capability = read_capability(ws["cap"], state_root=ws["state"])
    old_lease_id = old_capability.lease_id
    old_token = old_capability.token
    with sqlite3.connect(ws["state_db"]) as connection:
        connection.execute(
            "UPDATE cutover_freeze SET expires_at = '2000-01-01T00:00:00+00:00' WHERE singleton = 1"
        )

    expired_status = json.loads(
        _run(
            "remember_me_cutover_transition",
            [
                "status",
                "--state-db",
                str(ws["state_db"]),
                "--configured-authority",
                "rm",
                "--rm-available",
                "true",
            ],
            output=output,
        )
    )
    assert expired_status["phase"] == "RM_FROZEN_ACCEPTANCE"
    assert expired_status["authority"] == "rm"
    assert expired_status["freeze_status"] == "expired"
    assert expired_status["freeze_active"] is False
    assert expired_status["lease_healthy"] is False
    assert expired_status["LOSSLESS_ROLLBACK_WINDOW_OPEN"] == "NO"
    assert expired_status["rollback_class_currently_available"] == "NONE"
    assert expired_status["next_legal_operator_actions"] == ["recover-expired-rm"]
    assert not AssetMutationGate(CutoverStateStore(ws["state_db"])).public_mutations_allowed()

    before_recovery_boot = CutoverStateStore(ws["state_db"]).get_snapshot()
    with sqlite3.connect(ws["state_db"]) as connection:
        before_d2_record = connection.execute(
            "SELECT schema_version, payload_json FROM d2_transition_record WHERE singleton = 1"
        ).fetchone()

    recovery_boot = _run_fresh_server_boot(ws)
    assert recovery_boot.returncode == 0, recovery_boot.stdout + recovery_boot.stderr
    assert "state_freeze_ambiguous" not in (recovery_boot.stdout + recovery_boot.stderr)
    assert json.loads(recovery_boot.stdout) == {
        "authority": "rm",
        "boot": "ok",
        "boot_mode": "EXPIRED_RM_RECOVERY",
        "coordination_pending": False,
        "durable_authority": "rm",
        "legacy_fallback_allowed": False,
        "mutation_error": "asset_write_frozen",
        "recovery_required": True,
        "selected_backend": "rm",
        "state": "frozen_rm_acceptance",
        "writes_allowed": False,
    }
    assert CutoverStateStore(ws["state_db"]).get_snapshot() == before_recovery_boot
    with sqlite3.connect(ws["state_db"]) as connection:
        assert connection.execute(
            "SELECT schema_version, payload_json FROM d2_transition_record WHERE singleton = 1"
        ).fetchone() == before_d2_record

    recovery_controller = CutoverTransitionController(ws["state_db"], capability_file=ws["cap"])
    checks_payload, evidence_payload = valid_rm_acceptance_artifact(
        CutoverStateStore(ws["state_db"]),
        recovery_controller,
        run_id="expired-acceptance-test-1",
    )
    checks = _write_json(ws["reports"] / "expired-rm-checks.json", checks_payload)
    _write_json(ws["reports"] / "rm-acceptance-evidence.json", evidence_payload)
    for command_args in (
        [
            "accept-rm",
            "--checks",
            str(checks),
        ],
        ["release-freeze-to-rm"],
        [
            "class-a-rollback",
            "--reason",
            "expired retained lease",
            "--mode",
            "prepare",
            "--rm-available",
            "true",
        ],
    ):
        _run(
            "remember_me_cutover_transition",
            [
                *command_args,
                "--state-db",
                str(ws["state_db"]),
                "--configured-authority",
                "rm",
                "--lease-capability-file",
                str(ws["cap"]),
            ],
            expected=2,
            output=output,
        )

    recovered_output = _run(
        "remember_me_cutover_transition",
        [
            "recover-expired-rm",
            "--state-db",
            str(ws["state_db"]),
            "--configured-authority",
            "rm",
            "--lease-capability-file",
            str(ws["cap"]),
            "--transition-identity",
            expired_status["transition_identity"],
            "--lease-ttl-seconds",
            "120",
            "--rm-available",
            "true",
        ],
        output=output,
    )
    assert "token" not in recovered_output.lower()
    new_capability = read_capability(ws["cap"], state_root=ws["state"])
    assert new_capability.lease_id != old_lease_id
    with pytest.raises(CutoverStateError, match="^freeze_lease_invalid$"):
        CutoverStateStore(ws["state_db"]).load_lease(old_lease_id, old_token)
    with sqlite3.connect(ws["state_db"]) as connection:
        transition_payload = json.loads(
            connection.execute(
                "SELECT payload_json FROM d2_transition_record WHERE singleton = 1"
            ).fetchone()[0]
        )
        lease_generation = connection.execute(
            "SELECT generation FROM cutover_freeze WHERE singleton = 1"
        ).fetchone()[0]
    assert transition_payload["lease_id"] == new_capability.lease_id
    assert transition_payload["lease_generation"] == lease_generation

    recovered_status = json.loads(
        _run(
            "remember_me_cutover_transition",
            [
                "status",
                "--state-db",
                str(ws["state_db"]),
                "--configured-authority",
                "rm",
                "--rm-available",
                "true",
            ],
            output=output,
        )
    )
    assert recovered_status["phase"] == "RM_FROZEN_ACCEPTANCE"
    assert recovered_status["authority"] == "rm"
    assert recovered_status["freeze_status"] == "active"
    assert recovered_status["freeze_active"] is True
    assert recovered_status["lease_healthy"] is True
    assert recovered_status["LOSSLESS_ROLLBACK_WINDOW_OPEN"] == "YES"
    assert recovered_status["rollback_class_currently_available"] == "CLASS_A"
    assert recovered_status["acceptance_status"] is None

    after_recovery_boot = _run_fresh_server_boot(ws)
    assert after_recovery_boot.returncode == 0, after_recovery_boot.stdout + after_recovery_boot.stderr
    assert json.loads(after_recovery_boot.stdout) == {
        "authority": "rm",
        "boot": "ok",
        "boot_mode": "NORMAL",
        "coordination_pending": False,
        "durable_authority": "rm",
        "legacy_fallback_allowed": False,
        "mutation_error": "asset_write_frozen",
        "recovery_required": False,
        "selected_backend": "rm",
        "state": "frozen_rm_acceptance",
        "writes_allowed": False,
    }

    _run(
        "remember_me_cutover_transition",
        [
            "release-freeze-to-rm",
            "--state-db",
            str(ws["state_db"]),
            "--configured-authority",
            "rm",
            "--lease-capability-file",
            str(ws["cap"]),
        ],
        expected=2,
        output=output,
    )
    _run_rm_acceptance(ws, output, passed=True)
    assert CutoverStateStore(ws["state_db"]).get_snapshot().state is CutoverState.FROZEN_RM_ACCEPTANCE
    assert old_token not in "\n".join(output)
    for path in ws["root"].rglob("*"):
        if path.is_file() and path != ws["cap"]:
            assert old_token.encode("utf-8") not in path.read_bytes()
    if os.name != "nt":
        assert stat.S_IMODE(ws["cap"].stat().st_mode) == 0o600


def test_legacy_expired_d2_record_boots_fresh_rm_runtime_without_writes(tmp_path):
    ws = _workspace(tmp_path)
    output: list[str] = []
    _run_to_ready(ws, output, enter_rm_acceptance=False)
    _run(
        "remember_me_cutover_transition",
        [
            "prepare-rm",
            "--state-db",
            str(ws["state_db"]),
            "--configured-authority",
            "legacy",
            "--lease-capability-file",
            str(ws["cap"]),
            "--evidence",
            str(ws["reports"] / "readiness-evidence.json"),
        ],
        output=output,
    )
    _run_rm_switch(ws, output)

    with sqlite3.connect(ws["state_db"]) as connection:
        connection.execute(
            "UPDATE cutover_freeze SET expires_at = '2000-01-01T00:00:00+00:00' WHERE singleton = 1"
        )
        row = connection.execute(
            "SELECT schema_version, payload_json FROM d2_transition_record WHERE singleton = 1"
        ).fetchone()
        assert row[0] == 1
        payload = json.loads(row[1])
        assert payload.pop("lease_generation") is not None
        connection.execute(
            "UPDATE d2_transition_record SET payload_json = ? WHERE singleton = 1",
            (json.dumps(payload, sort_keys=True),),
        )
        before_record = connection.execute(
            "SELECT schema_version, payload_json FROM d2_transition_record WHERE singleton = 1"
        ).fetchone()
    before_snapshot = CutoverStateStore(ws["state_db"]).get_snapshot()

    boot = _run_fresh_server_boot(ws, check_health=True)
    combined = boot.stdout + boot.stderr
    assert boot.returncode == 0, combined
    assert "state_freeze_ambiguous" not in combined
    assert json.loads(boot.stdout) == {
        "authority": "rm",
        "boot": "ok",
        "boot_mode": "EXPIRED_RM_RECOVERY",
        "coordination_pending": False,
        "durable_authority": "rm",
        "legacy_fallback_allowed": False,
        "mutation_error": "asset_write_frozen",
        "recovery_required": True,
        "selected_backend": "rm",
        "state": "frozen_rm_acceptance",
        "writes_allowed": False,
        "health_status": 200,
    }
    assert CutoverStateStore(ws["state_db"]).get_snapshot() == before_snapshot
    with sqlite3.connect(ws["state_db"]) as connection:
        assert connection.execute(
            "SELECT schema_version, payload_json FROM d2_transition_record WHERE singleton = 1"
        ).fetchone() == before_record

import importlib.util
import json
import sqlite3
from pathlib import Path
import sys
import threading
import time

import pytest


ROOT = Path(__file__).resolve().parents[1]
PROBE_PATH = ROOT / "scripts" / "rm_frozen_acceptance_probe.py"


def _probe_module():
    spec = importlib.util.spec_from_file_location("rm_frozen_acceptance_probe", PROBE_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _sqlite(present=False):
    return {
        "present": present,
        "size": 0,
        "integrity": None,
        "tables": (),
        "counts": {},
        "row_total": 0,
    }


def _tree():
    return {"present": False, "files": 0, "bytes": 0, "suffixes": {}, "digest": None}


def _snapshot():
    return {
        "state_db": _sqlite(),
        "rm_db": _sqlite(),
        "legacy_db": _sqlite(),
        "state_tree": _tree(),
        "rm_tree": _tree(),
        "legacy_tree": _tree(),
    }


def _mcp_message(*, structured=None, text_payload=None):
    result = {
        "content": [],
        "isError": False,
    }
    if structured is not None:
        result["structuredContent"] = structured
    if text_payload is not None:
        result["content"].append(
            {"type": "text", "text": json.dumps(text_payload)}
        )
    return {"jsonrpc": "2.0", "id": "probe-test", "result": result}


def test_tool_payload_accepts_direct_structured_compatibility_envelope():
    probe = _probe_module()
    payload = {"ok": False, "error": "asset_write_frozen"}
    assert probe._tool_payload({"structuredContent": payload}) == payload


def test_tool_payload_unwraps_nested_structured_error_envelope():
    probe = _probe_module()
    payload = {"ok": False, "error": "asset_unavailable"}
    assert probe._tool_payload(_mcp_message(structured={"result": payload})) == payload


def test_tool_payload_unwraps_nested_structured_empty_search_envelope():
    probe = _probe_module()
    payload = {"ok": True, "total": 0, "results": []}
    assert probe._tool_payload(_mcp_message(structured={"result": payload})) == payload


def test_tool_payload_unwraps_nested_structured_frozen_mutation_envelope():
    probe = _probe_module()
    payload = {"ok": False, "error": "asset_write_frozen"}
    assert probe._tool_payload(_mcp_message(structured={"result": payload})) == payload


def test_tool_payload_ignores_unusable_structured_content_and_uses_text():
    probe = _probe_module()
    payload = {"ok": False, "error": "asset_unavailable"}
    message = _mcp_message(
        structured={"result": {"unexpected": "wrapper"}},
        text_payload=payload,
    )
    assert probe._tool_payload(message) == payload


def test_tool_payload_accepts_text_only_compatibility_payload():
    probe = _probe_module()
    payload = {"ok": True, "total": 0, "results": []}
    assert probe._tool_payload(_mcp_message(text_payload=payload)) == payload


def test_tool_payload_preserves_direct_outer_result_fallback():
    probe = _probe_module()
    payload = {"ok": False, "error": "asset_unavailable"}
    assert probe._tool_payload({"result": payload}) == payload


def test_mcp_read_paths_accept_production_nested_fastmcp_responses(monkeypatch):
    probe = _probe_module()
    instance = probe.Probe()
    instance.mcp_token = "operator-token"
    missing = {"ok": False, "error": "asset_unavailable"}
    search = {"ok": True, "total": 0, "results": []}
    responses = {
        "rm_asset_search": search,
        "rm_asset_get": missing,
        "rm_asset_view": missing,
        "rm_asset_inspect": missing,
        "rm_asset_download_link": missing,
    }

    def mcp_tool(name, arguments):
        payload = responses[name]
        return probe.HttpResult(200, {}, b""), _mcp_message(
            structured={"result": payload},
            text_payload=payload,
        )

    monkeypatch.setattr(instance, "mcp_tool", mcp_tool)
    instance.run_mcp_reads()

    for name in (
        "metadata_get",
        "search",
        "download",
    ):
        assert instance.evidence[name]["status"] == "PASS"


def test_mcp_mutation_paths_accept_production_nested_fastmcp_responses(monkeypatch):
    probe = _probe_module()
    instance = probe.Probe()
    instance.mcp_token = "operator-token"
    frozen = {"ok": False, "error": "asset_write_frozen"}

    def mcp_tool(name, arguments):
        return probe.HttpResult(200, {}, b""), _mcp_message(
            structured={"result": frozen},
            text_payload=frozen,
        )

    monkeypatch.setattr(instance, "mcp_tool", mcp_tool)
    assert instance.run_mcp_mutations() is True

    for name in (
        "mcp_upload_rejected",
        "mcp_update_rejected",
        "public_reindex_rejected",
    ):
        assert instance.evidence[name]["status"] == "PASS"


def test_sqlite_fingerprint_reopen_compares_complete_observations():
    probe = _probe_module()
    first = _sqlite(True)
    second = dict(first)
    second["tables"] = ("assets",)
    second["counts"] = {"assets": 0}
    second["row_total"] = 0

    assert probe.sqlite_fingerprint_equal(first, first)
    assert not probe.sqlite_fingerprint_equal(first, second)


def test_probe_has_one_entry_for_every_acceptance_name_and_no_default_pass(
    monkeypatch,
):
    probe = _probe_module()
    snapshot = _snapshot()
    monkeypatch.setattr(probe, "persistent_snapshot", lambda: snapshot)
    instance = probe.Probe()
    checks, evidence = instance.finish()

    assert set(checks) == set(probe.CHECK_NAMES)
    assert all(value is False for value in checks.values())
    assert evidence["status"] == "INCOMPLETE"
    assert evidence["summary"]["side_effects_free"] is True


def test_persistent_snapshot_equal_detects_durable_change():
    probe = _probe_module()
    first = _snapshot()
    second = _snapshot()
    second["rm_db"]["size"] = 1
    assert not probe.persistent_snapshot_equal(first, second)


def test_fresh_process_binding_is_incomplete_without_trusted_external_evidence(monkeypatch):
    probe = _probe_module()
    payload = {
        "process_boot_id": "boot-a",
        "process_started_at": "2026-08-16T10:00:00+00:00",
        "platform_identity": {
            "instance_id": "instance-a",
            "git_commit": "commit-a",
            "service_id": "service-a",
        },
        "runtime_boot_validation_passed": True,
    }
    monkeypatch.delenv("RM_PROBE_TRUSTED_INSTANCE_ID", raising=False)
    monkeypatch.delenv("RM_PROBE_TRUSTED_GIT_COMMIT", raising=False)
    monkeypatch.delenv("RM_PROBE_TRUSTED_SERVICE_ID", raising=False)

    status, _, observed = probe.Probe._fresh_process_binding(payload)

    assert status == "INCOMPLETE"
    assert observed is False


def test_fresh_process_binding_requires_matching_boot_and_deployment(monkeypatch):
    probe = _probe_module()
    payload = {
        "process_boot_id": "boot-a",
        "process_started_at": "2026-08-16T10:00:00+00:00",
        "platform_identity": {
            "instance_id": "instance-a",
            "git_commit": "commit-a",
            "service_id": "service-a",
        },
        "runtime_boot_validation_passed": True,
    }
    monkeypatch.setenv("RM_PROBE_TRUSTED_INSTANCE_ID", "instance-a")
    monkeypatch.setenv("RM_PROBE_TRUSTED_GIT_COMMIT", "commit-a")
    monkeypatch.setenv("RM_PROBE_TRUSTED_SERVICE_ID", "service-a")

    status, _, observed = probe.Probe._fresh_process_binding(payload)

    assert status == "PASS"
    assert observed is True

    monkeypatch.setenv("RM_PROBE_TRUSTED_INSTANCE_ID", "instance-b")
    status, _, observed = probe.Probe._fresh_process_binding(payload)
    assert status == "FAIL"
    assert observed is False


def test_process_boot_id_alone_is_not_fresh_process_binding(monkeypatch):
    probe = _probe_module()
    payload = {
        "process_boot_id": "boot-a",
        "process_started_at": "2026-08-16T10:00:00+00:00",
        "runtime_boot_validation_passed": True,
    }
    monkeypatch.setenv("RM_PROBE_TRUSTED_INSTANCE_ID", "instance-a")
    monkeypatch.setenv("RM_PROBE_TRUSTED_GIT_COMMIT", "commit-a")
    monkeypatch.setenv("RM_PROBE_TRUSTED_SERVICE_ID", "service-a")

    status, _, observed = probe.Probe._fresh_process_binding(payload)

    assert status == "INCOMPLETE"
    assert observed is False


def test_fresh_process_binding_requires_commit_and_service_matches(monkeypatch):
    probe = _probe_module()
    payload = {
        "platform_identity": {
            "instance_id": "instance-a",
            "git_commit": "commit-a",
            "service_id": "service-a",
        }
    }
    monkeypatch.setenv("RM_PROBE_TRUSTED_INSTANCE_ID", "instance-a")
    monkeypatch.setenv("RM_PROBE_TRUSTED_GIT_COMMIT", "commit-b")
    monkeypatch.setenv("RM_PROBE_TRUSTED_SERVICE_ID", "service-a")
    status, _, observed = probe.Probe._fresh_process_binding(payload)
    assert status == "FAIL"
    assert observed is False

    monkeypatch.setenv("RM_PROBE_TRUSTED_GIT_COMMIT", "commit-a")
    monkeypatch.setenv("RM_PROBE_TRUSTED_SERVICE_ID", "service-b")
    status, _, observed = probe.Probe._fresh_process_binding(payload)
    assert status == "FAIL"
    assert observed is False


def test_sqlite_logical_fingerprint_detects_row_mutations_and_ignores_order(tmp_path):
    probe = _probe_module()

    def make_db(path, rows):
        connection = sqlite3.connect(path)
        connection.execute(
            "CREATE TABLE items (id INTEGER PRIMARY KEY, label TEXT, payload BLOB)"
        )
        connection.executemany("INSERT INTO items VALUES (?, ?, ?)", rows)
        connection.commit()
        connection.close()

    rows = [(1, "alpha", b"abc"), (2, "bravo", b"xyz")]
    first_path = tmp_path / "first.sqlite3"
    second_path = tmp_path / "second.sqlite3"
    make_db(first_path, rows)
    make_db(second_path, list(reversed(rows)))
    first = probe.sqlite_observation(first_path)
    second = probe.sqlite_observation(second_path)
    assert first["logical_digest"] == second["logical_digest"]

    connection = sqlite3.connect(first_path)
    connection.execute("UPDATE items SET label = 'alphA' WHERE id = 1")
    connection.commit()
    updated_text = probe.sqlite_observation(first_path)
    assert updated_text["logical_digest"] != first["logical_digest"]

    connection.execute("UPDATE items SET label = 'alpha', payload = ? WHERE id = 1", (b"abd",))
    connection.commit()
    updated_blob = probe.sqlite_observation(first_path)
    assert updated_blob["logical_digest"] != first["logical_digest"]

    connection.execute("INSERT INTO items VALUES (3, 'charlie', ?)", (b"123",))
    connection.commit()
    inserted = probe.sqlite_observation(first_path)
    assert inserted["logical_digest"] != updated_blob["logical_digest"]

    connection.execute("DELETE FROM items WHERE id = 3")
    connection.commit()
    deleted = probe.sqlite_observation(first_path)
    assert deleted["logical_digest"] == updated_blob["logical_digest"]
    connection.close()


def test_persistent_snapshot_complete_rejects_missing_durable_surfaces():
    probe = _probe_module()
    assert probe.persistent_snapshot_complete(_snapshot()) is False


def _ticket_subresults(probe, **overrides):
    values = {
        name: {"status": "PASS"}
        for name in probe.REQUIRED_TICKET_SUBRESULTS
    }
    for name, status in overrides.items():
        values[name] = {"status": status}
    return values


@pytest.mark.parametrize(
    ("override", "expected"),
    [
        ({"ephemeral_probe": "FAIL"}, "FAIL"),
        ({"ephemeral_probe": "INCOMPLETE"}, "INCOMPLETE"),
        ({"durable_mutation_performed": "FAIL"}, "FAIL"),
        ({"durable_mutation_performed": "INCOMPLETE"}, "INCOMPLETE"),
        ({"durable_fingerprint_unchanged": "PASS", "durable_mutation_performed": "FAIL"}, "FAIL"),
    ],
)
def test_ticket_aggregation_is_contradiction_safe(override, expected):
    probe = _probe_module()
    assert probe.ticket_recreation_status(_ticket_subresults(probe, **override)) == expected


def test_ticket_aggregation_requires_missing_top_level_and_mutation_evidence():
    probe = _probe_module()
    values = _ticket_subresults(probe)
    values.pop("ephemeral_probe")
    assert probe.ticket_recreation_status(values) == "INCOMPLETE"
    values = _ticket_subresults(probe)
    values["durable_mutation_performed"] = {"status": "INCOMPLETE", "observed": "false"}
    assert probe.ticket_recreation_status(values) == "INCOMPLETE"


def test_only_explicit_boolean_false_maps_to_pass_for_durable_mutation():
    probe = _probe_module()
    values = _ticket_subresults(probe)
    values["durable_mutation_performed"] = {"status": "PASS", "observed": False}
    assert probe.ticket_recreation_status(values) == "PASS"
    for non_boolean in ("false", 0, "", None):
        values = _ticket_subresults(probe)
        values["durable_mutation_performed"] = {
            "status": "PASS" if non_boolean is False else "INCOMPLETE"
        }
        assert probe.ticket_recreation_status(values) != "PASS"


@pytest.mark.parametrize(
    ("durable_value", "ephemeral_status", "durable_status"),
    [(True, "PASS", "FAIL"), (False, "PASS", "PASS"), ("false", "INCOMPLETE", "INCOMPLETE")],
)
def test_ephemeral_response_requires_explicit_no_durable_mutation(
    durable_value, ephemeral_status, durable_status
):
    probe = _probe_module()
    instance = probe.Probe()
    instance.mcp_token = "operator-token"
    payload = {
        "status": "PASS",
        "upload_ticket_recreated": True,
        "download_ticket_recreated": True,
        "verification_session_recreated": True,
        "ephemeral_cleanup_complete": True,
        "capability_not_exposed": True,
        "durable_mutation_performed": durable_value,
    }
    instance.http = lambda *args, **kwargs: probe.HttpResult(
        200, {}, json.dumps(payload).encode("utf-8")
    )
    instance.run_ephemeral_probe()
    assert instance.sub_evidence["ephemeral_probe"]["status"] == ephemeral_status
    assert instance.sub_evidence["durable_mutation_performed"]["status"] == durable_status
    expected = "FAIL" if durable_status == "FAIL" else "INCOMPLETE"
    assert probe.ticket_recreation_status(instance.sub_evidence) == expected


def test_acceptance_artifact_placeholder_invalidates_previous_pass_and_binds_digest(tmp_path):
    probe = _probe_module()
    checks_path = tmp_path / "rm-acceptance-checks.json"
    evidence_path = tmp_path / "rm-acceptance-evidence.json"
    old = {"status": "PASS", "checks": {"old": True}}
    probe._write_json(checks_path, old, "old-run")
    placeholder_checks, placeholder_evidence = probe._placeholder_artifacts(
        "new-run", "2026-08-17T00:00:00.000000+00:00"
    )
    probe._write_json(evidence_path, placeholder_evidence, "new-run")
    probe._write_json(checks_path, placeholder_checks, "new-run")
    assert checks_path.read_text(encoding="utf-8") != json.dumps(old)
    current = json.loads(checks_path.read_text(encoding="utf-8"))
    assert current["status"] == "INCOMPLETE"
    assert current["acceptance_run_id"] == "new-run"

    evidence = {
        "artifact_type": "rm_acceptance_evidence",
        "schema_version": probe.ACCEPTANCE_ARTIFACT_SCHEMA_VERSION,
        "acceptance_run_id": "new-run",
        "created_at": "2026-08-17T00:00:00.000000+00:00",
        "completed_at": "2026-08-17T00:00:01.000000+00:00",
        "status": "PASS",
    }
    artifact = probe._checks_artifact(
        {name: True for name in probe.CHECK_NAMES},
        evidence,
    )
    assert artifact["evidence_sha256"] == probe._canonical_json_digest(evidence)
    evidence["status"] = "FAIL"
    assert artifact["evidence_sha256"] != probe._canonical_json_digest(evidence)


def test_canonical_output_lock_prevents_cross_run_pairing(tmp_path):
    probe = _probe_module()
    lock_path = tmp_path / "rm-acceptance.lock"
    checks_path = tmp_path / "rm-acceptance-checks.json"
    evidence_path = tmp_path / "rm-acceptance-evidence.json"
    observations = []

    def publish(run_id):
        with probe._CanonicalOutputLock(lock_path):
            probe._write_json(evidence_path, {"run": run_id}, run_id)
            time.sleep(0.01)
            probe._write_json(checks_path, {"run": run_id}, run_id)
            observations.append(
                json.loads(checks_path.read_text())["run"]
                == json.loads(evidence_path.read_text())["run"]
            )

    threads = [threading.Thread(target=publish, args=(f"run-{index}",)) for index in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert observations == [True, True]


def test_tree_fingerprint_rejects_symlink_without_following_external_target(tmp_path):
    probe = _probe_module()
    root = tmp_path / "managed"
    root.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("must not be read", encoding="utf-8")
    link = root / "linked.txt"
    try:
        link.symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation is unavailable")
    observation = probe.tree_observation(root)
    assert observation["present"] is None
    assert observation["error"] == "unsafe_tree_entry"

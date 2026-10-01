import importlib
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import pytest

import remember_me_adapter as adapter_module
from remember_me.core import (
    AssetBlobVerificationResult,
    AssetVerificationCompletion,
    AssetVerificationPage,
    AssetVerificationSnapshot,
    BeginAssetVerificationRequest,
    CompleteAssetVerificationRequest,
    ListAssetVerificationPageRequest,
    RememberMeService,
    VerifyAssetBlobRequest,
)
from remember_me_adapter import (
    EXPECTED_MCP_TOOLS,
    RememberMeAdapter,
    RememberMeAdapterError,
    inspect_remember_me_contract,
    validate_remember_me_contract,
)


from remember_me_dependency import DEPENDENCY

ROOT = Path(__file__).resolve().parent.parent
EXPECTED_VERSION = DEPENDENCY.version
EXPECTED_TAG = DEPENDENCY.tag
EXPECTED_COMMIT = DEPENDENCY.commit
EXPECTED_TREE = DEPENDENCY.tree
EXPECTED_ARCHIVE_SHA256 = DEPENDENCY.sha256
EXPECTED_ARCHIVE_URL = DEPENDENCY.url
OLD_COMMIT = "184e223c6392fd14dd5cfa73227d41f46d90e3c8"


def _requirement_line():
    return next(
        line.strip()
        for line in (ROOT / "requirements.txt").read_text(
            encoding="utf-8"
        ).splitlines()
        if line.strip().startswith("remember-me @")
    )


def test_dependency_is_immutable_release_asset_and_digest_pinned():
    line = _requirement_line()
    integration_text = (ROOT / "docs" / "remember-me-integration.md").read_text(
        encoding="utf-8"
    )

    assert line == "remember-me @ {}#sha256={}".format(
        EXPECTED_ARCHIVE_URL,
        EXPECTED_ARCHIVE_SHA256,
    )
    assert len(EXPECTED_COMMIT) == 40
    assert EXPECTED_VERSION in EXPECTED_ARCHIVE_URL
    assert EXPECTED_TAG in EXPECTED_ARCHIVE_URL
    assert EXPECTED_COMMIT in integration_text
    assert EXPECTED_TREE in integration_text
    assert "git+" not in line
    assert "remember-me[" not in line
    assert "/main" not in line
    assert "/tarball/" not in line
    assert "/archive/refs/" not in line
    assert OLD_COMMIT not in line


def test_adapter_import_has_no_storage_or_protocol_side_effects(tmp_path):
    script = """
import json
import sys
from pathlib import Path
before = sorted(item.name for item in Path.cwd().iterdir())
import remember_me_adapter
after = sorted(item.name for item in Path.cwd().iterdir())
print(json.dumps({
    "before": before,
    "after": after,
    "remember_me_loaded": any(
        name == "remember_me" or name.startswith("remember_me.")
        for name in sys.modules
    ),
    "server_loaded": "server" in sys.modules,
}))
"""
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT)
    env["OMBRE_BUCKETS_DIR"] = str(tmp_path / "must-not-be-read")
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=tmp_path,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    payload = __import__("json").loads(completed.stdout)

    assert payload["before"] == payload["after"]
    assert payload["remember_me_loaded"] is False
    assert payload["server_loaded"] is False
    assert not (tmp_path / "must-not-be-read").exists()
    assert not list(tmp_path.rglob("assets.sqlite3"))


def test_installed_contract_matches_pinned_public_package():
    contract = inspect_remember_me_contract()

    assert validate_remember_me_contract(contract) is contract
    assert contract.mcp_tools == EXPECTED_MCP_TOOLS
    assert "remember_me.standalone" not in sys.modules


def test_public_asset_verification_contract_is_available():
    assert all(
        item is not None
        for item in (
            BeginAssetVerificationRequest,
            ListAssetVerificationPageRequest,
            VerifyAssetBlobRequest,
            CompleteAssetVerificationRequest,
            AssetVerificationSnapshot,
            AssetVerificationPage,
            AssetBlobVerificationResult,
            AssetVerificationCompletion,
        )
    )
    assert all(
        callable(getattr(RememberMeService, method_name, None))
        for method_name in (
            "begin_asset_verification",
            "list_asset_verification_page",
            "verify_asset_blob",
            "complete_asset_verification",
        )
    )


def test_contract_inspection_failure_is_redacted():
    private_value = "D:\\private\\site-packages\\secret"
    with patch.object(
        adapter_module.metadata,
        "distribution",
        side_effect=RuntimeError(private_value),
    ):
        with pytest.raises(RememberMeAdapterError) as captured:
            inspect_remember_me_contract()

    assert str(captured.value) == "remember_me_contract_unavailable"
    assert private_value not in str(captured.value)


@pytest.mark.parametrize(
    ("field", "bad_value"),
    [
        ("distribution_name", "wrong"),
        ("package_version", "0.1.0.dev4"),
        ("data_compatibility", "wrong"),
        ("sanitizer_id", "wrong"),
        ("pillow_range", "Pillow>=10.4,<11"),
        ("mcp_tools", EXPECTED_MCP_TOOLS[:-1]),
        ("mcp_tools", EXPECTED_MCP_TOOLS + ("extra",)),
        ("mcp_tools", tuple(reversed(EXPECTED_MCP_TOOLS))),
    ],
)
def test_contract_mismatches_fail_closed_without_values(field, bad_value):
    contract = inspect_remember_me_contract()

    with pytest.raises(RememberMeAdapterError) as captured:
        validate_remember_me_contract(replace(contract, **{field: bad_value}))

    message = str(captured.value)
    assert message == "remember_me_contract_mismatch:{}".format(field)
    assert str(bad_value) not in message


def test_runtime_is_created_only_explicitly_and_reused_for_same_root(tmp_path):
    instance = RememberMeAdapter()
    root = tmp_path / "runtime"

    assert instance.runtime_created is False
    assert not root.exists()
    runtime = instance.create_runtime(root)

    assert instance.runtime_created is True
    assert (root / "assets.sqlite3").is_file()
    assert instance.create_runtime(root) is runtime

    with pytest.raises(
        RememberMeAdapterError,
        match="^remember_me_runtime_already_created$",
    ):
        instance.create_runtime(tmp_path / "other")


def test_second_adapter_cannot_create_writer_for_same_root(tmp_path):
    root = tmp_path / "runtime"
    first = RememberMeAdapter()
    second = RememberMeAdapter()
    first.create_runtime(root)

    with pytest.raises(
        RememberMeAdapterError,
        match="^remember_me_data_root_already_owned$",
    ):
        second.create_runtime(root)


def test_runtime_requires_explicit_path_without_leaking_value(tmp_path):
    value = str(tmp_path / "private")
    with pytest.raises(RememberMeAdapterError) as captured:
        RememberMeAdapter().create_runtime(value)

    assert str(captured.value) == "remember_me_data_root_must_be_path"
    assert value not in str(captured.value)


def test_adapter_module_does_not_import_standalone_or_server():
    sys.modules.pop("remember_me.standalone", None)
    sys.modules.pop("server", None)
    importlib.reload(adapter_module)

    assert "remember_me.standalone" not in sys.modules
    assert "server" not in sys.modules


@pytest.mark.parametrize("change", [
    lambda t: t.replace("# BEGIN REMEMBER-ME PIN", "# WRONG"),
    lambda t: t + t[t.index("# BEGIN REMEMBER-ME PIN"):t.index("# END REMEMBER-ME PIN") + len("# END REMEMBER-ME PIN")],
    lambda t: t.replace("# tree:", "# unknown:"),
    lambda t: t.replace("# version: 0.1.0", "# version: 0.1.0.dev7"),
    lambda t: t.replace("# tag: v0.1.0", "# tag: v0.1.1"),
    lambda t: t.replace("#sha256=", "#sha256=NOTHEX"),
    lambda t: t + "\nremember_me==0.1.0\n",
    lambda t: t.replace("remember-me @", " remember-me @"),
    lambda t: t.replace("/download/v0.1.0/", "/download/v0.1.1/"),
    lambda t: t.replace("# commit: ", "# commit: A"),
])
def test_fixed_dependency_stanza_rejects_missing_duplicate_or_malformed(change):
    from remember_me_dependency import parse_dependency
    with pytest.raises(ValueError, match="^remember_me_pin_invalid$"):
        parse_dependency(change((ROOT / "requirements.txt").read_text()))


def test_dependency_path_does_not_depend_on_cwd(tmp_path):
    import json
    script = "import json; from remember_me_dependency import DEPENDENCY; print(json.dumps(DEPENDENCY.__dict__))"
    output = subprocess.check_output([sys.executable, "-c", script], cwd=tmp_path, env=os.environ.copy(), text=True)
    assert json.loads(output) == DEPENDENCY.__dict__
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("archive", [
    {"hash": "sha256=" + DEPENDENCY.sha256},
    {"hashes": {"sha256": DEPENDENCY.sha256}},
    {"hash": "sha256=" + DEPENDENCY.sha256, "hashes": {"sha256": DEPENDENCY.sha256}},
])
def test_standard_direct_url_hash_representations(archive):
    import json
    adapter_module._validate_archive_provenance(json.dumps({"url": DEPENDENCY.url, "archive_info": archive}))


@pytest.mark.parametrize("value", [
    None, "{}", "not-json",
    '{"url":"x","url":"y","archive_info":{}}',
    {"url": "file:///local/archive.tar.gz", "archive_info": {"hashes": {"sha256": DEPENDENCY.sha256}}},
    {"url": DEPENDENCY.url, "archive_info": {}},
    {"url": DEPENDENCY.url, "archive_info": {"hashes": {"sha256": "0" * 64}}},
    {"url": DEPENDENCY.url, "archive_info": {"hash": "sha256=" + "0" * 64, "hashes": {"sha256": DEPENDENCY.sha256}}},
    {"url": DEPENDENCY.url, "archive_info": {"hash": None, "hashes": {"sha256": DEPENDENCY.sha256}}},
    {"url": DEPENDENCY.url, "dir_info": {}},
])
def test_wrong_or_conflicting_installed_provenance_rejected(value):
    import json
    raw = json.dumps(value) if isinstance(value, dict) else value
    with pytest.raises(adapter_module.RememberMeAdapterError, match="^remember_me_contract_mismatch:provenance$"):
        adapter_module._validate_archive_provenance(raw)


def test_runtime_cannot_bypass_provenance_even_with_contract_mock(tmp_path, monkeypatch):
    real = adapter_module.metadata.distribution("remember-me")
    class WrongSource:
        def __getattr__(self, name): return getattr(real, name)
        def read_text(self, name): return '{}' if name == "direct_url.json" else real.read_text(name)
    monkeypatch.setattr(adapter_module.metadata, "distribution", lambda _: WrongSource())
    monkeypatch.setattr(adapter_module, "validate_remember_me_contract", lambda: None)
    root = tmp_path / "blocked"
    with pytest.raises(adapter_module.RememberMeAdapterError, match="provenance"):
        RememberMeAdapter().create_runtime(root)
    assert not root.exists()


def test_same_version_shadow_module_is_rejected_before_storage(tmp_path, monkeypatch):
    import remember_me.metadata as rm_metadata
    monkeypatch.setattr(rm_metadata, "__file__", str(tmp_path / "metadata.py"))
    root = tmp_path / "blocked"
    with pytest.raises(adapter_module.RememberMeAdapterError, match="module_source"):
        RememberMeAdapter().create_runtime(root)
    assert not root.exists()


def test_changed_recorded_source_hash_rejected_without_editing_install(monkeypatch):
    import copy
    real = adapter_module.metadata.distribution("remember-me")
    records = [copy.copy(p) for p in real.files]
    record = next(p for p in records if str(p) == "remember_me/metadata.py")
    record.hash = adapter_module.metadata.FileHash("sha256=wrong")
    class ChangedRecord:
        files = records
        def locate_file(self, p): return real.locate_file(p)
    with pytest.raises(adapter_module.RememberMeAdapterError, match="module_source"):
        adapter_module._validate_distribution_modules(ChangedRecord())


def test_reused_runtime_rechecks_provenance(tmp_path, monkeypatch):
    owner = RememberMeAdapter()
    runtime = owner.create_runtime(tmp_path / "runtime")
    def fail(): raise adapter_module.RememberMeAdapterError("remember_me_contract_mismatch:provenance")
    monkeypatch.setattr(adapter_module, "_checked_distribution", fail)
    with pytest.raises(adapter_module.RememberMeAdapterError, match="provenance"):
        owner.create_runtime(tmp_path / "runtime")
    assert owner._runtime is runtime

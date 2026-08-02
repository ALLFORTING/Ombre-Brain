from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

from scripts import backup_v2_key_tool as key_tool


PASSPHRASE = b"synthetic-test-passphrase"


def _disable_host_acl(monkeypatch):
    monkeypatch.setattr(key_tool, "_harden_private_key_acl", lambda path: None)
    monkeypatch.setattr(key_tool, "_verify_private_key_protection", lambda path: None)


def test_generate_requires_new_absolute_directory_outside_repo(tmp_path, monkeypatch):
    _disable_host_acl(monkeypatch)
    existing = tmp_path / "existing"
    existing.mkdir()
    with pytest.raises(key_tool.KeyToolError):
        key_tool.generate(str(existing), passphrase_provider=lambda: (PASSPHRASE, PASSPHRASE), environ={})
    with pytest.raises(key_tool.KeyToolError):
        key_tool.generate("relative", passphrase_provider=lambda: (PASSPHRASE, PASSPHRASE), environ={})
    inside_repo = key_tool.ROOT / "recipient-test-output"
    with pytest.raises(key_tool.KeyToolError):
        key_tool.generate(str(inside_repo), passphrase_provider=lambda: (PASSPHRASE, PASSPHRASE), environ={})


def test_generate_rejects_hosted_runtimes_and_bad_passphrases(tmp_path, monkeypatch):
    _disable_host_acl(monkeypatch)
    for env in ({"GITHUB_ACTIONS": "true"}, {"RENDER": "true"}, {"RENDER_SERVICE_ID": "srv"}):
        with pytest.raises(key_tool.KeyToolError) as error:
            key_tool.generate(
                str(tmp_path / f"out-{len(env)}"),
                passphrase_provider=lambda: (PASSPHRASE, PASSPHRASE),
                environ=env,
            )
        assert error.value.code == "hosted_generation_denied"
    with pytest.raises(key_tool.KeyToolError):
        key_tool.generate(
            str(tmp_path / "mismatch"),
            passphrase_provider=lambda: (PASSPHRASE, b"different-test-passphrase"),
            environ={},
        )
    assert not (tmp_path / "mismatch").exists()


def test_generate_writes_encrypted_pkcs8_public_and_metadata(tmp_path, monkeypatch, capsys):
    _disable_host_acl(monkeypatch)
    output = tmp_path / "keys"
    result = key_tool.generate(
        str(output),
        passphrase_provider=lambda: (PASSPHRASE, PASSPHRASE),
        environ={},
    )
    assert sorted(path.name for path in output.iterdir()) == [
        key_tool.METADATA_NAME,
        key_tool.PRIVATE_NAME,
        key_tool.PUBLIC_NAME,
    ]
    private_bytes = (output / key_tool.PRIVATE_NAME).read_bytes()
    assert b"ENCRYPTED PRIVATE KEY" in private_bytes
    private_key = serialization.load_pem_private_key(private_bytes, password=PASSPHRASE)
    assert isinstance(private_key, X25519PrivateKey)
    public_text = (output / key_tool.PUBLIC_NAME).read_text(encoding="ascii")
    assert public_text.endswith("\n")
    assert len(__import__("base64").b64decode(public_text.strip(), validate=True)) == 32
    metadata = json.loads((output / key_tool.METADATA_NAME).read_text(encoding="utf-8"))
    assert metadata["fingerprint"] == result["fingerprint"]
    assert metadata["private_key_encrypted"] is True
    assert "private_key_pem" not in metadata
    assert PASSPHRASE.decode() not in capsys.readouterr().out


def test_inspect_public_never_opens_private_and_verify_keyset_detects_mismatch(
    tmp_path,
    monkeypatch,
):
    _disable_host_acl(monkeypatch)
    output = tmp_path / "keys"
    generated = key_tool.generate(
        str(output),
        passphrase_provider=lambda: (PASSPHRASE, PASSPHRASE),
        environ={},
    )
    inspected = key_tool.inspect_public(str(output / key_tool.PUBLIC_NAME))
    assert inspected == generated
    verified = key_tool.verify_keyset(
        str(output),
        passphrase_provider=lambda: PASSPHRASE,
    )
    assert verified == generated
    metadata_path = output / key_tool.METADATA_NAME
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["fingerprint"] = "x25519-sha256:" + "0" * 64
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
    with pytest.raises(key_tool.KeyToolError):
        key_tool.verify_keyset(str(output), passphrase_provider=lambda: PASSPHRASE)


def test_existing_files_are_never_overwritten_and_partial_failure_cleans_output(
    tmp_path,
    monkeypatch,
):
    def fail_after_private(path: Path):
        raise key_tool.KeyToolError("private_key_acl_invalid")

    monkeypatch.setattr(key_tool, "_harden_private_key_acl", fail_after_private)
    output = tmp_path / "partial"
    with pytest.raises(key_tool.KeyToolError):
        key_tool.generate(
            str(output),
            passphrase_provider=lambda: (PASSPHRASE, PASSPHRASE),
            environ={},
        )
    assert not output.exists()


def test_interruption_after_directory_creation_cleans_output(tmp_path, monkeypatch):
    output = tmp_path / "interrupted"

    def interrupt():
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        key_tool.generate(str(output), passphrase_provider=interrupt, environ={})
    assert not output.exists()


def _valid_acl():
    return {
        "current_sid": "S-1-5-21-123",
        "owner_sid": "S-1-5-21-123",
        "protected": True,
        "rules": [
            {
                "sid": "S-1-5-21-123",
                "inherited": False,
                "type": 0,
                "has_full_control": True,
                "inheritance_flags": 0,
                "propagation_flags": 0,
            }
        ],
    }


@pytest.mark.parametrize(
    "mutator",
    [
        lambda acl: acl.update(owner_sid="S-1-5-21-999"),
        lambda acl: acl.update(protected=False),
        lambda acl: acl["rules"][0].update(inherited=True),
        lambda acl: acl["rules"][0].update(sid="S-1-5-21-999"),
        lambda acl: acl["rules"][0].update(type=1),
        lambda acl: acl["rules"][0].update(has_full_control=False),
        lambda acl: acl["rules"][0].update(inheritance_flags=1),
        lambda acl: acl["rules"][0].update(propagation_flags=1),
    ],
)
def test_windows_acl_verification_rejects_unsafe_variants(mutator):
    acl = _valid_acl()
    mutator(acl)
    with pytest.raises(key_tool.KeyToolError) as error:
        key_tool._validate_windows_acl(acl)
    assert error.value.code == "private_key_acl_invalid"


def test_windows_acl_verification_uses_sid_data_not_display_names():
    acl = _valid_acl()
    acl["rules"][0]["display_name"] = "Everyone"
    key_tool._validate_windows_acl(acl)


def test_windows_acl_hardening_uses_argument_safe_machine_readable_acl(
    monkeypatch,
    tmp_path,
):
    private_path = tmp_path / "recipient-private-key.pem"
    private_path.write_bytes(b"synthetic")
    monkeypatch.setattr(key_tool.os, "name", "nt")

    calls = []
    acl = json.dumps(_valid_acl())

    def fake_powershell(script, path):
        calls.append((script, path))
        if script == key_tool._ACL_INSPECT_SCRIPT:
            return acl
        assert script == key_tool._ACL_APPLY_SCRIPT
        assert str(private_path) not in script
        return ""

    monkeypatch.setattr(key_tool, "_run_powershell", fake_powershell)
    key_tool._harden_private_key_acl(private_path)
    assert [path for _, path in calls] == [private_path, private_path]


def test_verify_keyset_rechecks_private_key_protection(tmp_path, monkeypatch):
    _disable_host_acl(monkeypatch)
    output = tmp_path / "keys"
    key_tool.generate(
        str(output),
        passphrase_provider=lambda: (PASSPHRASE, PASSPHRASE),
        environ={},
    )
    monkeypatch.setattr(
        key_tool,
        "_verify_private_key_protection",
        lambda path: (_ for _ in ()).throw(
            key_tool.KeyToolError("private_key_acl_invalid")
        ),
    )
    with pytest.raises(key_tool.KeyToolError) as error:
        key_tool.verify_keyset(str(output), passphrase_provider=lambda: PASSPHRASE)
    assert error.value.code == "private_key_acl_invalid"


@pytest.mark.skipif(os.name != "nt", reason="requires Windows ACL facilities")
def test_windows_acl_real_integration_smoke(tmp_path):
    output = tmp_path / "keys"
    generated = key_tool.generate(
        str(output),
        passphrase_provider=lambda: (PASSPHRASE, PASSPHRASE),
        environ={},
    )
    assert key_tool.verify_keyset(
        str(output),
        passphrase_provider=lambda: PASSPHRASE,
    ) == generated

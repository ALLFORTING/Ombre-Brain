from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

from scripts import backup_v2_key_tool as key_tool


PASSPHRASE = b"synthetic-test-passphrase"


def _disable_host_acl(monkeypatch):
    monkeypatch.setattr(key_tool, "_harden_private_key_acl", lambda path: None)


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


def test_windows_acl_hardening_uses_sid_and_fails_closed(monkeypatch, tmp_path):
    private_path = tmp_path / "recipient-private-key.pem"
    private_path.write_bytes(b"synthetic")
    monkeypatch.setattr(key_tool.os, "name", "nt")

    calls = []

    def fake_run(command, capture_output, text, check):
        calls.append(command)
        if command[0] == "powershell":
            return SimpleNamespace(returncode=0, stdout="S-1-5-21-123\n", stderr="")
        if command[:2] == ["icacls", str(private_path)] and "/inheritance:r" in command:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if command[:2] == ["icacls", str(private_path)] and "/grant:r" in command:
            assert "*S-1-5-21-123:F" in command
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if command == ["icacls", str(private_path)]:
            return SimpleNamespace(returncode=0, stdout="*S-1-5-21-123:(F)", stderr="")
        raise AssertionError(command)

    monkeypatch.setattr(key_tool.subprocess, "run", fake_run)
    key_tool._harden_private_key_acl(private_path)
    assert calls[0][0] == "powershell"
    assert calls[1][0] == "icacls"

    def failing_run(command, capture_output, text, check):
        return SimpleNamespace(returncode=1, stdout="", stderr="denied")

    monkeypatch.setattr(key_tool.subprocess, "run", failing_run)
    with pytest.raises(key_tool.KeyToolError):
        key_tool._harden_private_key_acl(private_path)

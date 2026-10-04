from pathlib import Path
import hashlib
import subprocess
import sys

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from offline_backup_bundle import prepare_backup_workspace, capture_bundle
from scripts import backup_v2_recovery as recovery


def synthetic_bundle(tmp_path):
    workspace = prepare_backup_workspace(tmp_path / "workspace")
    key = X25519PrivateKey.generate()
    (workspace.source_root / "synthetic.txt").write_text("synthetic recovery data")
    result = capture_bundle(workspace.root, key.public_key(), ob_commit_sha="1" * 40)
    pem = tmp_path / "encrypted.pem"
    pem.write_bytes(key.private_bytes(serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.BestAvailableEncryption(b"synthetic-passphrase")))
    return workspace, result, pem


def test_encrypted_pem_verify_restore_and_no_overwrite(tmp_path, monkeypatch):
    workspace, bundle, pem = synthetic_bundle(tmp_path)
    before = hashlib.sha256(pem.read_bytes()).hexdigest()
    monkeypatch.setattr(recovery.getpass, "getpass", lambda prompt: "synthetic-passphrase")
    verified = recovery.recover("verify", workspace.root, bundle.bundle_name, pem)
    assert verified["authenticated"] is True
    target = workspace.restored_root / ("2" * 32)
    restored = recovery.recover("restore", workspace.root, bundle.bundle_name, pem, target)
    assert restored["authenticated"] is True
    assert (target / "synthetic.txt").read_text() == "synthetic recovery data"
    with pytest.raises(recovery.RecoveryError, match="restore_target_invalid"):
        recovery.recover("restore", workspace.root, bundle.bundle_name, pem, target)
    assert hashlib.sha256(pem.read_bytes()).hexdigest() == before
    assert list(tmp_path.glob("*.pem")) == [pem]
    assert not list(workspace.temp_root.iterdir())


@pytest.mark.parametrize("kind", ["wrong_password", "plaintext", "no_secure_terminal"])
def test_key_failures_never_publish_restore(tmp_path, monkeypatch, kind):
    workspace, bundle, pem = synthetic_bundle(tmp_path)
    if kind == "plaintext":
        pem.write_bytes(b"-----BEGIN PRIVATE KEY-----\ninvalid synthetic fixture\n")
    def password(prompt):
        if kind == "no_secure_terminal":
            raise recovery.getpass.GetPassWarning()
        return "incorrect"
    monkeypatch.setattr(recovery.getpass, "getpass", password)
    target = workspace.restored_root / ("3" * 32)
    with pytest.raises(recovery.RecoveryError, match="encrypted_private_key_invalid"):
        recovery.recover("restore", workspace.root, bundle.bundle_name, pem, target)
    assert not target.exists()


def test_invalid_target_is_rejected_before_password(tmp_path, monkeypatch):
    workspace, bundle, pem = synthetic_bundle(tmp_path)
    def forbidden(prompt):
        raise AssertionError("password must not be requested")
    monkeypatch.setattr(recovery.getpass, "getpass", forbidden)
    for target in (tmp_path / ("4" * 32), Path("relative"), workspace.restored_root / "other"):
        with pytest.raises(recovery.RecoveryError, match="restore_target_invalid"):
            recovery.recover("restore", workspace.root, bundle.bundle_name, pem, target)


def test_direct_cli_import_path_and_no_passphrase_argument(tmp_path):
    result = subprocess.run([sys.executable, "-I", str(recovery.ROOT / "scripts/backup_v2_recovery.py"),
                             "restore", "--help"], cwd=tmp_path, capture_output=True, text=True)
    assert result.returncode == 0
    assert "--target" in result.stdout
    assert "--passphrase" not in result.stdout

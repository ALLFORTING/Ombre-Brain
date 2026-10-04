"""Offline encrypted-PEM backup-v2 verification and isolated recovery."""
from __future__ import annotations

import argparse
import getpass
import json
from pathlib import Path
import re
import sys
import warnings

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from offline_backup_bundle import (
    BackupBundleError, load_backup_workspace, restore_bundle, verify_bundle,
)


class RecoveryError(RuntimeError):
    pass


def load_encrypted_key(path: Path) -> X25519PrivateKey:
    try:
        if path.is_symlink() or path.stat().st_size > 16384:
            raise ValueError()
        pem = path.read_bytes()
        if not pem.startswith(b"-----BEGIN ENCRYPTED PRIVATE KEY-----"):
            raise ValueError()
        # Refuse getpass's fallback to echoed input when no secure terminal exists.
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            password = getpass.getpass("Private-key passphrase: ").encode("utf-8")
        if not password:
            raise ValueError()
        key = serialization.load_pem_private_key(pem, password=password)
        if not isinstance(key, X25519PrivateKey):
            raise ValueError()
        return key
    except (OSError, ValueError, TypeError, EOFError, getpass.GetPassWarning):
        raise RecoveryError("encrypted_private_key_invalid") from None


def recover(operation: str, workspace_path: Path, bundle_name: str,
            private_key_path: Path, target: Path | None = None) -> dict:
    workspace = load_backup_workspace(workspace_path)
    restore_name = None
    if operation == "restore":
        if target is None or not target.is_absolute():
            raise RecoveryError("restore_target_invalid")
        # Existing core publishes only under workspace/restored with no replacement.
        if (target.exists() or target.is_symlink()
                or target.parent != workspace.restored_root
                or re.fullmatch(r"[0-9a-f]{32}", target.name) is None):
            raise RecoveryError("restore_target_invalid")
        restore_name = target.name
    elif operation != "verify":
        raise RecoveryError("operation_invalid")
    key = load_encrypted_key(private_key_path)
    if operation == "verify":
        return verify_bundle(workspace.root, bundle_name, key)
    return restore_bundle(workspace.root, bundle_name, key, restore_name=restore_name)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="operation", required=True)
    for name in ("verify", "restore"):
        command = sub.add_parser(name)
        command.add_argument("--workspace", required=True, type=Path)
        command.add_argument("--bundle", required=True)
        command.add_argument("--private-key", required=True, type=Path)
        if name == "restore":
            command.add_argument("--target", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        result = recover(args.operation, args.workspace, args.bundle,
                         args.private_key, getattr(args, "target", None))
        print(json.dumps(result, sort_keys=True))
        return 0
    except RecoveryError as exc:
        print(str(exc), file=sys.stderr)
    except BackupBundleError as exc:
        print(exc.status, file=sys.stderr)
    except Exception:
        print("recovery_failed", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

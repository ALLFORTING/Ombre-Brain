"""Offline backup-v2 X25519 recipient key tool."""

from __future__ import annotations

import argparse
import base64
from contextlib import suppress
from datetime import datetime, timezone
import getpass
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
from typing import Callable

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import (
    X25519PrivateKey,
    X25519PublicKey,
)

ROOT = Path(__file__).resolve().parents[1]
PRIVATE_NAME = "recipient-private-key.pem"
PUBLIC_NAME = "recipient-public-key.b64"
METADATA_NAME = "recipient-key-metadata.json"
SCHEMA_VERSION = 1
MIN_PASSPHRASE_LENGTH = 16


class KeyToolError(RuntimeError):
    """Stable non-sensitive key tool failure."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def generate(
    output_dir: str,
    *,
    passphrase_provider: Callable[[], tuple[bytes, bytes]] | None = None,
    environ: dict[str, str] | None = None,
) -> dict[str, str]:
    env = os.environ if environ is None else environ
    _reject_hosted_runtime(env)
    destination = _validate_new_output_dir(output_dir)
    created_dir = False
    try:
        destination.mkdir(mode=0o700)
        created_dir = True
        passphrase, confirmation = (
            passphrase_provider() if passphrase_provider else _prompt_passphrases()
        )
        _validate_passphrases(passphrase, confirmation)
        private_key = X25519PrivateKey.generate()
        public_key = private_key.public_key()
        public_b64 = _public_key_b64(public_key)
        fingerprint = public_key_fingerprint(public_key)
        private_bytes = private_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.BestAvailableEncryption(passphrase),
        )
        metadata = {
            "schema_version": SCHEMA_VERSION,
            "algorithm": "X25519",
            "public_key_encoding": "raw-base64",
            "public_key_b64": public_b64,
            "fingerprint": fingerprint,
            "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "private_key_format": "encrypted-pkcs8-pem",
            "private_key_encrypted": True,
        }
        private_path = destination / PRIVATE_NAME
        public_path = destination / PUBLIC_NAME
        metadata_path = destination / METADATA_NAME
        _exclusive_write(private_path, private_bytes, 0o600)
        _harden_private_key_acl(private_path)
        _exclusive_write(public_path, (public_b64 + "\n").encode("ascii"), 0o644)
        _exclusive_write(
            metadata_path,
            (json.dumps(metadata, indent=2, sort_keys=True) + "\n").encode("utf-8"),
            0o644,
        )
        _fsync_directory(destination)
        if sorted(path.name for path in destination.iterdir()) != [
            METADATA_NAME,
            PRIVATE_NAME,
            PUBLIC_NAME,
        ]:
            raise KeyToolError("key_output_invalid")
        return {
            "algorithm": "X25519",
            "fingerprint": fingerprint,
            "public_key_b64": public_b64,
        }
    except Exception:
        if created_dir:
            shutil.rmtree(destination, ignore_errors=True)
        raise


def inspect_public(public_key: str) -> dict[str, str]:
    key = _load_public_key_b64(Path(public_key))
    return {
        "algorithm": "X25519",
        "public_key_b64": _public_key_b64(key),
        "fingerprint": public_key_fingerprint(key),
    }


def verify_keyset(
    key_dir: str,
    *,
    passphrase_provider: Callable[[], bytes] | None = None,
) -> dict[str, str]:
    root = Path(key_dir)
    private_path = root / PRIVATE_NAME
    public_path = root / PUBLIC_NAME
    metadata_path = root / METADATA_NAME
    public_key = _load_public_key_b64(public_path)
    metadata = _load_metadata(metadata_path)
    passphrase = passphrase_provider() if passphrase_provider else _prompt_passphrase()
    private_key = _load_private_key(private_path, passphrase)
    derived_public = private_key.public_key()
    public_b64 = _public_key_b64(public_key)
    fingerprint = public_key_fingerprint(public_key)
    if _public_key_b64(derived_public) != public_b64:
        raise KeyToolError("keyset_mismatch")
    if (
        metadata.get("algorithm") != "X25519"
        or metadata.get("public_key_encoding") != "raw-base64"
        or metadata.get("public_key_b64") != public_b64
        or metadata.get("fingerprint") != fingerprint
        or metadata.get("private_key_encrypted") is not True
    ):
        raise KeyToolError("keyset_mismatch")
    return {
        "algorithm": "X25519",
        "fingerprint": fingerprint,
        "public_key_b64": public_b64,
    }


def public_key_fingerprint(key: X25519PublicKey) -> str:
    from production_backup_capture import public_key_fingerprint as existing_behavior

    return existing_behavior(key)


def _reject_hosted_runtime(env: dict[str, str]) -> None:
    if env.get("GITHUB_ACTIONS") or env.get("RENDER") or env.get("RENDER_SERVICE_ID"):
        raise KeyToolError("hosted_generation_denied")


def _validate_new_output_dir(value: str) -> Path:
    path = Path(value)
    if not path.is_absolute() or path.exists():
        raise KeyToolError("output_directory_invalid")
    resolved_parent = path.parent.resolve(strict=True)
    candidate = resolved_parent / path.name
    repo_root = ROOT.resolve(strict=True)
    if candidate == repo_root or str(candidate).startswith(str(repo_root) + os.sep):
        raise KeyToolError("output_directory_invalid")
    return candidate


def _prompt_passphrases() -> tuple[bytes, bytes]:
    first = getpass.getpass("Passphrase: ").encode("utf-8")
    second = getpass.getpass("Confirm passphrase: ").encode("utf-8")
    return first, second


def _prompt_passphrase() -> bytes:
    return getpass.getpass("Passphrase: ").encode("utf-8")


def _validate_passphrases(first: bytes, second: bytes) -> None:
    if (
        not first
        or len(first) < MIN_PASSPHRASE_LENGTH
        or first != second
    ):
        raise KeyToolError("passphrase_invalid")


def _exclusive_write(path: Path, data: bytes, mode: int) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    fd = os.open(path, flags, mode)
    try:
        with os.fdopen(fd, "wb") as handle:
            fd = -1
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        if fd != -1:
            os.close(fd)
    with suppress(OSError):
        path.chmod(mode)


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    with suppress(OSError):
        fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def _harden_private_key_acl(path: Path) -> None:
    if os.name != "nt":
        mode = stat.S_IMODE(path.stat().st_mode)
        if mode & 0o077:
            raise KeyToolError("private_key_permissions_invalid")
        return
    sid = _current_user_sid()
    commands = [
        ["icacls", str(path), "/inheritance:r"],
        ["icacls", str(path), "/grant:r", f"*{sid}:F"],
    ]
    for command in commands:
        result = subprocess.run(command, capture_output=True, text=True, check=False)
        if result.returncode != 0:
            raise KeyToolError("private_key_acl_invalid")
    verify = subprocess.run(
        ["icacls", str(path)],
        capture_output=True,
        text=True,
        check=False,
    )
    if verify.returncode != 0 or f"*{sid}:" not in verify.stdout:
        raise KeyToolError("private_key_acl_invalid")


def _current_user_sid() -> str:
    command = [
        "powershell",
        "-NoProfile",
        "-Command",
        "[System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value",
    ]
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    sid = result.stdout.strip()
    if result.returncode != 0 or not sid.startswith("S-1-"):
        raise KeyToolError("private_key_acl_invalid")
    return sid


def _load_public_key_b64(path: Path) -> X25519PublicKey:
    try:
        text = path.read_text(encoding="ascii")
    except OSError as exc:
        raise KeyToolError("public_key_invalid") from exc
    if not text.endswith("\n") or text.strip() != text[:-1] or "\n" in text[:-1]:
        raise KeyToolError("public_key_invalid")
    try:
        raw = base64.b64decode(text[:-1], validate=True)
        if len(raw) != 32 or base64.b64encode(raw).decode("ascii") != text[:-1]:
            raise ValueError
        return X25519PublicKey.from_public_bytes(raw)
    except (TypeError, ValueError) as exc:
        raise KeyToolError("public_key_invalid") from exc


def _public_key_b64(key: X25519PublicKey) -> str:
    raw = key.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return base64.b64encode(raw).decode("ascii")


def _load_private_key(path: Path, passphrase: bytes) -> X25519PrivateKey:
    try:
        data = path.read_bytes()
        key = serialization.load_pem_private_key(data, password=passphrase)
    except (OSError, TypeError, ValueError) as exc:
        raise KeyToolError("private_key_invalid") from exc
    if not isinstance(key, X25519PrivateKey):
        raise KeyToolError("private_key_invalid")
    return key


def _load_metadata(path: Path) -> dict[str, object]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as exc:
        raise KeyToolError("metadata_invalid") from exc
    if not isinstance(data, dict) or _metadata_contains_private_material(data):
        raise KeyToolError("metadata_invalid")
    return data


def _metadata_contains_private_material(value: object) -> bool:
    allowed_private_fields = {"private_key_format", "private_key_encrypted"}
    forbidden_keys = {
        "passphrase",
        "password",
        "secret",
        "private_key",
        "private_key_bytes",
        "private_key_pem",
    }
    forbidden_text = ("begin private key", "passphrase", "password", "secret")
    if isinstance(value, dict):
        for key, item in value.items():
            normalized = str(key).casefold()
            if normalized in forbidden_keys and normalized not in allowed_private_fields:
                return True
            if normalized not in allowed_private_fields and "private" in normalized:
                return True
            if _metadata_contains_private_material(item):
                return True
        return False
    if isinstance(value, list):
        return any(_metadata_contains_private_material(item) for item in value)
    if isinstance(value, str):
        lowered = value.casefold()
        return any(marker in lowered for marker in forbidden_text)
    return False


def _print_result(result: dict[str, str]) -> None:
    print(json.dumps(result, sort_keys=True))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Offline backup-v2 X25519 key tool")
    subcommands = parser.add_subparsers(dest="command", required=True)
    generate_parser = subcommands.add_parser("generate")
    generate_parser.add_argument("--output-dir", required=True)
    inspect_parser = subcommands.add_parser("inspect-public")
    inspect_parser.add_argument("--public-key", required=True)
    verify_parser = subcommands.add_parser("verify-keyset")
    verify_parser.add_argument("--key-dir", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "generate":
            _print_result(generate(args.output_dir))
        elif args.command == "inspect-public":
            _print_result(inspect_public(args.public_key))
        elif args.command == "verify-keyset":
            _print_result(verify_keyset(args.key_dir))
        return 0
    except KeyToolError as exc:
        print(exc.code, file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

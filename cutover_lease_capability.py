"""Small, filesystem-safe capability file for the RM cutover lease.

The file is deliberately limited to the lease id and plaintext token needed to
rehydrate the active lease.  It is operator capability material, never
evidence.  Callers must keep the returned token in memory only.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import tempfile
from typing import Any


CAPABILITY_DIRNAME = "operator"
CAPABILITY_FILENAME = "lease-token.json"


class LeaseCapabilityError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class LeaseCapability:
    lease_id: str
    token: str


def capability_path(state_root: str | Path) -> Path:
    root = _absolute(state_root, "state_root_invalid")
    return root / CAPABILITY_DIRNAME / CAPABILITY_FILENAME


def _absolute(value: str | Path, code: str) -> Path:
    try:
        path = Path(value).expanduser()
    except (TypeError, ValueError, OSError) as exc:
        raise LeaseCapabilityError(code) from exc
    if not path.is_absolute():
        raise LeaseCapabilityError(code)
    return path.resolve(strict=False)


def _safe_target(path: str | Path, state_root: str | Path) -> Path:
    root = _absolute(state_root, "state_root_invalid")
    target = _absolute(path, "capability_path_invalid")
    expected = capability_path(root)
    if target != expected:
        raise LeaseCapabilityError("capability_path_invalid")
    for component in (root, root / CAPABILITY_DIRNAME):
        try:
            if component.is_symlink():
                raise LeaseCapabilityError("capability_path_unsafe")
        except OSError as exc:
            raise LeaseCapabilityError("capability_path_unsafe") from exc
    if target.exists() and target.is_symlink():
        raise LeaseCapabilityError("capability_path_unsafe")
    return target


def _json(capability: LeaseCapability) -> bytes:
    payload = {"lease_id": capability.lease_id, "token": capability.token}
    return (json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def read_capability(path: str | Path, *, state_root: str | Path) -> LeaseCapability:
    target = _safe_target(path, state_root)
    if not target.is_file():
        raise LeaseCapabilityError("capability_missing")
    try:
        raw = target.read_bytes()
        value: Any = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LeaseCapabilityError("capability_invalid") from exc
    if not isinstance(value, dict) or set(value) != {"lease_id", "token"}:
        raise LeaseCapabilityError("capability_invalid")
    lease_id = value.get("lease_id")
    token = value.get("token")
    if not isinstance(lease_id, str) or not lease_id or not isinstance(token, str) or not token:
        raise LeaseCapabilityError("capability_invalid")
    return LeaseCapability(lease_id=lease_id, token=token)


def write_capability(path: str | Path, capability: LeaseCapability, *, state_root: str | Path) -> None:
    target = _safe_target(path, state_root)
    if not isinstance(capability, LeaseCapability) or not capability.lease_id or not capability.token:
        raise LeaseCapabilityError("capability_invalid")
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        raise LeaseCapabilityError("capability_exists")
    raw_path: str | None = None
    fd: int | None = None
    try:
        fd, raw_path = tempfile.mkstemp(prefix=".lease-", dir=target.parent)
        os.chmod(raw_path, 0o600)
        with os.fdopen(fd, "wb") as stream:
            fd = None
            stream.write(_json(capability))
            stream.flush()
            os.fsync(stream.fileno())
        # Hard-link publication is atomic and does not replace an existing
        # target.  The temporary name is removed after publication.
        os.link(raw_path, target)
        os.chmod(target, 0o600)
    except FileExistsError as exc:
        raise LeaseCapabilityError("capability_exists") from exc
    except (OSError, TypeError, ValueError) as exc:
        raise LeaseCapabilityError("capability_write_failed") from exc
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        if raw_path is not None:
            try:
                os.unlink(raw_path)
            except OSError:
                pass


def remove_capability(path: str | Path, *, state_root: str | Path) -> bool:
    target = _safe_target(path, state_root)
    if not target.exists():
        return False
    if not target.is_file():
        raise LeaseCapabilityError("capability_path_unsafe")
    try:
        target.unlink()
    except OSError as exc:
        raise LeaseCapabilityError("capability_remove_failed") from exc
    return True


__all__ = [
    "CAPABILITY_DIRNAME",
    "CAPABILITY_FILENAME",
    "LeaseCapability",
    "LeaseCapabilityError",
    "capability_path",
    "read_capability",
    "remove_capability",
    "write_capability",
]

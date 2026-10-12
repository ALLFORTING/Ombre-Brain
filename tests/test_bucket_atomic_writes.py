"""create/update/touch/set_dormant/_time_ripple/archive publish bucket files atomically."""
import importlib
import os
import stat
import sys
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock

import pytest


@pytest.fixture
def manager(tmp_path, monkeypatch):
    monkeypatch.setenv("OMBRE_BUCKETS_DIR", str(tmp_path / "buckets"))
    monkeypatch.delenv("OMBRE_API_KEY", raising=False)
    sys.modules.pop("server", None)
    module = importlib.import_module("server")
    module.decay_engine.ensure_started = AsyncMock(return_value=None)
    return module.bucket_mgr


def _fail_replace(monkeypatch):
    def fail(*args, **kwargs):
        raise OSError("injected replace failure")
    monkeypatch.setattr("bucket_manager.os.replace", fail)


def _no_temporaries(path):
    return not [p for p in Path(path).parent.iterdir() if p.name.endswith(".tmp")]


@pytest.mark.asyncio
async def test_create_failure_publishes_nothing(manager, tmp_path, monkeypatch):
    _fail_replace(monkeypatch)
    with pytest.raises(OSError):
        await manager.create("atomic create body")
    assert not [p for p in (tmp_path / "buckets").rglob("*.md")]


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["update", "touch", "set_dormant", "time_ripple", "archive"])
async def test_failed_rewrite_keeps_original_bytes(manager, monkeypatch, operation):
    bucket_id = await manager.create("atomic original body")
    path = manager._find_bucket_file(bucket_id)
    before = Path(path).read_bytes()
    with monkeypatch.context() as scoped:
        _fail_replace(scoped)
        if operation == "update":
            assert await manager.update(bucket_id, importance=9) is False
        elif operation == "touch":
            with pytest.raises(OSError):
                await manager.touch(bucket_id)
        elif operation == "set_dormant":
            assert await manager.set_dormant(bucket_id, True) is False
        elif operation == "time_ripple":
            with pytest.raises(OSError):
                await manager._time_ripple("other", datetime.now())
        else:
            assert await manager.archive(bucket_id) is False
    assert Path(path).read_bytes() == before
    assert _no_temporaries(path)


@pytest.fixture
def controlled_umask():
    if os.name == "nt":
        pytest.skip("POSIX permission semantics")
    original = os.umask(0o027)
    try:
        yield
    finally:
        os.umask(original)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", [0o644, 0o640])
@pytest.mark.parametrize("operation", ["update", "touch"])
async def test_rewrite_preserves_permissions(manager, controlled_umask, mode, operation):
    bucket_id = await manager.create("permission original")
    path = manager._find_bucket_file(bucket_id)
    os.chmod(path, mode)
    if operation == "update":
        assert await manager.update(bucket_id, importance=9)
    else:
        await manager.touch(bucket_id)
    assert stat.S_IMODE(os.stat(path).st_mode) == mode


@pytest.mark.asyncio
@pytest.mark.parametrize("umask", [0o022, 0o027, 0o077])
async def test_create_permissions_follow_umask(manager, controlled_umask, umask):
    os.umask(umask)
    bucket_id = await manager.create("permission new bucket")
    path = manager._find_bucket_file(bucket_id)
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o666 & ~umask
    assert os.umask(umask) == umask


def test_root_rewrite_preserves_owner_before_mode(tmp_path, monkeypatch, controlled_umask):
    from bucket_manager import BucketManager
    path = tmp_path / "owned.md"
    path.write_bytes(b"original")
    os.chmod(path, 0o640)
    original = path.stat()
    calls = []
    real_chmod = os.fchmod
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setattr(os, "fchown", lambda fd, uid, gid: calls.append(("owner", uid, gid)))
    def chmod(fd, mode):
        calls.append(("mode", mode))
        real_chmod(fd, mode)
    monkeypatch.setattr(os, "fchmod", chmod)
    BucketManager._write_bytes_atomic(str(path), b"replacement")
    assert calls == [("owner", original.st_uid, original.st_gid), ("mode", 0o640)]
    assert path.read_bytes() == b"replacement"


def test_nonroot_rewrite_does_not_chown(tmp_path, monkeypatch, controlled_umask):
    from bucket_manager import BucketManager
    path = tmp_path / "owned.md"
    path.write_bytes(b"original")
    monkeypatch.setattr(os, "geteuid", lambda: 1000)
    def forbidden(*args):
        pytest.fail("non-root writer must not chown")
    monkeypatch.setattr(os, "fchown", forbidden)
    BucketManager._write_bytes_atomic(str(path), b"replacement")
    assert path.read_bytes() == b"replacement"


@pytest.mark.parametrize("metadata_call", ["fchmod", "fchown"])
def test_metadata_failure_keeps_original(tmp_path, monkeypatch, controlled_umask, metadata_call):
    from bucket_manager import BucketManager
    path = tmp_path / "original.md"
    path.write_bytes(b"original")
    os.chmod(path, 0o640)
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    def fail(*args):
        raise OSError("injected metadata failure")
    monkeypatch.setattr(os, metadata_call, fail)
    with pytest.raises(OSError, match="injected metadata failure"):
        BucketManager._write_bytes_atomic(str(path), b"replacement")
    assert path.read_bytes() == b"original"
    assert stat.S_IMODE(path.stat().st_mode) == 0o640
    assert _no_temporaries(path)

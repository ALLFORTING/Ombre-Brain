"""create/update/touch/set_dormant/_time_ripple/archive publish bucket files atomically."""
import importlib
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

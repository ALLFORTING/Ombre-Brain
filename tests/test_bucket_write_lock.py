"""Mutex lifecycle and real writer interleaving tests, using isolated storage."""
import ast
import asyncio
from concurrent.futures import ThreadPoolExecutor, TimeoutError
import importlib
from pathlib import Path
import select
import sqlite3
import subprocess
import sys
import threading

import frontmatter
import pytest

from bucket_manager import BucketManager
from bucket_write_lock import BucketWriteLockError, bucket_write_scope, initialize_bucket_write_lock


@pytest.mark.parametrize("failure", ["body", "begin", "rollback", "close"])
def test_scope_exception_always_closes_and_releases(tmp_path, monkeypatch, failure):
    initialize_bucket_write_lock(tmp_path)
    real_connect = sqlite3.connect
    closed = []
    class Connection:
        def __init__(self, *args, **kwargs):
            self.inner = real_connect(*args, **kwargs)
        @property
        def in_transaction(self):
            return self.inner.in_transaction
        def execute(self, statement):
            if failure == "begin":
                raise sqlite3.OperationalError("injected acquire")
            return self.inner.execute(statement)
        def rollback(self):
            if failure == "rollback":
                raise RuntimeError("injected rollback")
            self.inner.rollback()
        def close(self):
            closed.append(True)
            if failure == "close" and len(closed) == 1:
                raise RuntimeError("injected close")
            self.inner.close()
    with monkeypatch.context() as scoped:
        scoped.setattr("bucket_write_lock.sqlite3.connect", Connection)
        with pytest.raises((RuntimeError, BucketWriteLockError)):
            with bucket_write_scope(tmp_path):
                if failure == "body":
                    raise RuntimeError("injected body")
    assert closed
    with bucket_write_scope(tmp_path, timeout=0.1):
        pass


def test_initialize_commit_exception_closes_connection(tmp_path, monkeypatch):
    real_connect = sqlite3.connect
    closed = []
    class Connection:
        def __init__(self, *a, **kw): self.inner = real_connect(*a, **kw)
        def execute(self, *a): return self.inner.execute(*a)
        @property
        def in_transaction(self): return self.inner.in_transaction
        def commit(self): raise RuntimeError("commit failed")
        def rollback(self): self.inner.rollback()
        def close(self): self.inner.close(); closed.append(True)
    with monkeypatch.context() as scoped:
        scoped.setattr("bucket_write_lock.sqlite3.connect", Connection)
        with pytest.raises(RuntimeError, match="commit failed"):
            initialize_bucket_write_lock(tmp_path)
    assert closed
    initialize_bucket_write_lock(tmp_path)
    with bucket_write_scope(tmp_path): pass


def test_nested_scope_and_thread_timeout_are_fail_closed(tmp_path):
    initialize_bucket_write_lock(tmp_path)
    with ThreadPoolExecutor() as pool:
        with bucket_write_scope(tmp_path):
            with bucket_write_scope(tmp_path): pass
            def attempt():
                with bucket_write_scope(tmp_path, timeout=0.05):
                    pytest.fail("must not acquire")
            with pytest.raises(BucketWriteLockError, match="timeout"):
                pool.submit(attempt).result(timeout=2)
        pool.submit(lambda: _acquire(tmp_path)).result(timeout=2)


def _acquire(root):
    with bucket_write_scope(root, timeout=0.5): pass


@pytest.mark.asyncio
@pytest.mark.parametrize("writer", ["update", "touch", "set_dormant", "refresh_tg_summary", "archive"])
async def test_all_full_frontmatter_writers_wait_for_completion(test_config, writer):
    manager = BucketManager(test_config)
    bucket = await manager.create("body", todos=["task"])
    identity = (await manager.get(bucket))["metadata"]["todo_provenance"][0]["id"]
    other = BucketManager(test_config)
    entered, release, attempting = threading.Event(), threading.Event(), threading.Event()
    def authorize(_):
        entered.set()
        assert release.wait(5)
        return True
    def rewrite():
        attempting.set()
        if writer == "update": return asyncio.run(other.update(bucket, tags=["changed"]))
        if writer == "touch": return asyncio.run(other.touch(bucket, ripple_ids=set()))
        if writer == "set_dormant": return asyncio.run(other.set_dormant(bucket))
        if writer == "archive": return asyncio.run(other.archive(bucket))
        import hashlib
        return asyncio.run(other.refresh_tg_summary(bucket, "summary", hashlib.sha256(b"body").hexdigest()))
    with ThreadPoolExecutor(max_workers=2) as pool:
        completion = pool.submit(manager.complete_todo, bucket, identity, authorize)
        assert entered.wait(5)
        mutation = pool.submit(rewrite)
        assert attempting.wait(5)
        with pytest.raises(TimeoutError): mutation.result(timeout=0.05)
        release.set()
        result = completion.result(timeout=5)
        mutation.result(timeout=5)
    assert other.preview_todo_completion(bucket, identity)["target"]["done_at"] == result["done_at"]


@pytest.mark.asyncio
async def test_process_writer_cannot_enter_between_revalidation_and_commit(test_config):
    manager = BucketManager(test_config)
    bucket = await manager.create("body", todos=["task"])
    identity = (await manager.get(bucket))["metadata"]["todo_provenance"][0]["id"]
    script = r"""
import asyncio, sys
from bucket_manager import BucketManager
m = BucketManager({'buckets_dir': sys.argv[1]})
bucket = sys.argv[2]
old = asyncio.run(m.get(bucket))['metadata']['todo_provenance']
print('ready', flush=True)
sys.stdin.readline()
print('attempting', flush=True)
asyncio.run(m.update(bucket, todos=['task'], todo_provenance=old))
print(m.preview_todo_completion(bucket, old[0]['id'])['target']['done_at'], flush=True)
"""
    child = subprocess.Popen([sys.executable, "-u", "-c", script, test_config["buckets_dir"], bucket],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    entered, release = threading.Event(), threading.Event()
    try:
        assert child.stdout.readline().strip() == "ready"
        def authorize(_): entered.set(); return release.wait(5)
        with ThreadPoolExecutor() as pool:
            completion = pool.submit(manager.complete_todo, bucket, identity, authorize)
            assert entered.wait(5)
            child.stdin.write("go\n"); child.stdin.flush()
            assert child.stdout.readline().strip() == "attempting"
            assert not select.select([child.stdout], [], [], 0.1)[0]
            release.set()
            result = completion.result(timeout=5)
        assert child.stdout.readline().strip() == result["done_at"]
        assert child.wait(timeout=5) == 0, child.stderr.read()
    finally:
        release.set()
        if child.poll() is None: child.kill()
        child.communicate(timeout=5)


def test_managed_mutex_blocks_have_no_await():
    root = Path(__file__).resolve().parents[1]
    for name in ["bucket_manager.py", "add_timestamps.py", "reclassify_api.py", "reclassify_domains.py", "migrate_to_domains.py"]:
        tree = ast.parse((root / name).read_text())
        scopes = [node for node in ast.walk(tree) if isinstance(node, ast.With)
                  and any(isinstance(item.context_expr, ast.Call)
                          and getattr(item.context_expr.func, "id", None) == "bucket_write_scope" for item in node.items)]
        assert scopes
        for scope in scopes:
            assert not any(isinstance(node, ast.Await) for node in ast.walk(scope)), (name, scope.lineno)


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["add_timestamps", "migrate_to_domains", "reclassify_domains"])
async def test_standalone_writers_preserve_completed_history(test_config, monkeypatch, name):
    manager = BucketManager(test_config)
    bucket = await manager.create("吃饭 外卖 奶茶", todos=["task"])
    identity = (await manager.get(bucket))["metadata"]["todo_provenance"][0]["id"]
    result = manager.complete_todo(bucket, identity, lambda _: True)
    module = importlib.import_module(name)
    root = Path(test_config["buckets_dir"])
    if name == "add_timestamps":
        monkeypatch.setattr(module, "BUCKETS_DIR", str(root))
        post = frontmatter.load((await manager.get(bucket))["path"])
        post.metadata.pop("created_at"); post.metadata.pop("updated_at")
        Path((await manager.get(bucket))["path"]).write_text(frontmatter.dumps(post))
        module.main()
    else:
        monkeypatch.setattr(module, "DYNAMIC_DIR", str(root / "dynamic"))
        if name == "migrate_to_domains":
            path = Path((await manager.get(bucket))["path"])
            path.rename(root / "dynamic" / path.name)
            module.migrate()
        else:
            module.reclassify()
    assert manager.preview_todo_completion(bucket, identity)["target"]["done_at"] == result["done_at"]


def test_sqlite_timeout_releases_failed_acquisition(tmp_path):
    initialize_bucket_write_lock(tmp_path)
    external = sqlite3.connect(str(tmp_path / ".bucket-write.lock"), isolation_level=None)
    try:
        external.execute("BEGIN IMMEDIATE")
        with pytest.raises(BucketWriteLockError, match="timeout"):
            with bucket_write_scope(tmp_path, timeout=0.05):
                pytest.fail("must not fall back to unlocked execution")
    finally:
        external.rollback()
        external.close()
    with bucket_write_scope(tmp_path, timeout=0.05):
        pass


@pytest.mark.asyncio
async def test_concurrent_completions_commit_only_once(test_config):
    manager = BucketManager(test_config)
    bucket = await manager.create("body", todos=["task"])
    identity = (await manager.get(bucket))["metadata"]["todo_provenance"][0]["id"]
    second = BucketManager(test_config)
    consumed = []
    def authorize(_):
        consumed.append(True)
        return True
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = [future.result(timeout=5) for future in [
            pool.submit(manager.complete_todo, bucket, identity, authorize),
            pool.submit(second.complete_todo, bucket, identity, authorize)]]
    assert sorted(r["status"] for r in results) == ["already_completed", "completed"]
    assert results[0]["done_at"] == results[1]["done_at"]
    assert consumed == [True]


@pytest.mark.asyncio
async def test_api_writer_rereads_after_await(test_config, monkeypatch):
    from types import SimpleNamespace
    import utils
    manager = BucketManager(test_config)
    bucket = await manager.create("body", todos=["task"])
    identity = (await manager.get(bucket))["metadata"]["todo_provenance"][0]["id"]
    module = importlib.import_module("reclassify_api")
    root = Path(test_config["buckets_dir"])
    monkeypatch.setattr(module, "DATA_DIR", str(root / "dynamic"))
    monkeypatch.setattr(module, "UNCLASS_DIR", str(root / "dynamic" / "未分类"))
    monkeypatch.setattr(utils, "load_config", lambda: test_config)
    completion = {}
    async def analyze(**_):
        completion.update(manager.complete_todo(bucket, identity, lambda _: True))
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
            content='{"domain":["学习"],"tags":["changed"],"suggested_name":"new name"}'))])
    monkeypatch.setattr(module, "AsyncOpenAI", lambda **_: SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=analyze))))
    await module.reclassify()
    assert completion["status"] == "completed"
    assert manager.preview_todo_completion(bucket, identity)["target"]["done_at"] == completion["done_at"]
    assert (await manager.get(bucket))["metadata"]["tags"] == ["changed"]

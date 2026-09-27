"""W-9 forward invariants, using only isolated synthetic bucket storage."""

import ast
import asyncio
import os
from pathlib import Path
import select
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from unittest.mock import Mock

import frontmatter
import pytest

from bucket_manager import BucketManager, SupersessionError
from bucket_write_lock import BucketWriteLockError


@pytest.fixture
def server(tmp_path, monkeypatch):
    from tests.test_phase2_superseded_by import _load_server
    monkeypatch.delenv("OMBRE_DIGEST_API_KEY", raising=False)
    return _load_server(tmp_path, monkeypatch)


def raw_bucket(manager, identity, *, filename=None, **metadata):
    path = Path(manager.dynamic_dir) / "synthetic" / (filename or f"{identity}.md")
    path.parent.mkdir(parents=True, exist_ok=True)
    post = frontmatter.Post("synthetic body", id=identity, type="dynamic", **metadata)
    path.write_text(frontmatter.dumps(post), encoding="utf-8")
    return path


def corrupt(manager, identity, **fields):
    path = Path(manager._find_bucket_file(identity))
    post = frontmatter.load(path)
    post.metadata.update(fields)
    path.write_text(frontmatter.dumps(post), encoding="utf-8")
    return path


def bucket_bytes(manager):
    return {str(path.relative_to(manager.base_dir)): path.read_bytes()
            for directory in (manager.dynamic_dir, manager.permanent_dir,
                              manager.archive_dir, manager.feel_dir)
            for path in Path(directory).rglob("*.md")}


async def make(manager, count):
    return [await manager.create(f"body {index}") for index in range(count)]


def token(preview):
    assert "merge preview" in preview, preview
    return preview.split("confirm_token:", 1)[1].strip()


@pytest.mark.asyncio
async def test_normal_supersession_preserves_forward_reverse_and_timestamp(server, monkeypatch):
    a, b = await make(server.bucket_mgr, 2)
    class Clock(datetime):
        tick = datetime(2026, 9, 27, 1)
        @classmethod
        def now(cls, tz=None):
            return cls.tick
    monkeypatch.setattr(server, "datetime", Clock)
    assert f"superseded_by={b}" in await server.trace(a, superseded_by=b)
    first = (await server.bucket_mgr.get(a))["metadata"]["superseded_at"]
    Clock.tick += timedelta(seconds=1)
    await server.trace(a, superseded_by=b)
    metadata = (await server.bucket_mgr.get(a))["metadata"]
    assert metadata["superseded_at"] != first
    assert metadata["superseded_by"] == b
    assert (await server.bucket_mgr.get(b))["metadata"]["supersedes"] == [a]
    assert server.bucket_mgr.get_history(a) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("entry", ["trace", "direct"])
async def test_self_loop_is_rejected_without_bucket_writes(server, entry):
    a = (await make(server.bucket_mgr, 1))[0]
    for historical in (False, True):
        if historical:
            corrupt(server.bucket_mgr, a, superseded_by=a)
        before = bucket_bytes(server.bucket_mgr)
        if entry == "trace":
            assert "不能指向自身" in await server.trace(a, superseded_by=a)
        else:
            with pytest.raises(SupersessionError, match="supersession_self_loop"):
                await server.bucket_mgr.update(a, superseded_by=a)
        assert bucket_bytes(server.bucket_mgr) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [2, 3, 7])
async def test_two_node_and_long_cycles_are_rejected(server, size):
    ids = await make(server.bucket_mgr, size)
    for source, target in zip(ids, ids[1:]):
        await server.trace(source, superseded_by=target)
    before = bucket_bytes(server.bucket_mgr)
    result = await server.trace(ids[-1], superseded_by=ids[0], content="must not replace")
    assert "supersession_cycle" in result
    assert bucket_bytes(server.bucket_mgr) == before


@pytest.mark.asyncio
async def test_direct_update_cannot_bypass_cycle_guard(server):
    a, b = await make(server.bucket_mgr, 2)
    await server.bucket_mgr.update(b, superseded_by=a)
    before = bucket_bytes(server.bucket_mgr)
    with pytest.raises(SupersessionError, match="supersession_cycle"):
        await server.bucket_mgr.update(a, superseded_by=b, _supersession_reverse=False)
    assert bucket_bytes(server.bucket_mgr) == before
    assert "supersedes" not in (await server.bucket_mgr.get(a))["metadata"]


@pytest.mark.asyncio
async def test_durable_update_without_marker_cannot_bypass_cycle_guard(server):
    a, b = await make(server.bucket_mgr, 2)
    await server.bucket_mgr.update(b, superseded_by=a)
    before = bucket_bytes(server.bucket_mgr)
    with pytest.raises(SupersessionError, match="supersession_cycle"):
        await server.bucket_mgr.apply_import_operation("w9:cycle", operation_kind="update",
            target_bucket_id=a, payload={"kwargs": {"superseded_by": b}})
    assert bucket_bytes(server.bucket_mgr) == before
    assert not server.bucket_mgr.inspect_import_operation("w9:cycle")["marker"]


@pytest.mark.asyncio
async def test_durable_marker_replay_does_not_republish_forward(server):
    a, b = await make(server.bucket_mgr, 2)
    args = dict(operation_kind="update", target_bucket_id=a,
                payload={"kwargs": {"superseded_by": b, "superseded_at": "fixed"}})
    await server.bucket_mgr.apply_import_operation("w9:replay", **args)
    corrupt(server.bucket_mgr, b, superseded_by=a)
    before = bucket_bytes(server.bucket_mgr)
    await server.bucket_mgr.apply_import_operation("w9:replay", **args)
    assert bucket_bytes(server.bucket_mgr) == before


@pytest.mark.asyncio
async def test_forward_guard_runs_before_content_history_and_marker_publish(server, monkeypatch):
    a, b = await make(server.bucket_mgr, 2)
    await server.bucket_mgr.update(b, superseded_by=a)
    history = Mock(side_effect=AssertionError("history must not precede guard"))
    monkeypatch.setattr(server.bucket_mgr, "record_history", history)
    with pytest.raises(SupersessionError):
        await server.bucket_mgr.apply_import_operation("w9:history", operation_kind="update",
            target_bucket_id=a, payload={"kwargs": {"content": "unsafe", "superseded_by": b}})
    history.assert_not_called()
    assert not server.bucket_mgr.inspect_import_operation("w9:history")["marker"]


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["missing", "bad_yaml", "no_header", "bom_header", "io"])
@pytest.mark.parametrize("immediate", [False, True])
async def test_missing_and_unreadable_successor_chain_fail_closed(server, monkeypatch, fault, immediate):
    a, b, c = await make(server.bucket_mgr, 3)
    await server.bucket_mgr.update(b, superseded_by=c)
    victim = b if immediate else c
    path = Path(server.bucket_mgr._find_bucket_file(victim))
    if fault == "missing":
        path.unlink()
    elif fault == "bad_yaml":
        path.write_text("---\nid: [\n---\nbody", encoding="utf-8")
    elif fault == "no_header":
        path.write_text("body without metadata", encoding="utf-8")
    elif fault == "bom_header":
        path.write_text("\ufeff" + path.read_text(encoding="utf-8"), encoding="utf-8")
    else:
        original = Path.read_text
        def deny(self, *args, **kwargs):
            if self == path:
                raise OSError("synthetic unreadable bucket")
            return original(self, *args, **kwargs)
        monkeypatch.setattr(Path, "read_text", deny)
    before = bucket_bytes(server.bucket_mgr)
    with pytest.raises(SupersessionError):
        await server.bucket_mgr.update(a, superseded_by=b)
    assert bucket_bytes(server.bucket_mgr) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [False, 0, 17, [], ["missing"], {"target": "missing"}])
async def test_malformed_forward_is_not_treated_as_terminal(server, value):
    a, b = await make(server.bucket_mgr, 2)
    corrupt(server.bucket_mgr, b, superseded_by=value)
    before = bucket_bytes(server.bucket_mgr)
    with pytest.raises(SupersessionError, match="supersession_malformed_forward"):
        await server.bucket_mgr.update(a, superseded_by=b)
    assert bucket_bytes(server.bucket_mgr) == before


@pytest.mark.asyncio
async def test_duplicate_forward_key_is_rejected(server):
    a, b = await make(server.bucket_mgr, 2)
    path = Path(server.bucket_mgr._find_bucket_file(b))
    path.write_text(f"---\nid: {b}\nsuperseded_by: none\nsuperseded_by: {a}\n---\nbody", encoding="utf-8")
    before = bucket_bytes(server.bucket_mgr)
    with pytest.raises(SupersessionError, match="supersession_bucket_unreadable"):
        await server.bucket_mgr.update(a, superseded_by=b)
    assert bucket_bytes(server.bucket_mgr) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("position", ["source", "target", "intermediate"])
@pytest.mark.parametrize("fault", ["duplicate", "conflict", "fallback_alias", "directory"])
async def test_duplicate_conflicting_and_ambiguous_identity_fail_closed(server, position, fault):
    a, b, c = await make(server.bucket_mgr, 3)
    await server.bucket_mgr.update(b, superseded_by=c)
    identity = {"source": a, "target": b, "intermediate": c}[position]
    if fault == "duplicate":
        raw_bucket(server.bucket_mgr, identity, filename=f"duplicate_{identity}.md")
    elif fault == "conflict":
        raw_bucket(server.bucket_mgr, identity, filename="different-identity.md")
    elif fault == "fallback_alias":
        path = raw_bucket(server.bucket_mgr, identity, filename=f"legacy_{identity}.md")
        post = frontmatter.load(path)
        post.metadata.pop("id")
        path.write_text(frontmatter.dumps(post), encoding="utf-8")
    else:
        real = Path(server.bucket_mgr.archive_dir)
        real.rmdir()
        real.symlink_to(server.bucket_mgr.dynamic_dir, target_is_directory=True)
    before = bucket_bytes(server.bucket_mgr)
    with pytest.raises(SupersessionError):
        await server.bucket_mgr.update(a, superseded_by=b)
    assert bucket_bytes(server.bucket_mgr) == before


@pytest.mark.asyncio
async def test_legacy_non_hex_identity_remains_supported(bucket_mgr):
    raw_bucket(bucket_mgr, "old-fact", filename="title_old-fact.md")
    raw_bucket(bucket_mgr, "新事实")
    assert await bucket_mgr.update("old-fact", superseded_by="新事实")
    fallback = raw_bucket(bucket_mgr, "legacy")
    post = frontmatter.load(fallback)
    post.metadata.pop("id")
    fallback.write_text(frontmatter.dumps(post), encoding="utf-8")
    assert await bucket_mgr.update("legacy", superseded_by="old-fact")


@pytest.mark.asyncio
async def test_only_forward_edges_participate_in_cycle_detection(server):
    a, b, c = await make(server.bucket_mgr, 3)
    corrupt(server.bucket_mgr, b, supersedes=[a, b, c], related_buckets=f"{a},{c}")
    corrupt(server.bucket_mgr, c, supersedes=[b], related_buckets=b)
    assert await server.bucket_mgr.update(a, superseded_by=b)
    corrupt(server.bucket_mgr, b, superseded_by=c, supersedes=[])
    corrupt(server.bucket_mgr, c, superseded_by=a, related_buckets="", supersedes=[])
    # A's already existing edge is exempt; a new source entering this cycle is not.
    d = await server.bucket_mgr.create("outside")
    with pytest.raises(SupersessionError, match="supersession_cycle"):
        await server.bucket_mgr.update(d, superseded_by=b)


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [1, 2, 4])
async def test_clear_breaks_historical_self_and_multi_node_cycles(server, size):
    ids = await make(server.bucket_mgr, size)
    for index, identity in enumerate(ids):
        corrupt(server.bucket_mgr, identity, superseded_by=ids[(index + 1) % size],
                supersedes=[ids[index - 1]], superseded_at="historical")
    result = await server.trace(ids[0], superseded_by="")
    assert "superseded_by=" in result
    metadata = (await server.bucket_mgr.get(ids[0]))["metadata"]
    assert "superseded_by" not in metadata and "superseded_at" not in metadata
    successor = (await server.bucket_mgr.get(ids[1 % size]))["metadata"]
    assert ids[0] not in successor.get("supersedes", [])


@pytest.mark.asyncio
async def test_none_breaks_edge_and_preserves_obsolete_timestamp_semantics(server):
    a, b = await make(server.bucket_mgr, 2)
    corrupt(server.bucket_mgr, a, superseded_by=b, superseded_at="historical", supersedes=[b])
    corrupt(server.bucket_mgr, b, superseded_by=a, supersedes=[a])
    assert "superseded_by=none" in await server.trace(a, superseded_by="none")
    metadata = (await server.bucket_mgr.get(a))["metadata"]
    assert metadata["superseded_by"] == "none"
    assert metadata["superseded_at"] and metadata["superseded_at"] != "historical"
    assert a not in (await server.bucket_mgr.get(b))["metadata"]["supersedes"]
    before = bucket_bytes(server.bucket_mgr)
    await server.trace(a)
    await server.trace(a, superseded_by=None)
    assert bucket_bytes(server.bucket_mgr) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("value", ["", "none"])
@pytest.mark.parametrize("fault", ["missing", "unreadable", "malformed", "ambiguous"])
async def test_clear_is_not_blocked_by_downstream_corruption(server, value, fault):
    a, b, c = await make(server.bucket_mgr, 3)
    await server.trace(a, superseded_by=b)
    await server.trace(b, superseded_by=c)
    if fault == "missing":
        Path(server.bucket_mgr._find_bucket_file(b)).unlink()
    elif fault == "unreadable":
        Path(server.bucket_mgr._find_bucket_file(b)).write_text("---\nbroken: [\n---", encoding="utf-8")
    elif fault == "ambiguous":
        raw_bucket(server.bucket_mgr, b, filename=f"duplicate_{b}.md")
    else:
        corrupt(server.bucket_mgr, b, superseded_by=[c])
    assert "superseded_by=" in await server.trace(a, superseded_by=value)
    metadata = (await server.bucket_mgr.get(a))["metadata"]
    assert metadata.get("superseded_by") == ("none" if value else None)


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["healthy", "cycle", "missing_downstream", "malformed_downstream",
                                      "missing_immediate", "duplicate_immediate", "unreadable_immediate"])
async def test_same_edge_preserves_legal_and_historical_graph_behavior(server, fault):
    a, b = await make(server.bucket_mgr, 2)
    await server.trace(a, superseded_by=b)
    corrupt(server.bucket_mgr, a, superseded_at="historical")
    if fault == "cycle":
        corrupt(server.bucket_mgr, b, superseded_by=a)
    elif fault == "missing_downstream":
        corrupt(server.bucket_mgr, b, superseded_by="missing")
    elif fault == "malformed_downstream":
        corrupt(server.bucket_mgr, b, superseded_by={"target": "missing"})
    elif fault == "duplicate_immediate":
        raw_bucket(server.bucket_mgr, b, filename=f"duplicate_{b}.md")
    elif fault == "missing_immediate":
        Path(server.bucket_mgr._find_bucket_file(b)).unlink()
    elif fault == "unreadable_immediate":
        Path(server.bucket_mgr._find_bucket_file(b)).write_text("---\nbroken: [\n---", encoding="utf-8")
    before = bucket_bytes(server.bucket_mgr)
    result = await server.trace(a, superseded_by=b)
    if fault.endswith("immediate"):
        assert bucket_bytes(server.bucket_mgr) == before
        assert "superseded_by=" not in result
    else:
        assert f"superseded_by={b}" in result
        assert (await server.bucket_mgr.get(a))["metadata"]["superseded_at"] != "historical"


@pytest.mark.asyncio
async def test_changed_edge_cannot_use_same_edge_exemption(server, monkeypatch):
    manager = server.bucket_mgr
    a, b, c = await make(manager, 3)
    await manager.update(a, superseded_by=b)
    manager.preflight_supersession(a, b)
    async def cleanup(_):
        await manager.update(a, superseded_by=c)
        await manager.update(b, superseded_by=a)
    monkeypatch.setattr(manager, "_delete_ordinary_embedding", cleanup)
    with pytest.raises(SupersessionError, match="supersession_cycle"):
        await manager.update(a, superseded_by=b, sealed=1)
    assert (await manager.get(a))["metadata"]["superseded_by"] == c
    assert (await manager.get(a))["metadata"]["sealed"] == 0


@pytest.mark.asyncio
async def test_identity_catalog_is_reloaded_inside_publish_lock(server, monkeypatch):
    manager = server.bucket_mgr
    a, b = await make(manager, 2)
    manager.preflight_supersession(a, b)
    current = {}
    async def cleanup(_):
        raw_bucket(manager, b, filename=f"duplicate_{b}.md")
        current.update(bucket_bytes(manager))
    monkeypatch.setattr(manager, "_delete_ordinary_embedding", cleanup)
    with pytest.raises(SupersessionError, match="supersession_duplicate_identity"):
        await manager.update(a, superseded_by=b, sealed=1)
    assert bucket_bytes(manager) == current


@pytest.mark.asyncio
async def test_unrelated_historical_cycle_is_not_repaired_or_globally_rejected(server):
    a, b, x, y, z = await make(server.bucket_mgr, 5)
    corrupt(server.bucket_mgr, x, superseded_by=y)
    corrupt(server.bucket_mgr, y, superseded_by=x)
    corrupt(server.bucket_mgr, z, superseded_by=["malformed-unrelated"])
    before = {i: Path(server.bucket_mgr._find_bucket_file(i)).read_bytes() for i in (x, y, z)}
    assert f"superseded_by={b}" in await server.trace(a, superseded_by=b)
    assert before == {i: Path(server.bucket_mgr._find_bucket_file(i)).read_bytes() for i in (x, y, z)}


WORKER = r'''
import asyncio, os, sys
from bucket_manager import BucketManager, SupersessionError
root, source, target, mode = sys.argv[1:]
if mode == 'direct':
    manager = BucketManager({'buckets_dir': root})
else:
    os.environ['OMBRE_BUCKETS_DIR'] = root
    os.environ.pop('OMBRE_API_KEY', None)
    import server
    manager = server.bucket_mgr
    snapshot = asyncio.run(manager.get(source))
print('ready', flush=True)
sys.stdin.readline()
print('attempting', flush=True)
try:
    if mode == 'direct':
        asyncio.run(manager.update(source, superseded_by=target))
        result = 'allow'
    elif mode == 'trace':
        result = asyncio.run(server.trace(source, superseded_by=target))
    else:
        ok, result = asyncio.run(server._apply_supersession(snapshot, target))
        assert ok, result
    print(result, flush=True)
except SupersessionError as exc:
    print(exc.code, flush=True)
'''


def child(manager, source, target, mode="direct"):
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    process = subprocess.Popen([sys.executable, "-u", "-c", WORKER,
        str(manager.base_dir), source, target, mode], stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
    assert process.stdout.readline().strip() == "ready"
    return process


def start(process):
    process.stdin.write("go\n")
    process.stdin.flush()


def finish(process):
    output, error = process.communicate(timeout=20)
    assert process.returncode == 0, error
    return output


def stop(process):
    if process.poll() is None:
        process.kill()
    process.communicate(timeout=5)


@pytest.mark.asyncio
async def test_opposing_process_writers_cannot_jointly_create_cycle(bucket_mgr):
    a, b = await make(bucket_mgr, 2)
    processes = [child(bucket_mgr, a, b), child(bucket_mgr, b, a)]
    try:
        for process in processes:
            start(process)
        outputs = [finish(process) for process in processes]
        assert sum("supersession_cycle" in result for result in outputs) == 1
        assert sum("allow" in result for result in outputs) == 1
        edges = [(await bucket_mgr.get(i))["metadata"].get("superseded_by") for i in (a, b)]
        assert edges in ([b, None], [None, a])
    finally:
        for process in processes:
            stop(process)


@pytest.mark.asyncio
async def test_process_cannot_enter_between_final_validation_and_publish(bucket_mgr, monkeypatch):
    a, b = await make(bucket_mgr, 2)
    process = child(bucket_mgr, b, a)
    entered, release = threading.Event(), threading.Event()
    original = bucket_mgr._validate_supersession_forward_locked
    def pause(*args, **kwargs):
        result = original(*args, **kwargs)
        entered.set()
        assert release.wait(5)
        return result
    monkeypatch.setattr(bucket_mgr, "_validate_supersession_forward_locked", pause)
    try:
        with ThreadPoolExecutor() as pool:
            future = pool.submit(asyncio.run, bucket_mgr.update(a, superseded_by=b))
            assert entered.wait(5)
            start(process)
            assert process.stdout.readline().strip() == "attempting"
            assert not select.select([process.stdout], [], [], 0.15)[0]
            release.set()
            assert future.result(timeout=10)
        assert "supersession_cycle" in finish(process)
    finally:
        release.set()
        stop(process)


@pytest.mark.asyncio
async def test_lock_failure_never_falls_back_to_unlocked_publish(bucket_mgr, monkeypatch):
    from contextlib import contextmanager
    a, b = await make(bucket_mgr, 2)
    before = bucket_bytes(bucket_mgr)
    @contextmanager
    def unavailable(_):
        raise BucketWriteLockError("synthetic lock failure")
        yield
    monkeypatch.setattr("bucket_manager.bucket_write_scope", unavailable)
    with pytest.raises(BucketWriteLockError):
        await bucket_mgr.update(a, superseded_by=b)
    assert bucket_bytes(bucket_mgr) == before


@pytest.mark.asyncio
async def test_concurrent_trace_adds_preserve_shared_successor_reverse_ids(server):
    a, b, target = await make(server.bucket_mgr, 3)
    processes = [child(server.bucket_mgr, i, target, "trace") for i in (a, b)]
    try:
        for process in processes:
            start(process)
        assert all("superseded_by=" in finish(process) for process in processes)
        assert set((await server.bucket_mgr.get(target))["metadata"]["supersedes"]) == {a, b}
    finally:
        for process in processes:
            stop(process)


@pytest.mark.asyncio
async def test_concurrent_trace_relinks_use_current_source_and_reverse_state(server):
    a, old, b, c = await make(server.bucket_mgr, 4)
    await server.trace(a, superseded_by=old)
    # Both workers retain the same old server snapshot before either publishes.
    processes = [child(server.bucket_mgr, a, target, "stale_apply") for target in (b, c)]
    try:
        for process in processes:
            start(process)
        for process in processes:
            finish(process)
        final = (await server.bucket_mgr.get(a))["metadata"]["superseded_by"]
        for target in (old, b, c):
            reverse = (await server.bucket_mgr.get(target))["metadata"].get("supersedes", [])
            assert (a in reverse) == (target == final)
    finally:
        for process in processes:
            stop(process)


@pytest.mark.asyncio
async def test_failed_pair_compensation_cannot_recreate_unsafe_forward(server, monkeypatch):
    a, b = await make(server.bucket_mgr, 2)
    corrupt(server.bucket_mgr, a, superseded_by=b, superseded_at="old", supersedes=[b])
    corrupt(server.bucket_mgr, b, superseded_by=a, supersedes=[a])
    original = server.bucket_mgr._write_post_atomic
    fail_path = server.bucket_mgr._find_bucket_file(b)
    def fail(path, post):
        if path == fail_path:
            raise OSError("synthetic reverse failure")
        return original(path, post)
    monkeypatch.setattr(server.bucket_mgr, "_write_post_atomic", fail)
    assert "修改失败" in await server.trace(a, superseded_by="")
    assert "superseded_by" not in (await server.bucket_mgr.get(a))["metadata"]


@pytest.mark.asyncio
async def test_lifecycle_compensation_cannot_recreate_unsafe_forward(server, monkeypatch):
    a, b, c = await make(server.bucket_mgr, 3)
    corrupt(server.bucket_mgr, a, superseded_by=b)
    corrupt(server.bucket_mgr, b, superseded_by=a)
    monkeypatch.setattr(server.bucket_mgr, "_move_bucket", Mock(side_effect=OSError("move failed")))
    assert not await server.bucket_mgr.update(a, superseded_by=c, permanent=1)
    assert (await server.bucket_mgr.get(a))["metadata"]["superseded_by"] == c


@pytest.mark.asyncio
@pytest.mark.parametrize("extra_inbound", [False, True])
async def test_merge_preview_rejects_complete_indirect_cycle_plan(server, extra_inbound):
    t, x, i, s, other = await make(server.bucket_mgr, 5)
    for a, b in ((t, x), (x, i), (i, s)):
        await server.trace(a, superseded_by=b)
    if extra_inbound:
        await server.trace(other, superseded_by=s)
    before = bucket_bytes(server.bucket_mgr)
    result = await server.trace(t, merge=s)
    assert "supersession_cycle" in result and "confirm_token" not in result
    assert bucket_bytes(server.bucket_mgr) == before
    assert server.bucket_mgr.read_merge_operations() == []


@pytest.mark.asyncio
async def test_merge_valid_plan_preserves_r7_reverse_steps_and_source_hash(server):
    t, s, i, successor = await make(server.bucket_mgr, 4)
    await server.trace(s, superseded_by=successor)
    await server.trace(i, superseded_by=s)
    preview = await server.trace(t, merge=s)
    result = await server.trace(t, merge=s, confirm_token=token(preview))
    assert "已合并" in result, result
    assert await server.bucket_mgr.get(s) is None
    assert (await server.bucket_mgr.get(i))["metadata"]["superseded_by"] == t
    assert i in (await server.bucket_mgr.get(t))["metadata"]["supersedes"]
    assert s not in (await server.bucket_mgr.get(successor))["metadata"]["supersedes"]


async def merge_fixture(server):
    t, x, i, s = await make(server.bucket_mgr, 4)
    await server.trace(t, superseded_by=x)
    await server.trace(i, superseded_by=s)
    return t, x, i, s


@pytest.mark.asyncio
async def test_merge_publish_rechecks_changes_after_preflight(server, monkeypatch):
    t, x, i, s = await merge_fixture(server)
    preview = await server.trace(t, merge=s)
    original = server.bucket_mgr.validate_supersession_rewire
    def change(*args, **kwargs):
        result = original(*args, **kwargs)
        if not kwargs.get("planning"):
            corrupt(server.bucket_mgr, x, superseded_by=i)
        return result
    monkeypatch.setattr(server.bucket_mgr, "validate_supersession_rewire", change)
    result = await server.trace(t, merge=s, confirm_token=token(preview))
    assert "partial failure" in result and "supersession_cycle" in result
    assert (await server.bucket_mgr.get(i))["metadata"]["superseded_by"] == s
    assert (await server.bucket_mgr.get(t))["content"].count("body 3") == 1


@pytest.mark.asyncio
async def test_merge_resume_rejects_changed_chain_with_diagnostic_code(server, monkeypatch):
    t, x, i, s = await merge_fixture(server)
    preview = await server.trace(t, merge=s)
    original = server.bucket_mgr.apply_import_operation
    async def pause(key, **kwargs):
        if kwargs.get("target_bucket_id") == i:
            raise RuntimeError("synthetic pause before incoming publish")
        return await original(key, **kwargs)
    monkeypatch.setattr(server.bucket_mgr, "apply_import_operation", pause)
    failed = await server.trace(t, merge=s, confirm_token=token(preview))
    assert "partial failure" in failed
    monkeypatch.setattr(server.bucket_mgr, "apply_import_operation", original)
    await server.bucket_mgr.update(x, superseded_by=i)
    before = bucket_bytes(server.bucket_mgr)
    operation = server.bucket_mgr.read_merge_operations()[0]
    completed = list(operation["completed"])
    # Reinitialize the manager to exercise durable resume, not a cached graph.
    server.bucket_mgr = BucketManager(server.config)
    result = await server.trace(t, merge=s)
    assert "supersession_cycle" in result and operation["operation_id"] in result
    assert bucket_bytes(server.bucket_mgr) == before
    assert server.bucket_mgr.read_merge_operations()[0]["completed"] == completed
    assert (await server.bucket_mgr.get(t))["content"].count("body 3") == 1
    await server.bucket_mgr.update(x, superseded_by=None)
    assert "已合并" in await server.trace(t, merge=s)
    assert (await server.bucket_mgr.get(t))["content"].count("body 3") == 1


@pytest.mark.asyncio
async def test_merge_marker_replay_preserves_r7_no_body_reappend(server, monkeypatch):
    t, x, i, s = await merge_fixture(server)
    preview = await server.trace(t, merge=s)
    original = server.bucket_mgr.write_merge_operation
    failed = False
    def lose_progress(operation_id, plan, **kwargs):
        nonlocal failed
        if not failed and any(step.startswith("relation:") for step in kwargs.get("completed", [])):
            failed = True
            raise OSError("synthetic progress loss after marker")
        return original(operation_id, plan, **kwargs)
    monkeypatch.setattr(server.bucket_mgr, "write_merge_operation", lose_progress)
    assert "partial failure" in await server.trace(t, merge=s, confirm_token=token(preview))
    server.bucket_mgr = BucketManager(server.config)
    assert "已合并" in await server.trace(t, merge=s)
    assert (await server.bucket_mgr.get(t))["content"].count("body 3") == 1
    assert await server.bucket_mgr.get(s) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["sealed", "archived", "dormant"])
@pytest.mark.parametrize("cycle", [False, True])
async def test_supersession_guard_does_not_filter_sealed_archive_or_dormant_chain_nodes(server, state, cycle):
    a, b, c = await make(server.bucket_mgr, 3)
    await server.bucket_mgr.update(b, superseded_by=c)
    if cycle:
        await server.bucket_mgr.update(c, superseded_by=a)
    if state == "archived":
        await server.bucket_mgr.archive(c)
    else:
        await server.bucket_mgr.update(c, **{state: 1})
    corrupt(server.bucket_mgr, c, name="SECRET-DOWNSTREAM-NAME")
    result = await server.trace(a, superseded_by=b)
    assert "SECRET-DOWNSTREAM-NAME" not in result
    assert ("supersession_cycle" in result) == cycle
    if not cycle:
        assert f"superseded_by={b}" in result


def test_w9_helpers_and_publish_scopes_never_await_under_lock():
    root = Path(__file__).resolve().parents[1]
    tree = ast.parse((root / "bucket_manager.py").read_text(encoding="utf-8"))
    names = {"_supersession_catalog_locked", "_resolve_supersession_locked", "_successor_id_strict",
             "_validate_supersession_forward_locked", "_plan_supersession_reverse_locked",
             "preflight_supersession", "validate_supersession_rewire"}
    functions = {node.name: node for node in ast.walk(tree)
                 if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
    for name in names:
        assert isinstance(functions[name], ast.FunctionDef)
        assert not any(isinstance(node, ast.Await) for node in ast.walk(functions[name]))
    for node in ast.walk(tree):
        if isinstance(node, ast.With) and any(isinstance(item.context_expr, ast.Call)
                and getattr(item.context_expr.func, "id", None) == "bucket_write_scope" for item in node.items):
            assert not any(isinstance(child_node, ast.Await) for child_node in ast.walk(node))

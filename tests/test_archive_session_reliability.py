"""W-10/S-3: isolated durable evidence, real process exits and competing writers."""
import asyncio
import importlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from unittest.mock import AsyncMock

import frontmatter
import pytest

import archive_session_operations as operations
from bucket_manager import BucketManager
from embedding_engine import EmbeddingEngine


ARGS = dict(summary="session body", highlights="highlight", mood="calm",
            valence=.7, arousal=.4, letter="handoff", topics=["project/OB"])


def load(root, monkeypatch, *, enabled=False):
    for key in ("OMBRE_API_KEY", "OMBRE_EMBEDDING_API_KEY", "OMBRE_DIGEST_API_KEY",
                "OMBRE_RM_RUNTIME_ENABLED", "OMBRE_RM_DATA_ROOT", "OMBRE_HOOK_URL",
                "OMBRE_MCP_STATELESS_HTTP"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("OMBRE_BUCKETS_DIR", str(root))
    sys.modules.pop("server", None)
    server = importlib.import_module("server")
    server.config["embedding"] = {"independent": True, "api_key": "", "enabled": False,
                                 "model": "test-model"}
    server.decay_engine.ensure_started = AsyncMock(return_value=None)
    engine = server.bucket_mgr.embedding_engine
    engine.enabled = enabled
    engine._generate_embedding = AsyncMock(return_value=[.1, .2])
    return server


@pytest.fixture
def ob(tmp_path, monkeypatch):
    return load(tmp_path / "buckets", monkeypatch)


def store(ob):
    return operations.ArchiveSessionOperations(ob.config["buckets_dir"])


def counts(ob):
    root = Path(ob.config["buckets_dir"])
    with sqlite3.connect(root / "bucket_history.sqlite3") as conn:
        return (len(list((root / "archive").rglob("*.md"))),
                conn.execute("SELECT COUNT(*) FROM boot_delta_events").fetchone()[0],
                conn.execute("SELECT COUNT(*) FROM letters").fetchone()[0],
                len(ob._load_emotion_timeline()))


def fingerprint(ob):
    root = Path(ob.config["buckets_dir"])
    return {str(p.relative_to(root)): p.read_bytes()
            for p in root.rglob("*") if p.is_file() and p.name != ".bucket-write.lock"}


@pytest.mark.asyncio
async def test_same_id_replay_and_different_ids_same_content(ob):
    first = await ob.archive_session(**ARGS, operation_id="stable")
    assert "已归档" in first
    before = fingerprint(ob)
    assert await ob.archive_session(**ARGS, operation_id="stable") == first
    assert fingerprint(ob) == before
    second = await ob.archive_session(**ARGS, operation_id="different")
    assert second != first and counts(ob) == (2, 2, 2, 2)
    assert store(ob).lookup("stable")["status"] == "completed"


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", [
    {"summary": "changed"}, {"highlights": "changed"}, {"mood": "changed"},
    {"valence": .8}, {"arousal": .8}, {"valence": -1}, {"arousal": -1},
    {"letter": "changed"}, {"sealed": True}, {"topics": ["different"]},
])
async def test_every_relevant_changed_parameter_conflicts_without_writes(ob, changed):
    await ob.archive_session(**ARGS, operation_id="stable")
    before = fingerprint(ob)
    assert "archive_operation_payload_conflict" in await ob.archive_session(
        **(ARGS | changed), operation_id="stable")
    assert fingerprint(ob) == before


@pytest.mark.asyncio
async def test_canonical_equivalence_and_sentinel_difference(ob):
    args = ARGS | {"summary": " session body ", "topics": [" project/OB ", "project/OB", ""]}
    first = await ob.archive_session(**args, operation_id="canonical")
    assert await ob.archive_session(**ARGS, operation_id="canonical") == first
    await ob.archive_session(summary="other", operation_id="sentinel")
    assert "conflict" in await ob.archive_session(
        summary="other", valence=.5, arousal=.3, operation_id="sentinel")


@pytest.mark.asyncio
@pytest.mark.parametrize("identity", ["", " ", " x", "x ", "x/y", "中文", "x"*129, "\n", 3])
async def test_invalid_id_and_payload_are_cold_without_schema(tmp_path, monkeypatch, identity):
    root = tmp_path / "not-created"
    monkeypatch.setenv("OMBRE_BUCKETS_DIR", str(root))
    sys.modules.pop("server", None)
    server = importlib.import_module("server")
    assert server._runtime_components is None
    assert "invalid" in await server.archive_session("body", operation_id=identity)
    assert server._runtime_components is None and not root.exists()
    assert "不能为空" in await server.archive_session(" ", operation_id="valid")
    assert not root.exists()


@pytest.mark.asyncio
async def test_legacy_no_journal_and_direct_publish(ob):
    first = await ob.archive_session(**ARGS)
    second = await ob.archive_session(**ARGS)
    assert first != second and counts(ob) == (2, 2, 2, 2)
    with sqlite3.connect(Path(ob.config["buckets_dir"]) / "bucket_history.sqlite3") as conn:
        assert conn.execute("SELECT 1 FROM sqlite_master WHERE name=?", (operations.TABLE,)).fetchone() is None
    assert not list((Path(ob.config["buckets_dir"]) / "dynamic").rglob("*.md"))


@pytest.mark.asyncio
async def test_midnight_restart_and_cold_completed_replay(ob, monkeypatch):
    monkeypatch.setattr(operations, "archive_now", lambda: "2026-09-27T23:59:59")
    def fail(self, boundary, operation):
        if boundary == "after_publish":
            raise RuntimeError("private error must not appear in diagnostics")
    monkeypatch.setattr(operations.ArchiveSessionOperations, "checkpoint", fail)
    assert "storage_failed" in await ob.archive_session(**ARGS, operation_id="midnight")
    frozen = store(ob).lookup("midnight")["plan"]
    monkeypatch.setattr(operations.ArchiveSessionOperations, "checkpoint", lambda *args: None)
    monkeypatch.setattr(operations, "archive_now", lambda: "2026-09-28T00:01:00")
    restarted = load(Path(ob.config["buckets_dir"]), monkeypatch)
    result = await restarted.archive_session(**ARGS, operation_id="midnight")
    assert result == frozen["result_text"]
    assert store(restarted).lookup("midnight")["plan"] == frozen
    assert restarted._load_emotion_timeline()[0]["timestamp"] == "2026-09-27T23:59:59"
    root = Path(ob.config["buckets_dir"])
    before = fingerprint(restarted)
    sys.modules.pop("server", None)
    cold = importlib.import_module("server")
    assert cold._runtime_components is None
    assert await cold.archive_session(**ARGS, operation_id="midnight") == result
    assert "conflict" in await cold.archive_session(**(ARGS | {"summary": "changed"}), operation_id="midnight")
    assert cold._runtime_components is None and fingerprint(restarted) == before
    (root / frozen["relative_path"]).unlink()
    assert await cold.archive_session(**ARGS, operation_id="midnight") == result
    assert not (root / frozen["relative_path"]).exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("damage", ["truncated", "marker", "identity", "body", "import_marker"])
async def test_conflicting_file_is_blocked_never_overwritten(ob, monkeypatch, damage):
    def pause(self, boundary, operation):
        if boundary == "after_publish": raise RuntimeError("pause")
    monkeypatch.setattr(operations.ArchiveSessionOperations, "checkpoint", pause)
    await ob.archive_session(**ARGS, operation_id="file")
    plan = store(ob).lookup("file")["plan"]
    path = Path(ob.config["buckets_dir"]) / plan["relative_path"]
    post = frontmatter.load(path)
    if damage == "truncated":
        path.write_text("---\n", encoding="utf-8")
    else:
        if damage == "marker": post["_ob_import_operations"] = []
        if damage == "import_marker": post["_ob_import_operations"][0]["operation_kind"] = "create"
        if damage == "identity": post["id"] = "wrong"
        if damage == "body": post.content += "changed"
        path.write_text(frontmatter.dumps(post), encoding="utf-8")
    before = path.read_bytes()
    monkeypatch.setattr(operations.ArchiveSessionOperations, "checkpoint", lambda *args: None)
    assert "已归档" not in await ob.archive_session(**ARGS, operation_id="file")
    op = store(ob).lookup("file")
    assert op["status"] == "blocked" and path.read_bytes() == before
    assert op["last_error_code"] in {"archive_inventory_invalid", "archive_bucket_evidence_conflict"}


@pytest.mark.asyncio
async def test_archive_and_import_marker_namespaces_and_public_visibility(ob):
    result = await ob.archive_session(**ARGS, operation_id="PRIVATE_CALLER_ID")
    op = store(ob).lookup("PRIVATE_CALLER_ID")
    path = Path(ob.config["buckets_dir"]) / op["plan"]["relative_path"]
    post = frontmatter.load(path)
    key = post["_ob_import_operations"][0]["operation_key"]
    assert BucketManager._operation_marker(post, key) is None
    assert "PRIVATE_CALLER_ID" not in result and "PRIVATE_CALLER_ID" not in path.read_text()
    visible = await ob.bucket_mgr.get(op["bucket_id"])
    assert "_ob_import_operations" not in visible["metadata"]
    imported = await ob.bucket_mgr.create("imported", domain=["test"], _o5b_operation_key="import:one")
    marker_post = frontmatter.load(ob.bucket_mgr._find_bucket_file(imported))
    assert BucketManager._operation_marker(marker_post, "import:one")["operation_kind"] == "create"
    assert all(m["operation_kind"] != "archive_session" for m in marker_post["_ob_import_operations"])


@pytest.mark.asyncio
@pytest.mark.parametrize("table,field", [("letters", "content"), ("boot_delta_events", "event_type")])
async def test_transaction_receipt_conflict_is_blocked(ob, monkeypatch, table, field):
    def pause(self, boundary, operation):
        if boundary == "after_letter_commit": raise RuntimeError("pause")
    monkeypatch.setattr(operations.ArchiveSessionOperations, "checkpoint", pause)
    await ob.archive_session(**ARGS, operation_id="receipt")
    with sqlite3.connect(store(ob).db) as conn:
        conn.execute(f"UPDATE {table} SET {field}='wrong'")
    monkeypatch.setattr(operations.ArchiveSessionOperations, "checkpoint", lambda *args: None)
    assert "conflict" in await ob.archive_session(**ARGS, operation_id="receipt")
    assert store(ob).lookup("receipt")["status"] == "blocked"
    assert counts(ob)[:3] == (1, 1, 1)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["malformed", "conflict", "duplicates"])
async def test_emotion_conflict_never_overwrites_timeline(ob, monkeypatch, kind):
    def pause(self, boundary, operation):
        if boundary == "after_letter_commit": raise RuntimeError("pause")
    monkeypatch.setattr(operations.ArchiveSessionOperations, "checkpoint", pause)
    await ob.archive_session(**ARGS, operation_id="emotion")
    entry = store(ob).lookup("emotion")["plan"]["snapshot"]
    path = Path(ob._emotion_timeline_path())
    if kind == "malformed": path.write_text("{bad", encoding="utf-8")
    else: path.write_text(json.dumps([entry | {"valence": .1}] if kind == "conflict"
                                    else [entry, entry]), encoding="utf-8")
    before = path.read_bytes()
    monkeypatch.setattr(operations.ArchiveSessionOperations, "checkpoint", lambda *args: None)
    assert "已归档" not in await ob.archive_session(**ARGS, operation_id="emotion")
    assert path.read_bytes() == before and store(ob).lookup("emotion")["status"] == "blocked"


@pytest.mark.asyncio
async def test_local_vector_progress_loss_skips_provider(ob, monkeypatch):
    engine = ob.bucket_mgr.embedding_engine
    engine.enabled = True
    def pause(self, boundary, operation):
        if boundary == "after_vector_commit": raise RuntimeError("pause")
    monkeypatch.setattr(operations.ArchiveSessionOperations, "checkpoint", pause)
    await ob.archive_session(**ARGS, operation_id="vector")
    before = Path(engine.db_path).read_bytes()
    engine._generate_embedding.reset_mock()
    monkeypatch.setattr(operations.ArchiveSessionOperations, "checkpoint", lambda *args: None)
    assert "已归档" in await ob.archive_session(**ARGS, operation_id="vector")
    engine._generate_embedding.assert_not_awaited()
    assert Path(engine.db_path).read_bytes() == before


@pytest.mark.asyncio
async def test_provider_cancellation_pending_and_failure_best_effort(ob):
    engine = ob.bucket_mgr.embedding_engine
    engine.enabled = True
    entered = asyncio.Event()
    async def provider(*args, **kwargs):
        # The actual await begins with neither storage mutex nor journal transaction held.
        for db in (store(ob).db, store(ob).root / ".bucket-write.lock"):
            with sqlite3.connect(db, timeout=0) as conn:
                conn.execute("BEGIN IMMEDIATE")
                conn.rollback()
        entered.set()
        await asyncio.Event().wait()
    engine._generate_embedding = AsyncMock(side_effect=provider)
    task = asyncio.create_task(ob.archive_session(**ARGS, operation_id="cancel"))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError): await task
    op = store(ob).lookup("cancel")
    assert op["status"] == "pending" and op["embedding_resolution"] is None
    assert counts(ob) == (1, 1, 0, 0)
    engine._generate_embedding = AsyncMock(side_effect=RuntimeError("private provider error"))
    assert "已归档" in await ob.archive_session(**ARGS, operation_id="cancel")
    assert store(ob).lookup("cancel")["embedding_resolution"]["outcome"] == "best_effort_failed"
    engine._generate_embedding.reset_mock()
    await ob.archive_session(**ARGS, operation_id="cancel")
    engine._generate_embedding.assert_not_awaited()


@pytest.mark.asyncio
async def test_concurrent_provider_winner_cannot_be_overwritten(ob):
    engine = ob.bucket_mgr.embedding_engine
    engine.enabled = True
    both, release = asyncio.Event(), asyncio.Event()
    calls = 0
    async def provider(*args, **kwargs):
        nonlocal calls
        calls += 1
        number = calls
        if number == 2:
            both.set()
            await release.wait()
            return [.9, .9]
        await both.wait()
        return [.1, .2]
    engine._generate_embedding = AsyncMock(side_effect=provider)
    first = asyncio.create_task(ob.archive_session(**ARGS, operation_id="race"))
    second = asyncio.create_task(ob.archive_session(**ARGS, operation_id="race"))
    result = await asyncio.wait_for(first, 5)
    before = Path(engine.db_path).read_bytes()
    release.set()
    assert await asyncio.wait_for(second, 5) == result
    assert Path(engine.db_path).read_bytes() == before and counts(ob) == (1, 1, 1, 1)


@pytest.mark.asyncio
async def test_sealed_and_lazy_embedding_migration(ob):
    engine = ob.bucket_mgr.embedding_engine
    engine.enabled = True
    with sqlite3.connect(engine.db_path) as conn:
        assert "input_digest" not in {r[1] for r in conn.execute("PRAGMA table_info(embeddings)")}
    engine._store_embedding("old", [.1, .2])
    assert await engine.get_embedding("old") == [.1, .2]
    await ob.archive_session(**ARGS, sealed=True, operation_id="sealed")
    engine._generate_embedding.assert_not_awaited()
    assert ob._load_emotion_timeline() == []
    with sqlite3.connect(engine.db_path) as conn:
        assert "input_digest" not in {r[1] for r in conn.execute("PRAGMA table_info(embeddings)")}
    await ob.archive_session(**ARGS, operation_id="unsealed")
    op = store(ob).lookup("unsealed")
    engine._store_embedding(op["bucket_id"], [.3, .4])
    with sqlite3.connect(engine.db_path) as conn:
        assert conn.execute("SELECT input_digest FROM embeddings WHERE bucket_id=?",
                            (op["bucket_id"],)).fetchone()[0] == ""
        assert conn.execute("SELECT embedding FROM embeddings WHERE bucket_id='old'").fetchone()[0] == "[0.1, 0.2]"


CHILD = r"""
import asyncio, json, os, sys, time
from pathlib import Path
from unittest.mock import AsyncMock
root, key, boundary, control = sys.argv[1:]
for name in ('OMBRE_API_KEY','OMBRE_EMBEDDING_API_KEY','OMBRE_DIGEST_API_KEY',
             'OMBRE_RM_RUNTIME_ENABLED','OMBRE_RM_DATA_ROOT','OMBRE_HOOK_URL'):
    os.environ.pop(name, None)
os.environ['OMBRE_BUCKETS_DIR'] = root
if control:
    Path(control + '.' + str(os.getpid())).touch()
    while not Path(control).exists(): time.sleep(.01)
import server as s
import archive_session_operations as a
s.config['embedding'] = {'independent':True,'api_key':'','enabled':False,'model':'test-model'}
s.decay_engine.ensure_started = AsyncMock(return_value=None)
s.bucket_mgr.embedding_engine.enabled = boundary == 'after_vector_commit'
s.bucket_mgr.embedding_engine._generate_embedding = AsyncMock(return_value=[.1,.2])
def checkpoint(self, point, operation):
    if point == boundary: os._exit(77)
a.ArchiveSessionOperations.checkpoint = checkpoint
if key == 'hold-emotion':
    s.dehydrator.analyze = AsyncMock(return_value={
        'domain':['test'],'valence':.6,'arousal':.4,'tags':[],
        'suggested_name':'held','todos':[]})
    asyncio.run(s.hold('independent held memory', pinned=True, valence=.6, arousal=.4))
    print(json.dumps('hold'))
else:
    print(json.dumps(asyncio.run(s.archive_session(
        summary='session body', highlights='highlight', mood='calm', valence=.7,
        arousal=.4, letter='handoff', topics=['project/OB'], operation_id=key))))
"""


def child(root, key, boundary="", control=""):
    return subprocess.Popen([sys.executable, "-B", "-c", CHILD, str(root), key, boundary, str(control)],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", [
    "after_plan", "after_publish", "before_boot_commit", "after_boot_commit",
    "after_vector_commit", "before_letter_commit", "after_letter_commit",
    "after_emotion_replace", "after_completed_commit",
])
async def test_real_process_exit_then_restart(ob, monkeypatch, boundary):
    root = Path(ob.config["buckets_dir"])
    process = child(root, "crash", boundary)
    stdout, stderr = process.communicate(timeout=20)
    assert process.returncode == 77, (stdout, stderr)
    plan = store(ob).lookup("crash")["plan"]
    restarted = load(root, monkeypatch, enabled=boundary == "after_vector_commit")
    assert await restarted.archive_session(**ARGS, operation_id="crash") == plan["result_text"]
    assert counts(restarted) == (1, 1, 1, 1)
    assert store(restarted).lookup("crash")["plan"] == plan


@pytest.mark.asyncio
@pytest.mark.parametrize("keys", [("same", "same"), ("A", "B"), ("A", "hold-emotion")])
async def test_real_multiprocess_identity_name_and_timeline(ob, tmp_path, keys):
    control = tmp_path / "start"
    processes = [child(ob.config["buckets_dir"], key, control=control) for key in keys]
    try:
        async with asyncio.timeout(15):
            while len(list(tmp_path.glob("start.*"))) < 2: await asyncio.sleep(.01)
        control.touch()
        results = []
        for process in processes:
            stdout, stderr = await asyncio.to_thread(process.communicate, timeout=30)
            assert process.returncode == 0, (stdout, stderr)
            result = json.loads(stdout.splitlines()[-1])
            assert "已归档" in result or result == "hold", (result, stderr)
            results.append(result)
        if keys[0] == keys[1]:
            assert results[0] == results[1] and counts(ob) == (1, 1, 1, 1)
        elif keys[1] == "hold-emotion":
            assert counts(ob) == (1, 2, 1, 2)
            assert {entry['source'] for entry in ob._load_emotion_timeline()} == {'hold', 'archive'}
        else:
            assert results[0] != results[1] and counts(ob) == (2, 2, 2, 2)
            assert {store(ob).lookup(key)["session_name"].split("_")[-1] for key in keys} == {"01", "02"}
    finally:
        for process in processes:
            if process.poll() is None: process.kill()
            process.communicate()


@pytest.mark.asyncio
async def test_legacy_coordinates_with_reserved_names(ob, monkeypatch):
    def pause(self, boundary, operation):
        if boundary == "after_plan": raise RuntimeError("pause")
    monkeypatch.setattr(operations.ArchiveSessionOperations, "checkpoint", pause)
    await ob.archive_session(**ARGS, operation_id="reserved")
    first = store(ob).lookup("reserved")["session_name"]
    legacy = await ob.archive_session(**ARGS)
    assert first.endswith("_01") and "_02 bucket_id:" in legacy
    monkeypatch.setattr(operations.ArchiveSessionOperations, "checkpoint", lambda *args: None)
    assert "已归档" in await ob.archive_session(**ARGS, operation_id="reserved")


@pytest.mark.asyncio
async def test_id_bounds_case_sensitive_and_no_generic_create_or_archive(ob, monkeypatch):
    monkeypatch.setattr(ob.bucket_mgr, "create", AsyncMock(side_effect=AssertionError("generic create")))
    monkeypatch.setattr(ob.bucket_mgr, "archive", AsyncMock(side_effect=AssertionError("generic archive")))
    results = [await ob.archive_session("same", operation_id=key) for key in ("a"*128, "Case", "case")]
    assert all("已归档" in result for result in results) and len(set(results)) == 3


@pytest.mark.asyncio
async def test_partial_receipt_cannot_recreate_missing_emotion(ob, monkeypatch):
    original = operations.ArchiveSessionOperations.complete
    monkeypatch.setattr(operations.ArchiveSessionOperations, "complete",
                        lambda *args: (_ for _ in ()).throw(RuntimeError("pause")))
    await ob.archive_session(**ARGS, operation_id="emotion-receipt")
    timeline = Path(ob.config["buckets_dir"]) / ".emotion_timeline.json"
    timeline.write_text("[]", encoding="utf-8")
    monkeypatch.setattr(operations.ArchiveSessionOperations, "complete", original)
    assert "receipt_conflict" in await ob.archive_session(**ARGS, operation_id="emotion-receipt")
    assert timeline.read_text() == "[]"


@pytest.mark.asyncio
@pytest.mark.parametrize("raw", ['[NaN]', '[{"source":"hold","source":"archive"}]', '{}', '[1]'])
async def test_shared_emotion_writer_never_replaces_invalid_json(ob, raw):
    path = Path(ob.config["buckets_dir"]) / ".emotion_timeline.json"
    path.write_text(raw, encoding="utf-8")
    ob._record_emotion_snapshot(.5, .3, "hold", "hold-source")
    assert path.read_text() == raw


@pytest.mark.asyncio
async def test_completed_receipt_corruption_rejected_cold(ob, monkeypatch):
    await ob.archive_session(**ARGS, operation_id="receipt-corrupt")
    with sqlite3.connect(store(ob).db) as conn:
        conn.execute("UPDATE ob_archive_session_operations SET completed_receipt_json='{}'")
    sys.modules.pop("server", None)
    cold = importlib.import_module("server")
    assert "journal_invalid" in await cold.archive_session(**ARGS, operation_id="receipt-corrupt")
    assert cold._runtime_components is None


@pytest.mark.asyncio
async def test_vector_digest_conflict_never_overwrites(ob, monkeypatch):
    engine = ob.bucket_mgr.embedding_engine
    engine.enabled = True
    def pause(self, boundary, operation):
        if boundary == "after_vector_commit": raise RuntimeError("pause")
    monkeypatch.setattr(operations.ArchiveSessionOperations, "checkpoint", pause)
    await ob.archive_session(**ARGS, operation_id="bad-vector")
    with sqlite3.connect(engine.db_path) as conn:
        conn.execute("UPDATE embeddings SET input_digest='wrong'")
    before = Path(engine.db_path).read_bytes()
    engine._generate_embedding.reset_mock()
    monkeypatch.setattr(operations.ArchiveSessionOperations, "checkpoint", lambda *args: None)
    assert "receipt_conflict" in await ob.archive_session(**ARGS, operation_id="bad-vector")
    assert Path(engine.db_path).read_bytes() == before
    engine._generate_embedding.assert_not_awaited()


@pytest.mark.asyncio
async def test_direct_publish_retains_create_metadata_and_portable_hides_markers(ob, tmp_path):
    from portable_export import export_ordinary_portable
    await ob.archive_session(**ARGS, operation_id="PRIVATE_OPERATION")
    op = store(ob).lookup("PRIVATE_OPERATION")
    plan = op["plan"]
    post = frontmatter.loads(plan["file_text"])
    ordinary_id = await ob.bucket_mgr.create(post.content, name=plan["session_name"],
        tags=["session", "archive"], importance=5, domain=["session"], valence=.7, arousal=.4,
        topics=["project/OB"], provenance_kind="summary")
    ordinary = await ob.bucket_mgr.get(ordinary_id)
    visible = await ob.bucket_mgr.get(op["bucket_id"])
    excluded = {"id", "created", "last_active", "type"}
    assert {k:v for k,v in visible["metadata"].items() if k not in excluded} == {
        k:v for k,v in ordinary["metadata"].items() if k not in excluded}
    destination = tmp_path / "portable"
    await export_ordinary_portable(buckets_dir=ob.config["buckets_dir"],
                                  destination=destination, confirm_export=True)
    exported = b"".join(path.read_bytes() for path in destination.rglob("*") if path.is_file())
    assert b"PRIVATE_OPERATION" not in exported and b"_ob_import_operations" not in exported
    assert b"archive_session:" not in exported


@pytest.mark.asyncio
async def test_inventory_conflict_and_publish_failure_are_closed(ob, monkeypatch):
    bad = Path(ob.config["buckets_dir"]) / "dynamic" / "bad.md"
    bad.write_text("truncated", encoding="utf-8")
    assert "inventory_invalid" in await ob.archive_session(**ARGS, operation_id="unsafe")
    assert store(ob).lookup("unsafe") is None
    bad.unlink()
    writer = BucketManager._write_bytes_atomic
    monkeypatch.setattr(BucketManager, "_write_bytes_atomic", lambda *args: (_ for _ in ()).throw(OSError("private")))
    assert "storage_failed" in await ob.archive_session(**ARGS, operation_id="publish-failure")
    assert counts(ob) == (0, 0, 0, 0) and store(ob).lookup("publish-failure")["status"] == "blocked"
    monkeypatch.setattr(BucketManager, "_write_bytes_atomic", writer)
    assert "已归档" in await ob.archive_session(**ARGS, operation_id="publish-failure")


@pytest.mark.asyncio
async def test_name_allocation_uses_max_suffix_not_count(ob, monkeypatch):
    monkeypatch.setattr(operations, "archive_now", lambda: "2026-09-27T12:00:00")
    await ob.bucket_mgr.create("older", name="session_2026-09-27_09", domain=["session"])
    assert "session_2026-09-27_10" in await ob.archive_session("new", operation_id="max")


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", [
    {"summary": " "}, {"summary": None}, {"highlights": 1}, {"mood": None},
    {"letter": []}, {"valence": -2}, {"valence": 1.1}, {"valence": float("nan")},
    {"arousal": float("inf")}, {"arousal": True}, {"sealed": "true"},
    {"topics": "topic"}, {"topics": [1]},
])
async def test_invalid_payload_with_existing_database_is_cold_and_zero_write(ob, invalid):
    before = fingerprint(ob)
    sys.modules.pop("server", None)
    cold = importlib.import_module("server")
    assert "已归档" not in await cold.archive_session(**(ARGS | invalid), operation_id="valid-id")
    assert cold._runtime_components is None and fingerprint(ob) == before
    assert store(ob).lookup("valid-id") is None


def test_legacy_embedding_cold_start_leaves_schema_and_vectors_unchanged(ob):
    engine = ob.bucket_mgr.embedding_engine
    engine._store_embedding("existing", [.1, .2])
    before = Path(engine.db_path).read_bytes()
    restarted = EmbeddingEngine(ob.config)
    assert Path(restarted.db_path).read_bytes() == before
    with sqlite3.connect(restarted.db_path) as conn:
        assert "input_digest" not in {r[1] for r in conn.execute("PRAGMA table_info(embeddings)")}

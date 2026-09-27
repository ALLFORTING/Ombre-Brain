"""W-11: synthetic storage, canonical source proof, marking evidence and MCP receipts."""
import ast
import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import importlib
import json
from pathlib import Path
import sys
import threading
from unittest.mock import AsyncMock, Mock

import frontmatter
import pytest

import bucket_manager as bm_module
from bucket_manager import BucketManager
from bucket_write_lock import bucket_write_scope
from mcp.shared.memory import create_connected_server_and_client_session


MARKER = "[hold_feel_source_receipt]"
SOURCE_CODES = {
    "source_missing", "source_unreadable", "source_identity_malformed",
    "source_identity_duplicate", "source_identity_conflict", "source_identity_ambiguous",
}
KEYS = ["feel_created", "feel_reused", "bucket_id", "source_bucket_id",
        "source_marked", "source_mark_error"]
NEW_TIME = "2026-10-01T10:00:00.123456"
OLD_TIME = "2020-01-01T00:00:00"


@pytest.fixture
def ob(tmp_path, monkeypatch):
    for key in ("OMBRE_API_KEY", "OMBRE_EMBEDDING_API_KEY", "OMBRE_DIGEST_API_KEY",
                "OMBRE_RM_RUNTIME_ENABLED", "OMBRE_RM_DATA_ROOT", "OMBRE_HOOK_URL",
                "OMBRE_MCP_STATELESS_HTTP"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("OMBRE_BUCKETS_DIR", str(tmp_path / "buckets"))
    monkeypatch.setenv("OMBRE_CONFIG_PATH", str(tmp_path / "absent-config.yaml"))
    sys.modules.pop("server", None)
    server = importlib.import_module("server")
    server.config["embedding"] = {"independent": True, "api_key": "", "enabled": False,
                                 "model": "test-model"}
    server.decay_engine.ensure_started = AsyncMock(return_value=None)
    server.bucket_mgr.embedding_engine.enabled = False
    server._similarity_doorbell = AsyncMock(return_value="")
    server._detect_conflict_warning = AsyncMock(return_value="")
    server._auto_link_related = AsyncMock(return_value=[])
    server.dehydrator.analyze = AsyncMock(return_value={"tags": ["reflection"]})
    return server


def read_post(path):
    return frontmatter.loads(Path(path).read_text(encoding="utf-8"),
                             handler=bm_module._SupersessionHandler())


def save_post(path, post):
    BucketManager._write_post_atomic(str(path), post)


async def make_source(ob, **metadata):
    identity = await ob.bucket_mgr.create("private source body", tags=["original"],
                                          domain=["test"], provenance_kind="summary")
    path = Path(ob.bucket_mgr._find_bucket_file(identity))
    post = read_post(path)
    post.metadata.update(last_active=OLD_TIME, updated_at="2020-01-01",
                         private_field={"preserve": [1, 2]})
    post.metadata.update(metadata)
    save_post(path, post)
    return identity, path


def feel_paths(ob):
    return list(Path(ob.bucket_mgr.feel_dir).rglob("*.md"))


def snapshot(ob):
    return {str(p.relative_to(ob.bucket_mgr.base_dir)): p.read_bytes()
            for p in Path(ob.bucket_mgr.base_dir).rglob("*")
            if p.is_file() and p.name != ".bucket-write.lock"}


def receipt(text):
    lines = text.rsplit(MARKER + "\n", 1)[1].splitlines()
    assert len(lines) == 6
    result = dict(line.split("=", 1) for line in lines)
    assert list(result) == KEYS
    assert result["feel_reused"] == "false"
    result["bucket_id"] = json.loads(result["bucket_id"])
    result["source_bucket_id"] = json.loads(result["source_bucket_id"])
    return result


def assert_receipt(text, source_id, *, created=True, error="none"):
    data = receipt(text)
    assert data["feel_created"] == str(created).lower()
    assert data["source_bucket_id"] == source_id
    assert bool(data["bucket_id"]) is created
    assert data["source_marked"] == str(error == "none").lower()
    assert data["source_mark_error"] == error
    assert error in SOURCE_CODES | {"none", "write_failed", "write_outcome_unknown"}
    assert "private source body" not in text
    return data


def damage(ob, monkeypatch, identity, path, fault):
    if fault == "missing":
        path.unlink()
    elif fault == "unreadable":
        real_read = Path.read_text
        def fail_read(self, *args, **kwargs):
            if self == path:
                raise OSError("private path/content must not be exposed")
            return real_read(self, *args, **kwargs)
        monkeypatch.setattr(Path, "read_text", fail_read)
    elif fault.startswith("malformed_id"):
        post = read_post(path)
        post["id"] = {"malformed_id_number": 42, "malformed_id_null": None,
                      "malformed_id_blank": "", "malformed_id_space": " bad "}[fault]
        save_post(path, post)
    elif fault == "duplicate":
        save_post(path.with_name("duplicate_" + identity + ".md"), read_post(path))
    elif fault == "conflict":
        post = read_post(path)
        post["id"] = "another-canonical-id"
        save_post(path, post)
    elif fault == "misplaced_claim":
        save_post(path.with_name("unrelated-name.md"), read_post(path))
    elif fault == "fallback_alias":
        post = read_post(path)
        post.metadata.pop("id")
        save_post(path.with_name("legacy_" + identity + ".md"), post)
    elif fault == "duplicate_key":
        path.write_text(f"---\nid: {identity}\nid: {identity}\n---\nprivate source body",
                        encoding="utf-8")
    elif fault == "malformed_yaml":
        path.write_text("---\nid: [\n---\nprivate source body", encoding="utf-8")
    elif fault == "unterminated_frontmatter":
        path.write_text(f"---\nid: {identity}\nprivate source body", encoding="utf-8")
    elif fault == "missing_frontmatter":
        path.write_text("private source body", encoding="utf-8")
    elif fault == "non_mapping":
        path.write_text("---\n- not-a-map\n---\nprivate source body", encoding="utf-8")
    elif fault == "incomplete":
        real_catalog = ob.bucket_mgr._supersession_catalog_locked
        monkeypatch.setattr(ob.bucket_mgr, "_supersession_catalog_locked",
                            lambda: {**real_catalog(), "incomplete": True})
    elif fault == "unrelated_unreadable":
        path.with_name("unrelated.md").write_text("---\nid: [\n---\nprivate", encoding="utf-8")
    elif fault == "unrelated_unterminated":
        path.with_name("hidden.md").write_text(f"---\nid: {identity}\nprivate",
                                               encoding="utf-8")
    elif fault == "directory_symlink":
        (Path(ob.bucket_mgr.archive_dir) / "hidden").symlink_to(path.parent,
                                                               target_is_directory=True)
    elif fault == "file_symlink":
        path.with_name("alias_" + identity + ".md").symlink_to(path)
    else:
        raise AssertionError(fault)


INVALID = [
    ("missing", "source_missing"),
    ("unreadable", "source_unreadable"),
    ("malformed_id_number", "source_identity_malformed"),
    ("malformed_id_null", "source_identity_malformed"),
    ("malformed_id_blank", "source_identity_malformed"),
    ("malformed_id_space", "source_identity_malformed"),
    ("duplicate", "source_identity_duplicate"),
    ("conflict", "source_identity_conflict"),
    ("misplaced_claim", "source_identity_conflict"),
    ("fallback_alias", "source_identity_conflict"),
    ("duplicate_key", "source_unreadable"),
    ("malformed_yaml", "source_unreadable"),
    ("unterminated_frontmatter", "source_unreadable"),
    ("missing_frontmatter", "source_unreadable"),
    ("non_mapping", "source_unreadable"),
    ("incomplete", "source_identity_ambiguous"),
    ("unrelated_unreadable", "source_identity_ambiguous"),
    ("unrelated_unterminated", "source_identity_ambiguous"),
    ("directory_symlink", "source_identity_ambiguous"),
    ("file_symlink", "source_unreadable"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("fault,error", INVALID)
async def test_preflight_rejects_invalid_source_without_creating_feel(ob, monkeypatch, fault, error):
    identity, path = await make_source(ob)
    damage(ob, monkeypatch, identity, path, fault)
    before = snapshot(ob)
    ob.bucket_mgr.create = AsyncMock(wraps=ob.bucket_mgr.create)
    text = await ob.hold("reflection", feel=True, source_bucket=identity)
    assert text.startswith(f"feel 未创建；source 校验失败（{error}）。")
    assert_receipt(text, identity, created=False, error=error)
    ob.bucket_mgr.create.assert_not_awaited()
    ob.dehydrator.analyze.assert_not_awaited()
    assert not feel_paths(ob)
    assert snapshot(ob) == before
    assert str(path) not in text


@pytest.mark.asyncio
@pytest.mark.parametrize("identity,filename,include_id", [
    ("old-fact", "title_old-fact.md", True),
    ("新事实", "新事实.md", True),
    ("legacy", "legacy.md", False),
])
async def test_preflight_preserves_legacy_identity_rules(ob, identity, filename, include_id):
    directory = Path(ob.bucket_mgr.dynamic_dir) / "legacy"
    directory.mkdir()
    path = directory / filename
    post = frontmatter.Post("legacy source", type="dynamic", last_active=OLD_TIME)
    if include_id:
        post["id"] = identity
    save_post(path, post)
    assert ob.bucket_mgr.preview_feel_source(identity) == {"status": "valid"}
    text = await ob.hold("reflection", feel=True, source_bucket="  " + identity + "  ")
    assert_receipt(text, identity)
    assert read_post(path)["digested"] is True
    assert ("id" in read_post(path)) is include_id


@pytest.mark.parametrize("identity", ["", " bad ", 42, None])
def test_preview_reports_malformed_request_identity(ob, identity):
    assert ob.bucket_mgr.preview_feel_source(identity) == {"status": "source_identity_malformed"}


@pytest.mark.asyncio
@pytest.mark.parametrize("lifecycle", ["sealed", "dormant", "archived", "superseded", "feel"])
async def test_lifecycle_sources_remain_markable(ob, monkeypatch, lifecycle):
    identity, path = await make_source(ob)
    post = read_post(path)
    if lifecycle in ("sealed", "dormant"):
        post[lifecycle] = 1 if lifecycle == "sealed" else True
    elif lifecycle == "superseded":
        post["superseded_by"] = identity  # W-11 must not traverse or repair this cycle.
        post["supersedes"] = ["old-source"]
    elif lifecycle == "feel":
        post["type"] = "feel"
        target = Path(ob.bucket_mgr.feel_dir) / "synthetic" / path.name
        target.parent.mkdir(parents=True)
        path.rename(target)
        path = target
    else:
        post["type"] = "archived"
        target = Path(ob.bucket_mgr.archive_dir) / path.name
        path.rename(target)
        path = target
    save_post(path, post)
    before = read_post(path)
    monkeypatch.setattr(bm_module, "now_iso", lambda: NEW_TIME)
    text = await ob.hold("reflection", feel=True, source_bucket=identity, valence=.7)
    assert_receipt(text, identity)
    after = read_post(path)
    assert path.is_file()
    assert ob.bucket_mgr._find_bucket_file(identity) == str(path)
    changed = {k for k in before.metadata.keys() | after.metadata.keys()
               if before.get(k) != after.get(k)}
    assert changed <= {"digested", "model_valence", "last_active", "updated_at"}
    assert after.content == before.content


@pytest.mark.asyncio
@pytest.mark.parametrize("valence,previous", [(.6, None), (0, .2), (1, .2), (-1, None), (-1, .2)])
async def test_success_receipt_and_exact_source_mutation(ob, monkeypatch, valence, previous):
    fields = {} if previous is None else {"model_valence": previous}
    identity, path = await make_source(ob, **fields)
    before = read_post(path)
    monkeypatch.setattr(bm_module, "now_iso", lambda: NEW_TIME)
    text = await ob.hold("reflection", feel=True, source_bucket=identity, valence=valence)
    data = assert_receipt(text, identity)
    assert text.startswith("🫧feel→新建 " + data["bucket_id"])
    after = read_post(path)
    expected = dict(before.metadata, digested=True, last_active=NEW_TIME)
    expected["updated_at"] = bm_module._date_only()
    if valence != -1:
        expected["model_valence"] = valence
    assert after.metadata == expected
    assert after.content == before.content
    assert len(feel_paths(ob)) == 1
    feel = read_post(feel_paths(ob)[0])
    assert feel["id"] == data["bucket_id"]
    assert feel.get("source_bucket") == ""
    assert ob.bucket_mgr.get_history(identity) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["", " ", "\t\n", None])
async def test_source_receipt_is_omitted_when_not_requested(ob, source):
    ob.bucket_mgr.preview_feel_source = Mock(side_effect=AssertionError("not requested"))
    ob.bucket_mgr.mark_feel_source = Mock(side_effect=AssertionError("not requested"))
    text = await ob.hold("reflection", feel=True, source_bucket=source)
    identity = read_post(feel_paths(ob)[0])["id"]
    assert text == "🫧feel→" + await ob._format_hold_created(identity)
    assert MARKER not in text


@pytest.mark.asyncio
async def test_receipt_ids_are_single_line_json_strings(ob):
    source_id = 'legacy"\nsource_marked=true\\end'
    text = await ob.hold("reflection", feel=True, source_bucket=source_id)
    data = assert_receipt(text, source_id, created=False, error="source_missing")
    assert data["source_marked"] == "false"
    encoded = ob._format_hold_feel_source_receipt('feel"\n\\id', source_id, "none")
    assert len(encoded.splitlines()) == 7
    assert receipt(encoded)["bucket_id"] == 'feel"\n\\id'


@pytest.mark.asyncio
@pytest.mark.parametrize("fault,error", [
    ("missing", "source_missing"), ("unreadable", "source_unreadable"),
    ("duplicate", "source_identity_duplicate"), ("conflict", "source_identity_conflict"),
    ("incomplete", "source_identity_ambiguous"),
])
async def test_final_recheck_uses_fresh_identity_and_content(ob, monkeypatch, fault, error):
    identity, path = await make_source(ob)
    async def between(_):
        damage(ob, monkeypatch, identity, path, fault)
    ob._auto_link_related = between
    text = await ob.hold("reflection", feel=True, source_bucket=identity)
    data = assert_receipt(text, identity, error=error)
    assert f"source 标记失败（{error}），feel 保留。" in text
    assert read_post(feel_paths(ob)[0])["id"] == data["bucket_id"]


@pytest.mark.asyncio
@pytest.mark.parametrize("fault,error", [
    ("false", "write_failed"), ("before", "write_failed"),
    ("after", "none"), ("after_false", "none"),
    ("unknown", "write_outcome_unknown"), ("mismatch", "write_failed"),
])
async def test_write_outcomes_are_reconciled_and_feel_retained(ob, monkeypatch, fault, error):
    identity, path = await make_source(ob)
    before = path.read_bytes()
    real_write = ob.bucket_mgr._write_bytes_atomic
    def writer(target, payload):
        assert str(target) == str(path)
        if fault == "false":
            return False
        if fault == "before":
            raise OSError("private exception path/content")
        real_write(target, payload)
        if fault == "after":
            raise OSError("private exception after replace")
        if fault == "after_false":
            return False
        if fault == "unknown":
            damage(ob, monkeypatch, identity, path, "unreadable")
            raise OSError("private exception with unreadable outcome")
        if fault == "mismatch":
            post = read_post(path)
            post["unexpected"] = "must not be rolled back"
            real_write(target, frontmatter.dumps(post).encode())
    monkeypatch.setattr(ob.bucket_mgr, "_write_bytes_atomic", writer)
    text = await ob.hold("reflection", feel=True, source_bucket=identity, valence=.6)
    assert_receipt(text, identity, error=error)
    assert len(feel_paths(ob)) == 1
    assert "private exception" not in text and str(path) not in text
    if fault in ("false", "before"):
        assert path.read_bytes() == before
    if fault in ("after", "after_false"):
        assert read_post(path)["model_valence"] == .6
    if fault == "unknown":
        assert "source 标记结果无法确认，feel 保留。" in text
    if fault == "mismatch":
        assert read_post(path)["unexpected"] == "must not be rolled back"


@pytest.mark.asyncio
async def test_real_post_replace_sync_failure_is_verified(ob, monkeypatch):
    identity, path = await make_source(ob)
    real_sync = BucketManager._sync_directory
    def sync(directory):
        if str(directory) == str(path.parent):
            raise OSError("synthetic directory sync failure")
        return real_sync(directory)
    monkeypatch.setattr(BucketManager, "_sync_directory", staticmethod(sync))
    text = await ob.hold("reflection", feel=True, source_bucket=identity)
    assert_receipt(text, identity)
    assert read_post(path)["digested"] is True
    assert not list(path.parent.glob("*.tmp"))


@pytest.mark.asyncio
@pytest.mark.parametrize("requested,previous", [(.6, .6), (.8, .6), (-1, .6), (-1, None)])
async def test_already_satisfied_still_refreshes_activity(ob, monkeypatch, requested, previous):
    fields = {"digested": True}
    if previous is not None:
        fields["model_valence"] = previous
    identity, path = await make_source(ob, **fields)
    monkeypatch.setattr(bm_module, "now_iso", lambda: NEW_TIME)
    real_write = ob.bucket_mgr._write_bytes_atomic
    writer = Mock(wraps=real_write)
    monkeypatch.setattr(ob.bucket_mgr, "_write_bytes_atomic", writer)
    text = await ob.hold("reflection", feel=True, source_bucket=identity, valence=requested)
    assert_receipt(text, identity)
    writer.assert_called_once()
    post = read_post(path)
    assert post["last_active"] == NEW_TIME
    assert post["updated_at"] != "2020-01-01"
    if requested != -1:
        assert post["model_valence"] == requested
    elif previous is not None:
        assert post["model_valence"] == previous
    else:
        assert "model_valence" not in post


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["false", "exception"])
async def test_already_satisfied_does_not_mask_failed_timestamp_update(ob, monkeypatch, fault):
    identity, path = await make_source(ob, digested=True, model_valence=.6)
    before = path.read_bytes()
    monkeypatch.setattr(bm_module, "now_iso", lambda: NEW_TIME)
    def fail(*_):
        if fault == "false":
            return False
        raise OSError("failed before publication")
    monkeypatch.setattr(ob.bucket_mgr, "_write_bytes_atomic", fail)
    text = await ob.hold("reflection", feel=True, source_bucket=identity, valence=.6)
    assert_receipt(text, identity, error="write_failed")
    assert path.read_bytes() == before


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["success", "false", "exception", "after_false", "after_exception"])
async def test_identical_old_payload_cannot_prove_failed_current_write(ob, monkeypatch, fault):
    identity, path = await make_source(ob, digested=True, model_valence=.6,
                                       last_active=NEW_TIME, updated_at=bm_module._date_only())
    before = path.read_bytes()
    monkeypatch.setattr(bm_module, "now_iso", lambda: NEW_TIME)
    real_write = ob.bucket_mgr._write_bytes_atomic
    def writer(target, payload):
        assert payload == before
        if fault == "false":
            return False
        if fault == "exception":
            raise OSError("failed before publication")
        real_write(target, payload)
        if fault == "after_false":
            return False
        if fault == "after_exception":
            raise OSError("failed after identical publication")
    mocked = Mock(side_effect=writer)
    monkeypatch.setattr(ob.bucket_mgr, "_write_bytes_atomic", mocked)
    text = await ob.hold("reflection", feel=True, source_bucket=identity, valence=.6)
    error = "none" if fault == "success" else "write_outcome_unknown"
    assert_receipt(text, identity, error=error)
    mocked.assert_called_once()
    assert path.read_bytes() == before
    assert len(feel_paths(ob)) == 1


@pytest.mark.asyncio
async def test_retry_creates_distinct_feels(ob):
    identity, _ = await make_source(ob)
    texts = [await ob.hold("same reflection", feel=True, source_bucket=identity) for _ in range(2)]
    ids = [assert_receipt(text, identity)["bucket_id"] for text in texts]
    assert len(set(ids)) == 2
    assert len(feel_paths(ob)) == 2


def barrier(ob, between=None):
    arrivals = 0
    ready = asyncio.Event()
    async def related(_):
        nonlocal arrivals
        arrivals += 1
        if arrivals == 2:
            if between:
                between()
            ready.set()
        await ready.wait()
    ob._auto_link_related = related


@pytest.mark.asyncio
@pytest.mark.parametrize("values", [(.6, .6), (.4, .8)])
async def test_concurrent_calls_create_independent_feels_and_own_receipts(ob, monkeypatch, values):
    identity, path = await make_source(ob)
    commits = []
    real_write = ob.bucket_mgr._write_bytes_atomic
    def record(target, payload):
        real_write(target, payload)
        commits.append(read_post(path)["model_valence"])
    monkeypatch.setattr(ob.bucket_mgr, "_write_bytes_atomic", record)
    barrier(ob)
    texts = await asyncio.wait_for(asyncio.gather(*[
        ob.hold("same reflection", feel=True, source_bucket=identity, valence=v) for v in values
    ]), timeout=10)
    ids = [assert_receipt(text, identity)["bucket_id"] for text in texts]
    assert len(set(ids)) == 2 and len(feel_paths(ob)) == 2
    assert sorted(commits) == sorted(values)
    assert read_post(path)["model_valence"] == commits[-1]


@pytest.mark.asyncio
async def test_marker_preserves_latest_unrelated_metadata(ob):
    identity, path = await make_source(ob)
    ready, proceed, entering = threading.Event(), threading.Event(), threading.Event()
    def other_writer():
        with bucket_write_scope(ob.bucket_mgr.base_dir):
            post = read_post(path)
            ready.set()
            assert proceed.wait(5)
            post["tags"] = ["latest"]
            post["private_field"] = {"preserve": ["new"]}
            save_post(path, post)
    def mark():
        entering.set()
        return ob.bucket_mgr.mark_feel_source(identity, model_valence=.7)
    assert ob.bucket_mgr.preview_feel_source(identity) == {"status": "valid"}
    with ThreadPoolExecutor(max_workers=2) as pool:
        writer = pool.submit(other_writer)
        try:
            assert ready.wait(5)
            marking = pool.submit(mark)
            assert entering.wait(5)
        finally:
            proceed.set()
        writer.result(timeout=5)
        assert marking.result(timeout=5)["status"] == "marked"
    post = read_post(path)
    assert post["tags"] == ["latest"]
    assert post["private_field"] == {"preserve": ["new"]}
    assert post["digested"] is True and post["model_valence"] == .7


@pytest.mark.asyncio
async def test_concurrent_identity_conflict_fails_closed(ob, monkeypatch):
    identity, path = await make_source(ob)
    barrier(ob, lambda: damage(ob, monkeypatch, identity, path, "duplicate"))
    texts = await asyncio.wait_for(asyncio.gather(*[
        ob.hold("reflection", feel=True, source_bucket=identity) for _ in range(2)
    ]), timeout=10)
    for text in texts:
        assert_receipt(text, identity, error="source_identity_duplicate")
    assert len(feel_paths(ob)) == 2
    assert read_post(path).get("digested") is not True


@pytest.mark.asyncio
async def test_existing_post_create_order_is_preserved(ob, monkeypatch):
    identity, _ = await make_source(ob)
    calls = []
    real_preview, real_create = ob.bucket_mgr.preview_feel_source, ob.bucket_mgr.create
    real_update, real_mark = ob.bucket_mgr.update, ob.bucket_mgr.mark_feel_source
    real_format = ob._format_hold_created
    def preview(*args):
        calls.append("preflight")
        return real_preview(*args)
    async def analyze(_):
        calls.append("analyze")
        return {"tags": []}
    async def create(**kwargs):
        calls.append("create")
        return await real_create(**kwargs)
    async def update(*args, **kwargs):
        calls.append("trigger")
        return await real_update(*args, **kwargs)
    async def related(_):
        calls.append("related")
    def mark(*args, **kwargs):
        calls.append("mark")
        return real_mark(*args, **kwargs)
    async def display(identity):
        calls.append("format")
        return await real_format(identity)
    monkeypatch.setattr(ob.bucket_mgr, "preview_feel_source", preview)
    monkeypatch.setattr(ob.dehydrator, "analyze", analyze)
    monkeypatch.setattr(ob.bucket_mgr, "create", create)
    monkeypatch.setattr(ob.bucket_mgr, "update", update)
    monkeypatch.setattr(ob.bucket_mgr, "mark_feel_source", mark)
    monkeypatch.setattr(ob, "_record_emotion_snapshot", lambda *_: calls.append("emotion"))
    monkeypatch.setattr(ob, "_auto_link_related", related)
    monkeypatch.setattr(ob, "_format_hold_created", display)
    text = await ob.hold("reflection", feel=True, source_bucket=identity,
                         valence=.6, arousal=.4, trigger_date="2026-10-02")
    assert_receipt(text, identity)
    assert calls == ["preflight", "analyze", "create", "emotion", "trigger", "related", "mark", "format"]


@pytest.mark.asyncio
@pytest.mark.parametrize("kwargs", [{"content": ""}, {"importance": 0},
                                    {"valence": 2}, {"trigger_date": "not-a-date"}])
async def test_common_validation_precedes_preflight(ob, kwargs):
    ob.bucket_mgr.preview_feel_source = Mock(side_effect=AssertionError("validation first"))
    text = await ob.hold(**{"content": "reflection", "feel": True, "source_bucket": "missing",
                           **kwargs})
    assert MARKER not in text
    assert not feel_paths(ob)


@pytest.mark.asyncio
@pytest.mark.parametrize("published", [False, True])
async def test_create_exception_does_not_fabricate_negative_receipt(ob, monkeypatch, published):
    identity, _ = await make_source(ob)
    real_create = ob.bucket_mgr.create
    async def fail(**kwargs):
        if published:
            await real_create(**kwargs)
        raise RuntimeError("original create error")
    monkeypatch.setattr(ob.bucket_mgr, "create", fail)
    ob.bucket_mgr.mark_feel_source = Mock(side_effect=AssertionError("create not returned"))
    with pytest.raises(RuntimeError, match="original create error"):
        await ob.hold("reflection", feel=True, source_bucket=identity)
    assert len(feel_paths(ob)) == int(published)


@pytest.mark.asyncio
@pytest.mark.parametrize("step", ["emotion", "trigger", "related"])
async def test_existing_ancillary_exception_paths_are_unchanged(ob, monkeypatch, step):
    identity, _ = await make_source(ob)
    def fail(*args, **kwargs):
        raise RuntimeError("ancillary failure")
    if step == "emotion":
        monkeypatch.setattr(ob, "_record_emotion_snapshot", fail)
    elif step == "trigger":
        monkeypatch.setattr(ob.bucket_mgr, "update", AsyncMock(side_effect=fail))
    else:
        monkeypatch.setattr(ob, "_auto_link_related", AsyncMock(side_effect=fail))
    ob.bucket_mgr.mark_feel_source = Mock(side_effect=AssertionError("not reached"))
    with pytest.raises(RuntimeError, match="ancillary failure"):
        await ob.hold("reflection", feel=True, source_bucket=identity,
                      valence=.6, arousal=.4, trigger_date="2026-10-02")
    assert len(feel_paths(ob)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", [False, True])
async def test_source_receipt_survives_display_formatting_failure(ob, monkeypatch, missing):
    identity, path = await make_source(ob)
    if missing:
        async def related(_):
            path.unlink()
        ob._auto_link_related = related
    monkeypatch.setattr(ob, "_format_hold_created",
                        AsyncMock(side_effect=ValueError("private formatter failure")))
    text = await ob.hold("reflection", feel=True, source_bucket=identity)
    data = assert_receipt(text, identity, error="source_missing" if missing else "none")
    assert text.startswith("🫧feel→新建 " + data["bucket_id"] + "\n")
    assert "private formatter failure" not in text


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["success", "partial", "preflight"])
async def test_mcp_feel_source_receipt_compatibility(ob, case):
    identity, path = await make_source(ob)
    if case == "preflight":
        path.unlink()
    elif case == "partial":
        async def related(_):
            path.unlink()
        ob._auto_link_related = related
    async with create_connected_server_and_client_session(ob.mcp) as client:
        tool = next(t for t in (await client.list_tools()).tools if t.name == "hold")
        assert set(tool.outputSchema["properties"]) == {"result"}
        result = await client.call_tool("hold", {"content": "reflection", "feel": True,
                                                "source_bucket": identity})
    assert result.isError is False
    text = "\n".join(item.text for item in result.content if item.type == "text")
    assert result.structuredContent == {"result": text}
    assert_receipt(text, identity, created=case != "preflight",
                   error="none" if case == "success" else "source_missing")
    if case != "preflight":
        assert text.startswith("🫧feel→新建 ")


@pytest.mark.asyncio
async def test_marker_lock_failure_is_redacted_and_preflight_returns_no_snapshot(ob, monkeypatch):
    identity, _ = await make_source(ob)
    assert ob.bucket_mgr.preview_feel_source(identity) == {"status": "valid"}
    @contextmanager
    def unavailable(*args, **kwargs):
        raise RuntimeError("private root lock path")
        yield
    monkeypatch.setattr(bm_module, "bucket_write_scope", unavailable)
    assert ob.bucket_mgr.preview_feel_source(identity) == {"status": "source_identity_ambiguous"}
    assert ob.bucket_mgr.mark_feel_source(identity) == {"status": "write_failed"}


def test_marker_is_guarded_and_contains_no_await():
    assert BucketManager.mark_feel_source.__maintenance_guarded__ == "bucket_feel_source_mark"
    tree = ast.parse(Path(bm_module.__file__).read_text(encoding="utf-8"))
    method = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)
                  and n.name == "mark_feel_source")
    assert not any(isinstance(n, ast.Await) for n in ast.walk(method))

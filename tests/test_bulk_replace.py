"""bulk_replace: literal batch replace with dry-run plan, one-shot token and undo."""
import hashlib
import importlib
import re
import sqlite3
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import frontmatter
import pytest

import bulk_replace as br

OLD_STAMP = "2025-01-02T03:04:05"


def _load_server(tmp_path, monkeypatch):
    monkeypatch.setenv("OMBRE_BUCKETS_DIR", str(tmp_path / "buckets"))
    monkeypatch.delenv("OMBRE_API_KEY", raising=False)
    sys.modules.pop("server", None)
    server = importlib.import_module("server")
    server.decay_engine.ensure_started = AsyncMock(return_value=None)
    server.bucket_mgr.embedding_engine = None
    return server


def _age(server, bucket_id):
    path = server.bucket_mgr._find_bucket_file(bucket_id)
    post = frontmatter.load(path)
    post["last_active"] = OLD_STAMP
    post["updated_at"] = "2025-01-02"
    post["created"] = OLD_STAMP
    Path(path).write_text(frontmatter.dumps(post), encoding="utf-8")


def _post(server, bucket_id):
    return frontmatter.load(server.bucket_mgr._find_bucket_file(bucket_id))


def _token(text):
    return re.search(r"confirm_token: (\S+)", text).group(1)


def _db(server):
    return sqlite3.connect(server.bucket_mgr.history_db_path)


def _rows(server, sql, args=()):
    with _db(server) as conn:
        return conn.execute(sql, args).fetchall()


RULES = [{"from": "旧城", "to": "新城"}]


async def _seed(server):
    a = await server.bucket_mgr.create(content="我在旧城工作，旧城很大。", name="旧城日记",
                                       tags=["旧城", "工作"], domain=["日常"])
    b = await server.bucket_mgr.create(content="与地名无关的内容", name="无关", tags=["x"], domain=["日常"])
    sealed = await server.bucket_mgr.create(content="秘密旧城SEALEDBODY", name="封", domain=["日常"])
    await server.bucket_mgr.update(sealed, sealed=1)
    for bucket_id in (a, b):
        _age(server, bucket_id)
    server.bucket_mgr.record_letter("信里写着旧城和旧城。", "s1")
    server.bucket_mgr.record_letter("封存信旧城LETTERSEALED", "s2", sealed=True)
    return a, b, sealed


@pytest.mark.asyncio
async def test_dry_run_shows_diff_and_writes_nothing(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    a, b, sealed = await _seed(server)
    snapshot = {i: Path(server.bucket_mgr._find_bucket_file(i)).read_bytes() for i in (a, b, sealed)}
    letters_before = _rows(server, "SELECT id, content, sealed FROM letters ORDER BY id")
    history_before = _rows(server, "SELECT COUNT(*) FROM bucket_history")
    events_before = _rows(server, "SELECT COUNT(*) FROM boot_delta_events")
    revisions_before = _rows(server, "SELECT COUNT(*) FROM bucket_revisions")

    out = await server.bulk_replace(RULES)

    assert "no changes made" in out and "plan_hash:" in out and "confirm_token:" in out
    assert "【旧城 → 新城】" in out and a in out and "letter:1" in out
    assert "sealed 桶 1 个、sealed 信 1 封" in out
    assert "SEALEDBODY" not in out and "LETTERSEALED" not in out and sealed not in out
    assert {i: Path(server.bucket_mgr._find_bucket_file(i)).read_bytes() for i in (a, b, sealed)} == snapshot
    assert _rows(server, "SELECT id, content, sealed FROM letters ORDER BY id") == letters_before
    assert _rows(server, "SELECT COUNT(*) FROM bucket_history") == history_before
    assert _rows(server, "SELECT COUNT(*) FROM boot_delta_events") == events_before
    assert _rows(server, "SELECT COUNT(*) FROM bucket_revisions") == revisions_before


@pytest.mark.asyncio
async def test_execute_applies_keeps_time_fields_and_is_undoable(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    a, b, sealed = await _seed(server)
    sealed_bytes = Path(server.bucket_mgr._find_bucket_file(sealed)).read_bytes()
    original = _post(server, a)
    before_content, before_meta = original.content, dict(original.metadata)
    events_before = _rows(server, "SELECT COUNT(*) FROM boot_delta_events")

    token = _token(await server.bulk_replace(RULES))
    out = await server.bulk_replace(RULES, dry_run=False, confirm_token=token)

    assert "已更新 2 个（桶 1 / 信 1）" in out
    post = _post(server, a)
    assert post.content == "我在新城工作，新城很大。"
    assert post["name"] == "新城日记" and post["tags"] == ["新城", "工作"]
    assert post["provenance_kind"] == "unknown"
    for field in ("last_active", "updated_at", "created"):
        assert post[field] == before_meta[field]
    assert Path(server.bucket_mgr._find_bucket_file(sealed)).read_bytes() == sealed_bytes
    assert _rows(server, "SELECT COUNT(*) FROM boot_delta_events") == events_before
    assert _post(server, b).content == "与地名无关的内容"

    revisions = server.bucket_mgr.list_bucket_revisions(a)
    assert [r["op_kind"] for r in revisions] == ["bulk_replace"]
    assert not server.bucket_mgr.list_bucket_revisions(b)
    assert _rows(server, "SELECT old_content FROM bucket_history WHERE bucket_id=?", (a,))[0][0] == before_content

    plan = server.bucket_mgr.plan_restore(a, revisions[0]["ref"])
    await server.bucket_mgr.execute_restore(plan, actor="test")
    restored = _post(server, a)
    assert restored.content == before_content and restored["name"] == before_meta["name"]
    assert restored["tags"] == before_meta["tags"] and restored["last_active"] == OLD_STAMP


@pytest.mark.asyncio
async def test_letters_replaced_sealed_skipped_and_restorable(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    await _seed(server)
    token = _token(await server.bulk_replace(RULES))
    out = await server.bulk_replace(RULES, dry_run=False, confirm_token=token)
    op_id = re.search(r"op_id=(\S+)", out).group(1)

    rows = {r[0]: r[1:] for r in _rows(server, "SELECT id, content, created_at, session_id, sealed FROM letters")}
    assert rows[1][0] == "信里写着新城和新城。" and rows[1][2] == "s1" and rows[1][3] == 0
    assert rows[2][0] == "封存信旧城LETTERSEALED" and rows[2][3] == 1
    saved = _rows(server, "SELECT old_content, change_type FROM bucket_history WHERE bucket_id='letter:1'")
    assert saved == [("信里写着旧城和旧城。", op_id)]

    assert server.bucket_mgr.restore_bulk_replace_letters(op_id) == {"restored": [1], "missing": []}
    assert _rows(server, "SELECT content FROM letters WHERE id=1") == [("信里写着旧城和旧城。",)]


@pytest.mark.asyncio
async def test_token_is_one_shot_and_bound_to_the_plan(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    a, _, _ = await _seed(server)
    before = Path(server.bucket_mgr._find_bucket_file(a)).read_bytes()

    assert "invalid" in await server.bulk_replace(RULES, dry_run=False)
    assert "invalid" in await server.bulk_replace(RULES, dry_run=False, confirm_token="nope")

    token = _token(await server.bulk_replace(RULES))
    other = [{"from": "旧城", "to": "别处"}]
    assert "invalid" in await server.bulk_replace(other, dry_run=False, confirm_token=token)
    assert "invalid" in await server.bulk_replace(RULES, fields="content", dry_run=False, confirm_token=token)
    assert Path(server.bucket_mgr._find_bucket_file(a)).read_bytes() == before

    assert token in server._mutation_confirm_tokens
    assert "已执行" in await server.bulk_replace(RULES, dry_run=False, confirm_token=token)
    assert token not in server._mutation_confirm_tokens
    # Replaying after a successful run finds nothing left to change.
    assert "nothing matches" in await server.bulk_replace(RULES, dry_run=False, confirm_token=token)


@pytest.mark.asyncio
async def test_token_is_void_when_a_bucket_changes_after_preview(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    a, _, _ = await _seed(server)
    token = _token(await server.bulk_replace(RULES))
    await server.bucket_mgr.update(a, content="我在旧城工作，旧城很大。又加了一句。")

    out = await server.bulk_replace(RULES, dry_run=False, confirm_token=token)

    assert "invalid" in out
    assert "旧城" in _post(server, a).content


@pytest.mark.asyncio
async def test_bucket_ids_scope_limits_a_rule(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    a = await server.bucket_mgr.create(content="旧城一号", name="甲", domain=["d"])
    b = await server.bucket_mgr.create(content="旧城二号", name="乙", domain=["d"])
    rules = [{"from": "旧城", "to": "新城", "bucket_ids": [a, "ghost"]}]

    preview = await server.bulk_replace(rules, scope="buckets")
    assert "ghost" in preview and "读不到" in preview
    await server.bulk_replace(rules, scope="buckets", dry_run=False, confirm_token=_token(preview))

    assert _post(server, a).content == "新城一号"
    assert _post(server, b).content == "旧城二号"


@pytest.mark.asyncio
async def test_fields_subset_and_provenance_keep(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    a = await server.bucket_mgr.create(content="旧城", name="旧城", tags=["旧城"], domain=["d"],
                                       provenance_kind="summary")
    token = _token(await server.bulk_replace(RULES, fields="name", scope="buckets"))
    await server.bulk_replace(RULES, fields="name", scope="buckets", dry_run=False, confirm_token=token)
    post = _post(server, a)
    assert (post["name"], post.content, post["tags"]) == ("新城", "旧城", ["旧城"])
    assert post["provenance_kind"] == "summary"

    token = _token(await server.bulk_replace(RULES, fields="content", provenance="keep", scope="buckets"))
    await server.bulk_replace(RULES, fields="content", provenance="keep", scope="buckets",
                              dry_run=False, confirm_token=token)
    assert _post(server, a)["provenance_kind"] == "summary"

    token = _token(await server.bulk_replace([{"from": "新城", "to": "X"}], scope="buckets", fields="name"))
    await server.bulk_replace([{"from": "新城", "to": "X"}], scope="buckets", fields="name",
                              dry_run=False, confirm_token=token)
    assert _post(server, a)["provenance_kind"] == "summary"


@pytest.mark.asyncio
async def test_no_match_issues_no_token(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    await server.bucket_mgr.create(content="nothing here", domain=["d"])
    out = await server.bulk_replace([{"from": "zzz", "to": "y"}])
    assert "confirm_token" not in out.replace("没有可改动的内容，未签发 confirm_token", "")
    assert "没有命中" in out
    assert "nothing matches" in await server.bulk_replace([{"from": "zzz", "to": "y"}], dry_run=False)


@pytest.mark.asyncio
async def test_invalid_requests_are_rejected_without_a_plan(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    for bad in ([], [{"from": "", "to": "x"}], [{"from": "a", "to": "a"}], [{"from": "a"}],
                [{"from": "a", "to": "b", "regex": True}], [{"from": "a", "to": "b", "bucket_ids": []}],
                [{"from": "a", "to": "b", "bucket_ids": [1]}], ["a"]):
        assert "rejected" in await server.bulk_replace(bad)
    assert "rejected" in await server.bulk_replace(RULES, fields="body")
    assert "rejected" in await server.bulk_replace(RULES, scope="vault")
    assert "rejected" in await server.bulk_replace(RULES, provenance="guess")


@pytest.mark.asyncio
async def test_apply_refuses_stale_preimage_and_sealed(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    a, _, sealed = await _seed(server)
    path = server.bucket_mgr._find_bucket_file(a)
    before = Path(path).read_bytes()
    kwargs = dict(changes={"content": "x"}, provenance="unknown", op_id="bulk_replace:t")
    assert await server.bucket_mgr.apply_bulk_replace_bucket(a, expected_preimage_sha256="0" * 64, **kwargs) == "stale"
    assert await server.bucket_mgr.apply_bulk_replace_bucket(sealed, expected_preimage_sha256="0" * 64, **kwargs) == "sealed"
    assert await server.bucket_mgr.apply_bulk_replace_bucket("nope", expected_preimage_sha256="0" * 64, **kwargs) == "missing"
    assert Path(path).read_bytes() == before
    assert server.bucket_mgr.apply_bulk_replace_letter(
        2, expected_preimage_sha256="0" * 64, new_content="x", op_id="bulk_replace:t") == "sealed"
    assert server.bucket_mgr.apply_bulk_replace_letter(
        1, expected_preimage_sha256="0" * 64, new_content="x", op_id="bulk_replace:t") == "stale"


@pytest.mark.asyncio
async def test_dry_run_paginates(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    for i in range(5):
        await server.bucket_mgr.create(content=f"旧城{i}", domain=["d"])
    first = await server.bulk_replace(RULES, scope="buckets", page_size=2)
    last = await server.bulk_replace(RULES, scope="buckets", page_size=2, page=3)
    assert "第 1/3 页" in first and "第 3/3 页" in last
    assert first.count("【旧城 → 新城】") == 2 and last.count("【旧城 → 新城】") == 1
    assert "命中 5 个，共 5 处" in first


def test_rules_apply_in_order_and_tags_collapse():
    rules = br.normalize_rules([{"from": "A", "to": "B"}, {"from": "B", "to": "C"}])
    source = {"id": "x", "name": "A", "tags": ["A", "C"], "content": "A-B", "preimage_sha256": "h"}
    planned = br.plan_bucket(source, rules, ["content", "name", "tags"], 3)
    assert planned["changes"] == {"content": "C-C", "name": "C", "tags": ["C"]}
    assert [h["rule"] for h in planned["hits"] if h["field"] == "content"] == [1, 2, 2]


def test_plan_payload_changes_with_any_input():
    scan = {"sources": [{"id": "x", "name": None, "tags": None, "content": "旧城",
                         "preimage_sha256": "h1", "extra": {"tg_summary": "旧城旧城"}}],
            "skipped_sealed": 0, "unreadable": 0, "duplicate_ids": 0}
    base = br.build_plan(scan, br.normalize_rules(RULES), ["content"], "unknown")
    again = br.build_plan(scan, br.normalize_rules(RULES), ["content"], "unknown")
    assert base["payload"] == again["payload"]
    assert base["untouched_fields"] == {"tg_summary": 2}
    changed = dict(scan, sources=[dict(scan["sources"][0], preimage_sha256="h2")])
    assert br.build_plan(changed, br.normalize_rules(RULES), ["content"], "unknown")["payload"] != base["payload"]
    assert br.build_plan(scan, br.normalize_rules(RULES), ["content"], "keep")["payload"] != base["payload"]
    digest = hashlib.sha256(b"x").hexdigest()
    assert len(digest) == 64

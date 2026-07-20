import importlib
import sys
from unittest.mock import AsyncMock

import frontmatter
import pytest


def _load_server(tmp_path, monkeypatch, seal="test-seal-a"):
    monkeypatch.setenv("OMBRE_BUCKETS_DIR", str(tmp_path / "buckets"))
    monkeypatch.setenv("OMBRE_RESPONSE_SEAL", seal)
    monkeypatch.delenv("OMBRE_API_KEY", raising=False)
    sys.modules.pop("server", None)
    server = importlib.import_module("server")
    server.decay_engine.ensure_started = AsyncMock(return_value=None)
    server.dehydrator.dehydrate = AsyncMock(
        side_effect=lambda content, metadata=None: content[:120]
    )
    return server


@pytest.mark.asyncio
async def test_archive_letter_boot_and_mailbox(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    pinned_id = await server.bucket_mgr.create(
        content="pinned startup principle body",
        name="Pinned startup",
        pinned=True,
    )
    todo_id = await server.bucket_mgr.create(
        content="todo carrier body",
        name="Todo carrier",
        importance=7,
    )
    todo_path = server.bucket_mgr._find_bucket_file(todo_id)
    post = frontmatter.load(todo_path)
    post["todos"] = ["finish boot validation"]
    with open(todo_path, "w", encoding="utf-8") as handle:
        handle.write(frontmatter.dumps(post))

    letter = "handoff letter exact body"
    await server.archive_session("session summary one", letter=letter)
    await server.archive_session("session summary two without letter")

    boot_result = await server.boot()
    mailbox_result = await server.breath(mailbox=True)

    assert pinned_id in boot_result
    assert "pinned startup principle body" in boot_result
    assert letter in boot_result
    assert "session summary two without letter" in boot_result
    assert "finish boot validation" in boot_result
    assert "seal: test-seal-a" in boot_result
    assert server.count_tokens_approx(boot_result) <= 8000

    assert letter in mailbox_result
    assert "seal: test-seal-a" in mailbox_result


@pytest.mark.asyncio
async def test_boot_pinned_index_defaults_to_2000_chars(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    long_pinned_body = "开机桶正文开始\n" + ("核心索引" * 180) + "\n澄当记事本。"
    too_long_body = ("临时热桶" * 600) + "SHOULD_NOT_APPEAR_AFTER_LIMIT"

    await server.bucket_mgr.create(
        content=long_pinned_body,
        name="Startup index",
        pinned=True,
    )
    await server.bucket_mgr.create(
        content=too_long_body,
        name="Temporary hot bucket",
        pinned=True,
    )

    boot_result = await server.boot()

    assert "=== boot: 开机索引 ===" in boot_result
    assert "澄当记事本。" in boot_result
    assert "SHOULD_NOT_APPEAR_AFTER_LIMIT" not in boot_result
    assert "seal: test-seal-a" in boot_result


@pytest.mark.asyncio
async def test_response_seal_reads_runtime_env_each_call(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch, seal="first-runtime-seal")

    first = await server.boot()
    monkeypatch.setenv("OMBRE_RESPONSE_SEAL", "second-runtime-seal")
    second = await server.boot()

    assert "seal: first-runtime-seal" in first
    assert "seal: second-runtime-seal" in second
    assert "seal: first-runtime-seal" not in second


@pytest.mark.asyncio
async def test_boot_hides_sealed_archive_bucket(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    sealed_id = await server.bucket_mgr.create(
        content="sealed archive boot filter control",
        domain=["session"],
        name="sealed_archive_boot_filter",
        sealed=True,
    )
    await server.bucket_mgr.archive(sealed_id)

    boot_result = await server.boot()

    assert sealed_id not in boot_result
    assert "sealed archive boot filter control" not in boot_result


@pytest.mark.asyncio
async def test_archive_session_sealed_true_persists_sealed_bucket(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)

    result = await server.archive_session("sealed archive persist control", sealed=True)
    bucket_id = result.split("bucket_id:", 1)[1].strip()
    bucket = await server.bucket_mgr.get(bucket_id)

    assert bucket["metadata"]["sealed"] == 1
    assert "archive" in bucket["path"]


@pytest.mark.asyncio
async def test_seal_letter_hides_default_mailbox_until_included(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    letter = "letter seal by id control"
    await server.archive_session("letter carrier", letter=letter)

    before = await server.breath(mailbox=True, mailbox_limit=10)
    result = await server.seal_letter(1, sealed=1)
    hidden = await server.breath(mailbox=True, mailbox_limit=10)
    included = await server.breath(mailbox=True, mailbox_limit=10, include_sealed=True)

    assert letter in before
    assert "letter_id:1 sealed" in result
    assert letter not in hidden
    assert letter in included
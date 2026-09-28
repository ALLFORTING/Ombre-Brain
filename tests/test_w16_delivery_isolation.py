import importlib
import sys
from datetime import date
from unittest.mock import AsyncMock

import pytest


TEST_TAGS = (["test", "audit"], "audit, test")


@pytest.fixture
def server(tmp_path, monkeypatch):
    monkeypatch.setenv("OMBRE_BUCKETS_DIR", str(tmp_path / "buckets"))
    monkeypatch.setenv("OMBRE_RM_RUNTIME_ENABLED", "0")
    monkeypatch.setenv("OMBRE_RM_HOOK_SKIP", "1")
    monkeypatch.delenv("OMBRE_API_KEY", raising=False)
    monkeypatch.delenv("OMBRE_EMBEDDING_API_KEY", raising=False)
    sys.modules.pop("server", None)
    module = importlib.import_module("server")
    module.decay_engine.ensure_started = AsyncMock(return_value=None)
    module.embedding_engine.enabled = False
    module.dehydrator.dehydrate = AsyncMock(
        side_effect=lambda content, metadata=None, **kwargs: content[:120]
    )
    return module


@pytest.mark.asyncio
@pytest.mark.parametrize("tags", TEST_TAGS)
@pytest.mark.parametrize("profile", ["talk", "code", "tg"])
async def test_boot_excludes_test_candidates_before_all_sections(server, tags, profile):
    await server.boot(profile=profile)
    hidden_id = await server.bucket_mgr.create(
        "excluded startup body", name="excluded startup", tags=tags,
        pinned=True, todos=["excluded todo"],
    )
    visible_id = await server.bucket_mgr.create(
        "ordinary startup body", name="ordinary startup", tags=["latest"],
        pinned=True, todos=["ordinary todo"],
    )
    for bucket_id in (hidden_id, visible_id):
        await server.bucket_mgr.update(bucket_id, trigger_date=date.today().isoformat())
    hidden_session = await server.bucket_mgr.create(
        "excluded session body", name="excluded session", tags=tags,
        domain=["session", "工程"], importance=9,
    )
    visible_session = await server.bucket_mgr.create(
        "ordinary session body", name="ordinary session", tags=["contest"],
        domain=["session", "工程"], importance=9,
    )
    for bucket_id in (hidden_session, visible_session):
        await server.bucket_mgr.archive(bucket_id)
    hidden_feel = await server.bucket_mgr.create(
        "excluded feel body", name="excluded feel", tags=tags, bucket_type="feel",
    )
    visible_feel = await server.bucket_mgr.create(
        "ordinary feel body", name="ordinary feel", tags=["Test"], bucket_type="feel",
    )

    result = await server.boot(profile=profile)

    for value in (hidden_id, hidden_session, hidden_feel, "excluded startup", "excluded todo",
                  "excluded session", "excluded feel"):
        assert value not in result
    assert visible_id in result
    assert "ordinary todo" in result
    config = server.BOOT_PROFILE_CONFIG[profile]
    if config["include_sessions"]:
        assert visible_session in result
    if config["include_echo"]:
        assert visible_feel in result
    assert "ordinary startup body" in result
    assert (await server.bucket_mgr.get(hidden_id))["metadata"]["trigger_last_seen"] == ""
    assert (await server.bucket_mgr.get(visible_id))["metadata"]["trigger_last_seen"] == date.today().isoformat()
    server.decay_engine.ensure_started.assert_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("tags", TEST_TAGS)
@pytest.mark.parametrize("include_provenance", [False, True])
async def test_todos_excludes_exact_test_tag_and_preserves_other_tags(server, tags, include_provenance):
    hidden_id = await server.bucket_mgr.create(
        "excluded carrier", name="excluded carrier", tags=tags, todos=["excluded task"],
    )
    visible_ids = []
    for index, other_tags in enumerate((["Test"], ["latest"], ["test/demo"], ["ｔｅｓｔ"], "contest,Test")):
        visible_ids.append(await server.bucket_mgr.create(
            "test in body", name=f"test in name {index}", tags=other_tags,
            todos=[f"ordinary task {index}"],
        ))

    result = await server.todos(include_provenance=include_provenance)

    assert hidden_id not in result
    assert "excluded task" not in result
    for index, bucket_id in enumerate(visible_ids):
        assert bucket_id in result
        assert f"ordinary task {index}" in result


@pytest.mark.asyncio
@pytest.mark.parametrize("tags", TEST_TAGS)
async def test_echo_excludes_test_feels_without_hiding_feel_retrieval(server, tags):
    hidden_id = await server.bucket_mgr.create(
        "excluded feel needle", name="excluded feel", tags=tags, bucket_type="feel",
    )
    hidden = await server.bucket_mgr.get(hidden_id)
    assert "暂无可见 feel" in server._format_feel_echo([hidden])
    visible_id = await server.bucket_mgr.create(
        "test substring remains a normal echo", name="ordinary feel", tags=["latest"],
        bucket_type="feel",
    )
    visible = await server.bucket_mgr.get(visible_id)

    echo = server._format_feel_echo([hidden, visible])
    retrieval = await server.breath(feels=True, query="excluded feel needle", touch=False)

    assert hidden_id not in echo
    assert visible_id in echo
    assert "test substring remains a normal echo" in echo
    assert hidden_id in retrieval


@pytest.mark.asyncio
@pytest.mark.parametrize("tags", TEST_TAGS)
async def test_boot_mailbox_filters_before_limit_without_global_letter_filter(server, tags):
    hidden_session = await server.bucket_mgr.create(
        "excluded session", name="excluded session", domain=["session"], tags=tags,
    )
    visible_session = await server.bucket_mgr.create(
        "ordinary session", name="ordinary session", domain=["session"], tags=["contest"],
    )
    await server.bucket_mgr.archive(hidden_session)
    await server.bucket_mgr.archive(visible_session)
    server.bucket_mgr.record_letter("older ordinary letter", visible_session)
    server.bucket_mgr.record_letter("latest eligible ordinary letter", visible_session)
    for index in range(55):
        server.bucket_mgr.record_letter(f"excluded handoff {index}", hidden_session)
    server.bucket_mgr.record_letter("newer sealed ordinary letter", visible_session, sealed=True)

    result = await server.boot()

    assert "latest eligible ordinary letter" in result
    assert "older ordinary letter" not in result
    assert "excluded handoff" not in result
    assert "newer sealed ordinary letter" not in result
    assert server.bucket_mgr.get_letters(1)[0]["content"] == "excluded handoff 54"
    assert "excluded handoff 54" in await server.breath(mailbox=True)
    assert "newer sealed ordinary letter" in await server.breath(mailbox=True, include_sealed=True)


@pytest.mark.asyncio
async def test_boot_mailbox_empty_when_all_test_and_accepts_unlinked_letter(server):
    session_id = await server.bucket_mgr.create("session", domain=["session"], tags=["test"])
    server.bucket_mgr.record_letter("excluded only letter", session_id)
    result = await server.boot()
    assert "暂无信件" in result
    assert "excluded only letter" not in result
    server.bucket_mgr.record_letter("unlinked ordinary letter", "missing-session")
    assert "unlinked ordinary letter" in await server.boot()


@pytest.mark.asyncio
@pytest.mark.parametrize("tags", TEST_TAGS)
async def test_test_buckets_remain_visible_in_other_tools_and_history(server, tags):
    bucket_id = await server.bucket_mgr.create(
        "test retrieval body", name="test retrieval name", tags=tags,
    )
    assert bucket_id in await server.dream(detail_ids=bucket_id)
    assert bucket_id in await server.dream()
    assert bucket_id in await server.breath(query="test retrieval", touch=False)
    assert bucket_id in await server.breath(tags_filter=["test"], touch=False)
    assert bucket_id in await server.pulse(show_all=True, touch=False)
    await server.bucket_mgr.update(bucket_id, content="updated test retrieval body")
    assert any("test retrieval body" in entry["old_content"] for entry in server.bucket_mgr.get_history(bucket_id))

import importlib
import sys
from unittest.mock import AsyncMock

import pytest


def _load_server(tmp_path, monkeypatch):
    monkeypatch.setenv("OMBRE_BUCKETS_DIR", str(tmp_path / "buckets"))
    monkeypatch.delenv("OMBRE_API_KEY", raising=False)
    monkeypatch.delenv("OMBRE_CONFLICT_DETECTION_ENABLED", raising=False)
    sys.modules.pop("server", None)
    server = importlib.import_module("server")
    server.decay_engine.ensure_started = AsyncMock(return_value=None)
    server.dehydrator.analyze = AsyncMock(
        return_value={
            "domain": ["test"],
            "valence": 0.5,
            "arousal": 0.3,
            "tags": ["conflict-test"],
            "suggested_name": "conflict-test",
        }
    )
    server.dehydrator.digest = AsyncMock(
        return_value=[
            {
                "name": "digest-item",
                "content": "new item content",
                "domain": ["test"],
                "valence": 0.5,
                "arousal": 0.3,
                "tags": ["conflict-test"],
                "importance": 5,
            }
        ]
    )
    return server


@pytest.mark.asyncio
async def test_hold_appends_conflict_warning(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    server._detect_conflict_warning = AsyncMock(return_value="bucket abc has conflicting date")

    result = await server.hold("new conflicting content")

    assert "conflict: bucket abc has conflicting date" in result


@pytest.mark.asyncio
async def test_hold_preserves_return_when_no_conflict(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    server._detect_conflict_warning = AsyncMock(return_value="")

    result = await server.hold("ordinary content")

    assert "conflict:" not in result


@pytest.mark.asyncio
async def test_hold_explicit_supersession_replaces_bucket_in_place(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    old_content = "fact_evolution_old_only_token: invoice due on July 6."
    replacement = "fact_evolution_new_only_token: invoice due on July 9."
    target_id = await server.bucket_mgr.create(content=old_content)
    before = await server.bucket_mgr.list_all(include_archive=False)
    server._detect_conflict_warning = AsyncMock(return_value="")

    result = await server.hold(replacement, supersedes_id=f"  {target_id}  ")

    after = await server.bucket_mgr.list_all(include_archive=False)
    target = await server.bucket_mgr.get(target_id)
    history = server.bucket_mgr.get_history(target_id)
    replacement_matches = await server.bucket_mgr.search(
        "fact_evolution_new_only_token", limit=10, include_sealed=False
    )
    old_matches = await server.bucket_mgr.search(
        "fact_evolution_old_only_token", limit=10, include_sealed=False
    )

    assert target_id in result
    assert len(after) == len(before)
    assert target["id"] == target_id
    assert target["content"] == replacement
    assert history[0]["old_content"] == old_content
    assert any(bucket["id"] == target_id for bucket in replacement_matches)
    assert not any(bucket["id"] == target_id for bucket in old_matches)
    server._detect_conflict_warning.assert_not_awaited()
    server.dehydrator.analyze.assert_not_awaited()


@pytest.mark.asyncio
async def test_hold_invalid_supersession_target_does_not_create_bucket(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    before = await server.bucket_mgr.list_all(include_archive=False)

    result = await server.hold(
        "fact_evolution_should_not_be_written", supersedes_id="missing-target"
    )

    after = await server.bucket_mgr.list_all(include_archive=False)
    assert len(after) == len(before)
    assert "not found or invalid" in result
    assert "missing-target" in result


@pytest.mark.asyncio
async def test_hold_without_supersedes_uses_normal_merge_path(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    server._detect_conflict_warning = AsyncMock(return_value="")
    server._merge_or_create = AsyncMock(return_value=("normal-path-id", False))

    result = await server.hold("fact_evolution_normal_path")

    server._merge_or_create.assert_awaited_once()
    assert "normal-path-id" in result


@pytest.mark.asyncio
async def test_grow_short_path_appends_conflict_warning(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    server._detect_conflict_warning = AsyncMock(return_value="bucket xyz has conflicting number")

    result = await server.grow("short conflict")

    assert "conflict: bucket xyz has conflicting number" in result


@pytest.mark.asyncio
async def test_grow_digest_path_appends_conflict_warning(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    server._detect_conflict_warning = AsyncMock(return_value="bucket old has conflicting fact")

    result = await server.grow("this is a longer diary entry that should use the digest path")

    assert "conflict: digest-item: bucket old has conflicting fact" in result


@pytest.mark.asyncio
async def test_conflict_detection_uses_lexical_fallback_candidates(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    old_id = await server.bucket_mgr.create(
        content="codex_conflict_marker Alpha invoice due date is 2026-07-06.",
        pinned=True,
    )
    server.bucket_mgr.search = AsyncMock(return_value=[])
    captured = {}

    async def fake_call(new_content, old_buckets):
        captured["old_ids"] = [bucket["id"] for bucket in old_buckets]
        return "bucket has conflicting date"

    server._call_conflict_api = fake_call

    warning = await server._detect_conflict_warning(
        "codex_conflict_marker Alpha invoice due date is 2026-07-01."
    )

    assert warning == "bucket has conflicting date"
    assert old_id in captured["old_ids"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "old_content,new_content",
    [
        (
            "Opus hallucination safeguards were recorded in 2026.",
            "The studio was founded in 2026.",
        ),
        (
            "Opus 5 幻觉与硬规则记录于 2026.09.12",
            "工作室建于 2026.09.12",
        ),
    ],
    ids=["shared-year-only", "real-shared-full-date-only"],
)
async def test_conflict_candidates_reject_time_only_overlap(
    tmp_path,
    monkeypatch,
    old_content,
    new_content,
):
    server = _load_server(tmp_path, monkeypatch)
    old_bucket = {
        "id": "e24f331b163e",
        "content": old_content,
        "metadata": {"name": "unrelated old event", "tags": []},
    }
    server.bucket_mgr.search = AsyncMock(return_value=[old_bucket])
    server.bucket_mgr.list_all = AsyncMock(return_value=[old_bucket])
    server._call_conflict_api = AsyncMock(return_value="")

    candidates = await server._conflict_candidate_buckets(new_content)
    warning = await server._detect_conflict_warning(new_content)

    assert candidates == []
    assert warning == ""
    server._call_conflict_api.assert_awaited_once_with(new_content, [])


@pytest.mark.asyncio
async def test_conflict_prompt_rejects_different_subjects_and_uncertainty(
    tmp_path,
    monkeypatch,
):
    server = _load_server(tmp_path, monkeypatch)
    monkeypatch.setattr(
        server,
        "_digest_api_config",
        lambda: ("test-key", "https://example.invalid/v1", "test-model"),
    )
    captured = {}

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"choices": [{"message": {"content": "无"}}]}

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, url, *, headers, json):
            captured["prompt"] = json["messages"][1]["content"]
            return FakeResponse()

    monkeypatch.setattr(server.httpx, "AsyncClient", lambda timeout: FakeClient())
    old_bucket = {
        "id": "different-subject-old",
        "content": "Alice invoice status is open on 2026.09.12.",
        "metadata": {"name": "Alice invoice", "tags": []},
    }
    server.bucket_mgr.search = AsyncMock(return_value=[old_bucket])
    server.bucket_mgr.list_all = AsyncMock(return_value=[old_bucket])

    warning = await server._detect_conflict_warning(
        "Bob invoice status is closed on 2026.09.12."
    )

    assert warning == ""
    assert "同一天发生的不同事件不构成矛盾" in captured["prompt"]
    assert "同一主体、同一事实槽位" in captured["prompt"]
    assert "任何不确定，一律只回答“无”" in captured["prompt"]


@pytest.mark.asyncio
async def test_conflict_detection_has_default_on_independent_switch(
    tmp_path,
    monkeypatch,
):
    server = _load_server(tmp_path, monkeypatch)

    assert server._conflict_detection_enabled() is True

    monkeypatch.setenv("OMBRE_CONFLICT_DETECTION_ENABLED", "false")
    server._conflict_candidate_buckets = AsyncMock(return_value=[])
    server._call_conflict_api = AsyncMock(return_value="conflict")

    warning = await server._detect_conflict_warning("new content")

    assert warning == ""
    server._conflict_candidate_buckets.assert_not_awaited()
    server._call_conflict_api.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "old_content,new_content,target_type,expected_reuse",
    [
        ("Ting prefers tea", " ting \n prefers tea ", "dynamic", True),
        ("Ting planned a Kyoto trip on June 1", "Ting planned a Kyoto trip on September 1", "dynamic", False),
        ("Ting attended a workshop", "Ting received a promotion", "dynamic", False),
        ("Ting plans to visit Kyoto", "Ting visited Kyoto", "dynamic", False),
        ("Ting lives in Beijing", "Ting lives in Shanghai", "dynamic", False),
        ("Ting likes tea", "Ting does not like tea", "dynamic", False),
        ("Alice likes tea", "Bob likes tea", "dynamic", False),
        ("Ting prefers tea", "Ting prefers coffee", "dynamic", False),
        ("Ting prefers tea", "ting prefers tea", "feel", False),
    ],
    ids=[
        "exact-normalized-duplicate",
        "same-topic-different-date",
        "same-person-different-event",
        "plan-vs-result",
        "old-vs-new-state",
        "positive-vs-negation",
        "different-entities",
        "high-score-alone",
        "feel-target-isolated",
    ],
)
async def test_automatic_merge_requires_a_deterministic_non_feel_duplicate(
    tmp_path,
    monkeypatch,
    old_content,
    new_content,
    target_type,
    expected_reuse,
):
    server = _load_server(tmp_path, monkeypatch)
    server.embedding_engine.enabled = False
    server.bucket_mgr.embedding_engine.enabled = False
    server.bucket_mgr.search = AsyncMock(
        return_value=[
            {
                "id": "existing-id",
                "score": 100,
                "content": old_content,
                "metadata": {
                    "type": target_type,
                    "name": "existing-name",
                    "pinned": False,
                    "protected": False,
                },
            }
        ]
    )
    server.dehydrator.merge = AsyncMock(
        side_effect=AssertionError("automatic LLM merge must not run")
    )

    result_id, reused = await server._merge_or_create(
        new_content,
        [],
        5,
        ["test"],
        0.5,
        0.3,
    )

    assert reused is expected_reuse
    if expected_reuse:
        assert result_id == "existing-name"
    else:
        assert result_id != "existing-id"
    server.dehydrator.merge.assert_not_awaited()


@pytest.mark.asyncio
async def test_feel_write_does_not_merge_into_a_factual_bucket(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    server.embedding_engine.enabled = False
    server.bucket_mgr.embedding_engine.enabled = False
    factual_id = await server.bucket_mgr.create("same factual content")
    server._detect_conflict_warning = AsyncMock(return_value="")
    server._merge_or_create = AsyncMock(
        side_effect=AssertionError("feel writes must bypass factual merge")
    )

    result = await server.hold("same factual content", feel=True)

    assert result.startswith("🫧feel→")
    buckets = await server.bucket_mgr.list_all(include_archive=False)
    assert len(buckets) == 2
    assert (await server.bucket_mgr.get(factual_id))["content"] == "same factual content"
    server._merge_or_create.assert_not_awaited()


@pytest.mark.asyncio
async def test_repeated_ingestion_baseline_suppresses_noise_without_collapsing_facts(
    tmp_path, monkeypatch
):
    server = _load_server(tmp_path, monkeypatch)
    server.embedding_engine.enabled = False
    server.bucket_mgr.embedding_engine.enabled = False

    facts = (
        "Ting planned a Kyoto trip on June 1",
        "Ting planned a Kyoto trip on September 1",
        "Ting canceled the Kyoto trip after a visa issue",
    )
    sequence = [facts[0], facts[0], facts[1], facts[1], facts[2]]
    outcomes = []

    for content in sequence:
        _, reused = await server._merge_or_create(
            content,
            [],
            5,
            ["test"],
            0.5,
            0.3,
        )
        outcomes.append(reused)

    buckets = await server.bucket_mgr.list_all(include_archive=False)
    contents = {bucket["content"] for bucket in buckets}

    assert outcomes == [False, True, False, True, False]
    assert len(buckets) == len(facts)
    assert contents == set(facts)

import importlib
import sys
from datetime import datetime, timedelta
from unittest.mock import AsyncMock

import pytest


def _freeze_day(server, monkeypatch, day):
    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            value = cls.fromisoformat(day + "T12:00:00")
            return value if tz is None else value.replace(tzinfo=tz)
    monkeypatch.setattr(server, "datetime", FrozenDateTime)


def _raw_trigger(server, bucket_id):
    import frontmatter
    return frontmatter.load(server.bucket_mgr._find_bucket_file(bucket_id))


def _history_counts(server):
    import sqlite3
    with sqlite3.connect(server.bucket_mgr.history_db_path) as conn:
        return tuple(conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                     for table in ("bucket_history", "bucket_revisions"))


@pytest.mark.parametrize("kwargs,overrides", [
    ({}, {}),
    ({"bucket_type": "feel"}, {"type": "feel", "domain": []}),
    ({"bucket_type": "archive"}, {"type": "archive"}),
    ({"pinned": True}, {"type": "permanent", "importance": 10, "pinned": True}),
    ({"protected": True}, {"importance": 10, "protected": True}),
    ({"provenance_kind": "summary"}, {"provenance_kind": "summary"}),
])
def test_plain_bucket_construction_preserves_full_pre_repeat_metadata(kwargs, overrides):
    from bucket_manager import BucketManager
    stamp, day = "2026-10-08T00:00:00", "2026-10-08"
    post = BucketManager._build_bucket_post(
        "fixture-default", "plain default body", created=stamp,
        last_active=stamp, created_date=day, _aliases=[], **kwargs)
    expected = {
        "id": "fixture-default", "name": "fixture-default", "tags": [], "domain": ["未分类"],
        "valence": 0.5, "arousal": 0.3, "importance": 5, "type": "dynamic",
        "created": stamp, "last_active": stamp, "created_at": day, "updated_at": day,
        "emotion_history": "[]", "related_buckets": "", "source_bucket": "",
        "trigger_date": "", "trigger_last_seen": "", "dormant": False, "sealed": 0,
        "activation_count": 0, "todos": [],
    }
    expected.update(overrides)
    assert post.metadata == expected
    assert post.content == "plain default body"


def test_c2_pure_fixture_serialization_preserves_frozen_pre_repeat_hashes(monkeypatch):
    import hashlib
    from pathlib import Path
    import frontmatter
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "deploy/lsf-zeabur"))
    import c2_seed
    # Frozen at the pre-PR125 builder with day=2026-10-08. This test needs
    # neither Git history nor a volume/initializer; only pure definitions.
    expected = {
        "c20000000001": ("dynamic/c2-v1/C2-NAME-EXACT_c20000000001.md", "63004518742823089df3b68b771bd29504778253e9c50e1e5e3cfdcb29d545f4"),
        "c20000000002": ("dynamic/c2-v1/C2-NAME-EXACT-NEAR_c20000000002.md", "2630373d6c45adf9e7dacc2fe60d055fc4c7af20613cb31c6bc40028dac724e2"),
        "c20000000003": ("dynamic/c2-v1/C2-TAG-TODAY_c20000000003.md", "7e24f25b6891bc952ce8b86107db9fcbf7019491d56389c6d75ba62a99dac0b0"),
        "c20000000004": ("dynamic/c2-v1/C2-BODY-LOW_c20000000004.md", "f90c3c8837a5ce1dd62555bb2ee41e94f1c1e62b8682ea7f67ac18bec945454c"),
        "c20000000005": ("dynamic/c2-v1/C2-TAG-YESTERDAY_c20000000005.md", "692248369cff7c25eb5c5d8f62b0d7d8fd58c623cf569ff0135928dd77d7e845"),
        "c20000000006": ("dynamic/c2-v1/C2-OTHER-DOMAIN_c20000000006.md", "1b0a0ab91377ddd6be1a78a66d5a3bba543c9755f9234e96a757b4a87e49efcb"),
        "c20000000007": ("dynamic/c2-v1/C2-DORMANT_c20000000007.md", "e51c8d2ffeeeb2841b614b844749b77ad695683054c7614321e2518646c95303"),
        "c20000000008": ("dynamic/c2-v1/C2-SEALED_c20000000008.md", "205b0ed25e82138aa2e0924d23961760e552a7b7fdcda1a9f728bdc57ac595ed"),
        "c20000000009": ("dynamic/c2-v1/C2-COMPRESSED_c20000000009.md", "c3a367af86dd3291b3bb2e2c98f46c7186e9a50738a5ac8437b1c7a9e09fa5bd"),
        "c20000000010": ("dynamic/c2-v1/C2-OLD_c20000000010.md", "df34b7496ae80f5c8e24407d77e811dad4f0b6fccf21359328af1dbf50667e1b"),
        "c20000000011": ("dynamic/c2-v1/C2-SUCCESSOR_c20000000011.md", "ff8594a7b0f3a5445d3002dc4ff8b1701b1bf5d322441d5f4bef426edd204885"),
        "c20000000012": ("feel/c2-v1/C2-FEEL_c20000000012.md", "88e1695c945ffda2b2c38bc647c99b36e5f1d8009969aa233b8e8c9f52e77071"),
        "c20000000013": ("archive/c2-v1/C2-SESSION_c20000000013.md", "d1c31ed7fae05329fbf4414053ebf7efee923bffe6edcbad8e36ec87d07469d5"),
        "c20000000014": ("permanent/c2-v1/C2-PIN_c20000000014.md", "c0496df313f312b7dbd409e1e66859f186e575a5cbd0c110aac72f22e7b14024"),
        "c20000000015": ("dynamic/c2-v1/C2-HISTORY_c20000000015.md", "56f31ad36c8a0329ec2da55a46c6064526e1a045afc01eb998b20ea3f02f62d4"),
        "c20000000016": ("dynamic/c2-v1/C2-PAGE-16_c20000000016.md", "c45f6cdc30d27f5eca3dafcc8d6866f6483e28dd1d35903dd184c37433097c50"),
        "c20000000017": ("dynamic/c2-v1/C2-PAGE-17_c20000000017.md", "1b13d9046ba0cf2bfb83d3245cb610411dad1f9869b2ba81ab275652c325789b"),
        "c20000000018": ("dynamic/c2-v1/C2-PAGE-18_c20000000018.md", "8aca013a6d3a5da1862f8e439c3dba3142a16b5a7e7aeeabff2cbfab75d3193b"),
        "c20000000019": ("dynamic/c2-v1/C2-PAGE-19_c20000000019.md", "02fd4da8876c7c1f892d8b741f4b4d7c1519bd30c87bc27a79dea0cc86d1f66b"),
        "c20000000020": ("dynamic/c2-v1/C2-PAGE-20_c20000000020.md", "b72734e6272e1956a53483af60b0c01b34198ca93b2a526b49bca219ae0dd851"),
        "c20000000021": ("dynamic/c2-v1/C2-PAGE-LONG_c20000000021.md", "af7ab2ac8eb473d8bd064be5d4719094799501ec9fdd0ffe6c901cf731678367"),
    }
    rows = c2_seed.definitions("2026-10-08")
    actual = {post["id"]: (path.as_posix(), hashlib.sha256(frontmatter.dumps(post).encode()).hexdigest())
              for path, post in rows}
    assert len(rows) == len(actual) == 21
    assert actual == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("entry", ["create", "hold", "feel", "pinned", "keyed-hold", "keyed-feel", "keyed-pinned"])
async def test_no_reminder_entrypoints_persist_only_original_trigger_fields(tmp_path, monkeypatch, entry):
    server = _load_server(tmp_path, monkeypatch)
    if entry == "create":
        bucket_id = await server.bucket_mgr.create("plain bucket entry")
    else:
        extra = {"operation_id": "plain-hold-entry"} if entry.startswith("keyed-") else {}
        if entry.endswith("feel"):
            extra["feel"] = True
        if entry.endswith("pinned"):
            extra["pinned"] = True
        result = await server.hold("plain bucket entry", **extra)
        buckets = await server.bucket_mgr.list_all()
        assert len(buckets) == 1, result
        bucket_id = buckets[0]["id"]
    raw = _raw_trigger(server, bucket_id)
    assert {key: value for key, value in raw.metadata.items() if key.startswith("trigger_")} == {
        "trigger_date": "", "trigger_last_seen": ""}
    loaded = await server.bucket_mgr.get(bucket_id)
    assert loaded["metadata"]["trigger_repeat"] == "once"  # Read default remains available.
    assert "trigger_repeat" not in _raw_trigger(server, bucket_id).metadata


@pytest.mark.asyncio
async def test_read_default_does_not_leak_through_edits_or_restore(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    bucket_id = await server.bucket_mgr.create("plain original body")
    assert (await server.bucket_mgr.get(bucket_id))["metadata"]["trigger_repeat"] == "once"
    await server.trace(bucket_id, name="plain renamed bucket")
    await server.trace(bucket_id, content="plain changed body")
    ref = next(row["ref"] for row in server.bucket_mgr.list_bucket_revisions(bucket_id) if row["op_kind"] == "replace")
    plan = server.bucket_mgr.plan_restore(bucket_id, ref)
    assert {key: value for key, value in plan["metadata"].items() if key.startswith("trigger_")} == {
        "trigger_date": "", "trigger_last_seen": ""}
    monkeypatch.setattr(server.bucket_mgr, "_refresh_ordinary_embedding_best_effort", AsyncMock())
    await server.bucket_mgr.execute_restore(plan, actor="test")
    await server.boot()
    assert {key: value for key, value in _raw_trigger(server, bucket_id).metadata.items() if key.startswith("trigger_")} == {
        "trigger_date": "", "trigger_last_seen": ""}


@pytest.mark.asyncio
async def test_once_three_day_window_expiry_receipt_and_search(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    _freeze_day(server, monkeypatch, "2026-10-17")
    await server.hold("unique expiry search phrase", trigger_date="2026-10-15")
    bucket_id = (await server.bucket_mgr.list_all())[0]["id"]
    before = _raw_trigger(server, bucket_id)
    counts = _history_counts(server)
    assert bucket_id in await server.boot()  # D+2, the third day
    _freeze_day(server, monkeypatch, "2026-10-18")
    result = await server.boot()
    assert f"已自动收起 1 条过期提醒：[bucket_id:{bucket_id}]" in result
    after = _raw_trigger(server, bucket_id)
    assert after["trigger_expired_at"] == "2026-10-18"
    assert after["trigger_expiry_notified_at"] == "2026-10-18"
    assert "已自动收起" not in await server.boot()
    assert bucket_id in await server.breath(query="unique expiry search phrase", touch=False)
    from bucket_manager import TRIGGER_FIELDS
    assert {k:v for k,v in after.metadata.items() if k not in TRIGGER_FIELDS} == {
        k:v for k,v in before.metadata.items() if k not in TRIGGER_FIELDS}
    assert after.content == before.content
    assert _history_counts(server) == counts


@pytest.mark.asyncio
@pytest.mark.parametrize("repeat,expected", [
    ("once", "2026-10-15"), ("daily", "2026-10-17"), ("weekly", "2026-10-22"),
    ("monthly", "2026-11-15"), ("yearly", "2027-10-15"),
])
async def test_five_frequencies_advance_only_once_after_display(tmp_path, monkeypatch, repeat, expected):
    server = _load_server(tmp_path, monkeypatch)
    _freeze_day(server, monkeypatch, "2026-10-16")
    await server.hold("frequency reminder", trigger_date="2026-10-15", trigger_repeat=repeat)
    bucket_id = (await server.bucket_mgr.list_all())[0]["id"]
    before, counts = _raw_trigger(server, bucket_id), _history_counts(server)
    assert bucket_id in await server.boot()
    meta = _raw_trigger(server, bucket_id).metadata
    assert meta["trigger_date"] == expected
    assert meta["trigger_anchor"] == "2026-10-15"
    assert meta["trigger_last_seen"] == "2026-10-16"
    assert bucket_id not in await server.boot()
    assert meta["last_active"] == before["last_active"]
    assert meta["updated_at"] == before["updated_at"]
    assert _history_counts(server) == counts


@pytest.mark.asyncio
@pytest.mark.parametrize("repeat,initial,days,expected", [
    ("monthly", "2026-01-31", ["2026-01-31", "2026-02-28"], ["2026-02-28", "2026-03-31"]),
    ("yearly", "2024-02-29", ["2024-02-29", "2025-02-28", "2026-02-28", "2027-02-28"],
     ["2025-02-28", "2026-02-28", "2027-02-28", "2028-02-29"]),
])
async def test_calendar_anchor_survives_clamping(tmp_path, monkeypatch, repeat, initial, days, expected):
    server = _load_server(tmp_path, monkeypatch)
    await server.hold("calendar anchor", trigger_date=initial, trigger_repeat=repeat)
    bucket_id = (await server.bucket_mgr.list_all())[0]["id"]
    for day, next_date in zip(days, expected):
        _freeze_day(server, monkeypatch, day)
        assert bucket_id in await server.boot()
        meta = _raw_trigger(server, bucket_id).metadata
        assert (meta["trigger_date"], meta["trigger_anchor"]) == (next_date, initial)


@pytest.mark.asyncio
@pytest.mark.parametrize("repeat,expected", [
    ("daily", "2026-10-19"), ("weekly", "2026-10-22"),
    ("monthly", "2026-11-15"), ("yearly", "2027-10-15"),
])
async def test_missed_window_skips_without_display_or_expiry(tmp_path, monkeypatch, repeat, expected):
    server = _load_server(tmp_path, monkeypatch)
    _freeze_day(server, monkeypatch, "2026-10-18")
    await server.hold("missed recurring reminder", trigger_date="2026-10-15", trigger_repeat=repeat)
    bucket_id = (await server.bucket_mgr.list_all())[0]["id"]
    before, counts = _raw_trigger(server, bucket_id), _history_counts(server)
    result = await server.boot()
    assert bucket_id not in result and "已自动收起" not in result
    after = _raw_trigger(server, bucket_id)
    assert after["trigger_date"] == expected
    assert not after.get("trigger_expired_at") and not after.get("trigger_expiry_notified_at")
    assert after["last_active"] == before["last_active"]
    assert after["updated_at"] == before["updated_at"]
    assert after.content == before.content and _history_counts(server) == counts


@pytest.mark.asyncio
@pytest.mark.parametrize("keyed", [False, True])
async def test_trace_frequency_only_reset_and_clear(tmp_path, monkeypatch, keyed):
    server = _load_server(tmp_path, monkeypatch)
    bucket_id = await server.bucket_mgr.create("trace schedule changes")
    sequence = iter(range(20))
    async def edit(**fields):
        extra = {"operation_id": f"trace-trigger-{next(sequence)}"} if keyed else {}
        return await server.trace(bucket_id, **fields, **extra)
    await edit(trigger_repeat="monthly")  # No date is legal; no guessed date.
    assert _raw_trigger(server, bucket_id)["trigger_repeat"] == "monthly"
    assert not _raw_trigger(server, bucket_id).get("trigger_date")
    await edit(trigger_date="2026-01-31")
    await server.bucket_mgr.update(bucket_id, trigger_last_seen="2026-02-01",
        trigger_expired_at="2026-02-01", trigger_expiry_notified_at="2026-02-01")
    await edit(name="unrelated metadata edit")
    assert _raw_trigger(server, bucket_id)["trigger_repeat"] == "monthly"
    await edit(trigger_repeat="yearly")
    meta = _raw_trigger(server, bucket_id).metadata
    assert (meta["trigger_date"], meta["trigger_anchor"], meta["trigger_last_seen"]) == (
        "2026-01-31", "2026-01-31", "2026-02-01")
    assert not meta.get("trigger_expired_at") and not meta.get("trigger_expiry_notified_at")
    await edit(trigger_date="2026-01-31")  # Same date is an explicit re-arm.
    assert not _raw_trigger(server, bucket_id).get("trigger_last_seen")
    await edit(trigger_date="none", trigger_repeat="daily")
    meta = _raw_trigger(server, bucket_id).metadata
    assert not meta["trigger_date"] and not meta["trigger_last_seen"]
    assert all(field not in meta for field in ("trigger_repeat", "trigger_anchor", "trigger_expired_at", "trigger_expiry_notified_at"))


@pytest.mark.asyncio
@pytest.mark.parametrize("value", ["", "hourly", "DAILY", " daily ", None, 1, []])
@pytest.mark.parametrize("keyed", [False, True])
async def test_invalid_repeat_rejected_before_any_write(tmp_path, monkeypatch, value, keyed):
    server = _load_server(tmp_path, monkeypatch)
    extra = {"operation_id": "invalid-repeat"} if keyed else {}
    result = await server.hold("must not be written", trigger_repeat=value, **extra)
    assert "trigger_repeat must be" in result
    assert await server.bucket_mgr.list_all() == []
    bucket_id = await server.bucket_mgr.create("unchanged invalid trace")
    path = server.bucket_mgr._find_bucket_file(bucket_id)
    from pathlib import Path
    before = Path(path).read_bytes()
    result = await server.trace(bucket_id, trigger_date="none", trigger_repeat=value, **extra)
    assert "trigger_repeat must be" in result
    assert Path(path).read_bytes() == before


@pytest.mark.asyncio
@pytest.mark.parametrize("keyed", [False, True])
@pytest.mark.parametrize("mode", ["ordinary", "feel", "pinned"])
async def test_hold_reachable_modes_preserve_repeat_and_retry(tmp_path, monkeypatch, keyed, mode):
    server = _load_server(tmp_path, monkeypatch)
    fields = {"feel": True} if mode == "feel" else {"pinned": True} if mode == "pinned" else {}
    if keyed:
        fields["operation_id"] = f"hold-trigger-{mode}"
    result = await server.hold("hold schedule modes", trigger_date="2026-10-15", trigger_repeat="monthly", **fields)
    buckets = await server.bucket_mgr.list_all()
    assert len(buckets) == 1, result
    bucket_id = buckets[0]["id"]
    meta = _raw_trigger(server, bucket_id).metadata
    assert (meta["trigger_date"], meta["trigger_repeat"], meta["trigger_anchor"]) == (
        "2026-10-15", "monthly", "2026-10-15")
    if keyed:
        assert result == await server.hold("hold schedule modes", trigger_date="2026-10-15", trigger_repeat="monthly", **fields)
        assert len(await server.bucket_mgr.list_all()) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("keyed", [False, True])
async def test_duplicate_hold_omission_preserves_frequency_and_conflict_refuses(tmp_path, monkeypatch, keyed):
    server = _load_server(tmp_path, monkeypatch)
    await server.hold("duplicate schedule", trigger_date="2026-10-15", trigger_repeat="daily")
    bucket = (await server.bucket_mgr.list_all())[0]
    server.bucket_mgr.search = AsyncMock(return_value=[bucket])
    extra = {"operation_id": "duplicate-trigger-omit"} if keyed else {}
    result = await server.hold("duplicate schedule", **extra)
    assert "reused=true" in result, result
    assert _raw_trigger(server, bucket["id"])["trigger_repeat"] == "daily"
    before = _raw_trigger(server, bucket["id"])
    extra = {"operation_id": "duplicate-trigger-conflict"} if keyed else {}
    result = await server.hold("duplicate schedule", trigger_repeat="weekly", **extra)
    assert "conflicting trigger_repeat" in result
    assert _raw_trigger(server, bucket["id"]).metadata == before.metadata


@pytest.mark.asyncio
async def test_legacy_bucket_defaults_once_and_malformed_repeat_is_not_guessed(tmp_path, monkeypatch):
    import frontmatter
    server = _load_server(tmp_path, monkeypatch)
    _freeze_day(server, monkeypatch, "2026-10-18")
    ids = [await server.bucket_mgr.create(text) for text in ("legacy reminder", "corrupt reminder")]
    for index, bucket_id in enumerate(ids):
        path = server.bucket_mgr._find_bucket_file(bucket_id)
        post = frontmatter.load(path)
        post["trigger_date"] = "2020-01-01"
        if index == 0:
            post.metadata.pop("trigger_repeat", None)
        else:
            post["trigger_repeat"] = "unknown-frequency"
        frontmatter.dump(post, path)
    result = await server.boot()
    assert f"已自动收起 1 条过期提醒：[bucket_id:{ids[0]}]" in result
    assert not _raw_trigger(server, ids[1]).get("trigger_expired_at")
    assert _raw_trigger(server, ids[1])["trigger_date"] == "2020-01-01"


@pytest.mark.asyncio
async def test_repeat_wire_presence_enum_and_illegal_values(tmp_path, monkeypatch):
    from mcp.shared.memory import create_connected_server_and_client_session
    server = _load_server(tmp_path, monkeypatch)
    bucket_id = await server.bucket_mgr.create("wire trigger")
    async with create_connected_server_and_client_session(server.mcp) as client:
        tools = {tool.name: tool for tool in (await client.list_tools()).tools}
        for name in ("hold", "trace"):
            schema = tools[name].inputSchema["properties"]["trigger_repeat"]
            assert schema["enum"] == ["once", "daily", "weekly", "monthly", "yearly"]
            assert schema["default"] == "once"
            for value in (None, "", "hourly", 2):
                fields = {"content": "not written"} if name == "hold" else {"bucket_id": bucket_id, "trigger_date": "none"}
                result = await client.call_tool(name, {**fields, "trigger_repeat": value})
                assert result.isError
                assert "trigger_repeat must be" in result.content[0].text
        await client.call_tool("trace", {"bucket_id": bucket_id, "trigger_date": "2026-10-15", "trigger_repeat": "weekly"})
        await client.call_tool("trace", {"bucket_id": bucket_id, "name": "wire omission"})
        assert _raw_trigger(server, bucket_id)["trigger_repeat"] == "weekly"
        await client.call_tool("trace", {"bucket_id": bucket_id, "trigger_repeat": "once"})
        assert _raw_trigger(server, bucket_id)["trigger_repeat"] == "once"


@pytest.mark.asyncio
async def test_batch_trace_repeat_and_missing_anchor_initialization(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    ids = [await server.bucket_mgr.create(f"batch trigger {index}") for index in range(2)]
    await server.trace(",".join(ids), trigger_date="2026-10-15", trigger_repeat="weekly")
    for bucket_id in ids:
        assert _raw_trigger(server, bucket_id)["trigger_repeat"] == "weekly"
    await server.bucket_mgr.update(ids[0], trigger_anchor=None, trigger_last_seen="2026-10-15")
    await server.trace(ids[0], trigger_repeat="monthly")
    assert _raw_trigger(server, ids[0])["trigger_anchor"] == "2026-10-15"
    assert _raw_trigger(server, ids[0])["trigger_last_seen"] == "2026-10-15"
    await server.trace(",".join(ids), trigger_date="none", trigger_repeat="daily")
    assert all(not _raw_trigger(server, bucket_id).get("trigger_anchor") for bucket_id in ids)


@pytest.mark.asyncio
async def test_restore_reminder_receipts_belong_to_exact_restored_schedule(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    bucket_id = await server.bucket_mgr.create("restore original body")
    await server.trace(bucket_id, trigger_date="2026-10-15", trigger_repeat="once")
    await server.bucket_mgr.update(bucket_id, trigger_last_seen="2026-10-16",
        trigger_expired_at="2026-10-18", trigger_expiry_notified_at="2026-10-18", _skip_revision=True)
    await server.trace(bucket_id, content="restore changed body")
    ref = next(row["ref"] for row in server.bucket_mgr.list_bucket_revisions(bucket_id) if row["op_kind"] == "replace")
    same = server.bucket_mgr.plan_restore(bucket_id, ref)["metadata"]
    assert same["trigger_expiry_notified_at"] == "2026-10-18"
    await server.trace(bucket_id, trigger_date="2026-11-15", trigger_repeat="monthly")
    await server.bucket_mgr.update(bucket_id, trigger_last_seen="2026-11-15", trigger_expired_at="2026-11-18",
        trigger_expiry_notified_at="2026-11-18", _skip_revision=True)
    changed = server.bucket_mgr.plan_restore(bucket_id, ref)["metadata"]
    assert changed["trigger_date"] == "2026-10-15"
    assert changed["trigger_repeat"] == "once"
    assert all(not changed.get(field) for field in ("trigger_last_seen", "trigger_expired_at", "trigger_expiry_notified_at"))


@pytest.mark.asyncio
@pytest.mark.parametrize("keyed", [False, True])
async def test_hold_frequency_without_date_and_keyed_fingerprint(tmp_path, monkeypatch, keyed):
    server = _load_server(tmp_path, monkeypatch)
    _freeze_day(server, monkeypatch, "2026-10-18")
    extra = {"operation_id": "frequency-before-date"} if keyed else {}
    result = await server.hold("save frequency first", trigger_repeat="weekly", **extra)
    buckets = await server.bucket_mgr.list_all()
    assert len(buckets) == 1, result
    meta = _raw_trigger(server, buckets[0]["id"]).metadata
    assert meta["trigger_repeat"] == "weekly" and not meta["trigger_date"]
    assert not meta.get("trigger_anchor")
    assert "save frequency first" not in await server.boot()
    if keyed:
        assert result == await server.hold("save frequency first", trigger_repeat="weekly", **extra)
        assert await server.hold("save frequency first", trigger_repeat="daily", **extra) == "operation_id_conflict"


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["due", "expiry"])
async def test_trigger_receipt_write_failure_keeps_retry_pending(tmp_path, monkeypatch, kind):
    server = _load_server(tmp_path, monkeypatch)
    _freeze_day(server, monkeypatch, "2026-10-18")
    bucket_id = await server.bucket_mgr.create("receipt retry", name="write-failure-marker")
    await server.trace(bucket_id, trigger_date="2026-10-18" if kind == "due" else "2026-10-01",
                       trigger_repeat="daily" if kind == "due" else "once")
    original = server.bucket_mgr._write_post_atomic
    failures = []
    def fail_receipt(path, post, **kwargs):
        receipt = "trigger_last_seen" if kind == "due" else "trigger_expiry_notified_at"
        if post.get(receipt) and not failures:
            failures.append(receipt)
            raise OSError("synthetic receipt disk failure")
        return original(path, post, **kwargs)
    monkeypatch.setattr(server.bucket_mgr, "_write_post_atomic", fail_receipt)
    first = await server.boot()
    assert "write-failure-marker" in first and failures
    meta = _raw_trigger(server, bucket_id).metadata
    if kind == "due":
        assert meta["trigger_date"] == "2026-10-18" and not meta["trigger_last_seen"]
    else:
        assert meta["trigger_expired_at"] == "2026-10-18" and not meta.get("trigger_expiry_notified_at")
    assert "write-failure-marker" in await server.boot()
    meta = _raw_trigger(server, bucket_id).metadata
    if kind == "due":
        assert meta["trigger_date"] == "2026-10-19"
    else:
        assert meta["trigger_expiry_notified_at"] == "2026-10-18"


@pytest.mark.asyncio
async def test_trigger_only_cas_checks_schedule_and_file_incarnation(tmp_path, monkeypatch):
    from pathlib import Path
    server = _load_server(tmp_path, monkeypatch)
    bucket_id = await server.bucket_mgr.create("CAS incarnation")
    await server.trace(bucket_id, trigger_date="2026-10-18", trigger_repeat="daily")
    prepared = server.bucket_mgr.prepare_boot_triggers([bucket_id], "2026-10-18")
    await server.trace(bucket_id, trigger_date="2026-10-18")
    assert not server.bucket_mgr.commit_boot_triggers(prepared["states"], {bucket_id: {"trigger_date": "2026-10-19"}})
    prepared = server.bucket_mgr.prepare_boot_triggers([bucket_id], "2026-10-18")
    path = Path(server.bucket_mgr._find_bucket_file(bucket_id))
    path.write_bytes(path.read_bytes())  # Same fields, a different publication.
    assert not server.bucket_mgr.commit_boot_triggers(prepared["states"], {bucket_id: {"trigger_date": "2026-10-19"}})
    assert _raw_trigger(server, bucket_id)["trigger_date"] == "2026-10-18"


@pytest.mark.asyncio
async def test_automatic_expiry_respects_confirmed_delete_admission(tmp_path, monkeypatch):
    from bucket_manager import BucketIdempotencyError
    server = _load_server(tmp_path, monkeypatch)
    _freeze_day(server, monkeypatch, "2026-10-18")
    bucket_id = await server.bucket_mgr.create("guarded expiry")
    healthy_id = await server.bucket_mgr.create("healthy admitted reminder", name="healthy-admission-marker")
    await server.trace(bucket_id, trigger_date="2026-10-01")
    await server.trace(healthy_id, trigger_date="2026-10-18")
    before = _raw_trigger(server, bucket_id).metadata.copy()
    original = server.bucket_mgr.assert_confirmed_delete_writable
    def deny(bucket):
        if bucket == bucket_id:
            raise BucketIdempotencyError("confirmed_delete_pending")
        return original(bucket)
    monkeypatch.setattr(server.bucket_mgr, "assert_confirmed_delete_writable", deny)
    result = await server.boot()
    assert "healthy-admission-marker" in result
    assert "已自动收起" not in result
    assert _raw_trigger(server, bucket_id).metadata == before
    monkeypatch.setattr(server.bucket_mgr, "assert_confirmed_delete_writable", original)
    assert f"已自动收起 1 条过期提醒：[bucket_id:{bucket_id}]" in await server.boot()


@pytest.mark.asyncio
@pytest.mark.parametrize("repeat", ["once", "weekly"])
async def test_prepare_io_failure_isolated_to_one_bucket_and_retryable(tmp_path, monkeypatch, repeat):
    from pathlib import Path
    server = _load_server(tmp_path, monkeypatch)
    _freeze_day(server, monkeypatch, "2026-10-18")
    failed_id = await server.bucket_mgr.create("failed background transition", name="failed-transition-marker")
    healthy_id = await server.bucket_mgr.create("healthy reminder", name="healthy-transition-marker")
    await server.trace(failed_id, trigger_date="2026-10-01", trigger_repeat=repeat)
    await server.trace(healthy_id, trigger_date="2026-10-18")
    path = Path(server.bucket_mgr._find_bucket_file(failed_id))
    before, counts = path.read_bytes(), _history_counts(server)
    original, failures = server.bucket_mgr._write_post_atomic, []
    def fail_initial_transition(target, post, **kwargs):
        if post["id"] == failed_id and not failures:
            failures.append(post["id"])
            raise OSError("synthetic initial transition failure")
        return original(target, post, **kwargs)
    monkeypatch.setattr(server.bucket_mgr, "_write_post_atomic", fail_initial_transition)
    result = await server.boot()
    assert "healthy-transition-marker" in result
    assert "failed-transition-marker" not in result and "已自动收起" not in result
    assert path.read_bytes() == before and failures == [failed_id]
    assert _raw_trigger(server, healthy_id)["trigger_last_seen"] == "2026-10-18"
    followup = await server.boot()
    meta = _raw_trigger(server, failed_id).metadata
    if repeat == "once":
        assert f"已自动收起 1 条过期提醒：[bucket_id:{failed_id}]" in followup
        assert meta["trigger_expired_at"] == meta["trigger_expiry_notified_at"] == "2026-10-18"
    else:
        assert "failed-transition-marker" not in followup and "已自动收起" not in followup
        assert meta["trigger_date"] == "2026-10-22"
        assert not meta.get("trigger_expired_at") and not meta.get("trigger_last_seen")
    assert _history_counts(server) == counts


@pytest.mark.asyncio
async def test_prepare_keeps_system_maintenance_refusal_outside_bucket_catch(tmp_path, monkeypatch):
    from contextlib import contextmanager
    from types import SimpleNamespace
    from maintenance_write_gate import MaintenanceWriteError
    server = _load_server(tmp_path, monkeypatch)
    bucket_id = await server.bucket_mgr.create("system freeze must propagate")
    await server.trace(bucket_id, trigger_date="2026-10-01")
    before = _raw_trigger(server, bucket_id).metadata.copy()
    @contextmanager
    def refuse(operation):
        raise MaintenanceWriteError("maintenance_frozen")
        yield
    monkeypatch.setattr(server.bucket_mgr, "write_coordinator", SimpleNamespace(writer_scope=refuse))
    with pytest.raises(MaintenanceWriteError, match="maintenance_frozen"):
        server.bucket_mgr.prepare_boot_triggers([bucket_id], "2026-10-18")
    assert _raw_trigger(server, bucket_id).metadata == before


def _load_server(tmp_path, monkeypatch):
    monkeypatch.setenv("OMBRE_BUCKETS_DIR", str(tmp_path / "buckets"))
    monkeypatch.delenv("OMBRE_API_KEY", raising=False)
    monkeypatch.delenv("OMBRE_TRIGGER_STALE_THRESHOLD", raising=False)
    monkeypatch.delenv("OMBRE_TRIGGER_STALE_LEAD", raising=False)
    sys.modules.pop("server", None)
    server = importlib.import_module("server")
    server.decay_engine.ensure_started = AsyncMock(return_value=None)
    server._recall_memory_candidates = AsyncMock(
        return_value={"candidates": [], "semantic": {"status": "disabled"}}
    )
    server.dehydrator.analyze = AsyncMock(
        return_value={
            "domain": ["trigger-test"],
            "valence": 0.5,
            "arousal": 0.3,
            "tags": ["trigger"],
            "suggested_name": "trigger-memory",
        }
    )
    return server


@pytest.mark.asyncio
async def test_boot_surfaces_due_trigger_once_per_day(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    today = datetime.now().date().isoformat()
    result = await server.hold(
        "remember this on trigger day",
        trigger_date=today,
    )
    bucket_id = result.split("→")[-1].split()[0] if "→" in result else ""
    if not bucket_id:
        bucket_id = (await server.bucket_mgr.list_all(include_archive=False))[0]["id"]

    first = await server.boot()
    second = await server.boot()
    bucket = await server.bucket_mgr.get(bucket_id)

    assert "boot: 今日浮现" in first
    assert bucket_id in first
    assert bucket_id not in second
    assert bucket["metadata"]["trigger_last_seen"] == today


@pytest.mark.asyncio
async def test_trace_sets_trigger_and_resolved_hides_it(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    yesterday = (datetime.now().date() - timedelta(days=1)).isoformat()
    bucket_id = await server.bucket_mgr.create(content="trace trigger body")

    await server.trace(bucket_id, trigger_date=yesterday)
    first = await server.boot()
    await server.trace(bucket_id, resolved=1)
    second = await server.boot()

    assert bucket_id in first
    assert bucket_id not in second


@pytest.mark.asyncio
async def test_invalid_trigger_date_is_rejected(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)

    hold_result = await server.hold("invalid trigger", trigger_date="2026/07/20")
    trace_result = await server.trace("missing", trigger_date="July 20")

    assert "trigger_date must use YYYY-MM-DD format" in hold_result
    assert "trigger_date must use YYYY-MM-DD format" in trace_result


@pytest.mark.asyncio
async def test_boot_only_marks_fully_emitted_trigger_items_seen(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    today = datetime.now().date().isoformat()
    ids = []
    for index in range(8):
        bucket_id = await server.bucket_mgr.create(
            ("触" * 280) + f"END_TRIGGER_{index}",
            name=f"due trigger {index}",
        )
        assert await server.bucket_mgr.update(bucket_id, trigger_date=today)
        ids.append(bucket_id)

    first = await server.boot(max_tokens=1000)
    seen = []
    for index, bucket_id in enumerate(ids):
        bucket = await server.bucket_mgr.get(bucket_id)
        complete = f"END_TRIGGER_{index}" in first
        assert (bucket["metadata"]["trigger_last_seen"] == today) is complete
        seen.append(complete)
    assert any(seen) and not all(seen)

    second = await server.boot(max_tokens=16000)
    for index, bucket_id in enumerate(ids):
        if not seen[index]:
            assert f"END_TRIGGER_{index}" in second
            bucket = await server.bucket_mgr.get(bucket_id)
            assert bucket["metadata"]["trigger_last_seen"] == today


@pytest.mark.asyncio
async def test_cas_recomposition_recomputes_trigger_consumption(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    await server.boot()
    today = datetime.now().date().isoformat()
    ids = []
    for index in range(8):
        bucket_id = await server.bucket_mgr.create(
            ("触" * 280) + f"END_CAS_{index}",
            name=f"CAS event {index} " + "变" * 70,
        )
        assert await server.bucket_mgr.update(bucket_id, trigger_date=today)
        ids.append(bucket_id)

    original = server.bucket_mgr.advance_boot_delta_checkpoint
    calls = 0

    def concurrent_advance(expected_event_id, next_event_id, profile="talk"):
        nonlocal calls
        calls += 1
        if calls == 1:
            assert original(
                expected_event_id,
                server.bucket_mgr.get_boot_delta_high_water(),
                profile=profile,
            )
            return False
        return original(expected_event_id, next_event_id, profile=profile)

    monkeypatch.setattr(server.bucket_mgr, "advance_boot_delta_checkpoint", concurrent_advance)
    result = await server.boot(max_tokens=1000)

    assert calls == 1
    assert "（无新增变化）" in result
    for index, bucket_id in enumerate(ids):
        bucket = await server.bucket_mgr.get(bucket_id)
        assert (bucket["metadata"]["trigger_last_seen"] == today) is (
            f"END_CAS_{index}" in result
        )


def _trigger_bucket(bucket_id="reminder", *, created="2026-10-15T09:00:00", **metadata):
    return {
        "id": bucket_id,
        "content": "the original reminder content",
        "metadata": {
            "name": bucket_id,
            "created": created,
            "trigger_date": datetime.now().date().isoformat(),
            "importance": 5,
            **metadata,
        },
    }


def _recall(*candidates, status="available"):
    return {"candidates": list(candidates), "semantic": {"status": status}}


def _candidate(bucket_id="newer", *, score=0.85, created="2026-10-15T10:00:00", **metadata):
    bucket = _trigger_bucket(bucket_id, created=created, **metadata)
    bucket["semantic_score"] = score
    bucket["content"] = "CANDIDATE_BODY_MUST_NOT_LEAK"
    return bucket


@pytest.mark.asyncio
async def test_trigger_hint_uses_semantic_rank_and_preserves_preview(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    reminder = _trigger_bucket()
    lexical = _candidate("lexical", score=0.81)
    lexical["score"] = 99
    semantic = _candidate("semantic", score=0.94, name="best\n\tname")
    semantic["score"] = 1
    server._recall_memory_candidates.return_value = _recall(lexical, semantic)
    update = AsyncMock()
    monkeypatch.setattr(server.bucket_mgr, "update", update)
    baseline = {}

    text, items = await server._format_due_triggers([reminder], baseline_out=baseline)

    assert text == baseline["text"] + "\n⚠ 可能已过时（相似 0.94）：[bucket_id:semantic] best name（2026-10-15）"
    assert "CANDIDATE_BODY_MUST_NOT_LEAK" not in text
    assert "[bucket_id:lexical]" not in text
    assert items == [("reminder", len(text))]
    assert baseline["items"] == [("reminder", len(baseline["text"]))]
    server._recall_memory_candidates.assert_awaited_once_with(reminder["content"], limit=8)
    update.assert_not_awaited()
    assert reminder["metadata"].get("resolved") is None
    assert reminder["metadata"].get("superseded_by") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("marker", ["new-id", "none", "  new-id  "])
async def test_superseded_due_trigger_is_skipped_before_recall(tmp_path, monkeypatch, marker):
    server = _load_server(tmp_path, monkeypatch)
    text, items = await server._format_due_triggers([_trigger_bucket(superseded_by=marker)])
    assert "（今日无到期提醒）" in text
    assert items == []
    server._recall_memory_candidates.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("case", [
    "self", "sealed", "test", "superseded", "dormant", "older", "equal",
    "missing_time", "bad_time", "date_only", "mixed_zone", "low_score",
    "no_score", "bad_score", "nan", "infinity",
])
async def test_trigger_hint_rejects_ineligible_candidate(tmp_path, monkeypatch, case):
    server = _load_server(tmp_path, monkeypatch)
    candidate = _candidate()
    if case == "self":
        candidate["id"] = "reminder"
    elif case in {"sealed", "dormant"}:
        candidate["metadata"][case] = True
    elif case == "test":
        candidate["metadata"]["tags"] = ["test"]
    elif case == "superseded":
        candidate["metadata"]["superseded_by"] = "none"
    elif case in {"older", "equal", "missing_time", "bad_time", "date_only", "mixed_zone"}:
        candidate["metadata"]["created"] = {
            "older": "2026-10-15T08:59:59", "equal": "2026-10-15T09:00:00",
            "missing_time": None, "bad_time": "invalid", "date_only": "2026-10-16",
            "mixed_zone": "2026-10-15T10:00:00+08:00",
        }[case]
    elif case == "no_score":
        candidate.pop("semantic_score")
    else:
        candidate["semantic_score"] = {
            "low_score": 0.6499, "bad_score": "invalid", "nan": float("nan"),
            "infinity": float("inf"),
        }[case]
    server._recall_memory_candidates.return_value = _recall(candidate)
    assert await server._trigger_stale_hint(_trigger_bucket()) == ""


@pytest.mark.asyncio
@pytest.mark.parametrize("created", [None, "invalid", "2026-10-15"])
async def test_trigger_hint_does_not_infer_missing_creation_time(tmp_path, monkeypatch, created):
    server = _load_server(tmp_path, monkeypatch)
    reminder = _trigger_bucket(created=created, created_at="2026-10-14")
    assert await server._trigger_stale_hint(reminder) == ""
    server._recall_memory_candidates.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("created,newer,expected", [
    ("2026-10-15T09:00:00", "2026-10-15T09:00:01", True),
    ("2026-10-15T09:00:00+08:00", "2026-10-15T01:00:01Z", True),
    ("2026-10-15T09:00:00+08:00", "2026-10-15T02:00:00+02:00", False),
    ("2026-10-15T09:00:00+08:00", "2026-10-15T01:00:00Z", False),
    ("2026-10-15T09:00:00+08:00", "2026-10-15T10:00:00", False),
])
async def test_trigger_hint_compares_creation_instants(tmp_path, monkeypatch, created, newer, expected):
    server = _load_server(tmp_path, monkeypatch)
    server._recall_memory_candidates.return_value = _recall(_candidate(created=newer, score=0.65))
    result = await server._trigger_stale_hint(_trigger_bucket(created=created))
    assert bool(result) is expected


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["disabled", "unavailable_or_empty_index", "provider_error"])
async def test_trigger_hint_requires_available_semantics(tmp_path, monkeypatch, status):
    server = _load_server(tmp_path, monkeypatch)
    server._recall_memory_candidates.return_value = _recall(_candidate(), status=status)
    text, _ = await server._format_due_triggers([_trigger_bucket()])
    assert "the original reminder content" in text
    assert "⚠" not in text


@pytest.mark.asyncio
async def test_trigger_hint_no_match_and_recall_failure_are_silent(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    server._recall_memory_candidates.return_value = _recall()
    baseline, _ = await server._format_due_triggers([_trigger_bucket()])
    server._recall_memory_candidates.side_effect = RuntimeError("HIDDEN_PROVIDER_ERROR")
    failed, _ = await server._format_due_triggers([_trigger_bucket()])
    assert failed == baseline
    assert "HIDDEN_PROVIDER_ERROR" not in failed


@pytest.mark.asyncio
async def test_trigger_hint_name_is_bounded_and_one_line(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    server._recall_memory_candidates.return_value = _recall(_candidate(name="长\n" * 100))
    hint = await server._trigger_stale_hint(_trigger_bucket())
    assert hint.count("\n") == 0
    assert hint.endswith("...（2026-10-15）")
    assert len(hint.split("] ", 1)[1].split("（", 1)[0]) <= 80


@pytest.mark.asyncio
async def test_trigger_hint_deadline_discards_partial_results_and_awaits_cancellation(tmp_path, monkeypatch):
    import asyncio

    server = _load_server(tmp_path, monkeypatch)
    monkeypatch.setattr(server, "_TRIGGER_HINT_TIMEOUT_SECONDS", 0.01)
    completed = []

    async def recall(content, limit):
        if server._recall_memory_candidates.await_count == 1:
            return _recall(_candidate())
        try:
            await asyncio.Event().wait()
        finally:
            completed.append("cancelled")

    server._recall_memory_candidates.side_effect = recall
    text, items = await server._format_due_triggers([
        _trigger_bucket("first"), _trigger_bucket("second"), _trigger_bucket("third"),
    ])
    assert "⚠" not in text
    assert len(items) == 3
    assert server._recall_memory_candidates.await_count == 2
    assert completed == ["cancelled"]


@pytest.mark.asyncio
async def test_trigger_hint_propagates_external_cancellation(tmp_path, monkeypatch):
    import asyncio

    server = _load_server(tmp_path, monkeypatch)
    started = asyncio.Event()
    stopped = asyncio.Event()

    async def recall(content, limit):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    server._recall_memory_candidates.side_effect = recall
    task = asyncio.create_task(server._trigger_stale_hints([_trigger_bucket()]))
    try:
        await started.wait()
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert stopped.is_set()


@pytest.mark.asyncio
@pytest.mark.parametrize("max_items,expected", [(5, 5), (10, 10), (99, 10), (0, 0)])
async def test_trigger_hint_recall_is_bounded_after_due_filtering(tmp_path, monkeypatch, max_items, expected):
    server = _load_server(tmp_path, monkeypatch)
    buckets = [
        _trigger_bucket("resolved", resolved=True),
        _trigger_bucket("sealed", sealed=1),
        _trigger_bucket("superseded", superseded_by="none"),
        _trigger_bucket("future", trigger_date="2099-01-01"),
        _trigger_bucket("seen", trigger_last_seen=datetime.now().date().isoformat()),
        *[_trigger_bucket(f"due-{index}") for index in range(12)],
    ]
    text, items = await server._format_due_triggers(buckets, max_items=max_items)
    assert len(items) == expected
    assert server._recall_memory_candidates.await_count == expected
    assert all(bucket_id.startswith("due-") for bucket_id, _ in items)
    assert all(f"[bucket_id:{name}]" not in text for name in ("resolved", "sealed", "superseded", "future", "seen"))


@pytest.mark.asyncio
@pytest.mark.parametrize("score,expected", [(0.6499, False), (0.65, True), (0.6501, True)])
async def test_trigger_hint_default_threshold_boundary(tmp_path, monkeypatch, score, expected):
    server = _load_server(tmp_path, monkeypatch)
    server._recall_memory_candidates.return_value = _recall(_candidate(score=score))
    hint = await server._trigger_stale_hint(_trigger_bucket())
    assert bool(hint) is expected
    if expected:
        assert hint == f"⚠ 可能已过时（相似 {score:.2f}）：[bucket_id:newer] newer（2026-10-15）"


@pytest.mark.asyncio
@pytest.mark.parametrize("threshold,score,expected", [
    ("0.90", 0.85, False), ("0.60", 0.64, True), ("0", 0.0, False),
    ("1", 0.9999, False), ("1", 1.0, True), (" 0.7 ", 0.70, True),
])
async def test_trigger_hint_threshold_override(tmp_path, monkeypatch, threshold, score, expected):
    server = _load_server(tmp_path, monkeypatch)
    monkeypatch.setenv("OMBRE_TRIGGER_STALE_THRESHOLD", threshold)
    server._recall_memory_candidates.return_value = _recall(_candidate(score=score))
    hint = await server._trigger_stale_hint(_trigger_bucket())
    assert bool(hint) is expected
    if expected:
        assert f"（相似 {score:.2f}）：" in hint


@pytest.mark.asyncio
@pytest.mark.parametrize("configured", [None, "", "  ", "broken", "NaN", "nan", "inf", "-inf", "1e999", "-0.01", "1.01"])
async def test_trigger_hint_invalid_threshold_falls_back(tmp_path, monkeypatch, configured):
    server = _load_server(tmp_path, monkeypatch)
    if configured is not None:
        monkeypatch.setenv("OMBRE_TRIGGER_STALE_THRESHOLD", configured)
    assert server._trigger_stale_threshold() == 0.65
    server._recall_memory_candidates.return_value = _recall(_candidate(score=0.6499))
    assert await server._trigger_stale_hint(_trigger_bucket()) == ""
    server._recall_memory_candidates.return_value = _recall(_candidate(score=0.65))
    assert "（相似 0.65）：" in await server._trigger_stale_hint(_trigger_bucket())


@pytest.mark.asyncio
async def test_trigger_hint_reloads_threshold_and_displays_one(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    server._recall_memory_candidates.return_value = _recall(_candidate(score=0.85))
    monkeypatch.setenv("OMBRE_TRIGGER_STALE_THRESHOLD", "0.90")
    assert await server._trigger_stale_hint(_trigger_bucket()) == ""
    monkeypatch.setenv("OMBRE_TRIGGER_STALE_THRESHOLD", "0.80")
    assert "（相似 0.85）：" in await server._trigger_stale_hint(_trigger_bucket())
    server._recall_memory_candidates.return_value = _recall(_candidate(score=1.0))
    assert await server._trigger_stale_hint(_trigger_bucket()) == (
        "⚠ 可能已过时（相似 1.00）：[bucket_id:newer] newer（2026-10-15）"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("case,score,expected", [
    ("missing", None, False), ("none", None, False),
    ("empty", "", False), ("zero", 0.0, False), ("positive", 0.01, True),
])
async def test_trigger_hint_zero_threshold_requires_explicit_score(
    tmp_path, monkeypatch, case, score, expected
):
    server = _load_server(tmp_path, monkeypatch)
    monkeypatch.setenv("OMBRE_TRIGGER_STALE_THRESHOLD", "0")
    monkeypatch.setenv("OMBRE_TRIGGER_STALE_LEAD", "0")
    candidate = _candidate(score=score)
    if case == "missing":
        candidate.pop("semantic_score")
    server._recall_memory_candidates.return_value = _recall(candidate)
    hint = await server._trigger_stale_hint(_trigger_bucket())
    assert bool(hint) is expected
    if expected:
        assert "（相似 0.01）：" in hint


@pytest.mark.asyncio
@pytest.mark.parametrize("first,second,expected", [
    (0.68, 0.66, False), (0.66, 0.64, False),
    (0.70, 0.64, True), (0.64, 0.50, False),
])
async def test_trigger_hint_requires_lead_including_below_threshold_runner_up(
    tmp_path, monkeypatch, first, second, expected
):
    server = _load_server(tmp_path, monkeypatch)
    server._recall_memory_candidates.return_value = _recall(
        _candidate("second", score=second), _candidate("first", score=first)
    )
    hint = await server._trigger_stale_hint(_trigger_bucket())
    assert bool(hint) is expected
    if expected:
        assert "[bucket_id:first]" in hint


@pytest.mark.asyncio
@pytest.mark.parametrize("score", [0.80, 1.0])
@pytest.mark.parametrize("lead", [None, "0"])
async def test_trigger_hint_ties_are_suppressed_in_either_order(
    tmp_path, monkeypatch, score, lead
):
    server = _load_server(tmp_path, monkeypatch)
    if lead is not None:
        monkeypatch.setenv("OMBRE_TRIGGER_STALE_LEAD", lead)
    candidates = [_candidate("one", score=score), _candidate("two", score=score)]
    for ordered in (candidates, list(reversed(candidates))):
        server._recall_memory_candidates.return_value = _recall(*ordered)
        assert await server._trigger_stale_hint(_trigger_bucket()) == ""


@pytest.mark.asyncio
@pytest.mark.parametrize("score,expected", [(0.0, False), (0.04, False), (0.05, True), (0.10, True)])
async def test_trigger_hint_single_candidate_uses_zero_runner_up(tmp_path, monkeypatch, score, expected):
    server = _load_server(tmp_path, monkeypatch)
    monkeypatch.setenv("OMBRE_TRIGGER_STALE_THRESHOLD", "0")
    server._recall_memory_candidates.return_value = _recall(_candidate(score=score))
    assert bool(await server._trigger_stale_hint(_trigger_bucket())) is expected


@pytest.mark.asyncio
@pytest.mark.parametrize("first,second,expected", [
    (0.70, 0.65, True), (0.80, 0.75, True),
    (0.70, 0.6500000001, False), (0.7000000001, 0.65, True),
])
async def test_trigger_hint_lead_boundary_uses_raw_scores(tmp_path, monkeypatch, first, second, expected):
    server = _load_server(tmp_path, monkeypatch)
    server._recall_memory_candidates.return_value = _recall(
        _candidate("first", score=first), _candidate("second", score=second)
    )
    assert bool(await server._trigger_stale_hint(_trigger_bucket())) is expected


@pytest.mark.asyncio
@pytest.mark.parametrize("lead,first,second,expected", [
    ("0", 0.80, 0.79, True), ("0.03", 0.80, 0.76, True),
    ("0.05", 0.80, 0.76, False), ("0.04", 0.80, 0.76, True),
    ("1", 1.0, None, True), ("1", 1.0, 0.01, False), (" 0.1 ", 0.80, 0.70, True),
])
async def test_trigger_hint_lead_override(tmp_path, monkeypatch, lead, first, second, expected):
    server = _load_server(tmp_path, monkeypatch)
    monkeypatch.setenv("OMBRE_TRIGGER_STALE_LEAD", lead)
    candidates = [_candidate("first", score=first)]
    if second is not None:
        candidates.append(_candidate("second", score=second))
    server._recall_memory_candidates.return_value = _recall(*candidates)
    assert bool(await server._trigger_stale_hint(_trigger_bucket())) is expected


@pytest.mark.asyncio
@pytest.mark.parametrize("configured", [None, "", "  ", "broken", "NaN", "nan", "inf", "-inf", "1e999", "-0.01", "1.01"])
async def test_trigger_hint_invalid_lead_falls_back(tmp_path, monkeypatch, configured):
    server = _load_server(tmp_path, monkeypatch)
    if configured is not None:
        monkeypatch.setenv("OMBRE_TRIGGER_STALE_LEAD", configured)
    assert server._trigger_stale_lead() == 0.05
    server._recall_memory_candidates.return_value = _recall(
        _candidate("first", score=0.80), _candidate("second", score=0.76)
    )
    assert await server._trigger_stale_hint(_trigger_bucket()) == ""
    server._recall_memory_candidates.return_value = _recall(
        _candidate("first", score=0.70), _candidate("second", score=0.65)
    )
    assert "[bucket_id:first]" in await server._trigger_stale_hint(_trigger_bucket())


@pytest.mark.asyncio
async def test_trigger_hint_reloads_lead_per_check(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    server._recall_memory_candidates.return_value = _recall(
        _candidate("first", score=0.80), _candidate("second", score=0.76)
    )
    monkeypatch.setenv("OMBRE_TRIGGER_STALE_LEAD", "0.03")
    assert "[bucket_id:first]" in await server._trigger_stale_hint(_trigger_bucket())
    monkeypatch.setenv("OMBRE_TRIGGER_STALE_LEAD", "0.05")
    assert await server._trigger_stale_hint(_trigger_bucket()) == ""
    monkeypatch.setenv("OMBRE_TRIGGER_STALE_LEAD", "broken")
    assert await server._trigger_stale_hint(_trigger_bucket()) == ""


@pytest.mark.asyncio
@pytest.mark.parametrize("case", [
    "self", "sealed", "test", "dormant", "superseded", "older", "equal", "mixed_zone",
    "bad_time", "missing_time", "missing_score", "none_score", "nan", "infinity",
])
async def test_trigger_hint_filters_runner_up_before_lead(tmp_path, monkeypatch, case):
    server = _load_server(tmp_path, monkeypatch)
    runner_up = _candidate("runner", score=0.89)
    if case == "self":
        runner_up["id"] = "reminder"
    elif case in {"sealed", "dormant"}:
        runner_up["metadata"][case] = True
    elif case == "test":
        runner_up["metadata"]["tags"] = ["test"]
    elif case == "superseded":
        runner_up["metadata"]["superseded_by"] = "none"
    elif case in {"older", "equal", "mixed_zone", "bad_time", "missing_time"}:
        runner_up["metadata"]["created"] = {
            "older": "2026-10-15T08:00:00", "equal": "2026-10-15T09:00:00",
            "mixed_zone": "2026-10-15T10:00:00+08:00", "bad_time": "invalid", "missing_time": None,
        }[case]
    elif case == "missing_score":
        runner_up.pop("semantic_score")
    else:
        runner_up["semantic_score"] = {
            "none_score": None, "nan": float("nan"), "infinity": float("inf"),
        }[case]
    server._recall_memory_candidates.return_value = _recall(
        runner_up, _candidate("first", score=0.90)
    )
    assert "[bucket_id:first]" in await server._trigger_stale_hint(_trigger_bucket())

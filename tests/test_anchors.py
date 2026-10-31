import frontmatter
import pytest
from datetime import datetime
from zoneinfo import ZoneInfo

from tests.test_boot_profiles import _load_server

TZ = ZoneInfo("Asia/Shanghai")


def _at(server, monkeypatch, stamp):
    now = datetime.fromisoformat(stamp).replace(tzinfo=TZ)
    monkeypatch.setattr(server.anchors_mod, "shanghai_now", lambda: now)


def _raw(server, bucket_id):
    return frontmatter.load(server.bucket_mgr._find_bucket_file(bucket_id))


async def _hold(server, text="婷感冒了，每个窗口记得问她好点没。", until="2026-11-07"):
    result = await server.hold(text, anchor_until=until)
    assert "锚点已建" in result, result
    return result.split("bucket_id=")[1].split()[0]


def test_builder_output_unchanged_no_anchor_fields():
    from bucket_manager import BucketManager
    post = BucketManager._build_bucket_post("x", "body", created="t", last_active="t", created_date="d", _aliases=[])
    assert not [k for k in post.metadata if k.startswith("anchor")]


@pytest.mark.asyncio
async def test_hold_creates_anchor_with_tag_and_only_anchor_until(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    _at(server, monkeypatch, "2026-10-31T12:00:00")
    bucket_id = await _hold(server)
    meta = _raw(server, bucket_id).metadata
    assert meta["anchor_until"] == "2026-11-07"
    assert "锚点" in meta["tags"]
    assert "anchor_log" not in meta and "anchor_ended_at" not in meta


@pytest.mark.asyncio
@pytest.mark.parametrize("kwargs,needle", [
    ({"anchor_until": "2026-10-30"}, "before today"),
    ({"anchor_until": "11/07"}, "YYYY-MM-DD"),
    ({"anchor_until": "2026-11-07", "feel": True}, "cannot be combined"),
    ({"anchor_until": "2026-11-07", "trigger_date": "2026-11-01"}, "cannot be combined"),
    ({"anchor_until": "2026-11-07", "operation_id": "k1"}, "operation_id"),
])
async def test_hold_anchor_rejections(tmp_path, monkeypatch, kwargs, needle):
    server = _load_server(tmp_path, monkeypatch)
    _at(server, monkeypatch, "2026-10-31T12:00:00")
    assert needle in await server.hold("内容", **kwargs)


@pytest.mark.asyncio
async def test_body_limit_and_active_limit(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    _at(server, monkeypatch, "2026-10-31T12:00:00")
    assert "最多 300 字" in await server.hold("长" * 301, anchor_until="2026-11-07")
    for i in range(4):
        await _hold(server, f"锚点内容{i}")
    assert "同时最多 4 个" in await server.hold("第五个", anchor_until="2026-11-07")


@pytest.mark.asyncio
@pytest.mark.parametrize("profile", ["talk", "code", "tg"])
async def test_boot_shows_full_anchor_every_profile_and_every_time(tmp_path, monkeypatch, profile):
    server = _load_server(tmp_path, monkeypatch)
    _at(server, monkeypatch, "2026-10-31T12:00:00")
    bucket_id = await _hold(server)
    for _ in range(3):
        result = await server.boot(profile=profile)
        assert "=== boot: 锚点 ===" in result
        assert "婷感冒了，每个窗口记得问她好点没。" in result
        assert f"[bucket_id:{bucket_id}] 有效至 11-07（还剩 7 天）" in result
    assert "anchor_ended_at" not in _raw(server, bucket_id).metadata


@pytest.mark.asyncio
async def test_no_anchor_section_when_none(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    assert "锚点" not in await server.boot(profile="tg")


@pytest.mark.asyncio
async def test_expiry_is_whole_day_then_hidden_and_reopenable(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    _at(server, monkeypatch, "2026-10-31T12:00:00")
    bucket_id = await _hold(server, until="2026-11-02")
    _at(server, monkeypatch, "2026-11-02T23:59:00")
    last_day = await server.boot(profile="talk")
    assert "今天最后一天" in last_day
    _at(server, monkeypatch, "2026-11-03T00:01:00")
    assert "=== boot: 锚点 ===" not in await server.boot(profile="talk")
    assert "已到期" in await server.dream(detail_ids=bucket_id)
    assert "锚点已到期" not in await server.trace(bucket_id, anchor_until="2026-11-05")
    assert "=== boot: 锚点 ===" in await server.boot(profile="talk")


@pytest.mark.asyncio
async def test_notes_timestamped_dedupe_five_shown_and_dream_shows_all(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    _at(server, monkeypatch, "2026-10-31T20:10:00")
    bucket_id = await _hold(server)
    assert "已追加" in await server.trace(bucket_id, anchor_note="（Opus 5.5 / TG）问过她喉咙，好点了")
    assert "ssss" not in await server.trace(bucket_id, anchor_note="（Opus 5.5 / TG）问过她喉咙，好点了")
    assert len(_raw(server, bucket_id)["anchor_log"]) == 1
    for minute in range(1, 7):
        _at(server, monkeypatch, f"2026-10-31T21:{minute:02d}:00")
        await server.trace(bucket_id, anchor_note=f"（CC / 窗口）记录{minute}")
    assert len(_raw(server, bucket_id)["anchor_log"]) == 7
    boot = await server.boot(profile="talk")
    assert "（更早 2 条，dream(detail_ids=\"%s\") 看全部）" % bucket_id in boot
    assert "10-31 20:10" not in boot and "记录6" in boot
    dream = await server.dream(detail_ids=bucket_id)
    assert "anchor_until=2026-11-07" in dream and "10-31 20:10" in dream and "记录6" in dream


@pytest.mark.asyncio
async def test_note_limits_and_inactive_rejected(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    _at(server, monkeypatch, "2026-10-31T12:00:00")
    bucket_id = await _hold(server, until="2026-10-31")
    assert "at most 80" in await server.trace(bucket_id, anchor_note="长" * 81)
    plain = await server.bucket_mgr.create("普通桶")
    assert "不是锚点" in await server.trace(plain, anchor_note="x")
    _at(server, monkeypatch, "2026-11-01T12:00:00")
    assert "不能再追加" in await server.trace(bucket_id, anchor_note="x")


@pytest.mark.asyncio
async def test_end_is_two_stage_and_hides_everywhere_but_keeps_log(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    _at(server, monkeypatch, "2026-10-31T12:00:00")
    bucket_id = await _hold(server)
    await server.trace(bucket_id, anchor_note="（a）x")
    preview = await server.trace(bucket_id, anchor_until="end")
    assert "preview" in preview and "confirm_token:" in preview
    assert "anchor_ended_at" not in _raw(server, bucket_id).metadata
    token = preview.split("confirm_token:")[1].strip()
    assert "invalid" in await server.trace(bucket_id, anchor_until="end", confirm_token="bad")
    assert "已结束" in await server.trace(bucket_id, anchor_until="end", confirm_token=token)
    meta = _raw(server, bucket_id).metadata
    assert meta["anchor_ended_at"] == "2026-10-31" and len(meta["anchor_log"]) == 1
    assert "=== boot: 锚点 ===" not in await server.boot(profile="tg")
    assert "已手动结束" in await server.dream(detail_ids=bucket_id)
    assert "无需重复" in await server.trace(bucket_id, anchor_until="end")
    await server.trace(bucket_id, anchor_until="2026-11-09")
    assert "anchor_ended_at" not in _raw(server, bucket_id).metadata
    assert "=== boot: 锚点 ===" in await server.boot(profile="tg")


@pytest.mark.asyncio
async def test_sealed_test_tag_and_superseded_not_shown(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    _at(server, monkeypatch, "2026-10-31T12:00:00")
    sealed = await _hold(server, "封存的锚点")
    tested = await _hold(server, "测试标签锚点")
    gone = await _hold(server, "被取代的锚点")
    await server.trace(sealed, sealed=1)
    await server.trace(tested, tags="test")
    successor = await server.bucket_mgr.create("接班桶")
    await server.trace(gone, superseded_by=successor)
    result = await server.boot(profile="talk")
    assert "=== boot: 锚点 ===" not in result
    for text in ("封存的锚点", "测试标签锚点", "被取代的锚点"):
        assert text not in result


@pytest.mark.asyncio
async def test_anchor_params_must_be_alone_and_reject_batch_and_operation_id(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    _at(server, monkeypatch, "2026-10-31T12:00:00")
    bucket_id = await _hold(server)
    assert "alone" in await server.trace(bucket_id, anchor_note="x", importance=7)
    assert "批量" in await server.trace(f"{bucket_id},{bucket_id}x", anchor_note="x")
    assert "operation_id" in await server.trace(bucket_id, anchor_note="x", operation_id="k")


@pytest.mark.asyncio
async def test_breath_marks_anchor(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    _at(server, monkeypatch, "2026-10-31T12:00:00")
    bucket = await server.bucket_mgr.get(await _hold(server))
    text = await server._append_bucket_extras("正文", bucket)
    assert "[锚点 至 11-07]" in text


@pytest.mark.asyncio
async def test_tg_budget_four_max_anchors_keep_full_bodies_and_fold_old_notes(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    _at(server, monkeypatch, "2026-10-31T12:00:00")
    ids = [await _hold(server, f"锚点{i}" + "字" * 290) for i in range(4)]
    for bucket_id in ids:
        for minute in range(5):
            await server.trace(bucket_id, anchor_note=f"（m / w）{'记' * 70}{minute}")
    result = await server.boot(profile="tg")
    for i, bucket_id in enumerate(ids):
        assert f"锚点{i}" + "字" * 290 in result
        assert f"[bucket_id:{bucket_id}] 有效至 11-07" in result
    assert "更早" in result and 'dream(detail_ids="%s")' % ids[0] in result
    talk = await server.boot(profile="talk")
    assert talk.count("（更早") == 0 and talk.count("记记记") >= 20


@pytest.mark.asyncio
async def test_tg_single_anchor_shows_five_recent_notes(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    _at(server, monkeypatch, "2026-10-31T12:00:00")
    bucket_id = await _hold(server)
    for minute in range(7):
        _at(server, monkeypatch, f"2026-10-31T13:{minute:02d}:00")
        await server.trace(bucket_id, anchor_note=f"（m / w）n{minute}")
    result = await server.boot(profile="tg")
    assert "n6" in result and "n2" in result and "n1" not in result and "更早 2 条" in result

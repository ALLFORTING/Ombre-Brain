import copy
import importlib
import re
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone
from math import gcd
from unittest.mock import AsyncMock

import frontmatter
import pytest

from boot_todos import (
    PROFILE_TODOS, TEXT_TRUNCATION, active_display_candidates,
    fit_todos, rotation_stride, shanghai_date, todo_page,
)
from utils import count_tokens_approx


DAY = date(2026, 9, 26)


def record(index, text="待办正文", **fields):
    return {
        "id": f"todo_{index:08x}-0000-4000-8000-000000000000",
        "text": text, "said_by": "unknown", "said_at": None,
        "source_bucket": None, **fields,
    }


def bucket(index, *, importance=5, text="待" * 30, **fields):
    rec = record(index, text)
    return {
        "id": f"{index:012x}", "content": "synthetic carrier",
        "metadata": {"importance": importance, "todos": [text],
                     "todo_provenance": [rec], **fields},
    }


def page_of(count=20, profile="talk", **kwargs):
    buckets = [bucket(i, **kwargs) for i in range(count)]
    return todo_page(active_display_candidates(buckets), profile, DAY)


def assert_counts(result, total):
    assert result.shown == len(result.entries)
    assert result.shown + result.hidden == total
    assert f"共 {total} 项未完成 | 本次显示 {result.shown} | 未显示 {result.hidden}" in result.header
    assert result.text == result.header + ("\n" + "\n".join(result.entries) if result.entries else "")


def test_active_identities_legacy_and_visibility_do_not_mutate():
    same = "same"
    data = [bucket(1, text=same), bucket(2, todos=["legacy", "legacy"], todo_provenance=None),
            bucket(3, sealed=1), bucket(4, resolved=True)]
    data[0]["metadata"]["todo_provenance"] = [
        record(1, same, done_at="2026-09-20T00:00:00"), record(5, same), record(6, same),
    ]
    before = copy.deepcopy(data)
    candidates = active_display_candidates(data)
    assert len(candidates) == 3
    assert {item.todo_id for item in candidates if item.todo_id} == {record(5)["id"], record(6)["id"]}
    assert sum(item.todo_id is None for item in candidates) == 1
    assert data == before
    # Conservative malformed contribution plus ordinary legacy has one display key.
    legacy = bucket(7, text="legacy", todo_provenance=[
        {"text": "legacy", "said_by": "unknown"}, {"text": "legacy", "id": "bad"},
    ])
    assert len(active_display_candidates([legacy])) == 1


def test_priority_ties_ignore_attribution_times_and_input_order():
    data = [bucket(3, importance=2), bucket(2, importance=9), bucket(1, importance=9)]
    expected = active_display_candidates(data)
    assert [item.bucket_id for item in expected] == [f"{i:012x}" for i in (1, 2, 3)]
    changed = copy.deepcopy(list(reversed(data)))
    for source in changed:
        source["metadata"].update(created="1900-01-01", updated_at="2099-01-01", dormant=True)
        source["metadata"]["todo_provenance"][0].update(said_by="ting", said_at="1900-01-01")
    assert active_display_candidates(changed) == expected


def test_projection_and_bucket_conflicts_are_explicit():
    first = bucket(1)
    rec = first["metadata"]["todo_provenance"][0]
    first["metadata"]["todo_provenance"] = [
        {**rec, "done_at": "2026-09-20"}, {**rec, "done_at": "2026-09-21"},
    ]
    with pytest.raises(ValueError, match="completion conflict"):
        active_display_candidates([first])
    with pytest.raises(ValueError, match="bucket identity"):
        active_display_candidates([bucket(1), bucket(1, importance=9)])


@pytest.mark.parametrize("profile", PROFILE_TODOS)
def test_exhaustive_coprime_daily_starts_and_budget_independence(profile):
    P, q, _ = PROFILE_TODOS[profile]
    for M in range(2, 101):
        candidates = active_display_candidates([bucket(i) for i in range(M + P)])
        stride = rotation_stride(M, q)
        assert gcd(stride, M) == 1
        expected = min((s for s in range(1, M) if gcd(s, M) == 1), key=lambda s: (abs(s-q), s))
        assert stride == expected
        starts = {todo_page(candidates, profile, DAY + timedelta(days=d)).start for d in range(M)}
        assert starts == set(range(M))
        plan = todo_page(candidates, profile, DAY)
        original_order = plan.rotation
        for budget in (100, 300, 100000):
            result = fit_todos(plan, budget)
            assert_counts(result, M + P)
            assert plan.rotation == original_order and plan.start == DAY.toordinal() * stride % M
    assert rotation_stride(0, q) == rotation_stride(1, q) == 1


@pytest.mark.parametrize("profile", PROFILE_TODOS)
def test_dynamic_capacity_all_fit_and_same_day_budget_extension(profile):
    plan = page_of(25, profile)
    snapshots = []
    for budget in (180, 450, 100000):
        result = fit_todos(plan, budget)
        assert count_tokens_approx(result.text) <= budget
        assert_counts(result, 25)
        assert result == fit_todos(plan, budget)
        rotation_keys = [item.todo_id for item in plan.rotation]
        displayed = [next(identity for identity in rotation_keys if identity in row)
                     for row in result.entries if any(identity in row for identity in rotation_keys)]
        if result.hidden:
            assert displayed == [identity for identity in rotation_keys if identity in displayed]
        if displayed and result.hidden:
            assert displayed[0] == rotation_keys[0]
        snapshots.append(result)
    assert snapshots[0].shown < snapshots[1].shown < snapshots[2].shown == 25
    assert snapshots[-1].hidden == 0
    assert "本次已显示全部未完成事项" in snapshots[-1].header
    assert len(plan.priority) == PROFILE_TODOS[profile][0]


def test_collection_changes_are_deterministic():
    data = [bucket(i) for i in range(15)]
    for changed in (data, data[1:], data + [bucket(30)]):
        first = todo_page(active_display_candidates(changed), "talk", DAY)
        second = todo_page(active_display_candidates(list(reversed(changed))), "talk", DAY)
        assert first == second


def test_summary_only_one_slot_and_priority_rotation_pair():
    plan = page_of(12, "tg")
    summary = fit_todos(plan, 0)
    result = fit_todos(plan, count_tokens_approx(summary.text))
    assert result.shown == 0 and result.hidden == 12
    assert "未显示 ≠ 已完成" in result.text
    budgets = range(count_tokens_approx(summary.text), 300)
    singles = [fit_todos(plan, budget) for budget in budgets if fit_todos(plan, budget).shown == 1]
    assert singles
    assert all(plan.rotation[0].todo_id in result.entries[0] for result in singles)
    pair = next(fit_todos(plan, budget) for budget in range(100, 400)
                if fit_todos(plan, budget).shown == 2)
    assert any(plan.priority[0].todo_id in row for row in pair.entries)
    assert any(plan.rotation[0].todo_id in row for row in pair.entries)


def test_exact_n_and_n_plus_one_budget_counts_and_atomic_ids():
    plan = page_of(6, "tg", text="待" * 30)
    for N in (2, 3):
        # The last token before another atomic row becomes possible still emits N.
        results = [(budget, fit_todos(plan, budget)) for budget in range(100, 500)]
        budget, result = next((b, r) for (b, r), (_, nxt) in zip(results, results[1:])
                              if r.shown == N and nxt.shown == N + 1)
        assert count_tokens_approx(result.text) <= budget
        assert_counts(result, 6)
        assert_counts(fit_todos(plan, budget + 1), 6)
        for line in result.entries:
            assert re.search(r"\[bucket_id:[0-9a-f]{12}\] \[todo_id:todo_[0-9a-f-]{36}\] importance:5", line)


def test_long_body_legacy_and_long_identity_are_atomic():
    data = [bucket(1, text="待" * 200), bucket(2, text="旧" * 200, todo_provenance=None)]
    data[0]["id"] = "bucket-with-a-long-stable-identity-" * 4
    plan = todo_page(active_display_candidates(data), "talk", DAY)
    full = fit_todos(plan, 100000)
    assert TEXT_TRUNCATION in full.text
    assert data[0]["id"] in full.text and record(1)["id"] in full.text
    assert "[todo_id:null]" in full.text and "旧格式；不能单条 todo_done" in full.text
    for budget in (80, 150, 300):
        result = fit_todos(plan, budget)
        assert_counts(result, 2)
        for line in result.entries:
            if record(1)["id"] in line:
                assert data[0]["id"] in line
            else:
                assert "[todo_id:null]" in line and "旧格式；不能单条 todo_done" in line


def test_short_english_body_fits_without_costlier_truncation_marker():
    plan = page_of(25, "tg", text="abcdefghijklmnopqrst")
    full = fit_todos(plan, 100000)
    row = next(row for row in full.entries if plan.rotation[0].todo_id in row)
    header = fit_todos(plan, 0).header.replace("本次显示 0", "本次显示 1").replace("未显示 25", "未显示 24")
    budget = count_tokens_approx(header + "\n" + row)
    result = fit_todos(plan, budget)
    assert result.entries == (row,)
    assert_counts(result, 25)
    assert TEXT_TRUNCATION not in row
    assert count_tokens_approx(result.text) <= budget


def test_multiline_body_stays_one_display_row():
    plan = page_of(1, text="first\nsecond\r\nthird")
    result = fit_todos(plan, 100000)
    assert len(result.entries) == 1
    assert "first\\nsecond\\r\\nthird" in result.entries[0]
    assert len(result.entries[0].splitlines()) == 1
    assert_counts(result, 1)


def test_shanghai_boundary_and_missing_zone_is_not_local_fallback(monkeypatch):
    assert shanghai_date(datetime(2026, 9, 25, 15, 59, 59, tzinfo=timezone.utc)) == date(2026, 9, 25)
    assert shanghai_date(datetime(2026, 9, 25, 16, 0, 0, tzinfo=timezone.utc)) == DAY
    from zoneinfo import ZoneInfoNotFoundError
    import boot_todos
    def missing(_):
        raise ZoneInfoNotFoundError("synthetic unavailable timezone")
    monkeypatch.setattr(boot_todos, "ZoneInfo", missing)
    with pytest.raises(ZoneInfoNotFoundError):
        shanghai_date()


def test_tzdata_supplies_zone_without_system_database():
    check = subprocess.run([
        sys.executable, "-B", "-c",
        "from zoneinfo import ZoneInfo, reset_tzpath; "
        "from datetime import datetime; reset_tzpath(()); "
        "assert datetime(2026, 9, 26, tzinfo=ZoneInfo('Asia/Shanghai')).utcoffset().total_seconds() == 28800",
    ], capture_output=True, text=True)
    assert check.returncode == 0, check.stderr


@pytest.fixture
def server(tmp_path, monkeypatch):
    monkeypatch.setenv("OMBRE_BUCKETS_DIR", str(tmp_path / "buckets"))
    monkeypatch.setenv("OMBRE_RESPONSE_SEAL", "w4c-test-seal")
    monkeypatch.delenv("OMBRE_API_KEY", raising=False)
    sys.modules.pop("server", None)
    module = importlib.import_module("server")
    module.decay_engine.ensure_started = AsyncMock(return_value=None)
    monkeypatch.setattr(module, "shanghai_date", lambda: DAY)
    return module


@pytest.mark.asyncio
async def test_three_profiles_share_projection_archive_and_legacy_without_writes(server):
    identity = await server.bucket_mgr.create("synthetic body", todos=["finished", "pending"], importance=1)
    path = server.bucket_mgr._find_bucket_file(identity)
    post = frontmatter.load(path)
    done, pending = post["todo_provenance"]
    done["done_at"] = "2026-09-25T00:00:00"
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(frontmatter.dumps(post))
    await server.bucket_mgr.archive(identity)
    path = server.bucket_mgr._find_bucket_file(identity)
    legacy = await server.bucket_mgr.create("legacy body", todos=["legacy task"])
    legacy_path = server.bucket_mgr._find_bucket_file(legacy)
    post = frontmatter.load(legacy_path)
    post.metadata.pop("todo_provenance")
    with open(legacy_path, "w", encoding="utf-8") as handle:
        handle.write(frontmatter.dumps(post))
    before = {p: open(p, "rb").read() for p in (path, legacy_path)}
    for profile in PROFILE_TODOS:
        output = await server.boot(profile=profile)
        section = output.split("=== boot: 未完结 todos ===\n", 1)[1].split("\n\n", 1)[0]
        assert "共 2 项未完成" in section
        assert "pending" in section and "finished" not in section
        assert done["id"] not in section and pending["id"] in section
        assert "todo_id:null" in section and "legacy task" in section
        assert "todos()" not in section
        assert count_tokens_approx(output) <= server.BOOT_PROFILE_CONFIG[profile]["max_tokens"]
    assert {p: open(p, "rb").read() for p in before} == before


@pytest.mark.asyncio
async def test_dropped_identity_excluded_from_three_profiles_and_counts(server):
    identity = await server.bucket_mgr.create("carrier", todos=["abandoned", "active", "finished"])
    records = (await server.bucket_mgr.get(identity))["metadata"]["todo_provenance"]
    server.bucket_mgr.drop_todo(identity, records[0]["id"], lambda _: True)
    server.bucket_mgr.complete_todo(identity, records[2]["id"], lambda _: True)
    path = server.bucket_mgr._find_bucket_file(identity)
    before = open(path, "rb").read()
    for profile in PROFILE_TODOS:
        output = await server.boot(profile=profile)
        section = output.split("=== boot: 未完结 todos ===\n", 1)[1].split("\n\n", 1)[0]
        assert "共 1 项未完成 | 本次显示 1 | 未显示 0" in section
        assert records[1]["id"] in section
        assert records[0]["id"] not in section and records[2]["id"] not in section
        assert "abandoned" not in section and "finished" not in section
    assert open(path, "rb").read() == before


@pytest.mark.parametrize("profile", list(PROFILE_TODOS))
def test_drop_updates_tail_hidden_and_remaining_without_rotation_changes(profile):
    buckets = [bucket(i) for i in range(40)]
    before = todo_page(active_display_candidates(buckets), profile, DAY)
    dropped = before.rotation[0]
    for entry in buckets:
        if entry["id"] == dropped.bucket_id:
            entry["metadata"]["todo_provenance"][0]["dropped_at"] = "2026-09-26T12:00:00"
    after = todo_page(active_display_candidates(buckets), profile, DAY)
    assert len(after.candidates) == len(before.candidates) - 1
    assert dropped not in after.candidates and dropped not in after.rotation
    assert after.priority == before.priority
    fitted = fit_todos(after, 220)
    assert_counts(fitted, 39)
    assert fitted.hidden == 39 - fitted.shown
    assert dropped.todo_id not in fitted.text


@pytest.mark.asyncio
async def test_dual_terminal_boot_reports_conflict_and_keeps_checkpoint(server):
    checkpoint = server.bucket_mgr.get_boot_delta_checkpoint()
    bad = bucket(1)
    bad["metadata"]["todo_provenance"][0].update(done_at="2026-09-20", dropped_at="2026-09-21")
    server.bucket_mgr.list_all = AsyncMock(return_value=[bad])
    for profile in PROFILE_TODOS:
        result = await server.boot(profile=profile)
        assert "活动总数无法确认" in result and "共 0" not in result
    assert server.bucket_mgr.get_boot_delta_checkpoint() == checkpoint


@pytest.mark.asyncio
async def test_projection_error_does_not_claim_zero_or_consume_delta(server):
    checkpoint = server.bucket_mgr.get_boot_delta_checkpoint()
    bad = bucket(1)
    r = bad["metadata"]["todo_provenance"][0]
    bad["metadata"]["todo_provenance"] = [{**r, "done_at": "2026-09-20"}, {**r, "done_at": "2026-09-21"}]
    server.bucket_mgr.list_all = AsyncMock(return_value=[bad])
    result = await server.boot(profile="tg")
    assert "活动总数无法确认" in result and "共 0" not in result
    assert server.bucket_mgr.get_boot_delta_checkpoint() == checkpoint


@pytest.mark.asyncio
async def test_profile_counts_dynamic_limits_and_no_five_bucket_cap(server):
    data = [bucket(i, importance=1, text="待" * 400) for i in range(120)]
    server.bucket_mgr.list_all = AsyncMock(return_value=data)
    counts = []
    for profile in PROFILE_TODOS:
        output = await server.boot(profile=profile)
        section = output.split("=== boot: 未完结 todos ===\n", 1)[1].split("\n\n", 1)[0]
        T, S, hidden = map(int, re.search(r"共 (\d+) 项未完成 \| 本次显示 (\d+) \| 未显示 (\d+)", section).groups())
        assert T == 120 and S + hidden == 120 and S > 5
        assert S == sum(line.startswith("- [bucket_id:") for line in section.splitlines())
        assert count_tokens_approx(output) <= server.BOOT_PROFILE_CONFIG[profile]["max_tokens"]
        counts.append(S)
    assert len(set(counts)) > 1


@pytest.mark.asyncio
async def test_cas_refitting_reuses_page_and_final_counts(server, monkeypatch):
    await server.boot()
    await server.bucket_mgr.create("synthetic delta", todos=["pending"])
    data = [bucket(i, text="待" * 250) for i in range(40)]
    original_list = server.bucket_mgr.list_all
    async def listing(include_archive=False):
        return await original_list(include_archive=include_archive) + data
    monkeypatch.setattr(server.bucket_mgr, "list_all", listing)
    real_advance = server.bucket_mgr.advance_boot_delta_checkpoint
    attempts = []
    def advance(*args, **kwargs):
        attempts.append(args)
        return False if len(attempts) == 1 else real_advance(*args, **kwargs)
    monkeypatch.setattr(server.bucket_mgr, "advance_boot_delta_checkpoint", advance)
    real_fit = server._fit_sections_to_budget
    pages, emitted = [], []
    def fitting(sections, max_tokens, **kwargs):
        pages.append(kwargs["todo_display"])
        body, entries = real_fit(sections, min(max_tokens, 1000 if len(pages) == 1 else 2000), **kwargs)
        emitted.append(entries["todos"])
        return body, entries
    monkeypatch.setattr(server, "_fit_sections_to_budget", fitting)
    result = await server.boot()
    assert len(pages) >= 2 and all(page is pages[0] for page in pages)
    assert emitted[-1] in result
    final = emitted[-1]
    T, S, hidden = map(int, re.search(r"共 (\d+) 项未完成 \| 本次显示 (\d+) \| 未显示 (\d+)", final).groups())
    assert T == 41 and S + hidden == T
    assert S == sum(line.startswith("- [bucket_id:") for line in final.splitlines())


def test_global_fitter_reclaims_budget_and_keeps_other_sections_exact(server):
    plan = page_of(20, "tg")
    summary = fit_todos(plan, 0).text
    note = "=== note ===\n" + "留言" * 120
    sections = [("ting_note", "婷留言", note), ("todos", "todos", fit_todos(plan, 2000).text),
                ("pinned", "钉选", "钉" * 500)]
    for budget in (300, 600, 1500):
        body, emitted = server._fit_sections_to_budget(
            sections, budget, minimum_chars={"ting_note": len(note), "todos": 600, "pinned": 600},
            atomic_sections={"ting_note"}, todo_display=plan, return_sections=True,
        )
        assert count_tokens_approx(body) <= budget
        todo = emitted["todos"]
        T, S, hidden = map(int, re.search(r"共 (\d+) 项未完成 \| 本次显示 (\d+) \| 未显示 (\d+)", todo).groups())
        assert T == 20 and S + hidden == T
        assert S == sum(line.startswith("- [bucket_id:") for line in todo.splitlines())
        if "ting_note" in emitted:
            assert emitted["ting_note"] == note
        else:
            assert note not in body
        assert summary.splitlines()[0] in body

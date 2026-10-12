"""Synthetic storage tests for read-only derivative receipts after sealing."""
import importlib
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import frontmatter
import pytest


@pytest.fixture
def server(test_config, monkeypatch):
    import utils
    monkeypatch.setattr(utils, "load_config", lambda: test_config)
    monkeypatch.setenv("OMBRE_BUCKETS_DIR", test_config["buckets_dir"])
    monkeypatch.delenv("OMBRE_API_KEY", raising=False)
    sys.modules.pop("server", None)
    module = importlib.import_module("server")
    module.decay_engine.ensure_started = AsyncMock()
    module._fire_webhook = AsyncMock()
    return module


async def source(server, name="source"):
    return await server.bucket_mgr.create(content=f"source body {name}", name=name)


async def summary(server, sources, name="digest-summary", *, archived=False, sealed=False):
    identifier = await server.bucket_mgr.create(
        content=f"DERIVED_BODY_{name}", name=name, tags=["digest", "auto-digested"],
        domain=["project", "digest"], provenance_kind="summary", sealed=sealed,
    )
    assert await server.bucket_mgr.update(identifier, source_bucket=sources)
    if archived:
        assert await server.bucket_mgr.archive(identifier)
    return identifier


@pytest.mark.asyncio
@pytest.mark.parametrize("multiple", [False, True])
async def test_exact_source_id_single_or_multi_source(server, multiple):
    first, second = await source(server, "first"), await source(server, "second")
    references = f" {first}, {second}, {first} " if multiple else first
    digest = await summary(server, references, "open-summary")
    decoy = await summary(server, first + "-suffix", "prefix-decoy")
    result = await server.trace(first, sealed=1)
    assert f"- {digest} | open-summary" in result and decoy not in result
    assert "DERIVED_BODY" not in result and "可能还有未记录的副本" in result
    assert (await server.bucket_mgr.get(first))["metadata"]["sealed"] == 1
    assert (await server.bucket_mgr.get(digest))["metadata"]["sealed"] == 0
    if multiple:
        assert f"- {digest} | open-summary" in await server.trace(second, sealed=1)


@pytest.mark.asyncio
async def test_archive_and_sealed_copies_never_auto_seal_or_leak_details(server):
    original = await source(server)
    open_id = await summary(server, original, "archive-copy", archived=True)
    sealed_id = await summary(server, original, "PRIVATE_DERIVATIVE_NAME", sealed=True, archived=True)
    before = {}
    for identifier in (open_id, sealed_id):
        before[identifier] = Path((await server.bucket_mgr.get(identifier))["path"]).read_bytes()
    result = await server.trace(original, sealed=1)
    assert f"- {open_id} | archive-copy" in result and f"- {sealed_id}\n" in result
    assert "PRIVATE_DERIVATIVE_NAME" not in result and "DERIVED_BODY" not in result
    assert "auto-digested" not in result and "provenance_kind" not in result
    for identifier in (open_id, sealed_id):
        assert Path((await server.bucket_mgr.get(identifier))["path"]).read_bytes() == before[identifier]


@pytest.mark.asyncio
async def test_no_hits_preserve_exact_original_receipt(server):
    identifier = await source(server)
    assert await server.trace(identifier, sealed=1) == f"已修改记忆桶 {identifier}: sealed=1"
    assert await server.trace(identifier, sealed=0) == f"已修改记忆桶 {identifier}: sealed=0"
    assert await server.trace(identifier, importance=7) == f"已修改记忆桶 {identifier}: importance=7"


@pytest.mark.asyncio
async def test_reverse_pointer_and_related_are_not_descendant_copies(server):
    predecessor = await source(server, "predecessor")
    digest = await summary(server, predecessor, "current")
    await server.bucket_mgr.update(predecessor, digested=True, source_bucket=digest)
    related = await source(server, "related")
    server.bucket_mgr.mutate_related(digest, add=[related])
    ordinary = await source(server, "ordinary")
    await server.bucket_mgr.update(ordinary, source_bucket=digest, provenance_kind="summary")
    result = await server.trace(digest, sealed=1)
    assert result == f"已修改记忆桶 {digest}: sealed=1"
    assert all(identifier not in result for identifier in (predecessor, ordinary, related))


@pytest.mark.asyncio
@pytest.mark.parametrize("missing_marker", ["digest", "auto-digested", "domain"])
async def test_summary_semantics_require_writer_markers_together(server, missing_marker):
    original = await source(server)
    identifier = await summary(server, original)
    if missing_marker == "domain":
        await server.bucket_mgr.update(identifier, domain=["project"])
    else:
        await server.bucket_mgr.update(identifier, tags=[tag for tag in ["digest", "auto-digested"]
                                                        if tag != missing_marker])
    result = await server.trace(original, sealed=1)
    assert identifier not in result and "衍生" not in result


@pytest.mark.asyncio
async def test_redigest_forward_chain_does_not_report_predecessor_as_copy(server):
    original = await source(server)
    first = await summary(server, original, "first-summary")
    # Actual digest writer state transitions, including overwritten input list.
    await server.bucket_mgr.update(original, digested=True, source_bucket=first)
    second = await summary(server, first, "second-summary")
    await server.bucket_mgr.update(first, digested=True, source_bucket=second)
    result = await server.trace(original, sealed=1)
    assert f"- {first} | first-summary" in result and f"- {second} | second-summary" in result
    newest = await server.trace(second, sealed=1)
    assert newest == f"已修改记忆桶 {second}: sealed=1" and first not in newest
    middle = await server.trace(first, sealed=1)
    assert f"- {second}\n" in middle and original not in middle


@pytest.mark.asyncio
async def test_ambiguous_marked_multisource_is_not_inferred(server):
    original = await source(server)
    first, second = await summary(server, "", "first"), await summary(server, "", "second")
    await server.bucket_mgr.update(original, digested=True, source_bucket=f"{first},{second}")
    assert await server.trace(original, sealed=1) == f"已修改记忆桶 {original}: sealed=1"


@pytest.mark.asyncio
async def test_cycles_are_bounded_and_never_report_target_itself(server):
    first = await summary(server, "", "first")
    second = await summary(server, first, "second")
    await server.bucket_mgr.update(first, digested=True, source_bucket=second)
    await server.bucket_mgr.update(second, digested=True, source_bucket=first)
    result = await server.trace(first, sealed=1)
    assert f"- {second} | second" in result and f"- {first}" not in result


@pytest.mark.asyncio
async def test_batch_reports_each_success_and_missing_bucket_separately(server):
    first, second = await source(server, "first"), await source(server, "second")
    digest = await summary(server, f"{first},{second}", "shared")
    result = await server.trace(f"{first},missing-bucket,{second}", sealed=1)
    assert f"[{first}] 已修改记忆桶 {first}: sealed=1" in result
    assert f"[{second}] 已修改记忆桶 {second}: sealed=1" in result
    assert "[missing-bucket]" in result and "未找到" in result
    assert result.count(f"- {digest} | shared") == 2
    assert (await server.bucket_mgr.get(digest))["metadata"]["sealed"] == 0


@pytest.mark.asyncio
async def test_scan_failure_reports_seal_success_without_sensitive_exception(server, monkeypatch):
    original = await source(server)
    monkeypatch.setattr(server.bucket_mgr, "list_all", AsyncMock(
        side_effect=RuntimeError("PRIVATE_PATH PRIVATE_BODY PRIVATE_TOKEN")))
    result = await server.trace(original, sealed=1)
    assert result == f"已修改记忆桶 {original}: sealed=1\n已封存；副本清单暂无法确认。"
    assert "PRIVATE" not in result and (await server.bucket_mgr.get(original))["metadata"]["sealed"] == 1


@pytest.mark.asyncio
async def test_failed_or_missing_target_does_not_scan_or_claim_success(server, monkeypatch):
    original = await source(server)
    scan = AsyncMock(side_effect=AssertionError("do not scan after failed mutation"))
    monkeypatch.setattr(server.bucket_mgr, "list_all", scan)
    monkeypatch.setattr(server.bucket_mgr, "update", AsyncMock(return_value=False))
    assert await server.trace(original, sealed=1) == f"修改失败: {original}"
    missing = await server.trace("missing-bucket", sealed=1)
    assert "已封存" not in missing and "已修改" not in missing
    assert (await server.bucket_mgr.get(original))["metadata"]["sealed"] == 0
    scan.assert_not_called()


@pytest.mark.asyncio
async def test_name_visibility_rechecked_after_inventory(server, monkeypatch):
    original = await source(server)
    digest = await summary(server, original, "RACE_PRIVATE_NAME")
    get = server.bucket_mgr.get

    async def seal_before_report(identifier):
        if identifier == digest:
            path = Path((await get(identifier))["path"])
            post = frontmatter.load(path)
            post["sealed"] = 1
            path.write_text(frontmatter.dumps(post), encoding="utf-8")
        return await get(identifier)

    monkeypatch.setattr(server.bucket_mgr, "get", AsyncMock(side_effect=seal_before_report))
    result = await server.trace(original, sealed=1)
    assert f"- {digest}\n" in result and "RACE_PRIVATE_NAME" not in result
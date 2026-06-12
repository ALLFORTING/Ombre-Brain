import frontmatter
import pytest

from bucket_manager import BucketManager


@pytest.mark.asyncio
async def test_bucket_manager_applies_aliases_on_write_and_read(tmp_path):
    manager = BucketManager({"buckets_dir": str(tmp_path / "buckets")})
    bucket_id = await manager.create(
        content="婷易的内容",
        name="婷易记录",
        tags=["婷易", "朋友"],
        domain=["人物"],
    )

    bucket = await manager.get(bucket_id)

    assert bucket["content"] == "婷的内容"
    assert bucket["metadata"]["name"] == "婷记录"
    assert bucket["metadata"]["tags"] == ["婷", "朋友"]


@pytest.mark.asyncio
async def test_alias_cleanup_covers_dynamic_archive_and_preserves_dates(tmp_path):
    manager = BucketManager({"buckets_dir": str(tmp_path / "buckets")})
    dynamic_id = await manager.create(content="placeholder")
    archive_id = await manager.create(content="placeholder archive")
    await manager.archive(archive_id)

    for bucket_id in (dynamic_id, archive_id):
        path = manager._find_bucket_file(bucket_id)
        post = frontmatter.load(path)
        post.content = "婷易正文"
        post["name"] = "婷易名称"
        post["tags"] = ["婷易标签"]
        post["updated_at"] = "2026-01-02"
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(frontmatter.dumps(post))

    result = await manager.clean_display_aliases()

    assert result["scanned"] == 2
    assert result["changed_count"] == 2
    assert result["remaining"] == 0
    for bucket_id in (dynamic_id, archive_id):
        bucket = await manager.get(bucket_id)
        assert "婷易" not in bucket["content"]
        assert bucket["metadata"]["name"] == "婷名称"
        assert bucket["metadata"]["tags"] == ["婷标签"]
        assert bucket["metadata"]["updated_at"] == "2026-01-02"

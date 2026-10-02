"""Two planned fixtures, pure production metadata construction, exact readback."""
import hashlib
from pathlib import Path
import frontmatter
FIXTURES = [
    ("c10000000001", "OBWEB-LS-PUBLIC", "OBWEB-LS-SEED-PUBLIC-v1", False),
    ("c10000000002", "OBWEB-LS-HIDDEN", "OBWEB-LS-SEED-HIDDEN-v1", True),
]
def create_seed(ob):
    root = Path(ob.config["buckets_dir"])
    assert not root.exists(), "Fresh buckets root required; never overwrite prior evidence"
    from bucket_manager import BucketManager
    directory = root / "dynamic/obweb-ls"
    directory.mkdir(parents=True)
    for bid,name,body,sealed in FIXTURES:
        post = BucketManager._build_bucket_post(bid, body, tags=["obweb-ls"], importance=5,
                domain=["obweb-ls"], name=name, sealed=sealed)
        with (directory / (name + "_" + bid + ".md")).open("x", encoding="utf-8") as stream:
            stream.write(frontmatter.dumps(post))
async def readback(ob):
    rows = []
    for bid,name,body,sealed in FIXTURES:
        bucket = await ob.bucket_mgr.get(bid)
        assert bucket and bucket["content"] == body
        meta = bucket["metadata"]
        assert meta["id"] == bid and meta["name"] == name
        assert bool(meta["sealed"]) == sealed and meta["importance"] == 5
        assert meta["domain"] == ["obweb-ls"] and meta["tags"] == ["obweb-ls"]
        assert not meta.get("dormant") and not meta.get("resolved")
        rows.append(dict(id=bid, name=name, body_sha256=hashlib.sha256(body.encode()).hexdigest(),
                         sealed=sealed, importance=5, domain=meta["domain"], tags=meta["tags"],
                         body_readback_match=True))
    return rows


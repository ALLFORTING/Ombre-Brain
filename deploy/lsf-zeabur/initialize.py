"""One-time synthetic initialization; incomplete roots fail closed."""
import fcntl
import json
import os
from types import SimpleNamespace
from environment import BASELINE
from seed import create_seed
MARKER = {"format": 1, "baseline": BASELINE, "group": "L-SF", "fixture_ids": ["c10000000001", "c10000000002"]}

def lock_volume(root):
    handle = (root / ".service.lock").open("a")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BaseException:
        handle.close()
        raise RuntimeError("test_volume_already_in_use") from None
    return handle

def initialize(root):
    marker = root / ".synthetic-initialized.json"
    buckets = root / "buckets"
    stage = root / ".seed-staging"
    if marker.exists():
        if json.loads(marker.read_text()) != MARKER or not buckets.is_dir():
            raise RuntimeError("test_volume_identity_mismatch")
        return False  # Never re-seed or compare mutated fixture bodies on restart.
    if buckets.exists() or stage.exists() or (root / ".seed-marker.tmp").exists():
        raise RuntimeError("incomplete_seed_requires_review")
    stage.mkdir()
    create_seed(SimpleNamespace(config={"buckets_dir": str(stage / "buckets")}))
    os.rename(stage / "buckets", buckets)
    stage.rmdir()
    temp = root / ".seed-marker.tmp"
    with temp.open("x", encoding="utf-8") as stream:
        json.dump(MARKER, stream, sort_keys=True)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temp, marker)
    descriptor = os.open(root, os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return True

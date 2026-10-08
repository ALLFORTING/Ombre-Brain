#!/usr/bin/env python3
"""Operator-only: backfill baseline revisions from one v1 daily backup JSON.

Reads a ``backups/YYYY-MM-DD.json`` payload produced by ``/api/backup/export``
and records each bucket file in it as an ``op_kind=baseline`` revision, so
buckets whose legacy history is body-only gain one metadata baseline.

Dry-run is the default; ``--apply`` writes. Re-running is idempotent: every
row carries a ``baseline_key`` derived from the export time, path and file
digest, and existing keys are skipped. No server import, no runtime startup.
"""
import argparse
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import frontmatter

from bucket_revisions import BucketRevisionMixin, revision_snapshot
from bucket_write_lock import bucket_write_scope

LOCATIONS = ("permanent", "dynamic", "archive", "feel")


def baseline_entries(payload):
    """Return (exported_at, entries) for bucket files in one v1 backup payload."""
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise ValueError("unsupported_backup_schema")
    exported_at = str(payload.get("exported_at") or "")
    if not exported_at:
        raise ValueError("backup_exported_at_missing")
    entries, skipped = [], []
    for item in payload.get("files") or []:
        relative = str(item.get("path") or "")
        if relative.split("/", 1)[0] not in LOCATIONS or not relative.endswith(".md"):
            continue
        if item.get("encoding") != "utf-8":
            skipped.append({"path": relative, "reason": "not_utf8"})
            continue
        content = item.get("content") or ""
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        if item.get("sha256") and item["sha256"] != digest:
            skipped.append({"path": relative, "reason": "digest_mismatch"})
            continue
        try:
            post = frontmatter.loads(content)
        except Exception:
            skipped.append({"path": relative, "reason": "unparseable"})
            continue
        stem = Path(relative).name[:-3]
        bucket_id = str(post.metadata.get("id") or stem.rsplit("_", 1)[-1]).strip()
        if not bucket_id or not stem.endswith(bucket_id):
            skipped.append({"path": relative, "reason": "identity_unclear"})
            continue
        key = hashlib.sha256(f"baseline-v1|{exported_at}|{relative}|{digest}".encode("utf-8")).hexdigest()
        entries.append({"bucket_id": bucket_id, "relative_path": relative,
                        "snapshot": revision_snapshot(post, relative), "baseline_key": key})
    return exported_at, entries, skipped


def _existing_keys(db_path):
    if not db_path.exists():
        return set()
    with closing(sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True)) as conn:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='bucket_revisions'").fetchone():
            return set()
        return {row[0] for row in conn.execute(
            "SELECT baseline_key FROM bucket_revisions WHERE baseline_key IS NOT NULL")}


def migrate(buckets_dir, backup_path, *, apply=False):
    root = Path(buckets_dir).resolve()
    db_path = root / "bucket_history.sqlite3"
    payload = json.loads(Path(backup_path).read_text(encoding="utf-8"))
    exported_at, entries, skipped = baseline_entries(payload)
    known = _existing_keys(db_path)
    pending = [entry for entry in entries if entry["baseline_key"] not in known]
    summary = {"mode": "apply" if apply else "dry_run", "exported_at": exported_at,
               "bucket_files": len(entries), "already_present": len(entries) - len(pending),
               "to_insert": len(pending), "skipped": skipped, "inserted": 0}
    if not apply or not pending:
        return summary
    if not db_path.exists() or not (root / ".bucket-write.lock").exists():
        raise ValueError("buckets_dir_not_initialized")
    with bucket_write_scope(root), closing(sqlite3.connect(db_path)) as conn:
        BucketRevisionMixin._init_revision_schema(conn)
        conn.execute("BEGIN IMMEDIATE")
        present = {row[0] for row in conn.execute(
            "SELECT baseline_key FROM bucket_revisions WHERE baseline_key IS NOT NULL")}
        for entry in pending:
            if entry["baseline_key"] in present:
                continue
            BucketRevisionMixin._insert_revision(
                conn, entry["bucket_id"], entry["snapshot"], "baseline", exported_at,
                captured_at=exported_at, baseline_key=entry["baseline_key"])
            summary["inserted"] += 1
        conn.commit()
    return summary


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--buckets-dir", default=os.environ.get("OMBRE_BUCKETS_DIR"),
                        help="explicit OB buckets root (defaults to $OMBRE_BUCKETS_DIR)")
    parser.add_argument("--backup", required=True, type=Path, help="v1 backup JSON file")
    parser.add_argument("--apply", action="store_true", help="write rows (default: dry-run)")
    args = parser.parse_args(argv)
    if not args.buckets_dir or not Path(args.buckets_dir).is_dir():
        parser.error("buckets-dir must be an existing explicit directory")
    summary = migrate(args.buckets_dir, args.backup, apply=args.apply)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

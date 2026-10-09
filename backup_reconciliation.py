"""Read only semantic fingerprints of an already isolated staging snapshot."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sqlite3

import frontmatter

from offline_backup_bundle import BackupBundleError, _check_abort


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":"), default=str).encode("utf-8")).hexdigest()


def _group(records):
    records.sort(key=lambda item: item["id"])
    ids = [item["id"] for item in records]
    if len(ids) != len(set(ids)):
        raise BackupBundleError("manifest_invalid")
    return {"count": len(records), "sealed_count": sum(item["sealed"] for item in records),
            "ids": ids, "records": records, "digest": _digest(records)}


def snapshot_reconciliation(root: Path, entries: list[dict], *, abort_signal=None) -> dict:
    """No BucketManager initialization, aliases, touch, migration, or memory output."""
    buckets = []
    stores = {}
    try:
        for entry in entries:
            _check_abort(abort_signal)
            relative = entry["relative_path"]
            path = root / relative
            if (entry["entry_type"] == "regular" and path.suffix == ".md"
                    and relative.split("/", 1)[0] in {"permanent", "dynamic", "archive", "feel"}):
                post = frontmatter.load(path)
                identity = str(post.get("id", path.stem))
                if not identity:
                    raise ValueError()
                sealed = int(post.get("sealed", 0) or 0)
                if sealed not in (0, 1):
                    raise ValueError()
                buckets.append({"id": identity, "path": relative, "sealed": sealed,
                                "content_sha256": _digest(post.content),
                                "state_sha256": _digest(post.metadata)})
            elif entry["entry_type"] == "sqlite_snapshot":
                with sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True) as conn:
                    conn.row_factory = sqlite3.Row
                    for table, id_field, content_field in (("letters", "id", "content"),
                                                           ("notes", "note_id", "text")):
                        _check_abort(abort_signal)
                        if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                                            (table,)).fetchone():
                            continue
                        records = []
                        # Fixed identifiers, not input-controlled SQL.
                        for row in conn.execute(f'SELECT * FROM "{table}"'):
                            _check_abort(abort_signal)
                            state = dict(row)
                            content = state.pop(content_field)
                            sealed = state.get("sealed", 0)
                            if sealed not in (0, 1):
                                raise ValueError()
                            records.append({"id": str(state[id_field]), "sealed": sealed,
                                            "content_sha256": _digest(content),
                                            "state_sha256": _digest(state)})
                        stores[f"{relative}:{table}"] = _group(records)
        return {"schema_version": 1, "buckets": _group(buckets), "stores": stores}
    except BackupBundleError:
        raise
    except Exception:
        raise BackupBundleError("manifest_invalid") from None

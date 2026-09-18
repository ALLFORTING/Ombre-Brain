# Ordinary portable export

Phase 5.8a provides a local, CLI-only **ordinary portable export**. It is not
full disaster recovery, an importer, a cloud backup, or an MCP/Dashboard API.

Run it only with an explicit ordinary buckets directory, a new destination,
and explicit confirmation:

```bash
python portable_export.py \
  --buckets-dir /absolute/path/to/buckets \
  --destination /absolute/path/to/ombre-export \
  --confirm-export
```

The destination must not exist and must be outside the source directory. The
command prints only completion metadata and counts, never memory content.

## Format

The completed directory contains `manifest.json`, JSONL records under
`records/`, and cleaned asset blobs under `blobs/sha256/<sha256>`. The manifest
is written last and records the schema version, UTC export time, producer,
per-file SHA-256/byte counts, record counts, and omissions. JSON is canonical
and record ordering is deterministic except for `exported_at`.

It exports unsealed ordinary bucket bodies and frontmatter, available body
history, unsealed letters and notes, the emotion timeline when present, and
legacy AssetStore metadata plus verified privacy-cleaned stored bytes.

Historical bucket metadata is not available in current storage. Historical
frontmatter, provenance, todos, seal state, and links therefore cannot be
reconstructed by this export.

## Privacy and omissions

The portable profile excludes sealed buckets, their history, sealed letters,
and sealed notes. Links from visible buckets to non-visible buckets are removed
so exported records do not disclose hidden IDs. It exports neither Raw Evidence
nor the external Remember-Me data root. If `OMBRE_ASSET_AUTHORITY=rm`, the
command fails closed rather than copying an external authority.

It also excludes embeddings, dehydration cache, WAL/SHM and temporary files,
dashboard authentication, configuration/secrets, import state/journals,
boot-delta events/checkpoints, and runtime locks/state.

The exporter takes the existing process-local maintenance write freeze, uses
SQLite snapshots, verifies source inventory before and after capture, stages
the output, and only publishes a complete directory. It never mutates source
data. Independent external stores do not share that transaction boundary.

Sealed/privileged encrypted export, Remember-Me export, Raw Evidence export,
restore/import, scheduling, and remote transport are deliberately out of scope.

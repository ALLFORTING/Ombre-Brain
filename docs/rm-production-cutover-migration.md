# RM Production Cutover - Implementation C

Implementation C adds the local operator toolchain for the Remember-Me
production migration. It is intentionally separate from `server.py`: importing
the module does not start a server, select `OMBRE_ASSET_AUTHORITY`, change
Render configuration, deploy, or contact production.

## Explicit invocation

Every command requires absolute paths and an explicit report file:

```text
python -m remember_me_cutover_migration preflight-local \
  --legacy-root D:/data/buckets \
  --rm-root D:/data/buckets/remember-me \
  --state-db D:/data/buckets/state/migration.sqlite3 \
  --report D:/data/buckets/state/reports/cutover.json \
  --migration-identity rm-cutover-2026-08-14
```

The supported phases are `preflight-local`, `migrate`, `reconcile`, `verify`,
`reindex`, `inspect`, and `abort`. The input validator applies the accepted
Design A `validate_asset_storage_layout` rules and refuses relative paths,
owned-root collisions, symlinked roots, state database names outside the
accepted location, and dangerous report collisions.

The pinned contract inputs include package version, data-compatibility
identity, sanitizer/Pillow identity, MCP tool set, and the C checkpoint schema
version. They are explicit CLI options so a rehearsal cannot silently run
against a different RM package or state contract.

The normal sequence is:

1. Run `preflight-local` and review its structured JSON report.
2. Run `migrate` with `--resume` only when a previous checkpoint explicitly
   requires resumption.
3. Run `reconcile` and then `verify`; each is gated on its predecessor.
4. Run bounded `reindex` only after both gates pass. `--max-new-index-work`
   bounds new external embedding work.
5. Leave the state frozen at `frozen_ready_for_rm_switch`. Implementation C
   never calls the authority-switch transition; that belongs to D.

An ordinary failure does not release the freeze. The A `CutoverStateStore`
lease is renewed around each source read and target write, and every RM write
is preceded and followed by an ephemeral A `MutationCapability` check bound to
the active lease and migration purpose. A lost lease stops further writes and
returns exit status 7. A restarted process must explicitly resume after the
stale lease is recovered, or explicitly abort; an active lease is never
silently stolen.

## Source and target guarantees

The legacy source uses SQLite `mode=ro` with `PRAGMA query_only=ON` and reads
blob bytes directly. It never constructs `AssetStore` for the source and does
not create or mutate legacy files. A fixed keyset upper bound and a canonical
record/blob fingerprint are persisted in the checkpoint; any source change
blocks the run.

Migration uses the public Remember-Me Core `ImportAssetRequest` contract. It
preserves asset IDs, source/stored hashes, filenames, MIME/kind, dimensions,
timestamps, metadata, and tag timestamps. It does not import legacy vectors.
Reconciliation compares exact ID sets, metadata, tag timestamps, and stored
bytes/hashes through the public RM verification contract. Verification is a
separate gate and records blob counts, failure categories, and read-only RM
SQLite integrity checks.

Reindexing is RM-owned and resumable. It reports eligible/ineligible,
scanned/indexed/skipped/failed counters, provider/model fingerprint, external
call count, and the configured work budget. A disabled provider makes zero
external calls and reports `keyword_only` readiness; it does not authorize an
automatic production acceptance.

`inspect` is read-only and reports redacted state, lease, identity, checkpoint,
reconciliation, verification, and vector readiness. `abort` records the
operator reason before explicitly recovering an expired pre-switch freeze to
legacy authority. It never deletes RM partial/full state and never performs a
Class B rollback.

## Stable exit statuses

| Status | Meaning |
|---:|---|
| 0 | Success or bounded keyword-only readiness |
| 2 | Preflight failed |
| 3 | Migration blocked by a source/adapter condition |
| 4 | Migration/toolchain failure |
| 5 | Reconciliation or verification gate failed |
| 6 | Source changed since checkpoint |
| 7 | Freeze/lease lost or still owned elsewhere |
| 8 | Workspace, identity, or state invalid |
| 9 | Unexpected internal failure |

Reports contain tool/version, phase, timestamps, hashed identities, counts,
checkpoint summaries, freeze status, gate results, vector readiness, bounded
error codes, and exit status. They do not contain secrets, lease tokens, raw
asset content, or production access results.

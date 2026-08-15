# Remember-Me cutover operations — Implementation D1

This workspace is the D1 operational-safety layer for the accepted
Remember-Me production cutover. It is pinned to Ombre-Brain C commit
`bf3683852a30fcd09d44af80f2e114c7d9609a03` and the exact Remember-Me dev7
source commit `a00ea991442d7581a3856b178525a8e77da833fe`.

The operational module is intentionally not imported by `server.py`. It does
not choose an authority, change environment configuration, acquire a live
freeze, call an embedding provider, deploy, or execute a production
preflight. It only creates an explicitly requested backup and isolated
restore, or reads explicitly supplied local roots.

## Commands

Use the isolated Python 3.12 dev7 environment. Every root and report path
should be an absolute path. The backup destination must be new and outside
the managed roots.

```powershell
python -m remember_me_cutover_operations backup `
  --profile legacy-authoritative `
  --legacy-root D:\data\legacy `
  --rm-root D:\data\legacy\remember-me `
  --state-db D:\data\legacy\state\migration.sqlite3 `
  --destination D:\backups\rm-cutover-2026-08-15 `
  --report D:\reports\rm-cutover-backup.json
```

`frozen-ready` additionally requires the explicitly supplied RM root to
exist. Both profiles snapshot SQLite files through SQLite's backup API and
record SHA-256 hashes for regular files. Blob entries record only asset ID,
managed relative path, size, digest, and status; no asset content is placed
in reports.

```powershell
python -m remember_me_cutover_operations verify-backup `
  --backup D:\backups\rm-cutover-2026-08-15

python -m remember_me_cutover_operations restore `
  --backup D:\backups\rm-cutover-2026-08-15 `
  --legacy-root D:\rehearsal\legacy `
  --rm-root D:\rehearsal\remember-me `
  --state-root D:\rehearsal\state `
  --report D:\reports\rm-cutover-restore.json
```

Restore roots must be empty or new, distinct, and outside the backup. The
restore path does not update live defaults or authority configuration. It
reopens the legacy reader and state reader; when the restored RM database is
present it also initializes a local Remember-Me runtime solely under the
restored RM root.

```powershell
python -m remember_me_cutover_operations preflight `
  --legacy-root D:\data\legacy `
  --rm-root D:\data\legacy\remember-me `
  --state-db D:\data\legacy\state\migration.sqlite3 `
  --backup-root D:\backups\rm-cutover-2026-08-15 `
  --embedding-enabled false `
  --worker-count 1 `
  --multiprocess false `
  --report D:\reports\rm-cutover-preflight.json
```

Preflight is local and read-only. It reports storage ownership, SQLite
integrity, record/blob counts, missing and unreferenced blobs, target
classification, cutover state, exact dev7 contract, vector readiness without
provider calls, disk headroom, backup verification, and topology evidence.
Without explicit topology evidence it reports `UNKNOWN`; it never assumes a
single process is safe.

The readiness evaluator accepts a JSON evidence object and returns the pure
decision `READY_FOR_AUTHORITY_SWITCH: YES|NO`. A `NO` result is the safe
default for any missing or unknown hard gate. The evaluator itself cannot
perform a transition.

```powershell
python -m remember_me_cutover_operations readiness-gate `
  --evidence D:\reports\rm-cutover-readiness-evidence.json `
  --report D:\reports\rm-cutover-readiness.json
```

## Backup layout

Each backup has this fixed top-level shape:

```text
manifest.json
legacy/
remember-me/
state/
reports/
```

`manifest.json` contains the format/schema version, profile, timestamp,
component source IDs, package contract identity, cutover-state summary,
SQLite snapshot metadata, file counts/bytes/digests, blob manifest, and
explicit exclusions. Credential-looking files and SQLite sidecars are
excluded by name and never copied into the bundle. Process-local tickets and
capabilities are not persisted by this facility.

## D2 acceptance primitive

`run_frozen_acceptance_checks()` is the callback-driven primitive for D2. D2
supplies checks for RM health, reopen, authority consistency, MCP/Dashboard
routing, metadata/search/view/inspect/download reads, write rejection during
freeze, no legacy fallback/route, and ticket recreation across restart. The
primitive reports structured `PASS`, `FAIL`, or `UNKNOWN` evidence and never
changes cutover state.

The D1 result is operational readiness only. It is not production approval:
`PRODUCTION_PREFLIGHT_EXECUTED = NO`, `AUTHORITY_SWITCH_IMPLEMENTED = NO`,
and `PRODUCTION_CUTOVER_AUTHORIZED = NO`.

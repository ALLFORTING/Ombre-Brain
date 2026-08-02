# Maintenance write coverage

Schema version: `3`

Stage 8H-G1C uses one process-local `MaintenanceWriteCoordinator` for
production persistence mutations. Nested calls are reentrant and count as one
active writer.

| Component | Mutation operation | Persisted target | Lowest guarded boundary | Mode | Evidence |
| --- | --- | --- | --- | --- | --- |
| BucketManager | create/update/delete/dormant/archive | bucket Markdown and directories | hard public mutation method | async | G1C frozen tests and bucket regressions |
| BucketManager | read-side touch/time ripple | activation metadata in bucket Markdown | optional incidental mutation boundary | async | frozen read-availability tests |
| BucketManager | history/letters/seal | bucket history SQLite | history and letter methods | sync | AST coverage and full suite |
| BucketManager | alias cleanup/move | multiple bucket files | hard private mutation boundary | mixed | AST coverage and full suite |
| AssetStore | upload/import | asset SQLite plus image blob and cleanup | persist boundaries | sync | G1C frozen tests and AssetStore regressions |
| AssetStore | metadata/delete | asset SQLite plus blob quarantine | update/delete boundaries | sync | G1C frozen tests and AssetStore regressions |
| AssetStore | upload temp allocation | excluded transient upload | `create_temp_path` | sync | G1C frozen tests |
| Asset embeddings | index/delete/reindex | asset embedding table | final store and delete | mixed | semantic/reindex regressions |
| Bucket embeddings | store/delete | `embeddings.db` | store/delete methods | sync | full suite |
| Dehydrator | read-side cache store | dehydration cache SQLite | optional incidental mutation boundary | sync | frozen read-availability tests |
| Dehydrator | explicit cache invalidation | dehydration cache SQLite | hard cache mutation boundary | sync | frozen mutation tests |
| Migration state | lease/checkpoint/progress | migration state SQLite | public state mutations | sync | G1C and migration regressions |
| Legacy migration | migration batch | RM DB/blob plus checkpoint | batch runner | sync | Stage 8G-C/D regressions |
| Remember-Me adapter | ingest/update/delete/reindex/import | RM repository DB/blob | public adapter mutation | mixed | adapter/reindex regressions |
| Dashboard | password/config/vault writes | auth/config files | direct function or route | mixed | AST coverage and full suite |
| Emotion timeline | append snapshot | timeline JSON | snapshot writer | sync | AST coverage and full suite |
| Import engine | progress checkpoint | import state JSON | `ImportState.save` | sync | full suite |

Startup schema initialization is registered separately because the capture
controller cannot be used during startup. Capture fails closed when configured
for more than one worker. Browser, MCP, and import upload files are transient
staging only; formal AssetStore or bucket publication remains guarded.

`maintenance_write_coverage.py` first discovers every production Python module
in the repository, excluding only tests, virtual environments, build output and
caches. It then scans writable opens, file publication/removal, copy/move
operations, SQLite DML/DDL/commit, and non-constant SQL. Dynamic `open` modes,
dynamic or writable `os.open` flags, Path touch/rename/replace/unlink/link
methods, `copy2`, `copytree`, hard links, symlinks, and dynamic SQLite calls are
fail-closed discoveries. Exact non-Path name collisions are allowed only at
line-anchored call sites with a recorded reason. A module cannot evade coverage
by being absent from the registry.

Registrations declaring `guarded_mutation`, `guarded_async_mutation`, or
`guarded_http_mutation` are checked for the corresponding decorator. Manual
writer scopes are checked structurally, and guarded-caller-only helpers have a
narrow caller registry. Startup-only and isolated maintenance entries use
specific reason codes rather than file-wide exemptions. Tests remove a real
decorator and writer scope, introduce dynamic SQL, and add a new production
module with a bare write; each case fails coverage.

Reads remain available while draining or frozen. New writes fail immediately
with `maintenance_in_progress`; they do not wait for thaw. This guarantee is
limited to the current single Python process and single service instance.
Only registered read-side activation and dehydration-cache stores may decline
to enter and become no-ops during maintenance. They advance generation whenever
they really enter, and they do not suppress any other storage error. Explicit
cache invalidation and all user or administrator mutations remain hard-guarded.

Capture execution is controller-owned and asynchronous. Cancellation and the
monotonic maximum-freeze deadline set a cooperative abort signal checked by
inventory traversal, chunk hashing/copy, SQLite backup progress, archive
construction and encryption. The controller waits for the worker to exit and
contains all temporary or newly published output before the lease can thaw.
The initial inventory under the live lease is also authoritative for source
size and frozen free-space checks before staging. Encrypted output is bounded
while the temporary container is written, before formal publication. Delivery
hash workers receive the same join-before-release treatment, and completed
controller tasks are removed without removing terminal job evidence.

# Maintenance write coverage

Schema version: `1`

Stage 8H-G1C uses one process-local `MaintenanceWriteCoordinator` for
production persistence mutations. Nested calls are reentrant and count as one
active writer.

| Component | Mutation operation | Persisted target | Lowest guarded boundary | Mode | Evidence |
| --- | --- | --- | --- | --- | --- |
| BucketManager | create/update/delete/touch/dormant/archive | bucket Markdown and directories | public mutation method | async | G1C frozen tests and bucket regressions |
| BucketManager | history/letters/seal | bucket history SQLite | history and letter methods | sync | AST coverage and full suite |
| BucketManager | ripple/alias cleanup/move | multiple bucket files | private mutation boundary | mixed | AST coverage and full suite |
| AssetStore | upload/import | asset SQLite plus image blob and cleanup | persist boundaries | sync | G1C frozen tests and AssetStore regressions |
| AssetStore | metadata/delete | asset SQLite plus blob quarantine | update/delete boundaries | sync | G1C frozen tests and AssetStore regressions |
| AssetStore | upload temp allocation | excluded transient upload | `create_temp_path` | sync | G1C frozen tests |
| Asset embeddings | index/delete/reindex | asset embedding table | final store and delete | mixed | semantic/reindex regressions |
| Bucket embeddings | store/delete | `embeddings.db` | store/delete methods | sync | full suite |
| Dehydrator | cache store/invalidate | dehydration cache SQLite | cache mutation methods | sync | full suite |
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

`maintenance_write_coverage.py` scans production modules for writable opens,
file publication/removal, SQLite DML/commit, and related primitives. Each hit
must have a function-level registration or a narrow startup/transient reason.
A synthetic test proves that a newly added bare write is reported. There are no
file-level exemptions.

Reads remain available while draining or frozen. New writes fail immediately
with `maintenance_in_progress`; they do not wait for thaw. This guarantee is
limited to the current single Python process and single service instance.

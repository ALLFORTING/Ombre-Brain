# Remember-Me Integration

## Stage 8B baseline

Ombre-Brain pins the public Remember-Me Core to:

- official repository: `peanutsuee/Remember-Me`;
- current commit: `67240f5aa359ba94130b737b357f2f54190e6c3c`;
- archive SHA-256: `8139ece1e9e76464c01dadcc0817fbbe538e7bf59616f1c89252317d27e85053`;
- distribution: `remember-me`;
- package version: `0.1.0.dev6`;
- data compatibility: `ombre-brain-assets-v1`;
- sanitizer: `remember-me-pillow-v1`.

The dependency is an immutable GitHub source archive with a verified SHA-256
fragment. It does not use a branch, tag, Git checkout, Standalone extra,
submodule, vendored source, or sibling working tree.

Remember-Me is the long-term single public source for the image Core. Remember-Me
was originally created by Ting. The original creator is Ting (`peanutsuee`).

Stage 8B installs and validates that public Core only. The adapter is not
imported by `server.py`, `asset_dashboard.py`, or any production startup path.
Ombre-Brain still uses its existing `AssetStore`, nine existing `rm_asset_*`
tools, Dashboard routes, Viewer, transfer Tickets, authentication, and CSRF
behavior. This stage creates no second process, port, database, or runtime.

`remember_me_adapter.py` is the sole future host entry point. Importing it does
not read `OMBRE_BUCKETS_DIR`, create files, register MCP tools or HTTP routes,
or import `remember_me.standalone`. A caller must explicitly provide a `Path`
before one local runtime can be created. Stage 8C may use that boundary for
temporary runtime dual-run tests; Stage 8B does not connect it to production
configuration or data.

Stage 8G-B updates the immutable dependency to the public Stage 7H-A import
contract. `remember_me_import_adapter.py` is a local-fixture-only Host boundary
for one legacy image at a time. It calls only `RememberMeCore.import_asset()`;
it does not import RM repository or storage internals, wire a public MCP tool,
enable a runtime, execute Reindex, create Tickets or URLs, modify legacy data,
or implement production migration, dual-write, or shadow-write. The legacy
store exposes a read-only import record so tag timestamps are preserved while
the existing contained `resolve_file()` path remains the only blob locator. The
Stage 8G-B single-asset adapter is a local fixture utility and does not provide
a unified migration snapshot if another writer mutates legacy metadata between
`get_import_record()` and `resolve_file()`. Local fixture use must run without
concurrent legacy writes; any future production batch must solve this separately
with a write freeze, a single-transaction snapshot, or version checks.

## Stage 8G-C local migration batch core

Stage 8G-C adds only the local Host core needed to run one explicitly invoked,
bounded legacy `AssetStore` migration batch. It does not wire migration into
`server.py`, server startup, MCP, HTTP, the Dashboard, or production
configuration. The Remember-Me runtime remains default-off, no production image
has been migrated, and the legacy `AssetStore` remains the only production image
implementation.

Migration coordination lives in a separately and explicitly constructed
`migration.sqlite3`; it is never added to legacy `assets.sqlite3`, a
Remember-Me database, or a production configuration file. The state database
holds:

- one expiring, renewable, owner-token write-freeze lease;
- a monotonic legacy source generation;
- a versioned checkpoint bound to canonical source and target identities;
- a fixed initial upper-bound asset ID and cumulative migration counts.

An optional `AssetStore` write gate serializes each public legacy write with
freeze acquisition through `BEGIN IMMEDIATE` on the migration database. Before
touching legacy storage, a writer first commits a persistent
`write_uncertain` marker, then reacquires and retains the coordination
transaction through its complete legacy write lifecycle. Freeze acquisition
cannot claim safety while that transaction is active; if it races in the small
reacquisition interval, it sees the committed uncertainty marker and fails
closed. Once freeze acquisition commits, new
`create_temp_path`, `persist_upload`, `update_metadata`, and `delete` calls fail
closed. Legacy reads continue to work. Default `AssetStore(data_root)`
construction has no gate, creates no migration database, reads no migration
environment variable, and retains its existing behavior.

For a confirmed persistent legacy change, generation advancement and clearing
`write_uncertain` occur in the same migration-state commit. If the process
crashes, generation finalization fails, or that commit has an uncertain
outcome, the previously committed marker remains unless the generation advance
also committed. A later freeze or runner therefore stops with
`source_generation_uncertain` instead of trusting an old checkpoint. Known
no-op writes and failures that occur before any legacy mutation safely clear
the marker without advancing generation.

The runner pages strictly by `asset_id` using keyset pagination up to the
checkpoint's fixed upper bound. Each invocation processes at most its validated
batch size. It migrates an asset only by calling the Stage 8G-B
`LegacyAssetImportAdapter.import_asset()` with `dry_run=False`; it does not
construct the public Core request mapping itself or access Remember-Me
repository, storage, or database internals.

`imported` and `skipped_idempotent` results advance and persist the checkpoint
one asset at a time. A rejected asset blocks without advancing its cursor, and
an unexpected failure retains all earlier per-asset progress without advancing
the failed asset. A completed checkpoint is idempotent. If a legacy write
changes the persistent source generation between bounded batches, resume fails
closed with `source_changed_since_checkpoint`: it does not call the Adapter,
silently replace the snapshot generation, extend the upper bound, reset the
checkpoint, or restart from the beginning.

Stage 8G-C performs no Reindex, dual-write, shadow-write, or legacy deletion.
It provides no command-line entry point for real data. Production migration,
runtime enablement, cutover, and removal of the legacy implementation remain
future stages and require separate acceptance.

## Stage 8G-D local migration acceptance

Stage 8G-D adds local migration acceptance, reconciliation, and recovery
diagnostics on top of the Stage 8G-C batch core. It remains restricted to
factory-created Stage 8G-B fixtures and synthetic data. The production
Remember-Me runtime remains disabled, and the legacy `AssetStore` remains the
only production image implementation.

The run-to-completion coordinator repeatedly invokes the existing bounded
Stage 8G-C runner. It has validated batch and maximum-batch limits, releases
the runner's freeze between batches, detects no progress, and stops on
completed, blocked, failed, uncertain-source, changed-source, lease, or bounded
limit outcomes. It neither calls the single-asset Adapter nor changes the
checkpoint state machine.

Reconciliation starts only for a completed checkpoint whose canonical source
and target identities match the exact write-gated legacy store and the
Adapter's trusted fixture RM root. It acquires a short acceptance freeze,
then reads the checkpoint and source generation, pages the fixed legacy
snapshot by `asset_id`, and compares records obtained through the Adapter's
public-Core read contract. The lease is renewed during the scan and ownership
is checked after every target read and before the final result. Lease loss or
cleanup failure cannot produce a successful acceptance conclusion.
The public contract verifies IDs, source and stored hash metadata, filename,
MIME, kind, byte counts, dimensions, asset timestamps, title, description, and
tag values.

The pinned public Core does not expose persisted tag creation timestamps,
complete target inventory enumeration, unexpected or duplicate target
detection, a target-wide consistent snapshot, or cleaned blob bytes through
this trusted read contract. Stage 8G-D enforces these as a fixed unsupported
set. An Adapter declaration may add limitations but cannot remove a fixed
limitation; a declaration is not verification evidence. Each distinct check
is explicitly listed as unsupported in the structured report. A mismatch makes
the result failed even when other checks are unsupported. When all supported
fields match, the result remains `unsupported`: this stage has no reachable
`passed` result. Stored hash metadata is compared, but
`blob_verified_count` remains zero and the report does not claim byte-level
blob verification. Without target inventory enumeration,
`unexpected_target_count` remains unavailable rather than zero. Reports
contain bounded stable mismatch codes and counters, never image bytes, tokens,
raw database errors, internal objects, or filesystem paths.

Recovery diagnostics open existing state read-only and do not create a missing
database, clear an expired lease, or update any state. They
distinguish absent, resumable, active/expired lease, blocked, failed,
uncertain-source, changed-source, identity-incompatible, completed-unverified,
and partially verified conditions and return stable recommended action codes.
A completed checkpoint is unverified without a matching structured report.
A current, identity/generation/checkpoint-bound `unsupported` report is only
partially verified. Stage 8G-D does not produce `completed_verified`; even a
structurally consistent, manually constructed `passed` report is unsupported
as provenance and requires manual review.
They do not reset or delete checkpoints, clear uncertainty, force-release
leases, skip rejected assets, alter generation, or mark migration complete.

Opening `passed` and `completed_verified` in a future report contract requires
actual public read evidence for complete target inventory, unexpected and
duplicate detection, cleaned blob bytes, persisted tag creation timestamps,
and target snapshot consistency. It also requires a new report
contract/version that records the executed checks. Stage 8G-D does not claim
an attestation mechanism or a full-capability fake reader.

Stage 8G-D adds no production migration wiring and is not connected to server
startup, MCP, HTTP, the Dashboard, a CLI, or an environment-variable automatic
path. It does not access Render, migrate production images, run Reindex,
dual-write, shadow-write, clean up, or delete legacy data. Production
migration, runtime enablement, cutover, and legacy removal remain separate
future stages.

## Compatibility evidence

The controlled test environment uses Python 3.12, Pillow 12.3.0, and MCP
1.28.1. Synthetic PNG and JPEG fixtures produce byte-identical privacy-cleaned
outputs, stored hashes, MIME types, dimensions, extensions, and relative blob
paths under the current Ombre-Brain and pinned Remember-Me sanitizers.

Copied temporary Ombre-Brain data is readable and writable through the public
Core, then remains readable through the legacy Ombre-Brain store. Existing
asset rows, hashes, relative paths, and blobs remain unchanged during initial
repository construction. Initialization may add only:

- `asset_embeddings`;
- `idx_asset_embeddings_content_hash`;
- `idx_asset_embeddings_model`.

Production data must still be copied and accepted offline before any future
first connection. Stage 8B never reads a real buckets directory, database, or
image.

## Stage 8C Core shadow baseline

Stage 8C adds `remember_me_core_adapter.py`, a thin compatibility boundary over
the pinned public Remember-Me Core. It translates public Core DTOs and errors
into path-free Ombre-Brain structures without exposing source or stored hashes,
blob keys, relative paths, the data root, or database paths.

The adapter has no production caller. It is not imported by `server.py`, the
Dashboard, MCP registration, Viewer, transfer routes, authentication, or any
startup path. Production continues to use only the legacy `AssetStore`; there
is no automatic double write, background shadow task, telemetry, production
feature flag, second process, or second port. Vector search and embedding
reindex remain owned by the existing Ombre-Brain implementation.

Synthetic PNG and JPEG fixtures run the same business scenarios against
isolated legacy and Remember-Me temporary data roots. The shadow tests compare
privacy-cleaned bytes, stored hashes and relative paths internally, while
normalizing asset IDs and seconds-precision UTC timestamps at the compatibility
boundary. They also exercise sequential cross-runtime reopen behavior. The
tests never use a real `buckets_dir`, and legacy and Remember-Me writers are
never held open on the same data root.

Stage 8D may add a per-tool compatibility presenter after separately accepting
the preserved MCP snapshots. Stage 8C does not change either nine-tool
contract. Its rollback is to revert the Stage 8C adapter, tests, and this
section; no data or runtime switch is involved.

## Stage 8D MCP compatibility presenter

Stage 8D adds `remember_me_mcp_presenter.py` as an isolated presentation layer:

`Compatibility Presenter -> RememberMeCoreAdapter -> Remember-Me Core`

The Core Adapter owns Core DTO and error conversion. The Presenter owns only
the current Ombre-Brain JSON or `CallToolResult` envelopes. It does not register
tools, read host configuration, create a runtime, generate a Ticket, sign a
URL, or choose a data root. `server.py` has no Presenter import, and the legacy
`AssetStore` remains the only production image implementation.

The current OB contract already exposes `source_sha256` and `stored_sha256` in
metadata and download-link results. The Core Adapter has a narrow
`get_ob_public_metadata()` method for exactly those existing public fields.
No blob key, relative path, database path, data root, or additional hash is
exposed.

### Tool compatibility matrix

| Tool | Current production implementation | Stage 8D Presenter | RM Core | OB-retained capability | Future seam | Rollback |
| --- | --- | --- | --- | --- | --- | --- |
| `rm_asset_upload_link` | OB handler and source-aware upload Ticket store | Implemented by Stage 8F-I route flow | Yes | Authentication, 10-minute upload TTL, URL and expected-byte validation | Link creates a source-aware Ticket; route calls RM ingest only after browser upload is accepted | Restore Stage 8F-I files |
| `rm_asset_upload_status` | OB source-aware upload Ticket lifecycle | Implemented by Stage 8F-I route flow | Yes | Pending/uploading/completed state and status envelope | Status remains source-isolated over the upload Ticket result | Restore Stage 8F-I files |
| `rm_asset_get` | OB handler over legacy `AssetStore` | Implemented and test-only | Yes | Tool registration and JSON transport | Replace handler body with Presenter call after acceptance | Remove Stage 8D files |
| `rm_asset_update_metadata` | OB handler plus embedding refresh | Implemented and test-only | Yes | Tool registration and `AssetEmbeddingIndex.index_asset` side effect | OB handler calls Presenter, then retains embedding refresh | Remove Stage 8D files |
| `rm_asset_reindex_embeddings` | RM-enabled Presenter/Core; default-off legacy `AssetEmbeddingIndex` | Implemented by Stage 8F-J | Yes | Host provider configuration and exact four-counter MCP envelope | Presenter maps RM counters; enabled path never accesses the legacy index | Revert Stage 8F-J files |
| `rm_asset_search` | RM-enabled async Presenter/Core; default-off legacy fusion | Implemented by Stage 8F-H/J | Yes | Host provider configuration and exact public search envelope | RM Core performs query embedding, cosine, ranking and keyword fallback | Revert Stage 8F-H/J files |
| `rm_asset_download_link` | OB download Ticket store | Implemented with an injected seam | Yes | Five-minute download TTL, three-GET limit, Ticket, URL, authentication and headers | Inject a thin wrapper over the existing OB Ticket/URL helper | Remove Stage 8D files |
| `rm_asset_view` | OB handler and Viewer metadata | Implemented and test-only | Yes | Tool registration, `ASSET_VIEWER_TOOL_META`, resource URI and branding | Inject OB download collaborator and retain current registration | Remove Stage 8D files |
| `rm_asset_inspect` | OB handler over verified stored bytes | Implemented and test-only | Yes | Tool registration and external description | Replace handler body only after exact result acceptance | Remove Stage 8D files |

`RememberMeDownloadLinkCollaborator` is deliberately narrow. A test fake
returns deterministic `.invalid` URLs and fake Tickets. A future OB
implementation must wrap the existing download Ticket store and public-base-URL
logic; it must not create a second Ticket format. The source currently uses a
10-minute upload Ticket and a five-minute download Ticket, and Stage 8D changes
neither.

### Isolation and tests

Presenter unit tests use fixed records and a fake collaborator, then compare
complete OB envelopes for get, metadata update, download, view, inspect and
their errors. Real integration tests use synthetic PNG/JPEG files and isolated
temporary roots through the pinned RM Core. Runtime ownership remains enforced
by `RememberMeAdapter`; tests release one owner before reopening the same
temporary root. There is no automatic double write, background shadow task,
production feature flag, telemetry, or real `buckets_dir` access.

Stage 8D does not modify MCP registration, authentication, `/mcp?token=`,
Dashboard, CSRF, Viewer metadata, branding, Ticket stores, URL rules,
`EmbeddingEngine`, `AssetEmbeddingIndex`, dependencies, schema, or Render.
There is no production Presenter caller.

Stage 8E hardens the host collaborator and acceptance harness while keeping
them unwired. Production wiring remains deferred to Stage 8F and still must not
combine a tool-contract change, data format change, and runtime switch.
Reverting the Stage 8D Presenter, its tests, the narrow Core Adapter addition,
and this section fully rolls back this stage.

## Stage 8E Presenter hardening

Stage 8E removes the post-mutation read from the compatibility path for
`rm_asset_update_metadata`. `RememberMeCoreAdapter.update_ob_public_metadata()`
performs one Remember-Me mutation and converts the returned asset directly into
the existing OB public metadata shape. The older `update_metadata()` adapter
contract remains unchanged for Stage 8C callers.

`remember_me_download_links.py` adds a real but still unwired
`RememberMeObDownloadLinkCollaborator`. Its Ticket store, lock, clock, token
factory, public base URL, TTL, and capacity are explicit constructor
dependencies. It preserves the current five-minute Ticket payload and store
shape, URL-safe token constraints, and observable filename sanitizing,
extension, and 180-character truncation behavior. Invalid origins produce an
empty `download_url`; unknown collaborator failures are reduced to
`download_unavailable` without returning exception text or host details.

The collaborator and Presenter remain test-only boundaries. Stage 8E does not
modify `server.py`, MCP registration or tool schemas, the Viewer, Dashboard,
authentication, routes, Ticket globals, legacy `AssetStore`, or any production
startup path. There is no production caller, second writer, feature flag, or
runtime switch.

Production wiring is deferred to Stage 8F. Before that wiring can be accepted,
the collaborator must receive the same download Ticket store and lock consumed
by the existing download route; creating an isolated second store would yield
links that the route cannot redeem. Reverting the Stage 8E adapter,
collaborator, tests, and this section fully rolls back this hardening stage.

The safe MCP snapshots in `tests/fixtures/` record both nine-tool surfaces
separately. They deliberately preserve the current output-envelope, annotation,
Viewer metadata, and Ticket differences for a future Stage 8D compatibility
presenter. Both upload-link schemas omit the expected SHA-256 upload field.

## Stage 8F-A Host Runtime Bootstrap

Stage 8F-A adds only a default-off host bundle bootstrap in `server.py`. The
bundle can hold one Remember-Me runtime owner, a `RememberMeCoreAdapter`, the
compatibility Presenter, and an OB download-link collaborator. No MCP handler
or HTTP route calls the bundle in this stage.

`OMBRE_RM_RUNTIME_ENABLED` defaults to disabled. When disabled, startup returns
`None` for the host bundle, does not import `remember_me_host_runtime`, does not
read `OMBRE_RM_DATA_ROOT`, and does not create a Remember-Me runtime, data
directory, database, file, or lock. `OMBRE_RM_DATA_ROOT` has no default value,
and `config["buckets_dir"]` must not be used implicitly as a Remember-Me data
root.

When explicitly enabled, `OMBRE_RM_DATA_ROOT` must be present and absolute. A
missing, empty, invalid, relative, or failing runtime bootstrap fails closed and
prevents service startup. Startup logs only the stable texts
`remember-me runtime disabled`, `remember-me runtime enabled`, or
`remember-me runtime bootstrap failed`; it must not log the data root, exception
details, tokens, production domains, or user data.

The Stage 8F-A download collaborator receives the existing OB download Ticket
store and lock. The current `/rm/asset-download/{token}` route still redeems
Tickets through the old `AssetStore.resolve_file()` path. Sharing only the
Ticket store and lock is not sufficient to complete Remember-Me downloads:
before Stage 8F-B can switch `rm_asset_download_link` or `rm_asset_view`, the
download route must first abstract its asset resolver so an RM Ticket can be
resolved to the real RM Core blob.

Stage 8F-A does not deploy, migrate data, double write, shadow write, sync data,
change MCP schemas, add tools, add routes, switch the nine handlers, switch the
download route, touch Dashboard, Viewer, embedding search, Render, or real
production data. Rollback is to remove the host bootstrap module, the `server.py`
bootstrap assignment, tests, and this documentation.

## Stage 8F-B Download Resolver Abstraction

Stage 8F-B keeps the public download Ticket shape frozen at the same three
fields: `asset_id`, `expires_at`, and `get_count`. It adds only an in-process
source side table keyed by token. That table is guarded by the same download
lock, accepts only `legacy` or `remember_me`, and is never persisted or exposed
through MCP payloads, HTTP headers, logs, structured content, Dashboard, Viewer,
database rows, or files.

The `/rm/asset-download/{token}` route now resolves a Ticket according to that
internal source. A legacy Ticket can only use the existing `AssetStore` file
resolver. A Remember-Me Ticket can only use the Stage 8F-A host bundle's Core
Adapter to resolve clean bytes from Remember-Me Core. Missing legacy source rows
remain compatible and are treated as legacy. Unknown source rows fail closed and
retire the Ticket. There is no fallback across sources: RM failures do not try
`AssetStore`, and legacy failures do not try Remember-Me Core.

Default-off production behavior remains the same because the public MCP
`rm_asset_download_link` handler still creates legacy Tickets, `rm_asset_view`
still calls the legacy helper, and all nine image handlers continue to use the
old implementations. The upload route, Dashboard, Viewer, and embedding index
also remain on the old `AssetStore` path. The RM route capability can currently
be accepted only through isolated tests or a bundle-created internal RM Ticket;
no user-facing handler has switched to the Presenter.

Stage 8F-C may consider switching the first read-only handler after this route
resolver is accepted. Rollback is to remove the source side table, restore the
single legacy download resolver, remove `resolve_ob_download()`, remove the
collaborator source-store injection, and revert the Stage 8F-B tests and this
documentation. No data migration, double write, sync, Render change, or
production data access is involved.

## Stage 8F-C Wire rm_asset_get Only

Stage 8F-C switches only the read-only MCP `rm_asset_get` handler. With
the Remember-Me runtime flag absent or disabled, `rm_asset_get` continues
to use the existing `AssetStore` metadata path and keeps the legacy JSON
envelope. When the host runtime is explicitly enabled and bootstrapped,
`rm_asset_get` calls only the Stage 8F-A bundle Presenter and Core Adapter.

The enabled path is strict: a Remember-Me miss or failure returns the stable
`asset_unavailable` envelope and never falls back to the legacy `AssetStore`.
The Presenter normalizes the same public metadata fields and suppresses
malformed metadata, Core Adapter errors, JSON failures, and unknown
exceptions without exposing paths, blob keys, stored relative paths, data
roots, or exception text.

The other eight image handlers remain on the old implementation:
`rm_asset_upload_link`, `rm_asset_upload_status`,
`rm_asset_update_metadata`, `rm_asset_search`,
`rm_asset_reindex_embeddings`, `rm_asset_download_link`, `rm_asset_view`,
and `rm_asset_inspect`. Upload, download-link creation, Viewer, Inspect,
Search, Update, Dashboard, embedding, the Stage 8F-B download resolver,
Ticket shape, and source side table behavior are unchanged.

The current production Render environment must still keep the RM runtime
flag disabled. This stage is not a complete public migration release because
only one read-only handler is wired and no write, download-link, Viewer, or
search path has moved. Rollback is to restore the legacy `rm_asset_get`
handler and revert the Presenter hardening, Stage 8F-C tests, and this
documentation. Stage 8F-C performs no data migration, double write, shadow
write, sync, Render change, or production data access.


## Stage 8F-D Wire rm_asset_download_link Only

Stage 8F-D switches only the second read-only MCP handler,
`rm_asset_download_link`. With the Remember-Me runtime flag absent or disabled,
the handler continues to call the existing legacy download-link creator, keeping
legacy `AssetStore` behavior, Ticket TTL, filename handling, response fields,
and legacy Ticket source unchanged. When the host runtime is explicitly enabled
and bootstrapped, the handler calls only the Stage 8F-A bundle Presenter, Core
metadata lookup, and OB download-link collaborator.

The enabled path creates Remember-Me source Tickets in the existing in-process
source side table. The public Ticket body remains the same three fields:
`asset_id`, `expires_at`, and `get_count`; the source value is never returned in
MCP payloads, HTTP headers, logs, structured content, Dashboard, Viewer,
database rows, or files. The Stage 8F-B download route redeems those
Remember-Me Tickets through Core validation and returns the verified clean
bytes. `HEAD` checks do not increase `get_count`; `GET` returns bytes whose
SHA-256 matches `stored_sha256` and then increases `get_count`.

There is still no fallback across sources. Enabled misses, Core failures,
malformed metadata, collaborator failures, or handler exceptions return stable
`asset_unavailable`, `download_unavailable`, or `download_store_full` envelopes
and never call the legacy creator or `AssetStore`. The Presenter performs one
Core public-metadata query and one collaborator call on the success path, does
not read blobs, and does not expose paths, blob keys, stored relative paths,
source markers, data roots, or exception text.

At the end of this stage, `rm_asset_get` and `rm_asset_download_link` are wired.
The remaining seven image handlers stay on the old implementation:
`rm_asset_upload_link`, `rm_asset_upload_status`,
`rm_asset_update_metadata`, `rm_asset_search`,
`rm_asset_reindex_embeddings`, `rm_asset_view`, and `rm_asset_inspect`.
Upload, Viewer, Inspect, Search, Update, Reindex, Dashboard, embedding,
schema snapshots, tool counts, route counts, and the upload route are unchanged.

The current production Render environment must still keep the RM runtime flag
disabled. This is not a complete public migration release because uploads,
Viewer, Inspect, Search, Update, Reindex, and embedding remain legacy. Rollback
is to restore the `rm_asset_download_link` handler, revert the Presenter
hardening, Stage 8F-D tests, related static test updates, and this section. No
data migration, copy, double write, shadow write, sync, Render change, or
production data access is involved.

## Stage 8F-E Wire rm_asset_view Only

Stage 8F-E switches only the third read-only MCP handler, `rm_asset_view`.
With the Remember-Me runtime flag absent or disabled, the handler keeps the full
legacy Viewer behavior: it verifies bytes through the old `AssetStore` path,
creates the existing legacy fallback download Ticket, preserves the Viewer tool
metadata and static HTML resource, and ignores any invalid Remember-Me data-root
configuration without importing or creating the runtime.

When the host runtime is explicitly enabled and bootstrapped, `rm_asset_view`
uses only the Stage 8F-A bundle Presenter, Core `resolve_blob()` result, Core
public metadata, and the OB download-link collaborator. The inline image bytes
come from Remember-Me Core, are base64-encoded into the existing MCP Apps Viewer
`_meta.rememberMe` envelope, and the fallback Ticket source is `remember_me` in
the internal side table only. The public Ticket body remains exactly
`asset_id`, `expires_at`, and `get_count`; the source is not exposed in MCP
payloads, headers, logs, structured content, Dashboard, Viewer HTML, database
rows, or files.

The Stage 8F-B download route redeems the Viewer fallback Ticket through the
Remember-Me resolver and returns the Core-validated bytes. `HEAD` does not
increase `get_count`; `GET` returns bytes whose SHA-256 matches the asset
metadata and then increases `get_count`. There is still no fallback across
sources: enabled Viewer misses, Core failures, malformed image or metadata,
collaborator failures, source-store write failures, and unexpected exceptions
return only stable Viewer errors and never call legacy helpers or `AssetStore`.

The Presenter hardening keeps `resolve_blob()`, public metadata lookup, and
collaborator creation to one call each on the success path. It validates image
kind, MIME, dimensions, byte length, Pillow format, tags, and public metadata
without returning paths, blob keys, stored relative paths, data roots, source
markers, hashes, or exception text in the Viewer contract. Viewer
`download_store_full` collaborator failures are intentionally flattened to
`download_unavailable` because the Viewer error contract has no separate store
capacity code.

At the end of this stage, `rm_asset_get`, `rm_asset_download_link`, and
`rm_asset_view` are wired. The remaining six image handlers stay on the old
implementation: `rm_asset_upload_link`, `rm_asset_upload_status`,
`rm_asset_update_metadata`, `rm_asset_search`, `rm_asset_reindex_embeddings`,
and `rm_asset_inspect`. `rm_asset_inspect` still uses the legacy verified image
helper. Upload, Inspect, Search, Update, Reindex, Dashboard, embedding, schema
snapshots, tool counts, route counts, upload route behavior, Viewer static HTML,
and Viewer tool meta are otherwise unchanged.

The current production Render environment must still keep the RM runtime flag
disabled. This is not a complete public migration release because uploads,
Inspect, Search, Update, Reindex, and embedding remain legacy. Rollback is to
restore the `rm_asset_view` handler, revert the Presenter hardening, Stage 8F-E
tests, related static test updates, and this section. No data migration, copy,
double write, shadow write, sync, Render change, or production data access is
involved.

## Stage 8F-F Wire rm_asset_inspect Only

Stage 8F-F switches only the fourth read-only MCP handler,
`rm_asset_inspect`. With the Remember-Me runtime flag absent or disabled, the
handler keeps the full legacy Inspect behavior: it verifies bytes through the
old `AssetStore` path, applies the existing pixel-count guard, returns the
same TextContent plus ImageContent contract, and ignores invalid Remember-Me
data-root configuration without importing or creating the runtime.

When the host runtime is explicitly enabled and bootstrapped,
`rm_asset_inspect` uses only the Stage 8F-A bundle Presenter and Core
`resolve_blob()` path. Inspect bytes come from Remember-Me Core, are encoded
into the existing ImageContent response, and decode exactly to the Core clean
bytes. Inspect does not query public metadata, does not create a download
Ticket, does not expose or use a fallback URL, and never falls back to legacy
helpers in enabled mode.

The Presenter reuses the Stage 8F-E `_verified_image()` hardening for kind,
MIME, dimensions, byte length, Pillow format, tags, malformed mappings, Core
adapter errors, and unexpected exceptions. Inspect adds only output-envelope
hardening around base64 encoding, flattened structured metadata, TextContent,
ImageContent, and field access; envelope failures return the existing stable
`image_unavailable` Inspect error without leaking paths, tokens, data roots,
hashes, bytes, or exception text.

At the end of this stage, `rm_asset_get`, `rm_asset_download_link`,
`rm_asset_view`, and `rm_asset_inspect` are wired. The remaining five handlers
stay on the old implementation: `rm_asset_upload_link`,
`rm_asset_upload_status`, `rm_asset_update_metadata`, `rm_asset_search`, and
`rm_asset_reindex_embeddings`. Viewer resource HTML, Viewer fallback behavior,
the Stage 8F-B download route, upload route, Ticket three-field public shape,
source side table, schema snapshots, tool counts, route counts, Dashboard, and
embedding behavior are unchanged.

The current production Render environment must still keep the RM runtime flag
disabled. This is still not a complete public migration release because
uploads, Search, Update, Reindex, and embedding remain legacy. Rollback is to
restore the `rm_asset_inspect` handler, revert the Presenter envelope
hardening, Stage 8F-F tests, related static test updates, and this section. No
data migration, copy, double write, shadow write, sync, Render change, or
production data access is involved.

## Stage 8F-G Wire rm_asset_update_metadata Only

Stage 8F-G switches only `rm_asset_update_metadata`. It is the fifth wired
MCP handler and the first write operation in the Remember-Me integration. With
the Remember-Me runtime flag absent or disabled, the handler keeps the complete
legacy AssetStore metadata update path, including the best-effort legacy
embedding refresh. A legacy embedding refresh failure does not roll back the
metadata update.

When the host runtime is explicitly enabled and bootstrapped,
`rm_asset_update_metadata` calls only the Stage 8F-A bundle Presenter and the
Remember-Me Core metadata mutation. The enabled path performs exactly one Core
mutation, performs no post-mutation read, does not read blob bytes, does not
create a download Ticket, does not update the old AssetStore, does not refresh
the old embedding index, and never falls back to legacy. `None` preserves a
field, an empty string clears text fields, and an empty list clears tags; Core
remains the authority for validation and normalization.

The mutation may change only `title`, `description`, `tags`, and, when Core
decides the update is effective, `updated_at`. Blob bytes and immutable public
metadata such as asset id, hashes, byte counts, filename, MIME type, kind,
dimensions, and creation time remain unchanged. RM Search and Reindex are
still not wired, and enabled metadata updates intentionally do not shadow-write
to the legacy embedding index.

At the end of this stage, `rm_asset_get`, `rm_asset_download_link`,
`rm_asset_view`, `rm_asset_inspect`, and `rm_asset_update_metadata` are wired.
The remaining four handlers stay on the old implementation:
`rm_asset_upload_link`, `rm_asset_upload_status`, `rm_asset_search`, and
`rm_asset_reindex_embeddings`. Viewer, Inspect, the download route, upload
route, Ticket three-field public shape, source side table, schema snapshots,
tool counts, route counts, Dashboard, and static Viewer resources are
unchanged.

The current production Render environment must still keep the RM runtime flag
disabled. This is still not a complete public migration release because
uploads, Search, Reindex, and the RM embedding path remain separate follow-up
work. Rollback is to restore the `rm_asset_update_metadata` handler, revert the
Presenter hardening, Stage 8F-G tests, related static test updates, and this
section. No data migration, copy, double write, shadow write, sync, Render
change, or production data access is involved.

## Stage 8F-H Wire rm_asset_search Only

Stage 8F-H switches only `rm_asset_search`. It is the sixth wired handler.
When the RM runtime is disabled, the handler keeps the complete legacy Search
implementation: lexical AssetStore search, optional legacy embedding semantic
fallback, semantic scores, paging, filters, and legacy fallback behavior remain
unchanged. The disabled path does not import the RM runtime, read
`OMBRE_RM_DATA_ROOT`, or create RM runtime files.

When the RM runtime is enabled, `rm_asset_search` calls only the Stage 8F-A
bundle Presenter and RM Core Search once. Enabled Search does not call the old
AssetStore, old embedding index, per-item `get`, metadata read or update,
`resolve_blob`, `resolve_ob_download`, or any download collaborator. It creates
no Ticket, reads or writes no Ticket store, performs no embedding write, and
never falls back to legacy results. RM Core results are the only enabled Search
source.

Presenter Search returns only the existing public search envelope: top-level
`ok`, `total`, `offset`, `limit`, and `results`. Each result is cropped to the
public fields accepted by the current OB contract. Private fields such as
decoded bytes, hashes, blob keys, relative paths, data roots, backend/source
markers, download URLs, and internal repository fields are not exposed.
`semantic_score` is retained only when it is finite, between 0 and 1, and paired
with the `semantic` match reason; otherwise the whole Search result is rejected
with the stable Search error envelope.

At the end of this stage, `rm_asset_get`, `rm_asset_download_link`,
`rm_asset_view`, `rm_asset_inspect`, `rm_asset_update_metadata`, and
`rm_asset_search` are wired. The remaining three handlers stay on the old
implementation: `rm_asset_upload_link`, `rm_asset_upload_status`, and
`rm_asset_reindex_embeddings`. Viewer, Inspect, metadata update, download and
upload routes, Viewer fallback, Ticket three-field public shape, source side
table, schema snapshots, tool counts, route counts, Dashboard, static Viewer
resources, and the legacy Reindex path are unchanged.

The current production Render environment must still keep the RM runtime flag
disabled. This is still not a complete public migration release because upload
link/status and Reindex remain separate follow-up work. Rollback is to restore
the `rm_asset_search` handler, revert Presenter Search hardening, the optional
Core Adapter search-only error mapping, Stage 8F-H tests, related static test
updates, and this section. No data migration, copy, double write, shadow
write, sync, Render change, or production data access is involved.

## Stage 8F-I Wire Atomic RM Browser Upload Flow

Stage 8F-I switches `rm_asset_upload_link`, `rm_asset_upload_status`, and
`/rm/asset-upload/{token}` together as one atomic upload flow. The link creates
the upload Ticket, the browser route consumes that same Ticket and persists the
file, and status reads the same Ticket result. Switching only one part would
split the source of truth for a single upload lifecycle.

When the RM runtime is disabled, upload link, status, and browser POST keep the
complete legacy behavior: legacy validation, URL/path/TTL/max-bytes contracts,
legacy AssetStore filename sanitizer, `asset_store.create_temp_path`,
`asset_store.persist_upload`, image validation, deduplication, status payloads,
HTML pages, and error status codes remain unchanged. The disabled path does not
import the RM runtime, read `OMBRE_RM_DATA_ROOT`, or create RM runtime files.

When the RM runtime is enabled, link creates a `remember_me` source upload
Ticket. The source is stored only in an internal side table guarded by the same
upload lock as the existing upload item and token stores. The public link JSON,
status JSON, and HTML never include the source. Historical in-memory Tickets
without a source side-table entry are treated as legacy for compatibility;
unknown sources fail closed and are retired.

The upload route dispatches strictly by Ticket source, not by the current flag
alone. A legacy source writes only the old AssetStore. A `remember_me` source
uses a host-owned system temporary file, verifies streamed byte count and SHA-256
against the bytes read back from that temporary file, then calls RM Core through
`RememberMeCoreAdapter.ingest_ob_public_metadata()` exactly once. It does not
call `ingest_image`, Core get, metadata read, Search, update, blob/download
resolution, the old AssetStore, the old embedding index, or any download Ticket
collaborator. Temporary upload files are not placed in the repository, legacy
AssetStore data root, or RM data root, and are deleted after success or failure.

RM upload mutation results are normalized before status completion. The route
accepts only public OB metadata fields plus a strict boolean `deduplicated`,
requires 32-character lowercase hex asset IDs, 64-character lowercase hex source
and stored hashes, exact source-hash and decoded-byte matches, image kind,
PNG/JPEG MIME, positive dimensions, string timestamps/title/description, and
string-only tags. Private repository fields such as blob keys, stored relative
paths, backend/source markers, data roots, paths, download fields, and embedding
fields are cropped and never stored in the public status result.

Public contracts remain unchanged. Upload link still returns only `ok`,
`upload_id`, `upload_path`, `upload_url`, `status_path`, `expires_in_seconds`,
and `max_bytes`. Upload status still returns only the existing pending/completed
envelope fields. GET returns the existing upload HTML with the same security
headers. POST keeps the existing public success page with `asset_id`,
`stored_sha256`, `stored_bytes`, and `deduplicated`; errors return only HTTP
status codes and never expose source, paths, hashes, bytes, tokens, data roots,
or exception text.

At the end of this stage, `rm_asset_upload_link`, `rm_asset_upload_status`,
`rm_asset_get`, `rm_asset_download_link`, `rm_asset_view`, `rm_asset_inspect`,
`rm_asset_update_metadata`, and `rm_asset_search` are wired. Only
`rm_asset_reindex_embeddings` remains on the legacy implementation. The Viewer
resource, Viewer fallback, download route and Ticket side table, Dashboard,
diagnostic upload routes, schema snapshots, tool counts, route counts, and all
environment variables remain unchanged.

The current production Render environment must still keep the RM runtime flag
disabled. This is still not a complete public migration release because Reindex
remains follow-up work. Rollback is to restore the upload link/status handlers,
route source dispatch, upload source side table, host upload result hardening,
Stage 8F-I tests, related static test updates, and this section. No data
migration, copy, double write, shadow write, sync, Render change, or production
data access is involved.

## Stage 8F-J RM Reindex and Semantic Provider Wiring

Stage 8F-J updates the immutable Remember-Me dependency pin to
`5c430d3f265be059198fe230c1a0682e23e89e32` and completes 9/9
RM-enabled Core ownership. The Python RM Search API is async, so the OB
CoreAdapter, Presenter, and server handler now await it end to end without a
synchronous wrapper, event-loop bridge, or worker thread.

OB creates one process-lifetime `RememberMeVectorProviderAdapter` over the
existing `EmbeddingEngine` and passes that exact instance into the RM runtime
factory. Search and Reindex therefore use the same provider and model identity.
The adapter calls the public `EmbeddingEngine.embed_text()` entry once. Its
model identity contains a normalized backend label, a short SHA-256 fingerprint
of the normalized endpoint, and the stripped model name. Raw endpoints, user
information, passwords, query strings, fragments, API keys, and tokens are not
placed in the identity or public errors. The default standalone RM factory still
constructs `NullVectorProvider` when no Host provider is supplied.

When RM is enabled, `rm_asset_reindex_embeddings` calls the Presenter and Core
once. RM Core owns canonical text, content hashes, current/stale decisions,
provider calls, per-item isolation, and embedding persistence. Presenter hides
RM's internal `enabled` and `model_id` fields and preserves the existing sorted
four-counter JSON and stable `invalid_limit` / `asset_unavailable` envelopes.
Cancellation propagates through provider, Search, Reindex, CoreAdapter,
Presenter, and handler boundaries.

Default-off or unavailable runtime behavior remains on the legacy handlers. In
normal RM-enabled Search and Reindex, the old `AssetEmbeddingIndex`, legacy
asset database, provider loop, cosine code, and legacy vector rows are not
accessed. The legacy and RM embedding stores remain separate. This stage performs
no migration, copy, schema merge, dual write, shadow write, background backfill,
or deletion of old vectors. Bootstrap fails closed if the configured RM data
root resolves to the legacy OB asset root, preventing both stores from sharing
one `assets.sqlite3`. Existing legacy vectors remain intact but are not visible
to RM-enabled Search. A user who enables RM must explicitly run Reindex to create
vectors in the RM database; those new vectors are immediately consumed by Search
through the same runtime.

The nine tool names, registration order, input schemas, public Search fields,
Reindex counters, error envelopes, upload behavior, Viewer behavior, Tickets,
and default-off behavior remain unchanged. No Render deployment, production
configuration change, production data migration, or automatic backfill is part
of this stage.

## Rollback

Stage 8B has no runtime switch to undo. Reverting its commit removes the fixed
dependency, adapter, tests, snapshots, and this documentation. Existing image
code and data behavior remain untouched throughout.

## License and origin

Remember-Me uses CPAL-1.0 and includes preserved upstream MIT attribution.
Ombre-Brain does not modify Remember-Me source in this stage. Any future
Ombre-Brain modification of Remember-Me Covered Code must preserve applicable
origin and upstream notices and make the corresponding Covered Code and change
record available as required by the license.

# Remember-Me Integration

## Stage 8B baseline

Ombre-Brain pins the public Remember-Me Core to:

- official repository: `peanutsuee/Remember-Me`;
- commit: `184e223c6392fd14dd5cfa73227d41f46d90e3c8`;
- distribution: `remember-me`;
- package version: `0.1.0.dev5`;
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
| `rm_asset_upload_link` | OB handler and upload Ticket store | Not implemented | No | Authentication, 10-minute upload TTL, URL and expected-byte validation | Call RM ingest only after OB accepts and completes the upload | Remove Stage 8D files |
| `rm_asset_upload_status` | OB upload Ticket lifecycle | Not implemented | No | Pending/uploading/completed state and status envelope | None until the OB upload lifecycle is deliberately migrated | Remove Stage 8D files |
| `rm_asset_get` | OB handler over legacy `AssetStore` | Implemented and test-only | Yes | Tool registration and JSON transport | Replace handler body with Presenter call after acceptance | Remove Stage 8D files |
| `rm_asset_update_metadata` | OB handler plus embedding refresh | Implemented and test-only | Yes | Tool registration and `AssetEmbeddingIndex.index_asset` side effect | OB handler calls Presenter, then retains embedding refresh | Remove Stage 8D files |
| `rm_asset_reindex_embeddings` | OB `AssetEmbeddingIndex` | Not implemented | No | Provider, model, stale detection and counters | Revisit only after vector ownership is migrated | Remove Stage 8D files |
| `rm_asset_search` | OB keyword/vector fusion | Not implemented | No | `EmbeddingEngine`, semantic scores, fusion, sorting and fallback | Migrate search as one accepted contract, never half-switch | Remove Stage 8D files |
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
presenter. Both upload-link schemas omit `expected_sha256`.

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

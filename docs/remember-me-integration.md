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

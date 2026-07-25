# MCP Tool Audit

Audit date: July 24, 2026.

Scope: tools registered through `@mcp.tool` in `server.py`. HTTP custom routes are excluded. The fixed `ui://remember-me/asset-viewer.html` MCP resource is noted separately.

## Inventory Summary

The server registers **21 formal MCP tools by default**. The 15 diagnostic
tools remain implemented, but are registered only when `OMBRE_DIAG_TOOLS` is
explicitly enabled:

| Category | Count | Intended audience |
|---|---:|---|
| Core memory and session | 9 | Ordinary users and model workflows |
| Formal Remember-Me assets | 8 | Ordinary users and model workflows |
| Administration and maintenance | 4 | Operators or controlled maintenance |
| Diagnostic probes | 15 | Developers and acceptance testing only; disabled by default |
| **Default total** | **21** | |
| **Diagnostic-enabled total** | **36** | |

The ordinary tool surface no longer includes the diagnostic probes. Their
functions, routes, and tests remain available for development and acceptance
work. Set `OMBRE_DIAG_TOOLS` to `1`, `true`, `yes`, or `on` to register them;
empty and all other values keep them disabled. Ordinary users should not
enable this profile. The centralized `@diagnostic_tool` registrations in
`server.py` are the authoritative diagnostic inventory.

## Tool-by-Tool Review

All registrations are in `server.py`. Asset persistence is implemented with `asset_store.py`; asset vectors use `asset_embedding_index.py`; the MCP Apps viewer uses `asset_viewer.py` and generated `asset_viewer.html`.

| Tool | Module | Purpose | Audience | Formal | Overlap or misuse risk | Recommendation |
|---|---|---|---|---|---|---|
| `breath` | Memory retrieval | Surface or search memories, including semantic and filtered retrieval | User/model | Yes | Large parameter surface blurs search, surfacing, mailbox, and emotion modes | Keep; later put specialist modes behind workflows |
| `hold` | Memory write | Store one memory with tagging, merge, emotion, and trigger metadata | User/model | Yes | Can be confused with `grow` for long input | Keep; sharpen routing guidance |
| `grow` | Memory write | Split and archive journal-style content into multiple memories | User/model | Yes | Overlaps `hold` at the content-length boundary | Keep; add a write workflow that chooses `hold` or `grow` |
| `trace` | Memory mutation | Update, append, replace, relate, merge, seal, or delete buckets | User/model power operation | Yes | Highly overloaded and includes destructive actions | Keep; later introduce narrower action groups and confirmations |
| `archive_session` | Session memory | Save a conversation summary, mood, highlights, and handoff letter | Model workflow | Yes | Usually belongs to an end-of-session sequence | Keep; add a prompt/workflow wrapper |
| `todos` | Memory read | Summarize unresolved todos | User/model | Yes | Read result overlaps `boot` context | Keep compatibility; candidate resource |
| `boot` | Startup context | Assemble pinned context, letters, sessions, and todos | Model workflow | Yes | Composite operation is better invoked as a startup workflow | Keep; add a prompt/workflow entry |
| `pulse` | Memory status | Return system status and bucket listings | User/admin | Yes | Can be selected when `breath` is intended | Keep compatibility; candidate status resource |
| `dream` | Reflection workflow | Read recent memories or requested details for reflection | Model workflow | Yes | Name is ambiguous and overlaps `breath` detail retrieval | Keep; prefer a documented reflection prompt/workflow |
| `rm_asset_upload_link` | RM asset ingest | Create a signed browser upload for persistent assets | User/model | Yes | Confusable with the Stage 0 browser probe | Keep; make this the only ordinary upload tool |
| `rm_asset_upload_status` | RM asset ingest | Read persistent upload completion and metadata | User/model | Yes | Lifecycle companion rather than a user intent | Keep; present through an upload workflow |
| `rm_asset_get` | RM asset metadata | Read safe metadata for one asset | User/model | Yes | Overlaps search-result metadata | Keep compatibility; candidate `rm://assets/{asset_id}` resource |
| `rm_asset_update_metadata` | RM asset metadata | Replace title, description, or tags without changing bytes | User/model | Yes | May be called before the image is actually inspected | Keep; workflow should require inspection before visual claims |
| `rm_asset_search` | RM asset retrieval | Hybrid keyword, filter, and semantic search | User/model | Yes | Low risk; primary discovery entry | Keep |
| `rm_asset_download_link` | RM asset delivery | Create a short-lived direct download | User/model | Yes | Can be selected when inline display is intended | Keep; clarify download intent versus `rm_asset_view` |
| `rm_asset_view` | RM user display | Render one cleaned image to the user through MCP Apps | User-facing | Yes | Easily confused with model vision inspection | Keep separate; route user display here |
| `rm_asset_inspect` | RM model vision | Return one cleaned image as MCP `ImageContent` | Model-facing | Yes | Easily confused with user display or metadata reads | Keep separate; route visual understanding here |
| `rm_asset_reindex_embeddings` | RM maintenance | Backfill missing or stale asset vectors | Admin/maintenance | Maintenance | External API cost and unnecessary repeat calls | Keep, but make administrator-only |
| `digest` | Memory maintenance | Plan or run digestion of old low-importance memories | Admin/maintenance | Maintenance | Mutating mode can be invoked despite dry-run default | Keep, but make administrator-only and workflow-confirmed |
| `related_backfill` | Memory maintenance | Plan or write semantic related links | Admin/maintenance | Maintenance | Broad maintenance can be mistaken for search | Keep, but make administrator-only |
| `seal_letter` | Sealed memory | Hide or unhide a handoff letter | Admin/model internal | Maintenance | Narrow implementation detail exposed at top level | Keep compatibility; move behind sealed-memory administration |
| `asset_attachment_context_probe` | Stage 4 attachment boundary | Detect only whether standard MCP context exposes attachment reference, byte, or MIME signals | Developer diagnostics | No | A false result can be misread as disproving the separate Claude code-execution upload path | Keep diagnostic-only; Stage 4B uses the accepted container-to-signed-endpoint path and does not depend on this probe |
| `asset_ingest_probe` | Stage 0 transport | Test one-call base64 upload and hashing | Developer diagnostics | No | Looks like real upload and moves base64 through the model | Mark deprecated; diagnostic-only, then remove |
| `asset_ingest_begin` | Stage 0 transport | Start an in-memory chunked base64 upload | Developer diagnostics | No | Part of a four-tool state machine | Diagnostic-only; later merge into one action tool |
| `asset_ingest_chunk` | Stage 0 transport | Submit one ordered base64 chunk | Developer diagnostics | No | Model may attempt production transfer through repeated calls | Diagnostic-only; later merge, then remove |
| `asset_ingest_finish` | Stage 0 transport | Verify and discard a chunked upload | Developer diagnostics | No | Meaningless without preceding diagnostic state | Diagnostic-only; later merge, then remove |
| `asset_ingest_abort` | Stage 0 transport | Abort a chunked diagnostic upload | Developer diagnostics | No | Meaningless to ordinary users | Diagnostic-only; later merge, then remove |
| `asset_browser_upload_link` | Stage 0 transport | Create a temporary non-persistent browser upload | Developer diagnostics | No | Nearly identical flow to formal persistent upload | Hide from ordinary clients; later deprecate |
| `asset_browser_upload_status` | Stage 0 transport | Read temporary browser-upload results | Developer diagnostics | No | Easily confused with `rm_asset_upload_status` | Hide with its probe; later remove |
| `asset_render_probe` | Stage 0 vision | Return a built-in PNG as `ImageContent` | Developer diagnostics | No | May be mistaken for a stored user asset | Diagnostic-only |
| `asset_export_probe` | Stage 0 export | Return a built-in image as base64 JSON | Developer diagnostics | No | Encourages a transport rejected for real files | Mark deprecated first; diagnostic profile only |
| `asset_vision_challenge` | Stage 0 vision | Create a machine-scored blind visual trial | Developer diagnostics | No | Can be called during normal asset tasks | Diagnostic-only; group under one vision workflow |
| `asset_vision_verify` | Stage 0 vision | Score and consume a blind trial | Developer diagnostics | No | Requires hidden state and a preceding challenge | Diagnostic-only; group with challenge |
| `asset_vision_export` | Stage 0 vision | Export a trial as base64 JSON | Developer diagnostics | No | Model-relayed base64 is known to be unreliable | Mark deprecated; diagnostic profile only |
| `asset_vision_download_link` | Stage 0 vision | Create a short-lived trial download | Developer diagnostics | No | Can be confused with formal asset download | Diagnostic-only; group with challenge |
| `asset_vision_upload_challenge` | Stage 0 vision | Create a blind normal-attachment control trial | Developer diagnostics | No | Looks like an ordinary upload task | Diagnostic-only; group under one vision workflow |

## Existing MCP Resource

`ui://remember-me/asset-viewer.html` is a fixed MCP Apps UI resource registered in `server.py` and backed by `asset_viewer.py` plus the generated HTML bundle. It is correctly a resource rather than a user-selected tool and should remain paired with `rm_asset_view`.

## Highest-Priority Issues

1. **Diagnostic probes previously dominated the flat list.** The 15 probes now remain disabled by default and are restored only through the explicit diagnostic profile.
2. **Temporary and formal asset tools have confusingly similar names.** `asset_browser_upload_*` versus `rm_asset_upload_*`, and vision-download versus formal asset-download tools, invite incorrect selection.
3. **Some tools are protocols rather than user intentions.** Chunked ingest, blind vision testing, startup, and session archival are better represented by workflows or prompts.
4. **Read-only data is overrepresented as tools.** Asset metadata, todos, and status/list views are candidates for resources.
5. **Several mutating tools are too broad for an ordinary flat surface.** `trace`, `digest`, reindexing, backfill, and sealed-letter operations need clearer privilege and confirmation boundaries.

## Target Tool Structure

Do not replace the current surface in one release. The target is a smaller ordinary-user list with compatibility aliases retained during migration.

### Ordinary memory

- Keep `breath`, `hold`, and `grow` as recognizable core operations.
- Keep `trace` initially, then introduce narrower metadata, relationship, and destructive action groups before deprecating broad modes.
- Move startup, reflection, and session-close orchestration into prompts or workflows that call `boot`, `dream`, and `archive_session`.
- Offer todos and status as read-only resources while preserving existing tools during transition.

### Remember-Me assets

- Keep `rm_asset_search` as the discovery entry.
- Keep `rm_asset_view` and `rm_asset_inspect` separate: user display and model vision are distinct recipients and data channels.
- Keep `rm_asset_update_metadata` separate from inspection so visual claims and updates remain explicit.
- Present upload link plus status as one user-facing upload workflow without breaking the underlying tools.
- Add an asset metadata resource for `rm_asset_get`.
- Keep download as an explicit export action, not an automatic part of search.
- Restrict reindexing to administrators.

### Sealed and administration

- Group `digest`, `related_backfill`, `rm_asset_reindex_embeddings`, and `seal_letter` in an administrator profile or separately authorized set.
- Keep dry-run and explicit confirmation semantics for mutating maintenance.
- Treat `trace` delete and merge modes as privileged even if ordinary metadata edits remain available.

### Diagnostics

- Keep all Stage 0 tools hidden from ordinary Claude connections through the existing `OMBRE_DIAG_TOOLS` server profile.
- If retained, consolidate chunked ingest into one action-based diagnostic tool and vision trials into one diagnostic workflow.
- Deprecate base64 export probes first because accepted product paths no longer depend on model-relayed base64.
- Preserve acceptance tests after public diagnostic registration is removed.

## Compatibility-First Optimization Plan

### Phase 1: Documentation and routing

- Mark diagnostic and administrator tools clearly in descriptions and docs.
- Correct stale tool counts.
- Add concise "use this, not that" guidance for upload, view, inspect, and download.
- Make no schema or registration changes.

### Phase 2: Profiles and workflows

- Extend the existing diagnostic flag into ordinary, asset, and administrator profiles if later operational evidence supports it.
- Add prompts/workflows for startup, session close, RM upload, RM visual metadata generation, and diagnostic vision trials.
- Keep every existing tool callable for compatible clients.

### Phase 3: Resources and compatibility aliases

- Add read-only resources for asset metadata, todos, and status.
- Route new clients toward resources and workflows.
- Keep old tool names as compatibility aliases with deprecation metadata where the SDK supports it safely.

### Phase 4: Controlled retirement

- Diagnostic tools are already absent from the ordinary registration profile.
- Retire model-relayed base64 probes after a published compatibility window.
- Remove aliases only after usage review confirms that no supported client depends on them.

This staged approach reduces model selection errors without changing current behavior or forcing a flag-day migration.

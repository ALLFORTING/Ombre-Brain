# MCP Surface Architecture Audit

Audit baseline: `6b9035f15981b8ac8227193074ea2cb367a37edb` (the Stage 3B
merge commit on `main`).

Audit date: 2026-08-12.

This is a repository-only audit. It does not call a deployed Ombre-Brain
service, invoke production MCP tools, read production data, or change MCP
runtime behavior.

## 1. Scope and baseline

The audit covers the MCP-visible surface registered by `server.py`:

- tools registered through `@mcp.tool()`;
- conditionally registered diagnostic tools using the local
  `diagnostic_tool()` decorator;
- resources registered through `@mcp.resource()`;
- resource templates and prompts, if any;
- the registration tests, input schemas, descriptions, startup guidance,
  Remember-Me adapter boundary, and production-write coverage controls.

HTTP custom routes are recorded only where they explain an MCP boundary. They
are not counted as MCP tools, resources, prompts, or resource templates.

The source of truth for the current list is the registration logic in
`server.py`, checked against the executable surface test
`tests/test_mcp_tool_registration.py` and an isolated in-process MCP client
listing. Existing prose, including `docs/mcp-tool-audit.md`, is supporting
evidence rather than the authority for counts.

The Stage 3C mutation boundary is documentation only. No Python file, test,
schema, decorator, security registry, dependency, deployment file, or
Remember-Me implementation is changed by this audit.

## 2. Executive findings

1. The current surface is mechanically bounded and smaller than its historical
   diagnostic inventory suggests: 21 default tools, 36 tools with diagnostics
   enabled, one resource, and no prompts or resource templates. The 15
   diagnostic tools are already hidden by default; this is a verified existing
   behavior, not a Stage 3C proposal.

2. Twenty-one default-visible tools are not one homogeneous public product
   surface. They contain nine ordinary memory/session tools, eight ordinary
   Remember-Me asset tools, and four maintenance or sealed-memory operations.
   The MCP endpoint applies one optional bearer/query-token boundary to the
   endpoint; there is no separate per-tool authorization profile in the
   registration layer. Therefore “default-visible” must not be read as
   “ordinary end-user safe.”

3. The main architectural weakness is discoverability and contract ownership,
   not raw tool count. The repository has exact tool and signature assertions,
   a diagnostic-name set, and several prose inventories, but no single
   maintained contract manifest that explains audience, mutability, feature
   flags, and compatibility guarantees.

4. Startup guidance is contradictory across documentation layers. The
   Claude-specific guide makes `breath()` the mandatory first call and then
   prescribes `dream()` and `breath(domain="feel")`. The current README presents
   `boot()` as the recommended startup entry, followed by optional retrieval
   and reflection. The runtime enforces none of these sequences. This is a
   documentation and onboarding problem, not evidence that the runtime must
   be redesigned immediately.

5. Several names describe an implementation protocol rather than a user
   intent: `trace` contains update, relationship, merge, seal, and delete
   modes; `rm_asset_upload_link` and `rm_asset_upload_status` are a lifecycle;
   the hidden `asset_ingest_*` and `asset_vision_*` tools form diagnostic
   state machines. Compatibility-first wrappers or prompts may help, but
   immediate renaming or removal would be breaking and is not justified by
   this audit alone.

6. Read-labelled operations can have bounded persistence side effects. For
   example, memory surfacing can touch activation metadata, `boot()` records
   trigger observation, and status/listing paths may update dormant state.
   The public descriptions do not consistently disclose these effects. This
   matters because the repository has line/location-sensitive production-write
   coverage and because clients may treat a read-only label as a security
   guarantee.

7. The single resource is coherent as an MCP Apps viewer resource, but it is
   not a general asset-data resource. It returns static viewer HTML and is
   paired with `rm_asset_view`; image data is carried by the tool result and
   viewer metadata. No prompt currently exists, so clients that expose tools
   poorly or ignore resources have no protocol-level onboarding fallback.

8. The safest next implementation stage is a public-contract manifest and
   exposure test consolidation. It should establish a source-of-truth and
   preserve the exact current surface before any prompt, resource, description,
   registration, or deprecation work is attempted.

## 3. Current MCP surface inventory

### 3.1 Verified counts

| Configuration | Tools | Resources | Resource templates | Prompts |
|---|---:|---:|---:|---:|
| `OMBRE_DIAG_TOOLS` unset or disabled | 21 | 1 | 0 | 0 |
| `OMBRE_DIAG_TOOLS=1`, `true`, `yes`, or `on` | 36 | 1 | 0 | 0 |

The diagnostic flag parser is `_env_flag_enabled(value)` in `server.py`. It
converts the value to a string, trims surrounding whitespace, lowercases it,
and accepts exactly `1`, `true`, `yes`, and `on`. Empty, unset, `0`, `false`,
`off`, and other values disable diagnostics. The parser is covered by
`tests/test_mcp_tool_registration.py`.

The counts above were obtained in two ways:

- `tests/test_mcp_tool_registration.py`: 16 tests passed in the repository's
  complete Stage 8G/8H Python environment, including exact set, count,
  signature, flag-parsing, and diagnostic-inventory assertions;
- an isolated `create_connected_server_and_client_session(server.mcp)` listing
  with an isolated temporary bucket directory, which returned 21/1 and 36/1
  respectively.

The old six-tool module header in `server.py` is stale. It is not used as a
counting source.

### 3.2 Registration topology

There is one `FastMCP("Ombre Brain", host="0.0.0.0", port=OMBRE_PORT)` instance
in `server.py`. Registration is concentrated in that module. The diagnostic
decorator returns `mcp.tool(...)` only when `DIAGNOSTIC_TOOLS_ENABLED` is true;
otherwise it returns the original Python function without registering it.

The source also registers many `@mcp.custom_route` HTTP endpoints, including
health, authentication, upload/download, dashboard, import, and hook routes.
Those routes are part of the server application but are not entries in
`tools/list`, `resources/list`, or `prompts/list`.

The only MCP resource registration is `rm_asset_viewer_resource()` at the
`ASSET_VIEWER_URI` constant. No `@mcp.prompt()` registration, prompt registry,
resource-template registration, or `prompts/list` implementation appears in
the repository. `_asset_vision_prompt()` is an internal text builder for a
diagnostic result, not an MCP prompt.

### 3.3 Default product and maintenance tools

The following table contains every default-visible tool. “Read” describes the
primary user intent; the side-effect column records persistence or transient
state effects that matter for security and coverage. All implementations are
functions in `server.py` unless a collaborator is explicitly named.

| Name | Category and audience | Inputs (required; important optional/defaults) | Output/content | Primary effect | Contract risk | Evidence |
|---|---|---|---|---|---|---|
| `breath` | Memory retrieval; ordinary model workflow | None required; query, filters, `max_tokens=10000`, `max_results=5`, mode, dates, resonance, mailbox, feel, sealed/dormant controls | Text summary/full retrieval, optionally timeline/mailbox sections | Read intent; may touch activation metadata and response formatting | HIGH: 20 parameters and sentinel defaults | `server.py: breath`; registration signature assertions; README; `CLAUDE_PROMPT.md` |
| `hold` | Memory create/update; ordinary product | `content` required; tags, importance, pinned, feel, source bucket, valence/arousal, trigger date | Text status with bucket/name and conflict warning | Creates or merges a bucket; `feel` writes a separate reflection class | HIGH: merge and feel semantics | `server.py: hold`; memory write tests; README; `CLAUDE_PROMPT.md` |
| `grow` | Diary digestion/create; ordinary product | `content` required | Text summary of created/merged items | Creates or merges one or more buckets; short input has a separate fast path | MEDIUM: overlaps `hold` at short-content boundary | `server.py: grow`; `tests/test_existing_tools_smoke.py`; README |
| `trace` | Power mutation; ordinary or controlled operator | `bucket_id` required; metadata, content, related, append, merge, seal, and `delete` controls | Text mutation result | Update, append, replace, merge, relationship writes, seal, or delete | BREAKING for any split/rename; HIGH semantic risk because one tool is multi-operation | `server.py: trace`; trace/history/security tests; README |
| `seal_letter` | Sealed-memory administration | `letter_id` required; `sealed=1` | Text status | Changes handoff-letter visibility | MEDIUM; narrow internal operation is default-visible | `server.py: seal_letter`; registration signature assertions; README |
| `archive_session` | Session close/archive workflow | `summary` required; highlights, mood, valence/arousal (`-1` sentinel or 0–1), letter, sealed, topics | Text archive/session identifier | Creates archive bucket and may write mailbox letter | MEDIUM: workflow semantics and literal/range schema | `server.py: archive_session`; archive tests; README |
| `todos` | Task readout; ordinary product | None | Text grouped by unresolved, non-sealed bucket | Read-only intent; overlaps the `boot` section | LOW name risk; medium discovery overlap | `server.py: todos`; todo tests; README |
| `boot` | Startup context; model workflow | `pinned_chars=2000`, `max_tokens=8000` | Budget-capped text containing triggers, pinned items, mailbox, feel echo, sessions, todos | Read-oriented composite; records due-trigger observation | HIGH workflow significance despite small schema | `server.py: boot`; README; `CLAUDE_PROMPT.md` |
| `pulse` | Status/listing; user/operator readout | `include_archive=False`, `show_all=False`, `include_sealed=False` | Text status and bucket listing | Listing can mark buckets dormant through `_mark_dormant_buckets` | MEDIUM: broad result and side-effect ambiguity | `server.py: pulse`; README |
| `dream` | Reflection workflow; model workflow | `detail_ids=""` | Text summaries or requested details | Reads recent memory and can be followed by `hold(feel=True)` / `trace` | MEDIUM: ambiguous name and overlap with `breath` | `server.py: dream`; README; `CLAUDE_PROMPT.md` |
| `digest` | Memory maintenance; operator/controlled model | `dry_run=True`, `max_groups=10`, `confirm_token=""` | Text plan or execution result | Dry-run reads; confirmed mode creates/updates digestion outputs | HIGH: default-visible administrative write | `server.py: digest`; digest tests; README |
| `related_backfill` | Semantic maintenance; operator | `dry_run=True`, `limit=100`, `threshold=-1` | Text plan or backfill result | Dry-run reads; confirmed mode writes related links | HIGH: external/vector cost and broad write | `server.py: related_backfill`; related-backfill tests; README |

#### Default tool input and schema notes

The registration test freezes the exact required/optional parameter names for
all 21 tools. Particularly contract-sensitive shapes are:

- `breath`: the required set is empty; optional fields include query/search
  controls, `domain`, `valence`, `arousal`, `max_results`, `mode`, date range,
  resonance, mailbox, feel, tag/topic filters, and visibility flags.
- `hold` and `grow`: `content` is required. `hold` also exposes the write
  policy knobs (`pinned`, `feel`, `source_bucket`, emotion, trigger date).
- `trace`: `bucket_id` is required. The other fields use empty strings or
  negative sentinels to mean “leave unchanged”; `delete`, `merge`, and
  `append` alter operation mode.
- `archive_session`: `summary` is required. `valence` and `arousal` accept
  the literal sentinel `-1` or a value constrained to 0–1 by the annotated
  schema; `topics` is optional.
- `digest` and `related_backfill`: both default to dry-run, but their same
  public schemas can reach mutating paths when confirmation/flags allow it.

Most memory tools return plain text rather than a versioned JSON envelope.
That makes output parsing more fragile than the input schema assertions imply.

### 3.4 Default Remember-Me tools and resource

Nine `rm_asset_*` functions are registered by default. Eight are ordinary
asset product operations; `rm_asset_reindex_embeddings` is an administrative
maintenance operation even though it is exposed by default.

| Name | Category and audience | Inputs (required; important optional/defaults) | Output/content | Primary effect | Contract risk | Evidence |
|---|---|---|---|---|---|---|
| `rm_asset_upload_link` | Persistent asset ingest; ordinary product | `expected_bytes` required; filename and MIME optional | Text/JSON-like upload ticket and URL | Creates a transient upload ticket; subsequent HTTP POST persists cleaned asset data | HIGH: expected-byte contract and server-computed hash are security-sensitive | `server.py: rm_asset_upload_link`; RM upload tests; README; `skills/remember-me/SKILL.md` |
| `rm_asset_upload_status` | Persistent upload lifecycle; ordinary product | `upload_id` required | Metadata-only text/JSON-like status | Read-only status of pending/completed asset ticket | MEDIUM lifecycle coupling | `server.py: rm_asset_upload_status`; RM upload/status tests; README |
| `rm_asset_get` | Asset metadata retrieval; ordinary product | `asset_id` required | Metadata-only text/JSON-like result; no bytes or paths | Read-only | LOW if envelope remains stable; asset identity is sensitive | `server.py: rm_asset_get`; RM contract tests; README |
| `rm_asset_update_metadata` | Asset metadata update; ordinary product | `asset_id` required; nullable title, description, tags | Metadata-only result | Updates metadata and may refresh asset embedding | HIGH: update boundary and visual-claim workflow | `server.py: rm_asset_update_metadata`; RM wiring tests; README |
| `rm_asset_search` | Asset discovery; ordinary product | All optional: query, tags, kind, MIME, date range, `limit=20`, `offset=0` | Search result envelope, keyword plus optional semantic ranking | Read-only search; may invoke embedding provider | MEDIUM: stable ordering and fallback behavior | `server.py: rm_asset_search`; search/wiring tests; README |
| `rm_asset_reindex_embeddings` | Asset maintenance; operator | `asset_id=""`, `limit=100` | Bounded counter/result envelope | Writes or rebuilds vectors; can incur embedding-provider work | HIGH administrative/data-cost semantics | `server.py: rm_asset_reindex_embeddings`; reindex tests; RM integration docs |
| `rm_asset_download_link` | Asset export/delivery; ordinary product | `asset_id` required | Short-lived signed-link envelope | Creates transient download ticket; does not change asset bytes | HIGH: link lifetime/use-count contract | `server.py: rm_asset_download_link`; download tests; README |
| `rm_asset_view` | User-facing asset display; ordinary product | `asset_id` required | `CallToolResult` with MCP Apps metadata and signed-link fallback | Reads cleaned image; may create transient delivery ticket | HIGH recipient distinction: display, not model inspection | `server.py: rm_asset_view`; `asset_viewer.py`; viewer tests; README |
| `rm_asset_inspect` | Model-facing visual inspection; ordinary product | `asset_id` required | `CallToolResult` containing cleaned image `ImageContent` | Reads cleaned stored bytes for model vision; does not update metadata | HIGH data-channel distinction: inspect, not display | `server.py: rm_asset_inspect`; inspect/wiring tests; README |

The one resource is:

| URI/name | Registration and implementation | Returned content | Role and security | Tests/compatibility |
|---|---|---|---|---|
| `ui://remember-me/asset-viewer.html` / `remember-me-asset-viewer` | `rm_asset_viewer_resource()` in `server.py`, backed by constants and generated bundle in `asset_viewer.py` / `asset_viewer.html` | Static self-contained viewer HTML with the declared viewer MIME type and metadata | Presentation shell only; it does not itself expose asset bytes or paths. It is paired with `rm_asset_view` result metadata. | `tests/test_mcp_tool_registration.py`, `tests/test_asset_viewer.py`, and RM viewer/wiring tests. Clients may ignore resources or MCP Apps metadata. |

There are no `rm://assets/{id}` resource templates or asset-data resources.
The existing audit's suggestion of such a resource is a future option, not
current behavior.

### 3.5 Conditional diagnostic tools

All 15 tools below are absent from the default `tools/list` response and are
registered only when the diagnostic flag is enabled. Their implementation
functions remain in `server.py`; hiding them is registration-time behavior, not
deletion. They do not persist formal user assets, but several hold bounded
in-memory or temporary upload/trial state.

| Name | Diagnostic role | Inputs | Output/content | State/data relevance | Contract/docs/tests |
|---|---|---|---|---|---|
| `asset_attachment_context_probe` | Probe whether standard MCP context exposes attachment signals | Context plus optional reference/MIME strings | Boolean/source/parameter metadata; no attachment values | Read-only diagnostic; explicitly does not persist or log image data | `server.py`; `tests/test_asset_attachment_probe.py`; attachment-feasibility docs |
| `asset_ingest_probe` | One-call base64 transport/hash probe | Required base64; optional expected hash and MIME | Metadata result | Bounded decode only; no formal persistence | `server.py`; asset probe tests; README |
| `asset_ingest_begin` | Start chunked diagnostic upload | Expected bytes/hash, MIME, filename | Upload/session metadata | In-memory transient bytes/session | `server.py`; asset probe tests; README |
| `asset_ingest_chunk` | Append one ordered base64 chunk | Upload ID, chunk index, base64 | Text/metadata status | In-memory transient bytes | `server.py`; asset probe tests; README |
| `asset_ingest_finish` | Verify and discard chunked upload | Upload ID | Hash/size verification result | Consumes and discards transient state | `server.py`; asset probe tests; README |
| `asset_ingest_abort` | Cancel chunked diagnostic upload | Upload ID | Idempotent status | Discards transient state | `server.py`; asset probe tests; README |
| `asset_browser_upload_link` | Temporary browser upload transport probe | Expected bytes/hash, filename, MIME | Short-lived browser ticket/URL | Transient browser upload; no formal asset publication | `server.py` custom upload route; browser probe tests; README |
| `asset_browser_upload_status` | Temporary browser upload status | Upload ID | Metadata-only status | Reads transient state | `server.py`; browser probe tests; README |
| `asset_render_probe` | Return-path MCP `ImageContent` probe | None | `CallToolResult` with built-in PNG image content | Built-in test asset only; no user data | `server.py`; `tests/test_asset_probe.py`, `tests/test_asset_inspect.py`; README |
| `asset_export_probe` | File-view/base64 export probe | None | JSON-like base64/size/hash result | Diagnostic transport; model-relayed base64 is not formal upload | `server.py`; asset export tests; README |
| `asset_vision_challenge` | Create blind vision challenge | None | Text instructions plus random PNG `ImageContent` | In-memory challenge/trial state | `server.py`; vision probe tests; README |
| `asset_vision_verify` | Score one blind challenge answer | Trial ID, answer JSON | Score/status result | Consumes transient trial; no production asset | `server.py`; vision verification tests; README |
| `asset_vision_export` | Export live vision trial for file-view testing | Trial ID | Base64/metadata result | Transient trial data | `server.py`; vision export tests; README |
| `asset_vision_download_link` | Temporary trial download path | Trial ID | Short-lived signed-link result | Transient trial delivery | `server.py` custom route; vision download tests; README |
| `asset_vision_upload_challenge` | Create attachment re-upload control trial | None | Metadata-only challenge result | Transient trial state; no implicit MCP attachment contract | `server.py`; vision upload challenge tests; README |

The central `DIAGNOSTIC_TOOL_NAMES` set is a useful inventory assertion, but
the decorator calls are what actually register the functions. A future
manifest must avoid treating the set alone as the registration mechanism.

### 3.6 Other MCP-visible primitives

| Primitive | Current state | Consequence |
|---|---|---|
| Tools | 21 default, 36 diagnostic-enabled | Primary portable discovery and invocation surface |
| Resources | One static viewer resource | Useful only when the client lists/loads resources and supports the viewer contract |
| Resource templates | None found | No URI-parameterized MCP data contract exists |
| Prompts | None found | No protocol-level onboarding or workflow template exists |
| Custom HTTP routes | Present, including `/mcp`, auth, RM upload/download, dashboard, and hooks | Application routes, not entries in MCP primitive lists; their auth and write behavior still matters to the host architecture |

## 4. Public vs diagnostic classification

The correct distinction is three-way rather than a binary “public versus
hidden” split:

| Surface | Current count | Meaning |
|---|---:|---|
| Ordinary memory/session product | 9 | `breath`, `hold`, `grow`, `trace`, `archive_session`, `todos`, `boot`, `pulse`, `dream` |
| Ordinary Remember-Me product | 8 | All `rm_asset_*` tools except reindexing |
| Default-visible maintenance/sealed-memory | 4 | `digest`, `related_backfill`, `rm_asset_reindex_embeddings`, `seal_letter` |
| Diagnostic/internal transport and acceptance | 15 | `asset_*` tools behind `OMBRE_DIAG_TOOLS` |

The four maintenance tools are not diagnostic tools: they are registered by
default and can affect production data or provider cost. They should be
classified as controlled maintenance in any future manifest, even if the
current runtime leaves them in the same endpoint profile as ordinary tools.

The nine memory/session tools have these product roles:

- conversation bootstrap: `boot`;
- memory retrieval/surfacing: `breath`, with `pulse` as broad status/listing;
- memory create/write: `hold`, `grow`;
- memory update/power operation: `trace`;
- reflection/digestion workflow: `dream` and `hold(feel=True)`;
- session archive/handoff: `archive_session`;
- task readout: `todos`.

The eight ordinary RM roles are upload ticket, upload status, metadata read,
metadata update, search, download, user display, and model inspection. The
reindex operation belongs with maintenance because it changes vector state and
may call an embedding provider, even though it shares the `rm_asset_` naming
family.

## 5. Read/write/security classification

### 5.1 Primary classification

| Tool group | Read-only intent | Create/write/update | Destructive or administrative | Diagnostic |
|---|---|---|---|---|
| Memory | `breath`, `todos`, `pulse`, `dream` (with side effects noted below) | `hold`, `grow`, `archive_session` | `trace`, `seal_letter` | No |
| Maintenance | Dry-run `digest`, dry-run `related_backfill` | Confirmed `digest`, confirmed `related_backfill` | `rm_asset_reindex_embeddings` | No |
| Remember-Me | `rm_asset_upload_status`, `rm_asset_get`, `rm_asset_search`, `rm_asset_inspect` | Upload lifecycle grants later persistence; `rm_asset_update_metadata` updates metadata | `rm_asset_download_link` and `rm_asset_view` create transient delivery state; deletion is not an MCP tool | No |
| Diagnostics | Probe-only functions and status/verification operations | Bounded temporary upload/trial state only | No formal user-data delete/publish path | All 15 |

“Read-only” is a product-intent label, not a guarantee that no byte changes
occur. The following current paths are relevant:

- `breath` and related retrieval helpers can touch activation metadata under
  the optional writer scope;
- `boot` updates `trigger_last_seen` for due triggers;
- `pulse` can mark dormant buckets while building its listing;
- `dream` and hook paths can touch bucket activity;
- `rm_asset_download_link` and `rm_asset_view` create short-lived delivery
  tickets even though the asset is not changed;
- `rm_asset_update_metadata` updates metadata and can refresh embeddings;
- confirmed maintenance modes write buckets, relations, or embeddings.

These effects are not reasons to rename every tool. They are reasons for a
future manifest to distinguish `read`, `incidental_write`, `transient_write`,
`create`, `update`, `destructive`, and `provider_cost` rather than using one
boolean.

### 5.2 Write boundary, auth, confirmation, and idempotency

The MCP endpoint is wrapped by `add_mcp_auth_middleware()` when
`OMBRE_AUTH_TOKEN` is configured. It accepts a matching Bearer token or query
token for `/mcp` and its subpaths. When the variable is unset, the code
intentionally preserves anonymous legacy behavior and logs a warning. This is
endpoint authentication, not per-tool authorization.

Important boundaries include:

- memory writes ultimately cross `BucketManager` guarded methods such as
  `create`, `update`, `delete`, `archive`, and history/letter methods;
- asset metadata, upload publication, and deletion boundaries are guarded in
  `AssetStore` and the HTTP route layer;
- embedding/index writes are guarded in `AssetEmbeddingIndex` and
  `EmbeddingEngine`;
- dashboard and import routes have separate HTTP write guards, but those are
  application routes rather than MCP tool authorization;
- `trace(delete=True)` is irreversible at the product level and is not a
  separate tool with a separate confirmation protocol;
- `digest` and `related_backfill` default to dry-run and use explicit
  confirmation/controls, but still appear in default discovery;
- `rm_asset_upload_link` does not accept a client-supplied source hash. The
  server computes the authoritative source hash when the browser upload is
  received. This completed Stage 3B contract must not be reopened as a new
  defect.

Idempotency is mixed. Diagnostic abort is explicitly safe to repeat; upload
and download ticket operations have bounded lifecycle semantics; `trace`
update/delete and archive/session operations are not a general idempotency-key
API. A future manifest should record this per operation instead of implying
that all tool calls are retry-safe.

### 5.3 Production-data relevance

The memory tools and formal RM tools can read or mutate configured local
production data when deployed. Maintenance tools can mutate production data or
consume embedding/LLM capacity. Diagnostic tools are designed for temporary
transport or acceptance state and are not formal data publication tools, but
they can handle bounded transient bytes and must remain hidden by default.

No Stage 3C tool was invoked against production.

## 6. Conversation startup workflow

### 6.1 Runtime behavior

No startup sequence is enforced by `server.py`. Each of `boot`, `breath`,
`dream`, and `breath(domain="feel")` is independently callable. The server
also contains HTTP hook routes for breath/dream behavior, but hooks are not
MCP prompts and do not create a protocol-level required order.

Current tool semantics are:

- `boot()` is a composite context builder for pinned summaries, due triggers,
  mailbox, feel echo, recent sessions, and todos;
- `breath()` with no query is the surfacing/search entry point for unresolved
  memory; a query selects retrieval behavior;
- `dream()` reads recent memory summaries or requested details and is intended
  as a reflection operation;
- `breath(domain="feel")` or `breath(feels=True)` retrieves feel buckets;
- `archive_session()` is an end-of-session write, not a startup operation.

Therefore `boot` is recommended by current README guidance but is not
mandatory in the MCP contract. `breath` is described as mandatory in the
Claude-specific guide but is not mandatory in the runtime. `dream` is a
workflow recommendation, not a server precondition.

### 6.2 Documentation mismatch

The following guidance is materially different:

| Document | Stated flow | Audience/interpretation |
|---|---|---|
| `CLAUDE_PROMPT.md` | `breath()` first, then `dream()`, then `breath(domain="feel")`, then speak; it calls the first step non-optional | Claude-specific operating instructions |
| `README.md` tool section | Describes `boot()` as a one-shot startup context and `dream()` as conversation-start reflection | General product documentation, though examples are Claude-oriented |
| `README.md` conversation sequence | `boot()`, then optional `breath(query=...)`, then `dream(detail_ids="")`, then speak | Current recommended product flow |
| `README.md` hook section | A Claude Code SessionStart hook may call `breath`; `boot()` is the recommended current startup call and the old breath hook remains a lighter entry | Integration-specific behavior |
| `skills/remember-me/SKILL.md` and Stage 4 docs | Describe attachment save/retrieval as a separate Claude code-execution-to-signed-upload flow | Client capability workflow, not universal MCP context behavior |

The distinction must be explicit in future docs: a client-specific skill may
recommend a sequence, but the generic MCP contract should state the independent
tools, their safe ordering where relevant, and a fallback for clients that do
not discover prompts/resources.

### 6.3 Attachment constraint

The repository's accepted Remember-Me path explicitly says standard
FastMCP/MCP context does not automatically contain Claude chat attachments.
`asset_attachment_context_probe` only probes that boundary. The accepted
Claude-specific path uses the code-execution container to send the exact file
to the short-lived `rm_asset_upload_link` endpoint. It is unsafe to present
implicit attachment presence as universal MCP behavior or to make ordinary
tool semantics depend on it.

## 7. Prompt/resource roles

### 7.1 Prompt audit

There are zero MCP prompts. Consequently there are no MCP prompt names,
arguments, public/required status, or prompt compatibility contracts to
preserve today.

The repository has prompt-like material in three other forms:

- `CLAUDE_PROMPT.md`: a checked-in Claude instruction document, not
  `prompts/list` output;
- `skills/remember-me/SKILL.md`: a Claude skill, not an MCP prompt;
- `_asset_vision_prompt()` and related strings: internal diagnostic result
  text, not a discoverable prompt.

These artifacts serve onboarding and workflow guidance, but clients that do
not load repository documents will not see them. Adding a canonical prompt
could improve discoverability, but clients may ignore prompts, so critical
semantics must remain available through tool descriptions and documentation.

### 7.2 Resource audit

The single static UI resource has a coherent role: provide the viewer shell for
`rm_asset_view`. It does not duplicate `rm_asset_get`, does not return asset
metadata, and does not replace `rm_asset_inspect`. A future metadata resource
could be useful, but it would create a new URI contract and should not be
introduced merely because read-only tools look aesthetically less desirable.

Client support variance is a hard constraint:

- a tool-capable client can use the current ordinary surface without loading
  the resource;
- a client that lists resources but lacks MCP Apps rendering may load the HTML
  but not provide the intended inline UI;
- a client that ignores resources still needs `rm_asset_view`'s signed-link
  fallback and ordinary tool descriptions;
- a tool-only client will not receive a prompt-driven workflow automatically.

## 8. Documentation consistency

### 8.1 Verified consistent claims

The current README and `docs/mcp-tool-audit.md` agree on the important current
counts: 21 default tools, 15 conditional diagnostics, 36 diagnostic-enabled
tools, and an unaffected Remember-Me viewer resource. The registration test
confirms these claims.

The current docs also consistently describe the completed Stage 3B upload
contract: `rm_asset_upload_link` accepts expected bytes plus optional filename
and MIME, while the server computes the source hash. Stage 3C must not propose
adding `expected_sha256` back to the public signature.

The Remember-Me acceptance docs consistently distinguish standard MCP
attachment context from the accepted Claude code-execution upload path. This
is a client-boundary distinction, not a contradiction.

### 8.2 Contradictions or stale material

| Finding | Evidence | Impact | Priority |
|---|---|---|---|
| `server.py` module header still says it exposes six MCP tools | Module header versus 21/36 executable listing | Maintainers and code readers can use a false inventory | P2 |
| Startup order differs across generic README and Claude-specific guide | `README.md` recommends `boot`; `CLAUDE_PROMPT.md` makes `breath` mandatory and prescribes a four-step sequence | A client or maintainer cannot tell which behavior is protocol-required | P1 |
| Product docs mix generic MCP claims with Claude Code/Claude attachment behavior | README hook and Stage 4 sections versus standard MCP probe documentation | Generic clients may assume attachment context or hook execution they do not have | P1 |
| Maintenance tools are described in the flat tool list beside ordinary tools | README and `docs/mcp-tool-audit.md` list `digest`, backfill, reindex, and seal-letter among normal tools | Users/models may not distinguish operator actions from product reads/writes | P1 |
| Tool descriptions do not consistently state side effects | `breath`, `boot`, `pulse`, and delivery tools have read-like names while collaborators can write incidental/transient state | Security review and retry behavior are harder to reason about | P2 |
| Descriptions and docs are unevenly localized and sometimes terse | `hold`, `grow`, `trace`, and `pulse` docstrings contain terse Chinese-only or mixed guidance | Generic clients receive less actionable routing and safety information | P2 |
| Existing `docs/mcp-tool-audit.md` is a useful inventory but not an enforced contract manifest | Test constants, source decorators, and prose are separate artifacts | Future registration changes can update one layer and forget another | P1 |

No stale “1 / 36” count was accepted as current evidence. No completed Stage 3A
or Stage 3B work is reopened as an unfinished implementation task.

## 9. Client compatibility constraints

The following matrix is deliberately capability-oriented. It does not assert
undocumented product behavior.

| Client class | Reliable architectural assumption | Risk if design depends on more | Consequence for Ombre-Brain |
|---|---|---|---|
| Claude Desktop / desktop-style MCP client | Tool discovery and JSON-schema invocation are the baseline; resource/UI support must be verified per integration | A prompt/resource-only workflow may not appear or render | Keep critical workflows callable as tools; retain `rm_asset_view` fallback |
| Claude.ai remote MCP path | The repository acceptance record supports a separate code-execution/file path for the exact current attachment | Standard MCP context must not be assumed to carry chat attachments | Document the external upload boundary as Claude-specific capability, not MCP universal behavior |
| Generic MCP client | Names, descriptions, schemas, text results, and standard tool calls are the portable core | MCP Apps metadata, image content, prompts, or resources may be ignored | Preserve explicit tool semantics and text fallbacks; avoid resource-only critical data |
| Tool-capable but prompt/resource-poor client | Tools may be callable while prompts/resources are not discoverable | Onboarding or orchestration disappears | Provide a concise documented tool sequence and make each tool independently meaningful |
| Client without chat attachments in MCP context | No implicit attachment bytes/reference are available | Upload workflow may block or tempt model-relayed base64 | Use the accepted external signed-upload path where the client has file/network capabilities; otherwise report unsupported capability |
| Client with code execution/file capabilities outside standard MCP | It may read a local file and call a signed HTTP endpoint if its own policy allows | Capability, network approval, and authentication are client-specific | Keep the boundary outside MCP schema; never make `Context` attachment presence a required contract |

The practical rule is: tool descriptions and schemas are the minimum portable
contract; prompts and resources are additive discoverability layers; external
file execution is an adapter capability, not an MCP primitive.

## 10. Remember-Me MCP boundary

### 10.1 What belongs to the MCP-visible boundary

The nine `rm_asset_*` tools and the fixed viewer resource are the public MCP
boundary for image memory. Their names expose a stable product concept—asset
upload, status, metadata, search, delivery, display, inspection, and
reindexing—without exposing raw file paths or bytes in text results.

The host/adapter responsibilities sit behind this boundary:

- `remember_me_host_runtime.py` and related adapters select or construct the
  host integration;
- `remember_me_mcp_presenter.py` maps core results to the OB public envelopes;
- the host controls endpoint configuration and the external network provider;
- Remember-Me owns its core storage/index semantics when the host runtime is
  enabled;
- `server.py` owns MCP registration, HTTP ticket routes, authentication
  middleware, compatibility fallback, and client-facing tool result shape.

The MCP boundary should not expose core repository classes, storage paths,
migration checkpoints, provider fingerprints, source-generation leases, or
legacy-versus-RM database details. Those are host/adapter implementation
concepts documented for maintainers in `docs/remember-me-integration.md`.

### 10.2 Coupling risks

The main coupling risks for future surface work are:

- treating `rm_asset_view` and `rm_asset_inspect` as one operation would mix
  user display with model vision and could leak the wrong content channel;
- treating `rm_asset_upload_link` as a direct MCP byte-upload tool would undo
  the accepted browser-to-signed-endpoint boundary;
- adding RM-specific concepts to generic memory startup guidance would couple
  ordinary memory onboarding to optional image capability;
- changing public schemas while moving host adapters would make rollback and
  contract diagnosis difficult;
- creating a new asset resource that embeds RM storage semantics would make
  later host separation harder.

The audit finds no evidence that Stage 3C should modify RM ownership,
dependency pins, storage, upload contracts, or migration behavior.

## 11. Security and production-write coverage constraints

`docs/maintenance-write-coverage.md` and `maintenance_write_coverage.py`
describe a versioned AST audit (schema version 3) over production Python
modules. It discovers writable file operations, publication/removal, SQLite
DML/DDL/commit, dynamic SQL, and related write primitives. The registry records
the lowest guarded boundary, guarded callers, startup-only exceptions,
transient upload exclusions, and line-anchored non-Path allowlists.

The principal test is
`tests/test_stage8h_g1c_quiesced_capture.py::test_registered_production_write_coverage_is_complete`,
with additional mutation tests that remove guards/scopes, introduce dynamic
SQL, or add a new production module with an unregistered write.

MCP restructuring is a major Stage 3D+ risk because:

1. moving a tool implementation can move a write primitive to another module
   or function and make the registry incomplete;
2. moving a helper can invalidate a line-anchored allowlist entry even if the
   resulting runtime behavior appears unchanged;
3. changing decorators or call order can remove a guarded boundary or convert a
   hard mutation into an incidental writer;
4. the RM wiring tests and registration tests also inspect exact source slices,
   decorator counts, tool sets, and schemas;
5. a public-tool refactor can therefore require security-registry churn even
   when the MCP schema is intended to remain identical.

The required sequencing is to preserve or prove write coverage first, isolate
registration moves from contract changes, and run the full offline security
suite before any production-like acceptance. The registry and line numbers are
not changed in Stage 3C.

## 12. Compatibility-sensitive contracts

### 12.1 Current contract inventory

The executable registration test freezes required and optional parameter names
for all 21 formal tools. The most sensitive public characteristics are:

| Contract element | Current examples | Classification of an unchanged future move |
|---|---|---|
| Tool name | `breath`, `trace`, `rm_asset_view` | Rename/removal is BREAKING; alias is LOW-RISK if old name remains |
| Required parameter | `hold.content`, `trace.bucket_id`, `archive_session.summary` | Removing is usually LOW-RISK only for callers that never supplied it, but changing requiredness is UNKNOWN without usage evidence; adding a required parameter is BREAKING |
| Optional parameter/default | `breath.max_results=5`, `boot.max_tokens=8000`, `digest.dry_run=True` | Adding an optional parameter is LOW-RISK at protocol level; changing a default is BREAKING or UNKNOWN because semantics change |
| Sentinel/enums/literals | `trace` negative sentinels, archive `-1`/0–1, `mode`, `sealed`, `delete` | Removing or narrowing accepted values is BREAKING; adding a value is LOW-RISK only if old behavior and validators remain stable |
| Output type | Plain text for memory tools; JSON-like text for RM; `CallToolResult` for view/inspect | Changing transport/content type is BREAKING; additive JSON fields are LOW-RISK only for consumers that parse objects rather than strings |
| Error behavior | Text errors and stable RM error envelopes | Changing error names/status semantics is LOW-RISK to BREAKING depending on client parsing; UNKNOWN where docs/tests do not freeze it |
| Prompt names | None today | Adding is SAFE/LOW-RISK; changing a future prompt name requires alias/deprecation |
| Resource URI | `ui://remember-me/asset-viewer.html` | Reusing a URI for different content is BREAKING; adding a new URI is LOW-RISK; deprecate old URI rather than silently repurpose it |

Moving an implementation without changing the MCP schema is not automatically
SAFE: it is LOW-RISK only after exact list/schema/output tests and write
coverage pass. The current test suite's source-text assertions mean even a
semantically neutral move may require deliberate test and registry updates.

### 12.2 Public schema summary

The current required/optional shape is:

| Tool | Required | Optional/default-sensitive |
|---|---|---|
| `breath` | none | query, max tokens/results, domain, valence/arousal, importance, mode, recent/date filters, resonance, mailbox, feels, tags/topics, dormant/sealed flags |
| `hold` | content | tags, importance, pinned, feel, source bucket, valence/arousal, trigger date |
| `grow` | content | none |
| `trace` | bucket ID | name/domain/emotion/importance/tags, resolved/pinned/digested/dormant/sealed, content, related, merge, append, trigger date, delete |
| `archive_session` | summary | highlights, mood, valence/arousal, letter, sealed, topics |
| `todos` | none | none |
| `boot` | none | pinned chars, max tokens |
| `pulse` | none | include archive, show all, include sealed |
| `dream` | none | detail IDs |
| `digest` | none | dry run, max groups, confirm token |
| `related_backfill` | none | dry run, limit, threshold |
| `seal_letter` | letter ID | sealed |
| `rm_asset_upload_link` | expected bytes | filename, MIME type |
| `rm_asset_upload_status` | upload ID | none |
| `rm_asset_get` | asset ID | none |
| `rm_asset_update_metadata` | asset ID | nullable title, description, tags |
| `rm_asset_search` | none | query, tags, kind, MIME, creation dates, limit, offset |
| `rm_asset_reindex_embeddings` | none | asset ID, limit |
| `rm_asset_download_link` | asset ID | none |
| `rm_asset_view` | asset ID | tool metadata for viewer |
| `rm_asset_inspect` | asset ID | none |

The 15 diagnostic schemas are listed in the diagnostic table above. They are
not public product compatibility commitments while the diagnostic profile is
documented as developer/acceptance-only, but their current tests still make
them real compatibility obligations for acceptance tooling.

## 13. Architectural issues

Severity uses P1 for material client/security/maintainer risk, P2 for
worthwhile structural improvement, and P3 for optional polish. No P0 issue is
supported by the evidence.

| Priority | Issue and evidence | Client impact | Maintainer impact | Runtime/compat/security | Recommended future Stage |
|---|---|---|---|---|---|
| P1 | No single public contract manifest; source decorators, test constants, schemas, and prose are duplicated | Clients cannot reliably infer audience, flags, mutability, or guarantees | Surface changes can drift across tests/docs | No immediate runtime change; high regression risk during future refactor | 3D contract manifest/tests |
| P1 | Startup flow is contradictory and no sequence is protocol-enforced | A client may call the wrong first tool or skip expected reflection; tool-only clients lack canonical guidance | Docs and Claude-specific instructions are hard to keep aligned | Docs-only to correct; additive prompt later is low/medium contract risk | 3E onboarding/description cleanup, then 3F workflow prompt |
| P1 | Four maintenance/sealed operations share default discovery and endpoint auth with product tools | Ordinary clients/models may select reindex/backfill/digest/seal-letter | Reviewers cannot infer privilege from registration profile | Runtime authorization change would be security-sensitive and potentially breaking | 3H controlled profiles/authorization |
| P1 | Read intent and incidental/transient writes are not explicit | Retries and “read-only” assumptions can cause unexpected metadata/ticket changes | Production-write audits must trace tool-to-helper paths | High security/coverage relevance; no safe Stage 3C runtime fix | 3D manifest, then 3E descriptions |
| P2 | `trace` is a broad multi-operation power tool | Models can choose update, merge, seal, or delete from one schema | Tests and confirmation semantics are concentrated in one function | Splitting/renaming is breaking; wrappers are additive | 3H narrow compatibility tools |
| P2 | Formal and diagnostic asset lifecycles have similar names | `asset_browser_upload_*` can be confused with `rm_asset_upload_*`; vision download can be confused with formal download | Historical probes remain expensive to explain | Diagnostics are hidden already; retirement needs compatibility evidence | 3E docs first; optional 3H deprecation |
| P2 | Registration is concentrated in `server.py` and tests inspect source layout | No direct client effect if schemas stay fixed | Internal refactor can trigger source-test and write-registry churn | Runtime-neutral intent still has high security/test risk | 3G isolated registration refactor |
| P3 | One viewer resource and zero prompts leave discoverability uneven | Resource-poor clients have no benefit; resource-only clients cannot invoke core workflows | More guidance is duplicated in README, Claude prompt, and skill docs | Additive prompt/resource work is optional and client-dependent | 3F only after 3D |

The evidence does not support a claim that 21 default tools are inherently too
many. The more precise finding is that the surface mixes three audiences and
several workflow protocols without a maintained contract-level classification.

## 14. Options considered

| Option | Benefit | Compatibility cost | Complexity | Security/testing impact | Client portability | Recommendation |
|---|---|---|---|---|---|---|
| A. Leave runtime surface unchanged; improve docs only | Lowest risk; resolves stale count and startup confusion | None at protocol level | Low | Low; no registry churn | High, because tools remain available | Immediate default. Complete 3D/3E evidence before runtime changes |
| B. Keep all tools but organize descriptions/prompts/resources coherently | Better model routing while preserving names | Description changes are low-risk but can alter selection behavior | Low/medium | Must rerun listing/schema and content tests | High if tool fallbacks remain | Recommended after the manifest; likely 3E |
| C. Add one canonical onboarding/workflow prompt while preserving primitives | Gives prompt-capable clients a single entry and keeps old tools | Additive; clients may ignore it; prompt text becomes a contract | Medium | Low runtime data risk; new prompt tests needed | Medium, because tool-only clients ignore prompts | Worthwhile after docs contract; 3F |
| D. Add a high-level workflow/orchestration tool while preserving old tools | Makes startup/session/upload workflows easier to invoke | Additive schema, but orchestration can hide failures and create new write semantics | Medium/high | High: more writes in one call and more acceptance paths | Medium/high if it returns explicit text and partial status | Optional; do not do before manifest and workflow evidence |
| E. Reduce or rename public tools | Smaller flat list and potentially clearer intent | Renames/removals are BREAKING; migration aliases and usage data required | High | High: security/write coverage and exact tests move | Low/medium for generic clients | Avoid for now. No evidence that count alone requires it |
| F. Move user guidance to resources instead of prompts/docs | A discoverable static contract can be versioned and loaded | Resources may be ignored; new URI contract | Medium | Low if read-only, but resource drift is another source of truth | Medium/low | Optional supplement, never the only critical guidance |

The recommended target is therefore a layered architecture: preserve the
current callable tools, add a contract manifest and tests, clarify descriptions
and audience, then add an optional onboarding prompt/resource with explicit
tool fallbacks. Only later consider narrower aliases or controlled retirement.

## 15. Recommended target architecture

The target should have four explicit layers:

1. **Stable product tools.** Keep the current names and schemas while usage and
   compatibility evidence are gathered. Core memory and RM product operations
   remain callable for generic clients.
2. **Workflow guidance.** Add one canonical, client-neutral onboarding/session
   guide, optionally exposed as a prompt, but keep the same calls usable without
   prompt discovery. State that `boot()` is recommended, not a protocol
   precondition, unless a future runtime contract deliberately changes that.
3. **Controlled maintenance/diagnostic profiles.** Keep diagnostics hidden by
   default as they are today. Treat maintenance tools as a separate audience in
   documentation and, only after authorization design, in runtime exposure.
4. **Contract evidence.** Maintain a machine-readable or documentation-level
   manifest generated from or checked against registration, with exact listing,
   schema, resource, prompt, feature-flag, mutability, and compatibility tests.

This target deliberately does not require reducing 21 to an arbitrary smaller
number. It makes the existing surface intelligible first.

## 16. Backward compatibility policy

The following is a proposed policy for future stages, not current runtime
behavior.

### 16.1 Names and parameters

- Never rename or remove a tool, prompt, or resource URI in the same release as
  a replacement becomes available.
- Add new optional parameters only when the old default behavior is preserved.
  Document the new field and test both omitted and explicit values.
- Do not add required parameters to an existing tool. Add a new tool or a
  versioned workflow instead.
- Do not remove a parameter based only on repository search. Require evidence
  that supported callers do not use it and retain a compatibility shim when
  feasible.
- Preserve accepted sentinel values, enums, and literal ranges until a
  deprecation window closes.

### 16.2 Outputs and errors

- Preserve the top-level content type and existing stable fields.
- Treat changing plain-text output into structured content, or changing the
  meaning of an existing JSON-like field, as a contract change.
- Add fields additively only when clients that ignore unknown fields remain
  correct; update snapshots and docs together.
- Keep stable machine-readable error codes/envelopes for RM operations. Do not
  require clients to parse human-facing prose for new control flow.

### 16.3 Deprecation

- Mark an old tool/parameter/prompt/URI deprecated in the manifest and
  descriptions before removing it.
- Keep a compatibility alias for at least two release cycles or 90 days,
  whichever is longer, unless a security emergency requires faster action.
- During the window, record supported-caller usage where the deployment can do
  so without exposing user data. Do not infer safety from zero repository
  references alone.
- Remove only after the replacement, migration guidance, exact old/new
  behavior tests, and rollback plan are published.

### 16.4 Change isolation

Do not combine a public schema change, registration refactor, production-write
coverage rewrite, prompt redesign, and Remember-Me migration in one PR. Keep
contract changes separate from internal movement and keep RM ownership changes
separate from both.

## 17. Proposed implementation stages

These are future designs only. Stage 3C does not create their branches or
implement them.

### Stage 3D — Public MCP contract manifest and exposure tests

- **Goal:** Define one maintained inventory for tools, resource, prompts (when
  added), audience, default exposure, diagnostic flag, mutability, output type,
  auth relevance, and compatibility status. Make list/schema counts testable
  from that contract.
- **Likely files:** a new `docs/` manifest or generated contract document;
  `tests/test_mcp_tool_registration.py`; possibly a small test helper. Do not
  duplicate registration logic in a second runtime registry unless generation
  is proven safe.
- **Runtime semantic change:** NO.
- **MCP contract change:** NO; it describes and freezes the current contract.
- **Backward-compatibility risk:** LOW, provided the manifest is derived from
  current listing and does not alter decorators.
- **Security risk:** LOW, but the test must include diagnostic-off/on,
  resource, prompt, and schema checks.
- **Acceptance gate:** exact 21/36 tool sets, one resource, zero prompts and
  templates; exact required/optional schemas; no diagnostic leakage; full
  production-write coverage test; docs-only or test-only diff as scoped.
- **Rollback boundary:** remove the manifest/test helper without touching
  runtime.
- **Production acceptance needed:** NO; isolated MCP listing and offline suite
  are sufficient.

### Stage 3E — Description and onboarding consistency cleanup

- **Goal:** Replace the stale six-tool header, distinguish generic MCP contract
  from Claude-specific instructions, describe incidental/transient writes, and
  publish one canonical recommended startup flow with fallbacks.
- **Likely files:** `server.py` descriptions/header, `README.md`,
  `CLAUDE_PROMPT.md`, `docs/mcp-tool-audit.md`, and possibly the Remember-Me
  skill/docs.
- **Runtime semantic change:** NO intended; description text can influence
  model selection but must not change handler behavior.
- **MCP contract change:** YES, description/content-level only; LOW-RISK.
- **Backward-compatibility risk:** LOW, with snapshots and a clear change log.
- **Security risk:** LOW to MEDIUM because safety warnings and maintenance
  audience become more explicit.
- **Acceptance gate:** all counts and schemas unchanged; Markdown links/fences
  valid; descriptions mention destructive/side-effectful semantics; generic
  and Claude-specific flows are separated.
- **Rollback boundary:** revert documentation/description commit only.
- **Production acceptance needed:** NO for text-only edits; a staging listing
  is still useful.

### Stage 3F — Prompt/resource organization

- **Goal:** Add an optional canonical onboarding/session prompt and evaluate a
  read-only metadata/status resource only where it improves discoverability,
  while preserving tool fallbacks.
- **Likely files:** `server.py` registration, `asset_viewer.py` only if a UI
  resource is extended, docs, and new prompt/resource listing tests.
- **Runtime semantic change:** NO for a read-only/additive layer; workflow
  side effects must not be hidden inside prompt expansion.
- **MCP contract change:** YES, additive; LOW to MEDIUM.
- **Backward-compatibility risk:** MEDIUM because clients differ in prompt and
  resource support.
- **Security risk:** MEDIUM if a prompt encourages writes or if a new resource
  exposes metadata; keep data access explicit.
- **Acceptance gate:** old tools unchanged; prompt/resource listing tested;
  tool-only fallback documented and tested; resource contains no private paths,
  tokens, or implicit attachment assumptions.
- **Rollback boundary:** remove new prompt/resource while retaining old tools.
- **Production acceptance needed:** YES for any MCP Apps/UI or client rendering
  behavior; no production data access is required.

### Stage 3G — Internal registration refactor with schema preservation

- **Goal:** Reduce `server.py` registration concentration without changing
  names, schemas, output semantics, flags, or write boundaries.
- **Likely files:** `server.py`, narrowly selected registration modules, exact
  registration tests, and any affected source-layout assertions.
- **Runtime semantic change:** NO intended; startup/import topology changes.
- **MCP contract change:** NO intended.
- **Backward-compatibility risk:** MEDIUM because import order and metadata can
  change even when names remain identical.
- **Security risk:** HIGH because the write-coverage AST registry, line-anchored
  allowlist, guarded callers, and RM source-slice tests can be invalidated.
- **Acceptance gate:** byte-for-byte or normalized exact tool/resource listing
  comparison before/after; all signature tests; complete offline security and
  write-coverage suite; isolated startup/HTTP smoke; no new unguarded writes.
- **Rollback boundary:** revert the registration-only commit before any public
  contract change.
- **Production acceptance needed:** YES in a production-like staging profile
  before deployment, because import/startup and auth wiring are runtime paths.

### Stage 3H — Controlled maintenance/diagnostic profiles and compatibility

- **Goal:** Evaluate separate operator/diagnostic exposure and safely retire or
  alias obsolete diagnostic protocols only if supported-client evidence justifies
  it. Keep `OMBRE_DIAG_TOOLS` behavior backward compatible during transition.
- **Likely files:** `server.py`, auth/profile configuration, docs, registration
  tests, diagnostic acceptance tests, and security coverage tests.
- **Runtime semantic change:** YES.
- **MCP contract change:** YES if default exposure, names, or permissions change.
- **Backward-compatibility risk:** HIGH; do not rename/remove without the
  proposed deprecation window.
- **Security risk:** HIGH because tool audience and authorization boundaries
  change.
- **Acceptance gate:** explicit profile matrix, old-client compatibility path,
  no diagnostic leakage, per-tool authorization/confirmation tests, full write
  coverage, and review of provider-cost operations.
- **Rollback boundary:** restore the prior profile and aliases without changing
  stored data.
- **Production acceptance needed:** YES, with a production-like authorization
  and data fixture review; never begin by calling live user data.

### Stage 3I — Optional narrow compatibility tools and retirement

- **Goal:** If usage evidence supports it, add narrower wrappers for trace,
  asset lifecycle, or maintenance workflows and later deprecate ambiguous
  modes without a flag-day removal.
- **Likely files:** `server.py`, docs, manifest, tests, and compatibility
  telemetry/fixtures if available.
- **Runtime semantic change:** YES for new wrappers; existing behavior should
  remain unchanged initially.
- **MCP contract change:** YES, additive first; removal later is BREAKING.
- **Backward-compatibility risk:** HIGH.
- **Security risk:** HIGH for delete/merge/seal and maintenance wrappers.
- **Acceptance gate:** old/new equivalence where intended, explicit destructive
  confirmation, idempotency/error tests, write-coverage review, deprecation
  window, and client compatibility evidence.
- **Rollback boundary:** remove new wrappers and keep old aliases before any
  retirement.
- **Production acceptance needed:** YES for mutating wrappers; no RM migration
  should be coupled to this stage.

No stage above proposes changing Remember-Me ownership, pins, storage, upload
contracts, or migration behavior. Those concerns remain separate.

## 18. Explicit non-goals

Stage 3C does not:

- change `server.py`, any runtime Python module, tests, schemas, decorators, or
  security registries;
- implement, remove, rename, or reorganize MCP tools;
- implement `OMBRE_DIAG_TOOLS` (it already exists and is verified);
- re-open the completed `rm_asset_upload_link` hash-signature cleanup;
- change Remember-Me pins, storage, adapters, migration, upload/download
  contracts, or production configuration;
- add prompts, resources, templates, profiles, aliases, or orchestration tools;
- access production Ombre-Brain, real MCP endpoints, memories, tokens, image
  uploads, or deployment systems;
- claim that every MCP client supports prompts, resources, MCP Apps, image
  content, or chat attachments;
- reduce the tool count solely for aesthetic reasons.

## 19. Evidence / verification appendix

### 19.1 Source and test evidence

- `server.py`: one `FastMCP` instance; `_env_flag_enabled`; diagnostic
  decorator; 21 formal tool decorators; 15 conditional diagnostic decorators;
  one resource decorator; no prompt/resource-template decorators.
- `tests/test_mcp_tool_registration.py`: formal and diagnostic name sets,
  exact 21/36 counts, exact formal input schemas, accepted/rejected flag values,
  resource listing, and central diagnostic-name assertion.
- `tests/test_mcp_auth.py`: endpoint token behavior when
  `OMBRE_AUTH_TOKEN` is unset or configured.
- `tests/test_existing_tools_smoke.py` and tool-specific tests: memory tool
  execution smoke and behavior coverage in isolated storage.
- RM tests under `tests/test_asset_*.py` and
  `tests/test_remember_me_stage8*.py`: asset listing, viewer/inspect content,
  wiring, source/contract compatibility, search, upload, and reindex behavior.
- `docs/maintenance-write-coverage.md`, `maintenance_write_coverage.py`, and
  `tests/test_stage8h_g1c_quiesced_capture.py`: production-write discovery,
  registry, guard boundaries, line-sensitive exceptions, and mutation tests.
- `README.md`, `CLAUDE_PROMPT.md`, `INTERNALS.md`,
  `docs/mcp-tool-audit.md`, the Remember-Me docs, and
  `skills/remember-me/SKILL.md`: product intent, onboarding, compatibility,
  attachment, and historical context.

### 19.2 Mechanical checks performed

| Check | Result |
|---|---|
| Remote `main` baseline | Verified at `6b9035f15981b8ac8227193074ea2cb367a37edb` |
| Default in-process MCP listing | 21 tools, one resource, zero prompts/templates |
| Diagnostic-enabled in-process MCP listing | 36 tools, one resource, zero prompts/templates |
| `tests/test_mcp_tool_registration.py` in complete repository environment | 16 passed |
| Production access | Not performed |
| Frozen historical worktree | Read-only inspected; not modified |

### 19.3 Interpretation rules for future audits

- Count actual registration, not decorator definitions or comments.
- Treat source registration and tests as MCP surface truth; use docs to explain
  intent and history.
- Separate MCP primitives from HTTP custom routes.
- Separate default exposure from ordinary-user audience.
- Record side effects even when the primary intent is a read.
- Prefer symbol/path references over fragile line numbers, except when the
  production-write registry itself intentionally anchors a location.
- Re-run the relevant inventory whenever `main` changes MCP registration,
  schemas, descriptions, startup hooks, or Remember-Me host wiring.

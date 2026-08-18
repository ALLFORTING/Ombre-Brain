# OB Memory Layer Contract v1 — Closure Review

Status: docs-only closure review

Reviewed baseline: `main` at `8a3eb6e7ef28802ad9b2970b2c34e632811bec30`

Review branch: `docs/memory-layer-contract-v1`

Proposed deliverables:

- `docs/OB_MEMORY_LAYER_CONTRACT_v1.md`
- `docs/audits/OB_MEMORY_LAYER_CONTRACT_V1_CLOSURE_REVIEW.md`

No runtime control-flow, storage, schema, dependency, configuration, production-state, MCP, or Remember-Me behavior was changed. One authorized runtime wording-only change was made in `import_memory.py`.

The previous `preserve_raw` blocker was the runtime-embedded extraction prompt wording. It now describes preserving extracted `content` while skipping the subsequent merge/dehydration summary path, and explicitly disclaims uploaded-transcript, exact-quote, and immutable-source preservation.

## 1. Review scope and primary evidence

The closure review checked the three primary audit documents, current implementation symbols, focused regression tests, current model guidance, MCP surface evidence, and Remember-Me asset ownership evidence:

- `docs/audits/OB_POST_CUTOVER_MEMORY_ARCHITECTURE_AUDIT.md`
- `docs/audits/OB_MEMORY_LAYER_CONTRACT_v1_DRAFT.md`
- `docs/audits/OB_RECALL_DIAGNOSTIC_FIDELITY_REPAIR.md`
- `bucket_manager.py`, `server.py`, `import_memory.py`, and `asset_backend.py`
- `tests/test_recall_diagnostic_fidelity.py`, `tests/test_sealed.py`, `tests/test_boot_mailbox_seal.py`, `tests/test_breath_structured_filters.py`, `tests/test_trigger_memory.py`, `tests/test_rm_cutover_routing.py`, and related focused tests
- `CLAUDE_PROMPT.md`, `mcp_prompts.py`, `docs/mcp-public-contract.json`, `docs/mcp-surface-architecture-audit.md`, `docs/mcp-tool-audit.md`, `README.md`, `docs/remember-me-integration.md`, `INTERNALS.md`, `BEHAVIOR_SPEC.md`, and `.claude/hooks/session_breath.py`

The `prompts/` directory referenced by older discussion is not present in this repository; no contract claim was inferred from that absence.

## 2. Draft-to-final promotion

| Draft area | Closure decision | Final treatment |
|---|---|---|
| Sealed/privacy | Qualified public behavior | Promoted only to a scoped `PUBLIC GUARANTEE`: default exclusion, supported explicit opt-in, diagnostic metadata non-leak, and no equivalence between Dashboard auth and sealed inclusion. |
| Archive/session and feel | Qualified public behavior | Retained as scoped `PUBLIC GUARANTEE` for supported routes; exact representation and ranking remain unspecified. |
| Mailbox/letters | Qualified public behavior | Retained as a distinct intentional channel with default sealed handling. |
| Pinned and resolved | Qualified behavior with implementation detail | Retained as scoped public semantics; exact score, multiplier, storage, and ordering details were not promoted. |
| Boot and dream | Model orchestration | Retained as `MODEL GUIDANCE`; no mandatory lifecycle sequence was created. |
| `preserve_raw` | Import implementation behavior | Classified `CURRENT IMPLEMENTATION SEMANTIC`: post-extraction content preservation plus secondary merge/dehydration bypass, not immutable transcript evidence. |
| Recall diagnostics | Historical pre-O2 gap | Updated to the post-O2 truth: supported query diagnostics reuse runtime/composition logic and are side-effect-free; five routes remain explicitly unsupported/untraced. |
| Exact scores, thresholds, decay, ranking, token limits, schema, and storage | Implementation detail | Rejected as public guarantees and listed as non-guarantees. |
| Raw Evidence | Future design | Remains `DESIGN`; it is not part of Contract v1. |
| Remember-Me ownership | Cutover boundary | Retained as a separate ownership `PUBLIC GUARANTEE`; the contract does not reopen cutover. |

## 3. Evidence anchors

The following concrete repository evidence supports the bounded contract:

| Area | Evidence and conclusion |
|---|---|
| Bucket types and metadata | `bucket_manager.py` `create()` and `update()` show current permanent/dynamic/archive/feel handling and fields such as sealed, pinned, protected, resolved, digested, trigger, related, and source metadata. These establish current semantics, not a promise for exact storage shape. |
| Letters/mailbox | `bucket_manager.py` `get_letters()` defaults to `sealed=0` and has an explicit inclusion path; `server.py` mailbox formatting and `tests/test_boot_mailbox_seal.py` cover the boundary. |
| Recall admission and ranking | `bucket_manager.py` `search()` admits candidates before the resolved ranking penalty, with tests in `tests/test_recall_diagnostic_fidelity.py` and related Breath tests. Exact constants remain implementation semantics. |
| Sealed/dormant handling | `server.py` `_filter_breath_candidates()`/`_is_sealed()`, `bucket_manager.py` search eligibility, `tests/test_sealed.py`, and `tests/test_recall_diagnostic_fidelity.py::test_debug_excludes_sealed_and_dormant_metadata` support default exclusion and diagnostic non-leak. |
| Breath routes | `server.py` `_breath_impl()`, `_breath_filtered_impl()`, and `_compose_breath_query_matches()` distinguish normal query Breath and special/no-query/session/feel/importance routes. |
| O2 diagnostics | `server.py` `api_breath_debug()` uses the shared supported query composition path with `touch=False` and no diagnostic cache write. Unsupported paths return `unsupported_route`/`untraced`; the Dashboard renders them as untraced. |
| O2 regression coverage | `tests/test_recall_diagnostic_fidelity.py` covers semantic/hybrid state, exact tiering, threshold/penalty ordering, structured filters, sealed/dormant non-leak, token-budget surfacing, debug no-touch, normal touch-once, and Dashboard trace presentation. |
| Import semantics | `import_memory.py` extraction runs before the `preserve_raw` branch; selected item content is stored without the secondary `_merge_or_create_item`/dehydration path. No original-byte, source-span, message-ID, exact-quote, or import-run guarantee is evidenced. |
| Remember-Me authority | `asset_backend.py` runtime registry distinguishes Remember-Me and legacy authorities; `tests/test_rm_cutover_routing.py` and the Remember-Me adapter tests support the ownership boundary. |
| MCP public surface | `docs/mcp-public-contract.json` and `tests/test_mcp_tool_registration.py` provide MCP surface evidence. This contract does not change tool names, schemas, counts, or exposure behavior. |
| Model guidance | `CLAUDE_PROMPT.md` and `mcp_prompts.py` describe boot/Breath/dream/feel as intentional or optional guidance, not a mandatory protocol. |

## 4. Rejected public guarantees

The following were deliberately not promoted:

- exact lexical/semantic weights, score thresholds, decay coefficients, ranking formulas, resolved multipliers, and token budgets;
- exact metadata names, database columns, directories, SQLite/layout details, provider/embedding internals, and helper names;
- immutable raw transcript preservation or complete import provenance;
- all-route recall-diagnostic equivalence;
- a mandatory boot → Breath → dream → feel sequence;
- a general bucket-history undo, restore, or provenance API; and
- protected semantics beyond the explicitly documented privacy/visibility boundaries.

These exclusions prevent an implementation detail from becoming a permanent public promise merely because it is visible in the current source.

## 5. Conflicting and adjacent documentation inventory

Each item is classified using the required labels:

| Document | Classification | Closure treatment |
|---|---|---|
| `INTERNALS.md` numeric scoring/decay/storage tables and old resolved wording | `STALE_IMPLEMENTATION_DETAIL` | A minimal internal-snapshot qualifier now points maintainers to Contract v1 and states that exact values are not public guarantees; the useful tables remain intact. |
| `INTERNALS.md` mandatory lifecycle wording | `MODEL GUIDANCE` | The document now labels lifecycle guidance as optional and non-mandatory. The supported diagnostic boundary is also stated as query-runtime-only with special routes untraced. |
| `BEHAVIOR_SPEC.md` exact formulas, defaults, and old implementation notes | `STALE_IMPLEMENTATION_DETAIL` | A minimal historical-scope qualifier now states that exact values are not authoritative for v1; the historical specification is not rewritten. |
| `BEHAVIOR_SPEC.md` “must call Breath” sequence | `MODEL GUIDANCE` | The document is now explicitly scoped as historical/model guidance, not a mandatory runtime protocol. |
| `BEHAVIOR_SPEC.md` old resolved auto-archive bug note | `STALE_IMPLEMENTATION_DETAIL` | The historical-scope qualifier makes the old note non-current; current `bucket_manager.py` semantics and Contract v1 are authoritative. |
| `README.md` exact decay/scoring tables and dormant/feel implementation detail | `STALE_IMPLEMENTATION_DETAIL` | Minimal section qualifiers now label these as current implementation/configuration references, not stable public guarantees. |
| `CLAUDE_PROMPT.md` and `mcp_prompts.py` | `MODEL GUIDANCE` | Current optional/tool-intent guidance aligns with the contract when read as orchestration guidance, not storage/security policy. |
| `.claude/hooks/session_breath.py` | `MODEL GUIDANCE` | Operational integration behavior, not a mandatory public memory protocol. |
| `README.md` | `NO_CONFLICT` | No conflicting Contract v1 memory guarantee was found in the reviewed scope. MCP surface statements remain governed by their own evidence. |
| `docs/mcp-public-contract.json` | `NO_CONFLICT` | MCP surface source of truth; Contract v1 does not alter it. |
| `docs/mcp-surface-architecture-audit.md` and `docs/mcp-tool-audit.md` | `NO_CONFLICT` | Architecture/audit evidence, not a memory-layer guarantee list. |
| `docs/remember-me-integration.md` | `NO_CONFLICT` | Supports the separate Remember-Me ownership boundary. |
| The three primary memory audit documents | `NO_CONFLICT` | Historical evidence and draft context are preserved; the historical draft is not rewritten. |
| `import_memory.py` prompt wording that said “保留原文不摘要” | `PUBLIC_CONTRACT_CONFLICT` | Resolved by the minimal wording-only correction in the extraction prompt. No branch condition, boolean meaning, storage path, or schema changed; the prompt now matches the proven post-extraction/secondary-bypass semantics. |
| `.claude/hooks/session_breath.py` sequence comment | `MODEL GUIDANCE` | This is an opt-in runtime helper comment, not the public Contract. It was not modified because this pass forbids runtime/helper changes; `CLAUDE_PROMPT.md` remains the current optional model guidance. |

The former `PUBLIC_CONTRACT_CONFLICT` was wording-only and is now resolved. It was not evidence of a runtime guarantee and did not justify changing importer control flow.

## 6. Post-O2 diagnostic wording

The final contract and this closure report intentionally state:

- supported query diagnostics reuse the real runtime/composition path;
- debug execution is side-effect-free and does not perform normal Breath activation touch;
- normal Breath retains its separate activation-touch behavior;
- sealed/dormant metadata is not exposed by default diagnostics; and
- diagnostics cover the repaired supported query path, not every Breath route.

The five explicit unsupported/untraced routes are:

1. no-query surfacing;
2. no-query resonance;
3. session/archive topic route;
4. session/feel route; and
5. importance route.

The API/UI reports these as unsupported/untraced rather than implying runtime equivalence. They are not described as O2 regressions. They may be candidates for a future Breath observability expansion only if maintenance and user value justify it; they are not blockers for closing this bounded O2 repair.

## 7. Raw Evidence boundary

Raw Evidence is a future design, not an implemented v1 feature. `preserve_raw` preserves selected post-extraction item content and bypasses secondary import merge/dehydration. It does not preserve immutable uploaded bytes, original message/document identity, source spans, exact quotes, or durable import-run linkage. No such guarantee is inferred from the current importer or model prompt wording.

## 8. Remember-Me wording

The contract states that Remember-Me remains the authority for Remember-Me image assets after cutover. OB memory buckets are not their source of truth. This closure review does not reopen cutover, migrate assets, or make a live-production claim.

## 9. Contract adoption and required follow-up

The proposed contract is bounded and evidence-backed. This pass completed the required documentation corrections in `INTERNALS.md`, `BEHAVIOR_SPEC.md`, and `README.md`, plus the one authorized wording-only runtime correction in `import_memory.py`, without rewriting historical evidence.

The previous direct contract conflict is resolved. The exact correction was limited to the two prompt lines defining when to set `preserve_raw=true` and what the JSON field means. `preserve_raw` was not renamed, its type/default and branch conditions were not changed, and extraction, merge, dehydration, bucket creation, resume, parsing, chunking, and storage behavior were untouched.

Focused validation added `tests/test_import_memory_prompt.py`; its assertions protect the semantic boundary without freezing the full prompt. The existing import security gate was also run.

## 10. Validation and final recommendation

Static review confirms that the contract does not change MCP surface, Remember-Me ownership, import control flow, storage behavior, schema, or production state. The only runtime change in O3 was the authorized wording-only correction in `import_memory.py`: the `preserve_raw` prompt now describes preserving extracted content while bypassing the secondary merge/dehydration path. No storage, control-flow, schema, boolean/default, extraction, parsing, chunking, resume, or bucket-creation semantics changed.

The earlier validation failures were environment diagnoses, not O3 product failures:

- The bare shell exposed Python 3.8.8, which did not reproduce the repository’s Python 3.12 CI layout.
- The repository-local `.venv\\stage8gc` environment had the correct Remember-Me version repair available, but its package path was inside the Ombre-Brain repository. The existing provenance test intentionally requires `remember_me.__file__` to be outside the repository, so that layout cannot satisfy the assertion. The test was not modified or weakened.
- The earlier RM installation was stale dev6. The validation environment was repaired to the repository-pinned public dev7 artifact: version `0.1.0.dev7`, tag `v0.1.0-dev.7-public.1`, commit `a00ea991442d7581a3856b178525a8e77da833fe`, and SHA-256 `80a0b334f08db19c95c053537dec484be645f29fcf67898037e6641224012214`.
- Earlier collection errors also reflected a missing writable `OMBRE_BUCKETS_DIR`; the external validation used isolated temporary writable buckets and never used the repository `buckets` directory.

Approval was requested and granted for the temporary external environment and dependency installation. The successful validation environment was `C:\\Users\\HUAWEI\\AppData\\Local\\Temp\\ombre-brain-o3-validation-8dd29b2c228a4109af3270fb694ebfab`, using Python `3.12.13`. Provenance checks confirmed that Remember-Me resolved outside the repository, version and project metadata were `0.1.0.dev7`, the installed `direct_url.json` matched the pinned archive URL and SHA-256, all required adapter imports resolved, and both required service methods were asynchronous.

Validation evidence from that external CI-layout environment:

- `tests/test_remember_me_stage8f_j_reindex_wiring.py::test_target_rm_source_version_and_async_api_provenance`: `1 passed`.
- `tests/test_import_memory_prompt.py tests/test_security_import_gate.py`: `4 passed, 1 warning`.
- `tests/test_stage8h_g1c_quiesced_capture.py::test_registered_production_write_coverage_is_complete`: `1 passed`.
- Full offline command `python -m pytest -m "not external" -v --asyncio-mode=auto`: `1371 passed, 4 skipped, 7 deselected, 42 warnings`.
- `import_memory.py` AST validation: `PASS`.
- `git diff --check`: `PASS`.
- Changed-file trailing-whitespace scan: `PASS`.

No paid provider, real model generation, production data, or repository-local memory data was used. The full external offline suite passed, so the previous provenance and collection failures are conclusively environmental. The Raw Evidence boundary remains unchanged.

The final recommendation is:

`READY_TO_ADOPT`

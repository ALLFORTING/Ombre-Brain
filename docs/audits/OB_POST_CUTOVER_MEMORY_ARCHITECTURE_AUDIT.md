# OB Post-Cutover Memory Architecture Audit

Status: read-only architecture audit; documentation-only output
Date: 2026-08-18
Baseline: `audit/post-cutover-memory-architecture` at `3dc991cd66552e38481622423941e9d8e9b494ce`
Remote baseline checked: `origin/main` at `3dc991cd66552e38481622423941e9d8e9b494ce`

This report records current repository evidence. It does not authorize a runtime fix, a schema migration, a production action, a Remember-Me cutover change, or a new MCP surface.

## Executive result

| Audit | Finding | Problem confirmed | Runtime/storage action | Decision |
|---|---|---:|---|---|
| Raw Evidence | `preserve_raw` preserves post-extraction bucket wording and bypasses the import-time secondary merge/dehydration step; it does not preserve an immutable source transcript or source spans. | PARTIAL / YES | Runtime and durable evidence design would be required for exact provenance. | DESIGN (not implemented) |
| Recall Diagnostics | `/api/breath-debug` is a four-score simulation, not an execution trace of `breath` or `BucketManager.search`; sealed candidates and resolved-threshold timing are materially divergent. | YES | A separately authorized repair should instrument the real path. No repair is started here. | REPAIR (not executed) |
| Memory Layer Contract | The repository has several useful user-visible behaviors, but exact scoring, decay, ranking, and many metadata flags remain implementation semantics. | YES | Establish a qualified contract; do not freeze implementation constants as public guarantees. | DOCUMENT-ONLY |
| D — contract | A draft contract is supplied separately. | N/A | Documentation only. | DRAFT |
| E — evidence ledger | A maintenance/product evidence ledger is supplied separately; it is not runtime memory. | N/A | Documentation only. | ADOPT AS AUDIT PRACTICE |
| F — historical evidence | Local Git history and repository documents provide a usable evolution trail. GitHub review context, private transcripts, and production logs were not available to this audit. | PARTIAL | Mark unavailable sources explicitly. | DOCUMENT |

Overall: the post-cutover memory architecture is not ready for a claim of exact raw provenance or faithful recall diagnostics. The safe next step is to preserve the findings and design boundaries, not to repair the discovered behavior in this audit.

## 1. Entry gate and audit boundary

The entry state was verified before inspection:

- Current branch: `audit/post-cutover-memory-architecture`.
- `HEAD == origin/main == 3dc991cd66552e38481622423941e9d8e9b494ce`.
- Worktree and index were clean.
- No local merge, rebase, cherry-pick, bisect, or other interrupted Git operation was present.
- `buckets/` contains local SQLite/cache artifacts, but no repository-local `buckets/state` directory or obvious RM migration/freeze state was present. This is only a local repository observation, not a production-state assertion.
- The existing RM runbook is a historical/operator document and explicitly does not itself authorize production execution. The audit accepts the user-provided post-cutover state and does not reopen it.

Only files under `docs/audits/` were created. Runtime Python, tests, data, Git history, and production systems were not changed.

### Evidence anchors

The major findings are tied to concrete repository paths and symbols:

- Raw import: `import_memory.py` — `detect_and_parse`, `chunk_turns`, `ImportEngine.start`, `_process_single_chunk`, `_extract_memories`, and `_merge_or_create_item`; `server.py` — `/api/import/upload`.
- Runtime recall: `bucket_manager.py` — `BucketManager.search`, `_calc_topic_score`, `_calc_exact_match_score`; `server.py` — `_breath_impl`, `_breath_filtered_impl`, `_is_sealed`, and `/api/breath-debug`.
- Diagnostic UI: `dashboard.html` — `runBreathDebug()`.
- Privacy regression evidence: `tests/test_sealed.py` covers runtime sealed defaults; no equivalent `/api/breath-debug` sealed-exclusion test was found.
- Ownership boundary: `asset_backend.py` — `RuntimeAssetBackendRegistry`, `LegacyAssetBackend`, and `RememberMeAssetBackend`.
- Historical/document drift: local Git history and `INTERNALS.md`, compared with current `bucket_manager.py` and `decay_engine.py`.

## 2. Audit A — raw import and `preserve_raw` semantics

### 2.1 Current call chain

The current chain is:

1. `server.py` `/api/import/upload` reads an uploaded byte stream, decodes it as UTF-8 with replacement, and passes only decoded text, filename, `preserve_raw`, and `resume` to `import_engine.start`.
2. `import_memory.py` `ImportEngine.start` computes a short source hash, parses the source, chunks normalized turns, and persists progress in `buckets/import_state.json`.
3. `detect_and_parse` normalizes Claude JSON, ChatGPT JSON, or Markdown into role/content/timestamp turns.
4. `chunk_turns` converts turns into normalized `[用户]` / `[AI]` text chunks of approximately 10,000 tokens.
5. `_process_single_chunk` sends the chunk to `_extract_memories`; the extraction request is itself capped at `chunk_content[:12000]` and is performed before the raw branch is selected.
6. For each extracted item, `preserve_raw` is selected when the import-level flag is true or the model returns `item.preserve_raw == true`.
7. The raw branch calls `bucket_mgr.create(content=item["content"], ...)` and skips `_merge_or_create_item`, `dehydrator.dehydrate`, and `dehydrator.merge`.

The important semantic fact is that the content written in the raw branch is the model-produced `item["content"]`, not the uploaded source bytes, the original turn, or a source span. The branch bypasses the import-time secondary merge/dehydration step; it does not promise that later retrieval responses can never be dehydrated. The comment “store original content without summarization” describes the intended branch distinction but not the actual provenance boundary.

### 2.2 What is and is not preserved

| Evidence dimension | Current behavior | Assessment |
|---|---|---|
| Uploaded bytes | UTF-8 decoded with replacement; no immutable byte copy or digest-linked evidence object is stored in the bucket. | Not preserved as source evidence. |
| Source format | Parser choice is inferred, then discarded. | Not first-class provenance. |
| Conversation/thread/message IDs | Not extracted into bucket metadata. | Lost. |
| Source offsets and turn boundaries | Reassembled into normalized role lines, then rechunked. | Lost as exact spans. |
| Timestamps | Claude/ChatGPT timestamps may survive as chunk start/end; Markdown timestamps are empty. The per-item path does not write the chunk timestamps into the new bucket. | Partial and not item-linked. |
| Role labels | Normalized to Chinese role prefixes in chunk text. | Semantic normalization, not original source preservation. |
| Model extraction wording | Stored in the raw branch as `item.content`. | Preserved only after extraction. |
| LLM summarization/dehydration | Normal raw branch skips normal merge/dehydration; ordinary branch may call `dehydrator.merge`. | Correctly bypassed for that branch. |
| Provenance link from bucket to import run | Import state has source filename and 16-character hash, but that state is not attached to the bucket. | Missing. |
| Duplicate/resume identity | Resume reuses source hash and chunk progress; no durable import-run or source-span identity is stored with the bucket. | Operational resume exists; evidentiary identity does not. |

### 2.3 Privacy, retention, and future-route assessment

The current implementation does not persist the full uploaded transcript through `preserve_raw`; that reduces immediate raw-transcript retention but also means the option cannot support exact quotation or later source verification. If a future implementation stores exact raw evidence, it must define access control, encryption, retention, deletion, export, indexing exclusion, and a link from derived memory to source evidence. A raw transcript must not become ordinary searchable memory by accident.

| Candidate route | Feasibility | Provenance/privacy characteristics | Compatibility | Audit view |
|---|---|---|---|---|
| Extend ordinary memory buckets | Medium | Lowest isolation unless raw type is excluded from normal search and output; retention inherits bucket behavior. | Additive only if metadata and routes remain optional. | Not preferred without an explicit evidence boundary. |
| Same store with a strict `evidence` type | Medium | Can retain exact payload and provenance while excluding it from ordinary `breath`, embeddings, and dehydration. | Medium; requires a new type/schema contract. | Plausible design candidate; not implemented. |
| Separate raw-events/evidence store | Medium-low | Strongest isolation and independent retention/deletion/export controls; adds operational surface. | Additive but higher complexity. | Plausible design candidate; not implemented. |

Audit A verdict: `Problem Confirmed = YES/PARTIAL`; `Runtime Needed = YES` for exact raw semantics; `Storage Needed = YES` for durable source evidence; `Security Impact = MEDIUM` now and potentially HIGH if raw payload storage is added without isolation; `Decision = DESIGN (not implemented)`. No implementation is recommended within this audit.

## 3. Audit B — recall diagnostics fidelity

### 3.1 Actual retrieval path

`BucketManager.search` performs candidate enumeration, optional domain prefiltering, dormant exclusion, optional embedding retrieval, exact-match scoring, fuzzy topic scoring, emotion/time/importance scoring, threshold admission, hybrid reranking, match tiers, and sorting. The normal `server.py` `breath` path then adds route-specific behavior: sealed/date/tag/topic/session/feel/resonance/importance filters, pinned handling, no-query decay surfacing, dehydration, token budgets, hidden counts, and activation touches.

`/api/breath-debug` does something different:

- It enumerates all active buckets with `list_all(include_archive=False)`.
- It accepts only `q`, `valence`, and `arousal`.
- It calculates topic, emotion, time, importance, raw total, and normalized score.
- It applies the resolved multiplier before its threshold check.
- It returns candidate scores and a pass count.
- It does not call `BucketManager.search`, the embedding engine, the `breath` route, dehydration, token-budget logic, or final surfacing logic.

The Dashboard `runBreathDebug()` renders this endpoint as a “simulate Breath” view. The label creates a stronger equivalence expectation than the implementation supports.

### 3.2 Equivalence matrix

| Concern | Runtime `breath` / search | Debug endpoint | Classification | Severity |
|---|---|---|---|---|
| Active candidate set | Active candidates; ordinary search excludes dormant; sealed is gated by `include_sealed`. | All active candidates, including sealed and dormant. | `FALSE_POSITIVE_EXPLANATION`; sealed exposure is also a privacy defect. | HIGH for sealed; MEDIUM for dormant |
| Domain/date/recent filters | Supported across filtered routes and query post-filtering. | Not accepted or applied. | `UNCOVERED_PATH` | MEDIUM |
| Tags/topics | Structured filtered route supports them. | Not supported. | `UNCOVERED_PATH` | MEDIUM |
| Session and feel routes | Dedicated route semantics, ordering, and sealed gates. | Not represented. | `UNCOVERED_PATH` | MEDIUM |
| Resonance/importance/no-query surfacing | Uses emotion distance, importance sorting, or decay/pinned/cold-start/random surfacing. | Empty query becomes a four-score simulation. | `FALSE_POSITIVE_EXPLANATION` and `UNCOVERED_PATH` | MEDIUM |
| Lexical/fuzzy topic score | Uses `_calc_topic_score` as one component, plus exact score and hybrid admission/ranking. | Uses the topic component only. | `DISPLAY_ONLY_DIFFERENCE` for the component; not equivalent overall. | MEDIUM |
| Exact-match tier | `_calc_exact_match_score`, admission, `match_tier`, then tier-first sort. | No exact score or tier output. | `UNCOVERED_PATH` | MEDIUM |
| Embedding/semantic retrieval | Optional embedding candidate search; semantic admission at `>= 0.42`; hybrid score when available. | Never calls embedding search. | `UNCOVERED_PATH` | HIGH for diagnosis fidelity |
| Resolved timing | Admission is checked before the resolved `* 0.3` ranking penalty. | Applies `* 0.3` before `passed_threshold`. | `FALSE_NEGATIVE_EXPLANATION` | HIGH |
| Final surfacing | Pinned/regular split, dehydration, token cap, hidden count, and activation touch can change output. | Top 50 scores and pass count only. | `UNCOVERED_PATH` | MEDIUM |
| Privacy visibility | Normal route hides sealed by default; tests cover this behavior for `breath` and related routes. | Sealed name/domain/type and scores are returned to an authenticated caller. | `FALSE_POSITIVE_EXPLANATION` / privacy mismatch | HIGH |
| Side effects | `breath` may touch activation metadata for returned candidates. | Diagnostic is read-only. | `DISPLAY_ONLY_DIFFERENCE` | LOW/MEDIUM |

Concrete resolved-threshold counterexample: if the pre-penalty normalized score is `60` and the threshold is `50`, runtime admission succeeds and the result can later rank at `18` after the resolved penalty. Debug changes `60` to `18` first and reports a threshold failure. That is a false-negative explanation, not merely a display rounding issue.

The sealed finding is static: no production endpoint or private memory was queried. The code path is enough to show that `list_all(include_archive=False)` is not preceded by `_is_sealed` filtering in this endpoint. Authentication alone is not treated as authorization to enumerate sealed content.

Audit B verdict: `Problem Confirmed = YES`; `Final disposition = REPAIR`; `Execution = NOT STARTED`. The durable recommendation is to trace the actual `breath` execution path (including candidate IDs, filters, semantic provider state, admission, ranking, and final token-budget selection) rather than maintain a second scoring simulation. Repair remains separately authorized work and was not performed here.

## 4. Audit C — memory-layer semantics inventory

The classifications below distinguish externally visible behavior from current implementation details and model guidance. Exact weights, thresholds, decay coefficients, directory names, and penalty timing are not promoted to public guarantees merely because they are currently coded.

| Concept | Classification | Current meaning / boundary | Conflict or qualification |
|---|---|---|---|
| source transcript | CURRENT IMPLEMENTATION GAP | Parsed and normalized for import; no first-class immutable raw layer. | Conflicts with the stronger reading of `preserve_raw`. |
| extracted item | CURRENT IMPLEMENTATION SEMANTICS | LLM-produced name/content/metadata candidate before bucket creation. | Not source evidence. |
| dynamic bucket | CURRENT IMPLEMENTATION SEMANTICS | Ordinary active memory subject to search, decay, merge, and surfacing. | Exact lifecycle is not a public contract. |
| permanent | CURRENT IMPLEMENTATION SEMANTICS | Special bucket type with protected/permanent decay treatment. | Exact score is implementation. |
| archive | CURRENT IMPLEMENTATION SEMANTICS | Non-active storage and explicit session/archive retrieval paths. | Default inclusion differs by route. |
| session | PUBLIC GUARANTEE (qualified) | Session/archive tool and retrieval route expose a distinct conversation/session concept. | Storage layout and sorting constants remain implementation. |
| feel | PUBLIC GUARANTEE (qualified) | A separate reflective/emotional memory path, not ordinary dynamic recall. | Exact score and route details are implementation. |
| sealed | PUBLIC GUARANTEE | Hidden by default in covered runtime recall paths; explicit opt-in exists on selected routes. | `/api/breath-debug` currently conflicts with this privacy expectation. |
| pinned | PUBLIC GUARANTEE (qualified) | User-prioritized memory is protected from ordinary merge/decay treatment and surfaced with priority. | Exact `999.0` score is not public. |
| protected | CURRENT IMPLEMENTATION SEMANTICS | Internal protection behavior similar to pinned in parts of decay/update logic. | Less consistently exposed as a user concept. |
| resolved | PUBLIC GUARANTEE (qualified) | Remains stored and is not part of default unresolved surfacing; it can still be searched with a penalty. | Exact `0.3`, `0.05`, and `0.02` factors are implementation. |
| digested | CURRENT IMPLEMENTATION SEMANTICS | Source marked after a linked feel/reflection flow; affects later decay behavior. | Not a complete independent retrieval layer. |
| dormant | CURRENT IMPLEMENTATION SEMANTICS | Excluded by default from ordinary search/surfacing; explicit inclusion exists. | Threshold and activation rules are implementation. |
| trigger_date / trigger_last_seen | PUBLIC GUARANTEE (qualified) | Boot/trigger flow can use date and last-seen metadata to avoid repeated daily triggers. | Exact scheduling is implementation. |
| related_buckets | CURRENT IMPLEMENTATION SEMANTICS | Related IDs can be linked and displayed as context. | No stable public graph contract found. |
| source_bucket | CURRENT IMPLEMENTATION SEMANTICS | Links a feel/reflection to the source memory and can mark it digested. | Must not be confused with raw source provenance. |
| mailbox / letters | PUBLIC GUARANTEE (qualified) | Letter/mailbox tools provide a separate intentional message channel with sealed behavior. | SQLite table/schema is internal. |
| boot | MODEL GUIDANCE | Recommended contextual orientation and trigger evaluation. | Prompt guidance is not a mandatory ritual. |
| breath | PUBLIC GUARANTEE (qualified) | Primary recall/surfacing interface with route-specific filters and privacy defaults. | Exact scoring and side effects are not a public guarantee. |
| dream | MODEL GUIDANCE | Optional reflection/digestion workflow that may synthesize or connect memories. | Not a required memory lifecycle step. |
| archive_session | PUBLIC GUARANTEE (qualified) | Explicit session archival and later session retrieval/topics behavior. | Exact archive representation is implementation. |
| bucket history | CURRENT IMPLEMENTATION SEMANTICS | Write-ahead history for bucket replacement/append/delete. | Not documented as a general undo or provenance API. |
| dehydration | CURRENT IMPLEMENTATION SEMANTICS | Output compression/summarization layer after retrieval. | It is not raw preservation and is subject to token/API behavior. |
| Remember-Me image assets | PUBLIC/IMPLEMENTATION BOUNDARY | RM image ownership is authority-selected and delegated to the pinned RM Core; memory buckets remain OB-owned. | No cross-layer source/provenance link is present. |

### 4.1 Semantic conflicts

1. Import UI/prompt language says “preserve original/no summary,” while the implementation stores `item.content` after LLM extraction and does not attach source provenance.
2. The Dashboard calls `/api/breath-debug` a Breath simulation, while the endpoint omits semantic retrieval, route filters, sealed gating, final surfacing, and the runtime admission order.
3. `INTERNALS.md` contains historical numeric tables that disagree with the current code: it lists feel decay as `50.0` while `decay_engine.py` returns `15.0`, lists search time decay `0.1` while `bucket_manager.py` uses `0.02`, and describes older topic/search constants. These are documentation conflicts, not reasons to change runtime code.
4. Older resolved-memory descriptions in historical material can suggest automatic archival. The current implementation and later behavior-fix history show resolved items remain stored and are handled through flags/penalties; the historical wording must not be used as the current contract.

The draft contract in [OB_MEMORY_LAYER_CONTRACT_v1_DRAFT.md](OB_MEMORY_LAYER_CONTRACT_v1_DRAFT.md) resolves these conflicts by labeling each statement and by deferring exact implementation constants.

## 5. Audit D — draft Memory Layer Contract

Created: [OB_MEMORY_LAYER_CONTRACT_v1_DRAFT.md](OB_MEMORY_LAYER_CONTRACT_v1_DRAFT.md).

The draft defines qualified public guarantees for privacy, retrieval interfaces, session/feel/mailbox separation, and RM asset ownership; records implementation semantics separately; keeps `boot`, `dream`, and other reflective sequences as model guidance; and explicitly declines to create an exact-quote/raw-evidence guarantee until a durable evidence layer exists.

The draft is not a migration plan and does not change runtime behavior.

## 6. Audit E — evidence ledger

Created: [memory-evidence-ledger.md](memory-evidence-ledger.md).

The ledger is deliberately outside `buckets/`, embeddings, `breath`, `boot`, and `dream`. It records architecture evidence and future investigation inputs only. It contains no private transcript, production log, or invented user incident. It includes confirmed repository-level entries for the `preserve_raw` provenance gap, the recall-debug mismatch, the sealed diagnostic exposure path, and the documentation/runtime numeric drift.

## 7. Audit F — historical evidence

### 7.1 Local Git and repository evidence

| Evidence | What it establishes |
|---|---|
| `821546d` (2026-04-19) | Import feature and chunk/resume lineage. |
| `ccdffdb` (2026-04-21) | Behavior specification and B-01–B-10 correction lineage, including resolved-memory handling and activation behavior. |
| `56966ab` (2026-06-12) | Hybrid/semantic retrieval was added after the original fuzzy scoring path. |
| `2464c1f` and `ddea363` | Date-range and structured filter routes expanded the real retrieval surface. |
| `2557ac1`, `a3329dc`, and later sealed-related commits | Sealed-memory privacy became an explicit implementation concern and was strengthened by tests. |
| `507d228` | Archived-session topic/retrieval semantics were added as a separate path. |
| `2fe03fb` | Quiesced encrypted capture/evidence-channel work was kept separate from the ordinary memory import path. |
| `4069443`, `ba61a65`, `2452fa9`, `32e15b4`, and current merge `3dc991c` | Remember-Me authority/asset cutover lineage and post-cutover acceptance evidence. This audit does not reopen that work. |
| `asset_backend.py` | Current ownership boundary: legacy adapter versus authority-selected pinned RM Core; backend presence alone does not select RM. |

These local sources support an evolution story: memory retrieval grew from fuzzy scoring into hybrid and route-specific retrieval, while privacy and RM ownership became explicit concerns. They do not prove production-state details that are not checked into the repository.

### 7.2 Sources unavailable to this audit

The following were not accessed and are classified `UNAVAILABLE_TO_AUDIT`:

- GitHub review-thread comments, external PR discussion, and CI logs beyond what local merge history records.
- Private user transcripts, source exports, and user-reported recall incidents.
- Production databases, production bucket contents, live embedding indexes, provider health, and deployment logs.
- Any external RM operational state beyond the user-provided statement that cutover is complete.

No conclusion in this report relies on pretending those sources were observed.

## 8. Final Decision Matrix and non-actions

| Item | Decision |
|---|---|
| Raw Evidence | `DESIGN` — no raw-evidence implementation exists or was started. |
| Memory Layer Contract | `DOCUMENT-ONLY` — the draft records qualified semantics and creates no runtime guarantees. |
| Recall Diagnostics | `REPAIR` — disposition is repair, but execution is not started in this audit. |
| Exact raw import provenance | Do not claim it. Design an isolated evidence model before implementation. |
| Recall debug endpoint | Do not use it as proof of actual Breath recall. Repair/instrumentation requires separate authorization. |
| Memory semantics | Use the draft contract as a qualified documentation baseline; keep implementation constants explicitly non-public. |
| Remember-Me | Treat asset ownership as a separate boundary; do not reopen cutover. |
| Production/runtime/tests/schema | No change in this audit. |
| Git commit/push | Not performed. |

## 9. Verification record

Read-only commands included branch/base/status checks, repository/file inspection, `git log`/commit inspection, `rg` source searches, and static comparison of import, retrieval, debug, sealed, and RM ownership paths.

The targeted pytest command was attempted with `D:\pythonandjingsuan\python.exe`, `PYTHONDONTWRITEBYTECODE=1`, and `-p no:cacheprovider`. Collection failed before test execution because the environment lacks repository dependencies including `frontmatter`, `starlette`, and `pytest_asyncio`. No package installation was attempted.

After document creation, the expected verification is that `git diff --name-only` contains only these three files:

- `docs/audits/OB_POST_CUTOVER_MEMORY_ARCHITECTURE_AUDIT.md`
- `docs/audits/OB_MEMORY_LAYER_CONTRACT_v1_DRAFT.md`
- `docs/audits/memory-evidence-ledger.md`

The final Git status is reported in the handoff response after that check.

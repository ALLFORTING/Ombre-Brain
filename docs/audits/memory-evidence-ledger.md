# Memory Evidence Ledger

Status: audit/product-maintenance ledger; not runtime memory.
Date opened: 2026-08-18
Baseline: `3dc991cd66552e38481622423941e9d8e9b494ce`

This ledger records architecture evidence and future investigation prompts. It must not be stored in `buckets/`, embedded, surfaced by `breath`, loaded by `boot`, synthesized by `dream`, or treated as a user memory. No private transcript, production log, or user incident was invented for this initial ledger.

## Category key

- `E1` — exact quote, provenance, or source-fidelity dispute.
- `E2` — suspected recall failure or explanation mismatch requiring a real-path trace.
- `E3` — confirmed repository-level recall/diagnostic behavior gap; not proof of a production user incident.
- `E4` — provider, embedding-index, or infrastructure condition that can be mistaken for recall failure.

## Ledger

| ID | Date | Source | Category | Query / problem | Expected | Actual | Workaround | Time cost | Root cause | Confirmed | Bucket ID | Would raw events help? | Would faithful recall debug help? | Notes |
|---|---|---|---|---|---|---|---|---|---|---|---|---:|---:|---|
| MEL-001 | 2026-08-18 | `import_memory.py`, `server.py`, import UI/prompt | E1 | Does `preserve_raw` retain the exact imported source? | Immutable source text with source identity and location. | LLM extraction occurs first; raw branch preserves `item.content` and bypasses the import-time secondary merge/dehydration step, but attaches no source span or import-run identity. | None in current implementation; document the limitation. | Not measured | No first-class raw evidence/provenance layer. | YES | N/A | YES | NO | No private import was opened. |
| MEL-002 | 2026-08-18 | `bucket_manager.py`, `server.py` `/api/breath-debug`, Dashboard `runBreathDebug()` | E3 | Does the debug view explain the real `breath` result? | Same candidate eligibility, semantic retrieval, admission, ranking, filters, and final surfacing. | Debug is a four-score simulation; it omits embeddings and route filters, includes sealed/dormant candidates, and applies resolved penalty before threshold. | Treat debug as non-authoritative component diagnostics. | Not measured | Independent simulation diverged from the runtime path. | YES | N/A | NO | YES | Static repository finding; no production endpoint call. |
| MEL-003 | 2026-08-18 | `/api/breath-debug` and `_is_sealed`/runtime `breath` paths | E3 | Can a diagnostic enumerate sealed metadata? | Sealed records remain hidden unless the route explicitly supports and receives sealed inclusion. | Endpoint lists all active buckets before any sealed exclusion and returns name/domain/type/scores. | Do not use endpoint for sealed/privacy verification; defer code repair. | Not measured | Missing sealed gate in the debug candidate enumeration. | YES | N/A | NO | YES | Authentication is not treated as sealed-content authorization. |
| MEL-004 | 2026-08-18 | `bucket_manager.py`, `decay_engine.py`, `INTERNALS.md` | E1 | Are documented scoring/decay constants current? | Documentation matches the current implementation or labels history. | `INTERNALS.md` records stale values including feel `50.0` versus current `15.0`, and time decay `0.1` versus current `0.02`. | Treat current code/tests as present behavior; record the drift. | Not measured | Historical numeric table was not updated with implementation changes. | YES | N/A | NO | NO | Documentation-only conflict; no runtime change authorized. |
| MEL-005 | 2026-08-18 | Local Git history, RM asset adapter, cutover documents | E4 | Is the OB memory layer the owner of RM image assets? | RM assets follow the authority-selected RM Core boundary. | `asset_backend.py` keeps legacy assets and pinned RM Core behind a runtime registry; memory buckets are separate. | Keep audit scope separated; do not reopen cutover. | Not measured | Separate ownership domains, not a recall failure. | YES (repository boundary) | N/A | NO | NO | Production state beyond user-provided cutover completion is unavailable to this audit. |

## Evidence limitations

The ledger has no private user feedback, exact user queries, production bucket IDs, production logs, live provider/index health, or GitHub review-thread context. Those sources are `UNAVAILABLE_TO_AUDIT` for this baseline. Future entries should add exact timestamps, query text, route parameters, candidate IDs, provider/index state, and raw evidence references only when authorized and available.

## Maintenance rule

Adding an entry is documentation/maintenance work. It does not authorize changing retrieval, storing raw transcripts, changing retention, repairing diagnostics, or modifying production behavior.

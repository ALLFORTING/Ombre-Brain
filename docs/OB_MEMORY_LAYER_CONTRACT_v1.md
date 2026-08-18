# OB Memory Layer Contract v1

Status: proposed authoritative contract, documentation-only

Baseline reviewed: `main` at `8a3eb6e7ef28802ad9b2970b2c34e632811bec30`

This document defines the evidence-backed boundary for the OB memory layer after the post-cutover audit and O2 recall-diagnostic repair. It does not change runtime behavior, tests, schema, dependencies, production state, or Remember-Me cutover behavior.

Documenting current implementation semantics does not create new runtime guarantees. A behavior is a contract guarantee only where this document marks it `PUBLIC GUARANTEE` and the repository evidence supports that scope. Implementation details may change without being a contract break when the marked public behavior remains intact.

## 1. Scope and layer model

The contract covers the boundaries that are currently observable or relied upon by the OB application:

1. source input and model extraction;
2. OB memory buckets and their lifecycle metadata;
3. response transformation and supported recall/surfacing routes;
4. current diagnostic observability for supported query Breath; and
5. a future raw-evidence/provenance design boundary.

Remember-Me assets remain under the Remember-Me authority boundary. They are not made OB memory buckets by this contract, and this contract does not reopen or alter the Remember-Me cutover.

## 2. Stability and classification rules

Every contract statement uses exactly one of these classifications:

- `PUBLIC GUARANTEE` — a compatibility-sensitive behavior that callers, users, privacy controls, or supported integrations may rely on within the stated scope.
- `CURRENT IMPLEMENTATION SEMANTIC` — behavior observed in the current repository that is useful to document, but may change without creating a new public guarantee unless separately promoted through contract review.
- `MODEL GUIDANCE` — guidance for model/tool orchestration. It is not a storage, privacy, security, or lifecycle guarantee.
- `UNSPECIFIED / NOT GUARANTEED` — intentionally outside the v1 promise.

Exact scoring weights, thresholds, decay coefficients, ranking formulas, resolved multipliers, token limits, metadata field names, SQLite/layout details, embedding internals, exact-match implementation details, and helper names are not public guarantees merely because the current code contains them.

## 3. Memory concepts and semantics

| Concept | Classification | Contract boundary |
|---|---|---|
| permanent | `CURRENT IMPLEMENTATION SEMANTIC` | A current bucket type provides longer-lived/protected handling. Its directory, decay behavior, promotion rules, and exact persistence semantics are implementation details. |
| dynamic | `CURRENT IMPLEMENTATION SEMANTIC` | A current ordinary bucket type participates in active memory lifecycle behavior. Exact merge, decay, and ranking semantics are not promised here. |
| archive/session | `PUBLIC GUARANTEE` | OB supports explicit archive/session memory and supported retrieval of that material. The storage representation, ordering, and route-specific ranking are not guaranteed. |
| feel | `PUBLIC GUARANTEE` | OB exposes a separate feel/reflection channel and supported feel-related behavior. The exact storage model, decay, and relationship to every other route are not guaranteed. |
| sealed | `PUBLIC GUARANTEE` | Sealed content is excluded from ordinary/default recall where the route supports the default policy. Explicit sealed inclusion is opt-in where supported. Dashboard authentication alone is not equivalent to explicit sealed inclusion. Default diagnostics must not enumerate sealed content or sealed metadata. |
| pinned | `PUBLIC GUARANTEE` | A pinned item can receive user-prioritized treatment. The exact score, bucket placement, locking mechanism, or ordering contribution is not guaranteed. |
| protected | `CURRENT IMPLEMENTATION SEMANTIC` | The implementation has protection behavior associated with selected items/buckets. No broader protected-data policy is promised beyond separately stated privacy and visibility guarantees. |
| resolved | `PUBLIC GUARANTEE` | Resolution is not deletion. Resolved material remains stored, and ordinary unresolved surfacing excludes it where that route applies; supported query behavior may still retrieve it. Exact resolved ranking penalties and timing are not guaranteed. |
| digested | `CURRENT IMPLEMENTATION SEMANTIC` | The current reflection/digestion flow can mark source material as digested. Exact decay, retention, and repeat-digestion behavior are implementation semantics. |
| dormant | `CURRENT IMPLEMENTATION SEMANTIC` | Dormant material is excluded from default eligibility in the supported ordinary recall path unless the route explicitly includes dormant material. Exact dormancy thresholds and transition rules are not guaranteed. |
| trigger_date | `PUBLIC GUARANTEE` | A memory with supported prospective trigger-date metadata can participate in the boot-trigger behavior demonstrated by the repository tests. Exact scheduling, deduplication window, and metadata representation are not guaranteed. |
| trigger_last_seen | `CURRENT IMPLEMENTATION SEMANTIC` | The implementation records observation state for trigger handling. Its field shape, update timing, and retention are not a public data contract. |
| related_buckets | `CURRENT IMPLEMENTATION SEMANTIC` | Current memory records can carry related-bucket links used by related-memory behavior. A stable graph model, ordering, completeness, or referential-integrity API is not guaranteed. |
| source_bucket | `CURRENT IMPLEMENTATION SEMANTIC` | Current feel/reflection flows can retain a source-bucket relationship and use it in digestion behavior. This is not an immutable raw-evidence or import-provenance guarantee. |
| mailbox/letters | `PUBLIC GUARANTEE` | Letters/mailbox are a distinct intentional message channel. Default mailbox retrieval honors sealed exclusion, with explicit inclusion only where the supported route permits it. |
| boot | `MODEL GUIDANCE` | Boot is recommended context initialization/orientation guidance. It is not a mandatory protocol, a once-per-conversation guarantee, or a requirement that every other memory route execute in sequence. |
| breath | `PUBLIC GUARANTEE` | Breath is the primary supported recall/surfacing interface, with the documented privacy and supported-filter boundaries. Exact ranking arithmetic, token budget, candidate counts, and internal side effects are not public guarantees. |
| dream | `MODEL GUIDANCE` | Dream is an optional reflection/digestion behavior. It is not a required lifecycle stage or a promise that every conversation invokes it. |
| archive_session | `PUBLIC GUARANTEE` | An explicit archive-session route exists for writing/recalling session-oriented material within its supported scope. Exact topic representation and route implementation are not guaranteed. |
| bucket_history | `UNSPECIFIED / NOT GUARANTEED` | Internal history records may exist, but v1 promises no general undo, restore, audit-provenance, or complete user-visible history API. |
| Remember-Me image ownership boundary | `PUBLIC GUARANTEE` | Remember-Me remains the authority for Remember-Me image assets after cutover. OB memory buckets are not the source of truth for those assets, and this contract does not change that boundary. |
| imported-memory provenance / preserve_raw | `CURRENT IMPLEMENTATION SEMANTIC` | Import performs model extraction first. For selected extracted items, `preserve_raw` keeps the post-extraction item content and bypasses the import-time secondary merge/dehydration step. It does not promise uploaded bytes, original formatting, message IDs, source spans, exact quotes, or durable import-run linkage. |
| recall diagnostics | `PUBLIC GUARANTEE` | Supported query diagnostics reuse the runtime/composition truth and are side-effect-free. The guarantee is bounded to supported query diagnostics; the explicitly listed unsupported/untraced routes are not represented as runtime-equivalent. |

## 4. Privacy and visibility

The v1 privacy boundary is intentionally narrow and evidence-backed:

- sealed content is excluded from ordinary/default recall in the supported paths that implement the default policy;
- explicit sealed inclusion is a distinct opt-in where a route supports it;
- Dashboard authentication is not itself authorization to expose sealed content through every diagnostic or recall surface;
- default diagnostics must not leak sealed sentinel content, bucket IDs, names, domains, tags/topics, score details, candidate counts, or exclusion traces;
- tests such as `tests/test_sealed.py`, `tests/test_boot_mailbox_seal.py`, and `tests/test_recall_diagnostic_fidelity.py::test_debug_excludes_sealed_and_dormant_metadata` provide the current static evidence for these boundaries.

This document does not claim live-production validation. Any claim about deployed data, deployed configuration, or production exposure requires a separate authorized live validation.

## 5. Recall diagnostics after O2

For the supported query path, the repaired diagnostic implementation instruments/reuses the real runtime/composition path rather than maintaining an independent scoring simulation. Debug execution selects the side-effect-free mode (`touch=False`, and no diagnostic cache write), while normal Breath retains its activation-touch behavior separately.

The supported diagnostic truth includes the currently repaired behavior for lexical/query scoring, semantic participation/state, hybrid ranking, exact-match tiering, threshold admission before the resolved ranking penalty, sealed exclusion and metadata non-leak, dormant eligibility, supported structured filters, and final token-budget/surfaced-vs-not-surfaced explanation.

The following routes remain explicitly unsupported/untraced by the repaired diagnostic API/UI:

- no-query surfacing;
- no-query resonance;
- session/archive topic route;
- session/feel route; and
- importance route.

Those routes are reported as unsupported/untraced rather than being presented as runtime-equivalent. This is an explicit coverage boundary, not a claim that O2 introduced a regression. A future observability expansion may cover them only if its maintenance and user value justify the additional surface.

## 6. Import content and future raw evidence

`preserve_raw` has a precise current meaning:

1. the model extraction step still runs;
2. selected extracted item content is preserved as produced by extraction; and
3. the import-time secondary merge/dehydration path is bypassed for that selected item.

It is therefore post-extraction content preservation and a secondary-dehydration bypass. It is not immutable transcript preservation and is not a raw-evidence ledger. The current implementation does not establish a durable link to uploaded bytes, the original message/document identity, source spans, exact quotes, or an import run.

Raw Evidence remains a future design, classified `DESIGN` by the post-cutover audit. It is not part of Contract v1 until a separately reviewed implementation and evidence model exists.

## 7. Remember-Me boundary

Remember-Me image assets remain under Remember-Me authority after cutover. The runtime asset backend/registry can distinguish the Remember-Me authority from legacy/OB asset handling, but this contract does not turn that implementation fact into an obligation to reopen the cutover or migrate data. OB memory semantics must not be used as a substitute source of truth for Remember-Me assets.

## 8. Non-guarantees and change policy

The following remain outside v1 unless separately adopted:

- exact lexical/semantic weights, thresholds, decay coefficients, ranking formula, resolved multiplier, or token budgets;
- exact field names such as `trigger_last_seen`, storage columns, directory layout, or SQLite schema;
- embedding provider behavior and internal embedding details;
- exact-match implementation details and helper/function names;
- complete route-by-route diagnostic equivalence;
- immutable raw transcript/provenance evidence;
- a general bucket-history undo or audit API; and
- a mandatory boot → Breath → dream → feel sequence.

Future Raw Evidence, new recall modes, broader diagnostics, and any new public guarantee require separate evidence-backed contract review. Describing current implementation semantics here does not automatically promote them to such guarantees.

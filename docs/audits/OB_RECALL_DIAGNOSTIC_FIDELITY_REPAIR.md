# O2 Recall Diagnostic Fidelity Repair

Status: implementation record; no commit or push performed by this task
Date: 2026-08-18
Audit baseline: `29cd7c2c33c41ec7ffc5f9701f169eda8e45e427`
Repair branch: `fix/recall-diagnostic-fidelity`
Final verdict: `PARTIAL_REPAIR_WITH_EXPLICIT_GAPS`

## Architecture

Diagnostics reuse and instrument the real runtime and composition path rather than maintaining an independent scoring simulation. `BucketManager.search()` provides an optional internal trace, while the debug endpoint reuses the same supported query selection and final composition helpers as normal Breath.

Debug execution remains side-effect-free: it selects `touch=False` and disables dehydration-cache writes. Normal Breath retains its existing activation-touch behavior; the pre-existing query touch was relocated into the shared composition helper rather than duplicated.

No second scoring implementation, MCP tool, schema migration, production deployment, RM cutover change, or production-data operation was added.

## Confirmed repaired fidelity

- Lexical/query scoring is emitted from the real search loop.
- Semantic participation and state, including disabled and provider-error distinctions where available, are reported without guessing unavailable or empty-index states.
- Hybrid ranking is represented using the runtime semantic and lexical contributions.
- Exact-match tiering is reported from the runtime search logic.
- Threshold admission occurs before the resolved ranking penalty; the penalty affects ranking afterward.
- Sealed records are excluded by default and their metadata does not leak through diagnostics.
- Dormant eligibility follows the supported runtime candidate gates.
- Supported structured filters are represented in the active query trace.
- Final token-budget decisions explain surfaced versus not-surfaced results, including token-budget and max-results omissions.
- Debug side-effect safety is verified: diagnostics do not call `touch()` or write dehydration-cache entries.

## Compatibility and security decisions

- Normal `BucketManager.search()` callers receive the same result list and ranking by default; trace collection is opt-in.
- Normal query Breath uses the shared composition helper with the existing activation-touch and dehydration-cache behavior.
- Sealed inclusion remains opt-in through the existing explicit `include_sealed` semantics; default diagnostics are sealed-hidden.
- Provider errors are reduced to bounded error codes; secrets and raw provider exception bodies are not returned.
- No production data, private transcript, paid provider, schema migration, RM ownership, or production deployment was used.

## Production-write safety

- Twelve existing `server.py` entries were maintained as `PURE_LINE_SHIFT` anchor updates.
- The old `_breath_impl` query touch anchor was replaced by the relocated `_compose_breath_query_matches` anchor.
- All production-write classifications were unchanged.
- Production-write coverage result: `issues=0`.
- No wildcard, suppression, or safety-rule weakening was introduced.

## Validation evidence

Validation used temporary buckets and fake/local providers only:

- Dedicated production-write coverage test: `1 passed`.
- Focused validation: `60 passed, 1 warning`.
- Full offline suite: `1370 passed, 11 skipped, 42 warnings`.
- Security and asset-viewer Python tests: `73 passed, 1 warning`.
- Viewer JavaScript tests: `5 passed`.
- AST validation: `PASS`.
- `git diff --check`: `PASS`.

The normal Breath regression verifies one surfaced query result causes exactly one activation touch. The debug side-effect regression verifies the diagnostic path leaves activation metadata unchanged and makes zero touch calls.

## Explicit remaining gaps

The Dashboard debug form remains a query-oriented surface. These routes remain explicitly unsupported or untraced:

- no-query surfacing;
- no-query resonance;
- session/archive topic route;
- session/feel route;
- importance route.

The repaired diagnostic API and UI report these paths as `unsupported_route` or `untraced` rather than claiming runtime equivalence. They are not described as regressions introduced by O2 and are not silently promoted to completed coverage.

These explicit gaps are candidates for a future Breath observability expansion only if there is sufficient maintenance and user value. They are not blockers for closing this bounded O2 repair.

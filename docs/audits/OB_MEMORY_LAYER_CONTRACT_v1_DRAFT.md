# OB Memory Layer Contract v1 — Draft

Status: documentation draft; not a runtime contract until separately reviewed and adopted.
Date: 2026-08-18
Baseline: `3dc991cd66552e38481622423941e9d8e9b494ce`

This draft separates stable user-facing guarantees from current implementation semantics and model guidance. Recording an implementation detail here does not make it a new guarantee. No section authorizes a runtime change, schema migration, production action, RM cutover change, or test change.

## 1. Scope and layer model

The current architecture contains at least these distinct layers:

1. Source input: an uploaded conversation or an explicitly created memory.
2. Extraction: a model-produced candidate memory and metadata.
3. Memory bucket: the durable OB-owned memory object used by current retrieval and lifecycle code.
4. Response transformation: dehydration, token budgeting, formatting, and route-specific surfacing.
5. Evidence/provenance: a desired future layer for immutable source payloads, offsets, run identity, and auditability. This layer does not currently exist as a first-class import representation.

The Remember-Me image asset layer is a separate ownership domain. It must not be inferred from memory-bucket semantics.

## 2. Qualified public guarantees

These are the behaviors suitable for a public contract, subject to normal product/security review:

### Privacy and visibility

- Sealed memory is hidden by default in covered runtime recall paths.
- Explicit sealed inclusion, where supported, is an intentional caller choice and must not be inferred from ordinary authentication alone.
- The public privacy expectation is that diagnostics do not expose sealed metadata merely because a candidate is present in the active store; the current `/api/breath-debug` path is a documented static-code gap against that expectation, not a completed implementation guarantee.

### Retrieval interfaces

- `breath` is the primary OB recall/surfacing interface.
- `breath` supports distinct retrieval modes such as ordinary query, structured filters, session/archive retrieval, feel retrieval, resonance, importance, and mailbox behavior. The exact mode surface is implementation-versioned and must remain documented with tests when changed.
- Returned content may be transformed for response size. A response transformation is not evidence that the stored memory was the original source text.

### Memory categories

- Session/archive memories are distinguishable from ordinary dynamic memories through the supported archive/session interfaces.
- Feel/reflection memories are a separate conceptual channel from ordinary dynamic recall.
- Pinned memories are user-prioritized and are not treated as ordinary decaying dynamic content. `protected` remains a current implementation semantic unless separately promoted.
- Resolved memories remain stored; they are not defined as automatically deleted by being resolved. They may be excluded from default unresolved surfacing and may receive implementation-specific retrieval/decay treatment.

### Explicit channels

- Mailbox/letter operations are a separate intentional message channel and inherit the documented sealed behavior where applicable.
- Remember-Me image assets remain under the authority-selected RM Core boundary after cutover. OB memory buckets do not become the owner or source of truth for RM image assets.

The Remember-Me statement is supported here by the checked-in adapter/registry boundary and the user-provided post-cutover context; live production ownership was not independently validated in this audit.

## 3. Current implementation semantics, not public guarantees

The following are observations of the current repository and may change without violating the qualified contract unless separately promoted:

- Fuzzy topic, exact-match, emotion, time, importance, semantic, and hybrid scoring formulas.
- Threshold values, resolved multipliers, decay coefficients, match tiers, ranking order, and token limits.
- Directory names, SQLite table layouts, cache locations, and internal metadata field names.
- Activation touches performed by some `breath` routes.
- Dormant thresholds and activation-count rules.
- The exact shape of `related_buckets`, `source_bucket`, `digested`, and trigger metadata.
- The current `preserve_raw` branch, which bypasses ordinary merge/dehydration but stores post-extraction item content rather than immutable source evidence.
- The current `/api/breath-debug` endpoint, which is a diagnostic score simulation and not a faithful trace of all `breath` modes.
- The current `bucket_history` write-ahead records, which are not a general user-facing undo or source-provenance API.

## 4. Model guidance, not runtime protocol

The following are recommendations for model behavior and operator choreography, not mandatory memory lifecycle rules:

- Use `boot` for orientation when context is useful, then use selective recall.
- Use `breath` for targeted retrieval rather than assuming every memory should be surfaced.
- Use `dream` or feel/reflection workflows when synthesis is useful; they are optional.
- Use `hold`, `grow`, and `trace` according to the model’s judgment and the user’s intent.

Prompt guidance must not silently become a storage, deletion, or security guarantee.

## 5. Raw/evidence contract — intentionally not adopted

The system currently does not provide an exact-quote guarantee for imported conversations. `preserve_raw` currently means:

- preserve the selected post-extraction item content and bypass the import-time secondary merge/dehydration branch; and
- store the model-produced extracted item content in a bucket.

It does not currently guarantee preservation of:

- original uploaded bytes;
- original source format;
- conversation/thread/message identifiers;
- exact source offsets or turn boundaries;
- an immutable source-to-bucket link;
- an import-run identity attached to the resulting bucket.

A future evidence layer must be designed before this option is described as exact raw preservation. At minimum, the design should decide payload fidelity, provenance fields, retention/deletion, access control, encryption, export, indexing/embedding exclusion, duplicate/resume identity, and the link between evidence and derived memory. No such layer is implemented by this draft.

## 6. Diagnostic contract qualification

`/api/breath-debug` must be described as a component-score diagnostic until it traces the actual runtime path. It currently does not prove:

- semantic-provider participation or failure;
- exact-match tiering;
- structured/session/feel/resonance filters;
- sealed/dormant candidate eligibility;
- resolved threshold admission order;
- dehydration and final token-budget selection;
- no-query decay/pinned/cold-start/random surfacing.

Any future diagnostic contract should explicitly identify candidate source, filters, provider/index state, admission, ranking, final response selection, and whether the observation had side effects.

## 7. Conflict resolution rule

When prompt wording, historical documentation, current implementation, and tests differ:

1. Privacy and ownership boundaries require explicit review and must not be weakened by a diagnostic convenience.
2. Current tests and current runtime define present behavior, not historical prose.
3. Historical documents remain evidence, not current guarantees.
4. Exact implementation constants remain non-public unless separately adopted.
5. An unresolved conflict is recorded in the evidence ledger rather than silently normalized.

Known conflicts at this draft’s baseline include the `preserve_raw` wording/behavior gap, the debug-endpoint/real-Breath gap, and stale numeric tables in `INTERNALS.md`.

## 8. Non-goals

This draft does not:

- change runtime or tests;
- define a schema migration;
- select a raw-evidence storage route;
- repair recall diagnostics;
- authorize production inspection or modification;
- reopen Remember-Me cutover;
- define new MCP tools;
- promise private transcript retention or deletion behavior that is not implemented.

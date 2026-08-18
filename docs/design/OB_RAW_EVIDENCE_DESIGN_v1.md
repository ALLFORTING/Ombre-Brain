# Ombre-Brain Raw Evidence Design v1

Status: adopted product-policy design; documentation-only. This document does not change runtime behavior, schemas, APIs, tests, storage, deployment, or ownership.

Design baseline: `566e849c0c0f7a3835c19b62cfda6eaf43ba3cce` (`origin/main` at design start).

This design is intentionally separate from the adopted Memory Layer Contract v1. It defines the adopted Raw Evidence Foundation v1 policy and a possible future raw-evidence layer that can support provenance without turning source material into ordinary recalled memory. Terms marked `CURRENT FACT` describe the repository as inspected. Terms marked `REQUIRED INVARIANT`, `RECOMMENDED V1 DESIGN`, `PRODUCT DEFAULT TBD`, `IMPLEMENTATION DEFAULT TO BENCHMARK`, `IMPLEMENTATION DETAIL`, or `FUTURE / OUT OF SCOPE` are design classifications, not current guarantees.

## 1. Purpose, status, and scope

O4 defines the boundaries, entities, fidelity vocabulary, privacy rules, storage direction, lifecycle semantics, and future implementation stages for raw evidence.

The intended result is a reviewable design that can answer:

- what was actually captured;
- which source and import run produced it;
- which derived memory or transformation used it;
- what fidelity and integrity claims are justified;
- who may retrieve, export, correct, redact, or delete it; and
- how evidence can remain isolated from ordinary memory recall.

This is a design artifact only. It must not be read as a claim that any evidence capture, durable raw transcript, exact-span mapping, evidence search, evidence retrieval, encryption-at-rest scheme, MCP tool, dashboard UI, or production deployment exists today.

The adopted Foundation v1 scope includes evidence capture, source/evidence identity, fidelity classification, content integrity, evidence metadata, lineage/provenance foundation, import-run identity, retry/idempotency, privacy/sealed policy foundations, configurable finite retention, tombstone/redaction semantics, derived-memory provenance status, backup/restore consistency, integrity verification, safe observability, default-off rollout, and rollback independence from ordinary memory.

Foundation v1 explicitly excludes Dashboard evidence UI, evidence export capability, LLM evidence retrieval, evidence full-text or semantic search, evidence embeddings, legal holds, external-KMS implementation, automatic historical provenance matching, and automatic Raw Evidence participation in memory recall.

## 2. Current architectural boundary

`CURRENT FACT`: the current import path accepts an uploaded stream, decodes it to text with UTF-8 replacement, and passes decoded content, filename, `preserve_raw`, and `resume` into the import engine. The engine hashes that decoded content with a short hash, parses and chunks normalized turns, requests model extraction from bounded chunk text, and persists progress in `buckets/import_state.json`.

`CURRENT FACT`: when the existing `preserve_raw` path is selected, the importer preserves selected extracted item content and bypasses later merge/dehydration. It does not preserve uploaded bytes, original formatting, source message IDs, source spans, exact quotes, or a durable import-run relation. `preserve_raw` is therefore not an evidence-capture switch.

`CURRENT FACT`: normal memories are Markdown bucket files with YAML frontmatter and body content. `BucketManager` supports ordinary create, update, archive, delete, search, semantic ranking, and a SQLite history table. That history table is a memory-change recovery mechanism, not a source-provenance registry.

`CURRENT FACT`: current dashboard import, memory, archive, asset, and diagnostic surfaces are authenticated. Dashboard authentication is not equivalent to permission to include sealed material. Current MCP tools and Memory Contract v1 behavior are not changed by O4.

`CURRENT FACT`: Remember-Me image bytes have an explicit ownership boundary through the Remember-Me asset backend after cutover. Legacy asset storage and ordinary memory storage remain separate namespaces.

The proposed layer must be additive and isolated. Existing ordinary memory behavior must remain valid when evidence capture is disabled, unavailable, or rolled back.

## 3. Goals and non-goals

### Goals

1. Preserve provenance as a separately authorized object, with an honest fidelity level.
2. Keep derived memory and source evidence conceptually and operationally distinct.
3. Make retries, partial failure, correction, redaction, export, backup, and restore explicit.
4. Support privacy and sealed classification without enumeration or content leakage.
5. Provide an implementation path that is testable on the current Windows-compatible deployment shape.
6. Allow future retrieval only through a separately adopted contract and capability.

### Non-goals

- changing Memory Contract v1, current recall, or current normal MCP tools;
- retroactively claiming provenance for existing memories;
- copying Remember-Me-owned image blobs into Ombre-Brain;
- adding runtime code, tables, endpoints, tools, dashboard screens, migrations, or tests in O4;
- selecting exact retention durations, audit-retention periods, quota numbers, or other numeric defaults in O4;
- making evidence automatically searchable, embeddable, recallable, or visible to a model;
- promising permanent retention, instantaneous physical deletion, or cryptographic erasure;
- implementing legal holds, Dashboard evidence UI, evidence export, LLM retrieval, evidence search, embeddings, or external-KMS encryption in Foundation v1;
- treating an importer reconstruction as the original source.

## 4. Conceptual separation

The model has four separate concepts. They may be stored in one database or several stores, but their identifiers, authorization, and lifecycle must remain distinguishable.

| Concept | Meaning | Must not be confused with |
| --- | --- | --- |
| Source | An upstream origin or source context, such as a conversation, export, document, file, item, or external asset reference | an Ombre-Brain evidence object or a processing attempt |
| Raw Evidence | A capture made by Ombre-Brain at a stated boundary, with stated fidelity and integrity metadata | an unqualified promise of upstream original bytes |
| Derived Memory | An ordinary Ombre-Brain memory produced or changed by interpretation, extraction, summarization, merging, dehydration, or editing | source material or proof that the source still exists |
| Lineage | An append-only relation describing which evidence or memory inputs contributed to a transformation and what was produced | a copy of the source content |
| Import / Processing Run | A bounded operation, actor, parser, model, status, and retry context | a source identity or evidence revision |

`REQUIRED INVARIANT`: a normal memory record cannot become raw evidence merely by setting a boolean or renaming a field. A source reference cannot authorize content access by itself. A lineage edge cannot imply a higher fidelity than its evidence input supports.

## 5. Entity model

The recommended logical model is:

### `SourceRecord` or `SourceRef`

Use a distinct source record when one upstream source can be referenced by multiple imports; otherwise use a qualified value object embedded in the evidence metadata. It should contain `source_system`, `source_kind`, a nullable qualified upstream identifier, source-level policy/classification, and a source fingerprint or descriptor. It must preserve whether an identifier was supplied by the upstream system or generated locally.

### `EvidenceObject`

One logical captured occurrence. Suggested fields:

| Field group | Suggested content |
| --- | --- |
| Identity | Ombre-Brain evidence ID; source ref; nullable upstream conversation/document/item IDs with their namespaces |
| Run relation | import/processing run ID; capture actor; capture timestamp; optional upstream timestamp |
| Ordering | source ordinal, message sequence, page/record number, or nullable occurrence key |
| Content | revision ID; media type; language/format; byte/text length; content hash and algorithm/version |
| Fidelity | one or more supported fidelity capabilities; importer boundary and normalization version |
| Storage | opaque content reference; not an arbitrary filesystem path |
| Policy | ordinary, sealed, restricted/admin, redacted, or tombstoned state; retention/hold references |
| Lifecycle | captured, available, superseded, needs-revalidation, missing, or deleted status |

### `EvidenceRevision`

An append-only content version for an evidence object. A correction or redaction creates a new revision with a parent/replacement relation and reason. The previous revision is not rewritten in place; it may become superseded, redacted, or tombstoned according to policy.

### `DerivedMemory`

The existing bucket/memory entity remains the derived-memory authority. O4 proposes only an optional internal relation to evidence and lineage; it does not change its current public contract or recall semantics.

### `MemoryLineage`

An append-only edge or transformation record connecting one or more input evidence revisions and/or prior derived memories to an output memory or revision. Suggested fields include transformation kind, importer/extractor/model version, run ID, input/output IDs, source span only when available, confidence/quality metadata where useful, and status.

### `ImportRun`

One processing attempt or idempotent run context. Suggested fields include run ID, initiator, source descriptors, parser/importer version, source digest, mode, timestamps, counts, idempotency key, status, error category, and reconciliation state.

Run state is not evidence state. A failed run may leave valid evidence; an evidence tombstone does not erase the run audit record.

## 6. Source identity

Identity has four layers:

1. A qualified upstream identity, if the source system supplies one.
2. An Ombre-Brain source identity for the imported source or source context.
3. An Ombre-Brain evidence identity for each captured occurrence.
4. A content identity hash for physical deduplication and integrity checking.

Upstream IDs are scoped: an item ID is not globally unique without its source system, account/tenant scope where applicable, and parent conversation/document scope. The design must never synthesize an upstream ID and present it as upstream fact.

When no stable upstream ID exists, use a locally generated source occurrence key built from a source fingerprint, import scope, and ordinal or parser-determined position. Mark it as an uncertain local identity. A changed parser or changed source ordering must not silently turn one occurrence into another.

Source identity and content identity are different. Identical text from two sources is two logical evidence objects, although their physical payloads may share a content-addressed blob.

## 7. Fidelity levels

Fidelity is capability-based rather than a single `raw` boolean. Candidate names, from weakest to strongest, are:

| Level | Meaning | Claim permitted |
| --- | --- | --- |
| `DERIVED_ONLY` | Only a derived value exists; no evidence payload is captured | No source reconstruction claim |
| `IMPORT_SNAPSHOT` | The bytes or text seen at Ombre-Brain's import boundary were captured | “Importer snapshot,” not necessarily upstream original |
| `SOURCE_TEXT` | Source text was captured with sufficient fidelity to the source text contract | Exact source text claim only within that contract |
| `SOURCE_ITEM` | A source item/record/message was captured with its item identity and structure | Item-level provenance claim |
| `EXACT_SPAN` | Captured source and stable offsets or a verified mapping identify an exact span | Exact-span claim for the recorded range |
| `ORIGINAL_BYTES` | Original bytes were obtained and retained before lossy decoding or normalization | Original-byte claim, subject to source/transport scope |

These can be represented as an ordered level plus capability flags when a capture has, for example, original bytes and item structure. `SOURCE_TEXT` must not be inferred from normalized `[用户]`/`[AI]` chunks. `EXACT_SPAN` requires a mapping that survives parsing and normalization. A decoded UTF-8 replacement string is not `ORIGINAL_BYTES`.

Every user-visible or machine-consumed provenance claim must state its fidelity level and capture boundary. If the design cannot prove the stronger level, it must expose the weaker one.

## 8. Integrity and immutability

`RECOMMENDED V1 DESIGN`: use SHA-256 with an explicit algorithm/version field, content length, media type, and canonical byte representation. Store payloads by a content-addressed key such as `sha256/<digest>` in a dedicated evidence namespace. The hash is an integrity identifier, not an authorization credential.

`REQUIRED INVARIANT`: a captured content revision is write-once from the application perspective. Ordinary edits update annotations or lifecycle metadata only. Content correction and redaction create a new revision or a tombstone relation; they do not overwrite the old revision in place.

On write, read, backup, restore, and export, verify the payload hash and length. A mismatch enters a quarantined or integrity-failed state and must not be served as valid evidence. Hash algorithm/version must permit future migration without ambiguous comparisons.

Filesystem permissions and database access are part of the threat boundary, not proof of immutability. Administrative operators may still alter storage; integrity verification detects such alteration. Hashes and metadata can themselves be sensitive and must not be included in unauthorised listings, logs, or telemetry.

## 9. Lineage model

Lineage describes transformations, not just “related to.” A record should identify:

- input evidence revision IDs and/or prior memory revision IDs;
- output memory ID/revision or other derived artifact;
- transformation kind: import extraction, summarize, merge, dehydrate, edit, re-extract, redaction, or correction;
- import/run ID and parser, extractor, or model version;
- source span/offset mapping only when verified;
- status: complete, partial, superseded, needs-revalidation, evidence-missing, or orphaned;
- optional quality/confidence metadata that is clearly non-authoritative.

`REQUIRED INVARIANT`: lineage never upgrades fidelity. A memory derived from `IMPORT_SNAPSHOT` cannot be reported as derived from original bytes. A missing or redacted input changes lineage status; it does not get silently removed to make the memory appear fully sourced.

Lineage is append-only for historical transformations. A new extraction adds a new edge or transformation record. A correction can supersede an edge while preserving the historical relation.

## 10. Import ordering and failure semantics

The ideal future ordering is:

1. Authenticate the import actor and validate source type, privacy policy, size, count, and resource limits.
2. Establish source and run identity; retain the original request context without putting content in logs.
3. Capture the permitted source snapshot into a temporary/quiesced staging area.
4. Compute and record content identity, fidelity, length, media type, and capture boundary.
5. Commit evidence metadata and its payload reference as a captured unit, or leave only an explicitly quarantined unreferenced staging artifact for cleanup.
6. Parse/extract from the captured evidence or an explicitly identified derivative.
7. Create or update ordinary memory using existing memory semantics.
8. Append lineage and checkpoint run progress.
9. Mark the run complete only after required evidence, memory, and lineage states reconcile.

The design must not assume a cross-store transaction. Recommended states are `captured`, `processing`, `memory_written`, `lineage_pending`, `complete`, `failed`, `needs_reconcile`, `redacted`, and `tombstoned` as appropriate to the entity.

Failure behavior:

| Failure point | Required result |
| --- | --- |
| Evidence write fails | Do not claim evidence exists. Do not derive from it unless an explicitly approved non-evidence import mode says so. |
| Evidence succeeds, extraction fails | Retain valid evidence with a failed/retryable run; no fabricated lineage or memory. |
| Extraction succeeds, memory write fails | Retain evidence and retryable processing state; no claim that memory was created. |
| Memory succeeds, lineage write fails | Keep memory and evidence, mark `lineage_pending`/`needs_reconcile`, and reconcile idempotently. |
| Process crashes between steps | Resume using run/source/item identity and content digest; do not duplicate memory or lineage edges. |
| Integrity verification fails | Quarantine the payload and fail closed for retrieval/export until repaired or deleted under policy. |

Normal memory recall must not depend on evidence storage being available. If an import policy requires evidence capture, that import may fail closed while ordinary memory operations continue.

## 11. Privacy and sealed handling

Evidence has its own classification, even when it produces ordinary memory. Recommended states are `ordinary`, `sealed`, `restricted_admin`, `redacted`, and `tombstoned`. Classification is item-level where necessary; a mixed import must not become visible merely because another item in the batch is ordinary.

Effective evidence visibility should be the most restrictive applicable policy from the source, explicit import/evidence policy, and user classification. Legal holds are a future extension, not a Foundation v1 capability. Derived memory does not downgrade evidence. A sealed derived memory does not automatically make all source evidence sealed, but evidence supporting a sealed memory must never be exposed more broadly through that relation.

`REQUIRED INVARIANT`: default evidence enumeration, counts, search, existence checks, hashes, previews, error details, and model context fail closed for sealed/restricted records. An explicit capability is required, and dashboard login alone is not that capability.

Logs, metrics, traces, exception messages, progress responses, and audit records must carry safe IDs/statuses only. They must not carry raw content, source excerpts, unrestricted paths, or hashes that reveal hidden object existence.

Access grants should be scoped to operation and purpose: inspect, retrieve, export, redact, delete, restore, or administer. Foundation v1 has no Dashboard evidence viewer, export capability, or model retrieval surface. Each implemented security-sensitive operation should be auditable without recording the content itself.

## 12. Recall, search, and index boundaries

Evidence is not ordinary memory. Foundation v1 excludes evidence from:

- `breath` and other normal memory queries;
- exact, fuzzy, semantic, resonance, no-query, dream, archive, and session recall paths;
- automatic context injection and model prompts;
- ordinary bucket listing, counts, and memory-network views.

If evidence retrieval is later needed, it must be a separate, explicit, authenticated, scope-limited capability and contract. Foundation v1 provides no retrieval endpoint, tool, or UI. Results should return evidence IDs/revisions, fidelity, status, source identity fields allowed by policy, and bounded content only when authorized. Retrieval must not silently create a normal memory.

Foundation v1 has no evidence-specific full-text index, semantic index, or embedding index. Evidence may be addressed directly by identity and lineage operations only. A later discovery capability must use a dedicated namespace and access-aware index; sealed and redacted records must not leak through index statistics, autocomplete, result counts, or ranking behavior.

`REQUIRED INVARIANT`: no evidence search index is part of Foundation v1. Capture and lineage are validated without making raw content broadly discoverable.

## 13. Embedding policy

`REQUIRED INVARIANT`: do not embed raw evidence in Foundation v1. Reasons include privacy and provider leakage, difficult deletion and stale vectors, the risk of ordinary semantic recall ingesting evidence, and the absence of a separate evidence retrieval contract.

If a later separately adopted capability authorizes embeddings, use a separate evidence index and namespace, record embedding model/version and evidence revision, apply classification-aware access, support deletion/rebuild, and prohibit reuse by normal memory search. A local controlled provider may be preferable for sealed material, but that is future scope.

No current `embeddings.db` behavior is changed by this design.

## 14. Storage options and recommendation

### Option A: registry SQLite plus content-addressed filesystem blobs

Store source/evidence/revision/lineage/run metadata in a dedicated SQLite registry and payloads in a dedicated `evidence/` content-addressed root. This is the recommended direction.

Advantages: clear namespace separation; efficient handling of large text and binary payloads; content hashing and physical deduplication; append-only blob discipline; straightforward Windows-compatible file permissions; staged writes and garbage collection; selective backup.

Costs: metadata and payload commit across two surfaces; reconciliation and orphan cleanup are required; backup must include both registry and blobs consistently.

### Option B: SQLite-only payloads

Store metadata and payload BLOBs in one SQLite database.

Advantages: one transactional surface; simple metadata/payload atomicity; one restore unit.

Costs: large transcript or binary growth; locking and file-growth behavior; more expensive incremental backup; possible impact on current memory database operations; less convenient streaming and quarantine.

### Option C: ordinary Markdown bucket files

Store evidence in current bucket Markdown files with additional frontmatter.

This is not recommended. It couples privacy and source lifecycle to ordinary recall/list/delete APIs, cannot honestly provide append-only content revisions by convention alone, risks exposing evidence through existing backup and dashboard paths, and blurs current `preserve_raw` semantics.

### Recommendation

`RECOMMENDED V1 DESIGN`: choose Option A, with a dedicated registry and payload namespace outside ordinary bucket roots and outside Remember-Me-owned roots. Require explicit reconciliation states and hash verification. The exact schema, file layout, transaction implementation, and numeric quota values are `IMPLEMENTATION DETAIL` or `IMPLEMENTATION DEFAULT TO BENCHMARK` and are intentionally not designed as code in O4.

## 15. Encryption and secret handling

Mandatory Foundation v1 security invariants are logical access control, dedicated storage permissions, fail-closed sealed checks, no raw content in logs/telemetry/errors, safe file/storage boundaries, encrypted backups where supported by deployment, and no arbitrary path access. Foundation v1 has no external-KMS implementation.

Live Raw Evidence encryption at rest is not a current guarantee and must not be stated as one. External-KMS/envelope encryption remains future production hardening for deployments whose threat model requires it. Do not claim live Raw Evidence is encrypted at rest unless the actual deployment/storage implementation provides that guarantee.

Transport protection, secret rotation, backup key custody, and operator access are deployment concerns that must be resolved before a production evidence rollout.

## 16. Retention

Evidence retention is independent from derived-memory retention. Foundation v1 adopts policy-class retention with source-specific/user-specific overrides and a configurable finite default. It must not silently treat evidence as indefinitely retained. Legal holds are future/out of scope for v1.

The exact default duration is `PRODUCT DEFAULT TBD`; it does not block implementation planning because the retention and purge mechanism must be configurable. Backup expiry must follow an explicit documented lifecycle rather than silently retaining evidence forever.

`REQUIRED INVARIANT`: record policy version and decision time. If evidence expires, keep only the minimum lineage status needed to avoid fabricating provenance, such as `evidence_missing` or `source_expired`.

## 17. Deletion and cascade semantics

The adopted Foundation v1 model is logical deletion first, followed by policy-controlled physical purge. Legal holds are not implemented in v1; the lifecycle must leave an extension point for them.

| Deletion request | Foundation v1 behavior |
| --- | --- |
| Evidence object/revision | Tombstone/restrict access immediately, mark related lineage unavailable, and purge shared blobs only when safe under retention/deletion policy. |
| Derived memory | Do not automatically delete it when evidence disappears; retain it with truthful status such as `evidence_missing`, `source_redacted`, `needs_revalidation`, or `provenance_broken`. |
| Import run | Remove or tombstone run control data without deleting shared evidence or memory automatically. |
| Upstream source | Require review/confirmation unless both source identity and deletion authority are demonstrably trustworthy. |
| Account/full-data deletion | Use a staged tombstone-and-purge workflow for applicable evidence, lineage, derived memory, blobs, and any later live indexes. Backup copies follow the documented backup lifecycle. |

Deleting, expiring, redacting, or losing evidence must not automatically delete derived memory in v1. A future explicit combined action may delete both, but must require deliberate authorization and account for shared evidence.

Shared physical blobs are deletable only after no live revisions reference them and the applicable retention/deletion policy permits it. Do not promise instantaneous physical deletion from every backup or cryptographic erasure without a storage/key architecture that supports that claim.

## 18. Editing, correction, and redaction

Evidence content is not edited in place. Metadata annotations such as display label, classification, or review note may be mutable with an audit record. A content correction creates a new revision pointing to its parent, with actor, reason, time, and correction scope.

Redaction immediately tombstones/restricts normal access to the affected evidence, then follows policy-controlled physical purge. The application must not claim that physical purge is instantaneous or that a redacted revision contains the original content. Legal holds are not implemented in v1.

Lineage points to the revision actually used. Corrections or redactions supersede affected relations and mark derived memories as unavailable or needing revalidation where the mapping is known. Derived memories are not automatically deleted. Original hashes may remain in restricted audit metadata if policy permits, but even hashes can be sensitive and must not be exposed by default.

## 19. Export

The manifest/format design is retained for future compatibility, but Foundation v1 has no user/operator export capability, endpoint, tool, or UI. A future export must be a separate, versioned, authenticated operation. Its manifest should include:

- export format/version and creation time;
- evidence object and revision IDs;
- qualified source identity and capture boundary;
- fidelity level/capabilities;
- media type, length, algorithm/hash, and storage availability;
- privacy state, retention/hold state, and tombstone/redaction status;
- authorized lineage edges and import/processing run metadata;
- authorized derived-memory references or content, if selected;
- explicit unavailable/missing/redacted references rather than silently omitting them.

Content should be separate from the manifest, for example a manifest plus a controlled file tree or archive. A future export must not call bytes “original” unless the recorded fidelity supports that claim. Future export excludes sealed/restricted evidence by default; sealed export requires an explicit capability and audit record.

Current authenticated backup export is a general file snapshot, not a dedicated evidence export contract. Future evidence export must not inherit ordinary-memory exposure merely because evidence uses the same deployment. Future export requires separate authorization, auditing, sealed handling, and quotas.

## 20. Backup and restore

An evidence backup is consistent only when the registry, content-addressed blobs, lineage records, and import-run records are mutually verifiable. The backup manifest must record hash, size, classification, lifecycle state, and whether content is present.

Restore should first land in staging, verify manifest and payload hashes, validate namespace and policy, then publish references. Corrupt payloads are quarantined. Evidence without a registry reference is an orphan for reconciliation; memory without an available evidence revision is marked with broken/ missing provenance; evidence and memory without a lineage edge remain separately valid but incomplete.

`CURRENT FACT`: the repository has authenticated backup routes and offline encrypted/integrity-checked backup/capture patterns. Those patterns are useful inputs but do not constitute a Raw Evidence backup contract. Evidence must be a separately classified backup category, and raw content must not become public because a general backup JSON includes it. Backup copies must expire or purge under an explicit documented lifecycle rather than silently retaining evidence forever.

## 21. Deduplication and retry

Logical evidence identity represents a source occurrence, not merely its text. Physical deduplication may reuse a content-addressed blob by hash.

- Same content, different sources: distinct logical evidence objects; optional shared physical blob.
- Same source item, same run, retried: same idempotency key and no duplicate lineage edge.
- Same source imported in a new run: new run; reuse or relink evidence only under explicit source-identity policy, not by text matching alone.
- Upstream item IDs: unique only within a qualified source scope.
- No stable IDs: use local source occurrence identity and record uncertainty.

Retry state must distinguish an existing valid capture from an incomplete staging artifact, a failed transform, and an already committed memory. Content hash alone must not collapse two source occurrences or impersonate provenance.

## 22. Model access, MCP, and dashboard

### Model access

Foundation v1 gives no LLM access to Raw Evidence. Raw Evidence must never automatically enter Breath, ordinary recall, model context, resonance, Dream, or no-query surfacing. A future retrieval path must be explicit, separately contracted, authorized by evidence ID/revision and scope, token/item bounded, isolated as untrusted source text, and audited without recording content.

### MCP

O4 adds no MCP tool or resource. Any future evidence capability must be separately versioned, explicitly authorized, privacy-scoped, and excluded from normal memory tools. Reusing `breath` or ordinary memory search for evidence would violate the permanent boundary.

### Dashboard

`CURRENT FACT`: the dashboard can inspect memories, archives, imports, diagnostics, and assets. Foundation v1 adds no Raw Evidence browser, detail UI, sealed viewer, or lineage graph. A later controlled stage may offer scoped operator/admin inspection; dashboard authentication alone must never imply sealed-evidence authorization.

## 23. Remember-Me and preserve_raw relationship

Raw Evidence must not become an alternate Remember-Me asset store. It may store a safe reference to a Remember-Me asset ID or source relation when the relation is allowed, but must not copy Remember-Me-owned image bytes, claim asset ownership, or bypass the pinned Remember-Me authority. A source image reference does not imply permission to capture the image blob.

The import model keeps two independent choices:

| Choice | Meaning |
| --- | --- |
| Evidence capture | Whether permitted source material is captured with a declared fidelity; Foundation rollout is explicit opt-in/default-off |
| `preserve_raw` | Existing memory import behavior: preserve selected extracted content and skip later merge/dehydration |

`CURRENT FACT`: the existing `preserve_raw` behavior is not evidence capture and must remain unchanged by O4. No migration should reinterpret old `preserve_raw` memories as source evidence.

## 24. Memory Contract v1 relationship

O4 does not amend the adopted Memory Layer Contract v1, normal recall paths, sealed-memory behavior, current MCP tool counts, or current Remember-Me ownership. Existing memory records remain valid without evidence.

The permanent boundary is that Raw Evidence is not ordinary memory. An internal lineage relation can be introduced without changing public Memory Contract fields, provided normal clients cannot observe new claims or retrieval behavior. Any future user-visible evidence status, source fidelity, delete propagation, or retrieval surface requires a separately reviewed contract amendment or version.

## 25. Threat model

| Threat | Required or recommended mitigation |
| --- | --- |
| Evidence injected into ordinary recall | Separate namespace and query gates; no recall, model-context, index, or embedding participation |
| Sealed existence/count leakage | Fail-closed enumeration, counts, hashes, autocomplete, and errors |
| Raw content in logs/errors/telemetry | Restricted metadata-only audit; safe IDs/statuses only |
| Path traversal or arbitrary file read | Opaque registry references; canonical root checks; no user path input |
| Tampered blob | Hash/length verification on write, read, backup, and restore; quarantine mismatch |
| Hash confusion or algorithm change | Store algorithm/version and canonical bytes; do not use hash as auth |
| Duplicate-source impersonation | Qualified upstream scopes, local IDs marked as local, no text-only provenance |
| Stale or missing lineage | Append-only edges and explicit `needs_revalidation`/`evidence_missing` states |
| Unauthorized future export or restore | Separate capability, audited action, sealed exclusion by default, staged restore |
| Deletion bypass through shared blobs/backups | Reference checks, tombstone/purge workflow, explicit backup lifecycle |
| Malicious document or prompt injection | Treat source as untrusted data; bounded parser/model inputs; no policy authority in source text |
| Disk exhaustion or oversized import | Hard bounded quotas, preflight limits, streaming/staging, bounded retries, fail closed |
| External embedding/provider leakage | No embeddings or search in Foundation v1 |
| Remember-Me ownership bypass | Asset references only unless independently permitted; pinned RM authority remains authoritative |

## 26. Resource limits

Raw Evidence must have hard bounded resource limits and fail closed. Bounds cover evidence item size, source/import batch size, storage quota, oversized transcripts, large binary payloads, restore staging, retry work, and future export size.

Exact numeric thresholds are `IMPLEMENTATION DEFAULT TO BENCHMARK`; O4 deliberately selects no numbers. Limits must be configurable, source-kind aware, checked before allocation where possible, enforced while streaming, observable as safe counters, and unable to expand authorization. A failed capture must not leave an unbounded orphan or retry indefinitely.

## 27. Observability

Safe observability should cover authorized-scope counts, bytes/quota, capture and processing status, retry age, orphan evidence, orphan lineage, integrity failures, redactions, deletions, missing references, backup/restore verification, and reconciliation backlog.

Raw Evidence access and security-sensitive operations should produce restricted metadata-only audit records for authorized access, sealed access when a future sealed-access surface exists, deletion, redaction, policy change, future export, and integrity failure. Audit records must not contain Raw Evidence content. Exact audit-record retention is `PRODUCT DEFAULT TBD`; the period must be configurable and does not block implementation planning.

Default aggregates must exclude sealed/restricted records unless the observer has the corresponding capability. Do not emit content, source excerpts, arbitrary paths, or unrestricted hashes. Correlation IDs and opaque object IDs are acceptable when their existence is itself not sensitive; otherwise use a non-enumerating event reference.

## 28. Migration and compatibility

Existing memories cannot receive retroactive provenance merely because their content resembles an old transcript. They should remain explicitly classified as `legacy_memory_without_evidence` or equivalent.

Historical sources may be explicitly re-imported to create new Raw Evidence. Historical lineage may attach to existing memories only when mapping is explicitly and structurally validated. Automatic provenance matching by similarity, text, timestamps, or embeddings is rejected. Partial capture must be represented as partial attribution, not full-source provenance.

Old binaries should ignore the dedicated evidence namespace and continue normal memory behavior. New binaries must tolerate an absent, disabled, or unavailable evidence registry according to import policy. Any user-visible lineage field requires contract review. `preserve_raw` has no retroactive evidence meaning.

## 29. Feature rollout and rollback

Raw Evidence capture is explicit opt-in/default-off for the initial rollout. A future deployment-level default may be considered only after the feature matures. The capture decision remains independent from `preserve_raw`.

The feature flag off state must preserve current import, memory, recall, sealed, RM, and MCP behavior. Rollback disables capture and read surfaces while preserving enough registry state for later reconciliation or policy-approved cleanup. Evidence unavailability must not make ordinary memory recall fail. Partial deployment states must be represented and reconciled rather than silently treated as complete.

## 30. Adopted decisions and future implementation decomposition

### Adopted Foundation v1 policy

1. Retention is policy-class based with source-specific/user-specific overrides and a configurable finite default. The exact duration is `PRODUCT DEFAULT TBD`; backup expiry follows an explicit lifecycle.
2. Legal holds, hold authority, hold release, and upstream automatic holds are `FUTURE / OUT OF SCOPE` for v1.
3. Evidence disappearance never automatically deletes derived memory; lineage records truthful missing/redacted/revalidation status.
4. Ordinary memory deletion deletes only derived memory by default; supporting evidence follows its own lifecycle. Automatic cascade deletion is rejected.
5. Upstream deletion requires review/confirmation unless source identity and deletion authority are both demonstrably trustworthy.
6. Full-data/account deletion uses staged tombstone-and-purge across applicable live data, with backup lifecycle handling. No instantaneous physical-deletion or cryptographic-erasure promise is made.
7. Redaction immediately tombstones/restricts normal access, then follows policy-controlled physical purge; it does not automatically delete derived memory.
8. Foundation v1 has no Dashboard evidence UI, browser, detail view, sealed viewer, or lineage graph.
9. Foundation v1 has no user/operator evidence-export capability; the manifest/format design is retained for future compatibility.
10. Foundation v1 gives no LLM access to evidence and no automatic evidence inclusion in any normal recall/context path.
11. Foundation v1 has no evidence full-text index, semantic search, or embeddings; future discovery evaluates local/access-aware full-text search before semantic search.
12. Raw Evidence is hard bounded and fails closed. Numeric limits are `IMPLEMENTATION DEFAULT TO BENCHMARK`.
13. Foundation v1 requires access/storage boundaries, safe permissions, encrypted backups where supported, and no raw content in logs/telemetry/errors; external KMS/envelope encryption is future hardening.
14. Evidence capture is explicit opt-in/default-off and independent from `preserve_raw`.
15. Historical sources may be re-imported, but historical lineage requires explicit structural validation; automatic similarity matching is rejected.
16. Security-sensitive evidence operations use restricted metadata-only audit records. Exact audit retention is `PRODUCT DEFAULT TBD`.

### Decision matrix

| Decision area | Classification | O4 position |
| --- | --- | --- |
| Four-concept separation | REQUIRED INVARIANT | Source, evidence, derived memory, lineage, and run remain distinct |
| Qualified IDs and no fabricated upstream identity | REQUIRED INVARIANT | Scope upstream IDs; mark local uncertainty |
| Fidelity vocabulary | RECOMMENDED V1 DESIGN | Use capability-based levels including import snapshot and exact span |
| Append-only revisions and hash verification | REQUIRED INVARIANT | No in-place evidence content edits |
| Lineage status and reconciliation | REQUIRED INVARIANT | Preserve incomplete, missing, redacted, and revalidation states honestly |
| Import ordering/state machine | RECOMMENDED V1 DESIGN | Capture and identify evidence before deriving memory |
| Registry plus content-addressed blobs | RECOMMENDED V1 DESIGN | Preferred storage option |
| Exact SQLite schema/file layout | IMPLEMENTATION DETAIL | Define during implementation with crash/Windows tests |
| Privacy and fail-closed sealed handling | REQUIRED INVARIANT | Separate capability; no hidden enumeration |
| Retention policy | RECOMMENDED V1 DESIGN | Finite configurable policy classes; exact default is `PRODUCT DEFAULT TBD` |
| Legal holds | FUTURE / OUT OF SCOPE | No legal-hold feature in Foundation v1 |
| Delete cascade/invalidation | REQUIRED INVARIANT | Evidence and memory have independent default lifecycles |
| Source deletion propagation | REQUIRED INVARIANT | Review/confirm unless identity and authority are proven |
| Redaction | REQUIRED INVARIANT | Immediate tombstone/restriction followed by policy-controlled purge |
| Full-data deletion | REQUIRED INVARIANT | Staged tombstone-and-purge; no instant/cryptographic-erasure claim |
| Export capability | FUTURE / OUT OF SCOPE | Keep manifest design; no Foundation v1 export endpoint/tool/UI |
| Evidence index/search | FUTURE / OUT OF SCOPE | No full-text or semantic evidence search in Foundation v1 |
| Evidence embeddings | FUTURE / OUT OF SCOPE | No evidence embeddings in Foundation v1 |
| LLM retrieval | FUTURE / OUT OF SCOPE | No model access or automatic context inclusion in Foundation v1 |
| MCP tools | FUTURE / OUT OF SCOPE | O4 adds none |
| Dashboard UI | FUTURE / OUT OF SCOPE | No evidence inspection UI in Foundation v1 |
| Remember-Me blob ownership | REQUIRED INVARIANT | Reference only; no copy/cutover change |
| `preserve_raw` semantics | REQUIRED INVARIANT | Remains existing extracted-memory behavior |
| Memory Contract v1 | REQUIRED INVARIANT | No silent contract or recall change |
| Foundation security controls | REQUIRED INVARIANT | Access boundaries, permissions, safe logs, encrypted backups where supported |
| External KMS/envelope encryption | FUTURE / OUT OF SCOPE | No external-KMS implementation in Foundation v1 |
| Numeric limits and quotas | IMPLEMENTATION DEFAULT TO BENCHMARK | Hard bounded; exact values are not selected in O4 |
| Audit-record retention | IMPLEMENTATION DETAIL | Metadata-only and restricted; exact period is `PRODUCT DEFAULT TBD` |
| Evidence capture mode | REQUIRED INVARIANT | Explicit opt-in/default-off; independent from `preserve_raw` |
| Historical re-import | REQUIRED INVARIANT | Validated mapping only; no automatic similarity provenance |
| Safe observability | REQUIRED INVARIANT | No content or hidden-object leakage |
| Migration of old memories | REQUIRED INVARIANT | No fabricated provenance; legacy state remains explicit |
| Rollback | REQUIRED INVARIANT | Ordinary memory remains independent |

### Future implementation decomposition

| Stage | Scope | Explicit non-goals | Validation focus |
| --- | --- | --- | --- |
| O5A Raw Evidence storage/integrity foundation | Registry, IDs, fidelity metadata, CAS payload staging, permissions, hash verification | No Dashboard UI, export, retrieval, search, embeddings, legal holds, KMS, or normal recall integration | Crash recovery, hash mismatch, Windows paths/ACLs, orphan cleanup |
| O5B Opt-in import evidence capture | Explicit capture mode, source adapters, import-run state, bounded staging, idempotent retry | No automatic capture for existing imports, user retrieval, automatic recall, or embedding | Source fidelity, limits, privacy classification, retry behavior |
| O5C Lineage/provenance integration | Transform records, validated historical re-import mapping, derived-memory status, reconciliation | No fabricated legacy provenance or changed normal memory semantics | Extraction/merge/dehydrate/edit coverage and missing-input states |
| O5D Lifecycle | Configurable finite retention, tombstone/redaction, staged purge, source-deletion review, derived-memory independence | No legal holds, Dashboard UI, export capability, LLM retrieval, search, embeddings, or KMS | Shared-blob purge, backup lifecycle, redaction, deletion, rollback |
| O5E Backup/restore/observability hardening | Consistent evidence backup/restore, integrity verification, restricted metadata audit, quotas, safe metrics, feature flags | No public evidence surface or automatic recall coupling | Restore staging, corruption quarantine, audit safety, quota/failure drills |

Dashboard inspection, export, explicit LLM retrieval, search/indexing, embeddings, legal holds, and external-KMS encryption remain independently reviewed future capabilities. They are not required Foundation v1 stages.

### Accidental-guarantee audit

The adopted design still does not claim that Raw Evidence currently exists, that existing memories have provenance, or that historical original transcripts can be reconstructed. It does not claim fixed retention numbers, instantaneous physical deletion, cryptographic erasure, legal-hold capability, Dashboard evidence UI, export, LLM access, evidence search, embeddings, external-KMS encryption, Remember-Me image duplication, or production deployment. All implementation descriptions remain future/proposed.

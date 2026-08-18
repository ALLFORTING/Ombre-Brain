# O4 Raw Evidence Design v1 — Independent Review

Review status: design-only review of adopted Raw Evidence Foundation v1 decisions.

Reviewed baseline: `566e849c0c0f7a3835c19b62cfda6eaf43ba3cce` (`origin/main`).

Reviewed artifacts:

- `docs/design/OB_RAW_EVIDENCE_DESIGN_v1.md`
- `docs/OB_MEMORY_LAYER_CONTRACT_v1.md`
- `docs/audits/OB_MEMORY_LAYER_CONTRACT_V1_CLOSURE_REVIEW.md`
- `docs/audits/OB_POST_CUTOVER_MEMORY_ARCHITECTURE_AUDIT.md`
- current import, bucket, server, backup, asset, Remember-Me, dashboard, authentication, and relevant test files by static inspection.

No runtime code, test suite, schema migration, deployment, production data, commit, push, or PR was performed for O4.

## 1. Review method and scope

The review independently compared the design against the requested O4 topics, current O3/Memory Contract v1 boundaries, and the repository’s inspected implementation. Classifications used here are:

- `CURRENT FACT`: supported by the inspected repository/docs;
- `REQUIRED INVARIANT`: must hold if implemented;
- `PROPOSED DESIGN`: coherent recommended direction, not implementation;
- `PRODUCT DEFAULT TBD`: exact numeric/default value intentionally not selected;
- `IMPLEMENTATION DEFAULT TO BENCHMARK`: bounded value to be selected through implementation benchmarking;
- `FUTURE / OUT OF SCOPE`: explicitly excluded from Foundation v1;
- `UNKNOWN / NEEDS VALIDATION`: requires implementation or deployment validation.

## 2. Current architecture findings

| Finding | Classification | Review assessment |
| --- | --- | --- |
| Main baseline and branch alignment | CURRENT FACT | O4 branch was created from remote main at the reviewed SHA. |
| Existing import path | CURRENT FACT | Upload is decoded to text, parsed/chunked, extracted, and persisted through current import state. |
| Existing `preserve_raw` | CURRENT FACT | Preserves selected extracted memory content and bypasses later merge/dehydration; it is not source evidence. |
| Existing memory storage | CURRENT FACT | Markdown bucket files plus metadata/history/embedding behavior; no Raw Evidence registry. |
| Existing lineage | CURRENT FACT | No durable Raw Evidence lineage object is attached to current imported memories. |
| Existing sealed handling | CURRENT FACT | Current memory routes have sealed boundaries; dashboard authentication is not sealed inclusion authorization. |
| Existing backup | CURRENT FACT | General authenticated backup and offline encrypted/integrity-checked patterns exist, but no evidence-specific contract. |
| Remember-Me ownership | CURRENT FACT | RM asset routing/authority is separate; O4 must not copy or cut over RM blobs. |
| Current Raw Evidence implementation | CURRENT FACT | Not present; the design does not imply it is present. |

The design accurately preserves the O3 closure boundary: no control-flow, storage, schema, recall, RM, or MCP change is proposed as part of O4.

## 3. Requirements coverage

| Requested area | Classification | Assessment |
| --- | --- | --- |
| Source/evidence/derived memory/lineage/run separation | REQUIRED INVARIANT | Four concepts are explicit and not collapsed into `preserve_raw`. |
| Source identity | PROPOSED DESIGN | Qualified upstream IDs, local IDs, occurrence keys, and uncertainty are defined. |
| Fidelity | PROPOSED DESIGN | `DERIVED_ONLY`, `IMPORT_SNAPSHOT`, `SOURCE_TEXT`, `SOURCE_ITEM`, `EXACT_SPAN`, and `ORIGINAL_BYTES` are distinguished. |
| Immutability/integrity | REQUIRED INVARIANT | Append-only revisions, CAS recommendation, hash/length verification, and quarantine are defined. |
| Lineage | REQUIRED INVARIANT | Transform, input/output, version, span capability, and incomplete states are covered. |
| Import ordering/failure | PROPOSED DESIGN | Capture-before-derivation and reconciliation states cover partial failure. |
| Privacy/sealed | REQUIRED INVARIANT | Separate classification/capability and fail-closed enumeration are required. |
| Recall/search/index | REQUIRED INVARIANT | Evidence is excluded from normal recall, with separate future capability only. |
| Embeddings | FUTURE / OUT OF SCOPE | No evidence embeddings in Foundation v1. |
| Storage options | PROPOSED DESIGN | SQLite registry plus CAS blobs is recommended; SQLite-only and Markdown options are compared. |
| Encryption/secrets | REQUIRED INVARIANT | Foundation controls require access boundaries, safe permissions, supported encrypted backups, and no raw content in logs; external KMS is out of scope. |
| Retention | RECOMMENDED V1 DESIGN | Policy-class finite retention with source-specific overrides; exact duration is `PRODUCT DEFAULT TBD`. |
| Delete semantics | REQUIRED INVARIANT | Evidence and derived memory have independent default lifecycles; deletion uses tombstone then policy-controlled purge. |
| Export | FUTURE / OUT OF SCOPE | Manifest/format is retained, but Foundation v1 has no export capability. |
| Backup/restore | REQUIRED INVARIANT | Registry/blob/lineage consistency, staging, verification, quarantine, and missing states are covered. |
| Dedup/retry | REQUIRED INVARIANT | Logical occurrence identity is separated from physical content deduplication. |
| Editing/correction/redaction | REQUIRED INVARIANT | No in-place content edits; revisions/tombstones and revalidation are defined. |
| Model access | REQUIRED INVARIANT | No LLM evidence access or automatic context inclusion in Foundation v1; future retrieval is separate. |
| MCP | FUTURE / OUT OF SCOPE | O4 adds none; future tools require a separate contract. |
| Dashboard | FUTURE / OUT OF SCOPE | No Raw Evidence inspection UI in Foundation v1. |
| Remember-Me | REQUIRED INVARIANT | Reference-only relationship; RM remains authoritative for RM-owned bytes. |
| `preserve_raw` | REQUIRED INVARIANT | Existing meaning remains unchanged and independent from evidence capture. |
| Memory Contract v1 | REQUIRED INVARIANT | No silent public contract or recall change. |
| Threat model | REQUIRED INVARIANT | Content leakage, integrity, deletion, path, prompt injection, quota, and ownership risks are addressed. |
| Resource limits | REQUIRED INVARIANT | Hard bounded and fail-closed; exact values are `IMPLEMENTATION DEFAULT TO BENCHMARK`. |
| Observability | REQUIRED INVARIANT | Safe status/count/repair metrics and restricted metadata-only audit records exclude raw content. |
| Migration/compatibility | REQUIRED INVARIANT | No retroactive provenance; validated historical re-import only; old binaries ignore isolated namespace. |
| Feature flag/rollback | REQUIRED INVARIANT | Default-off and ordinary-memory independence are defined. |

## 4. Entity and contract review

The adopted `EvidenceObject`, `EvidenceRevision`, `MemoryLineage`, and `ImportRun` boundaries are sufficient for a first implementation plan. The optional `SourceRecord`/`SourceRef` distinction is appropriately left as a storage decision based on source reuse. The review agrees that source identity, logical evidence identity, content hash, and run identity must not be conflated.

The fidelity section is particularly important: current import decoding and normalized chunks cannot support an `ORIGINAL_BYTES`, `SOURCE_TEXT`, or `EXACT_SPAN` claim without a new capture boundary and mapping. The design correctly makes the claim conditional on captured material and verified mapping.

The lineage state model also correctly avoids erasing history when content is redacted, a source expires, or a registry write is delayed. This is a required correctness property, not a UI preference. The adopted rule that evidence disappearance never deletes derived memory preserves both rollback and user-data safety.

## 5. Storage and lifecycle review

The recommended registry-plus-content-addressed-blob design fits the repository better than putting evidence into normal Markdown buckets. The review accepts the tradeoff that two storage surfaces require explicit staging, reconciliation, orphan cleanup, and backup verification. SQLite-only payloads remain a reasonable alternative if benchmarks show the expected source sizes are small, but that decision belongs in implementation planning.

The adopted lifecycle is coherent: finite configurable retention, immediate tombstone/restriction, policy-controlled purge, independent evidence and memory deletion, staged full-data deletion, explicit upstream deletion review, and explicit backup expiry. Legal holds are intentionally out of scope for Foundation v1, so the design does not make implementation depend on a hold workflow. Exact retention and audit periods remain configurable defaults rather than architecture blockers.

## 6. Privacy, model, and interface review

The adopted boundary is coherent: evidence is not part of normal Breath/query/semantic/dream/archive/model context and does not enter the existing embedding namespace. Foundation v1 has no evidence UI, export, LLM retrieval, search, or embeddings. Sealed records must not leak through counts, hashes, autocomplete, errors, or backup behavior. Any future capability must be separately authorized.

The review agrees that O4 should add no MCP tool, dashboard evidence UI, export capability, or model retrieval. Treating imported source text as untrusted data is required because evidence may contain prompt injection or malicious instructions. The design leaves future capability boundaries without coupling them to Foundation v1.

## 7. Adopted decisions and remaining defaults

The previously unresolved policy choices are now adopted as follows:

1. Retention uses finite configurable policy classes with source-specific overrides; the exact default is `PRODUCT DEFAULT TBD`, and backup expiry follows an explicit lifecycle.
2. Legal holds are `FUTURE / OUT OF SCOPE` for Foundation v1.
3. Evidence disappearance never automatically deletes derived memories.
4. Ordinary memory deletion does not automatically delete supporting evidence.
5. Upstream deletion requires review unless source identity and deletion authority are both trustworthy.
6. Full-data deletion uses staged tombstone-and-purge; no instantaneous physical-deletion or cryptographic-erasure promise is made.
7. Redaction immediately restricts access, then uses policy-controlled purge, without automatic memory deletion.
8. Foundation v1 has no Dashboard evidence UI, evidence export, LLM retrieval, evidence search, or embeddings.
9. Raw Evidence is hard bounded and fail-closed; numeric thresholds are `IMPLEMENTATION DEFAULT TO BENCHMARK`.
10. Foundation security uses storage/access boundaries, supported encrypted backups, and content-free logs; external KMS is future hardening.
11. Capture is explicit opt-in/default-off and independent from `preserve_raw`.
12. Historical re-import is allowed, but historical lineage requires explicit structural validation; automatic similarity matching is rejected.
13. Security-sensitive evidence operations use restricted metadata-only audit records; exact audit retention is `PRODUCT DEFAULT TBD`.

The review finds no remaining subjective product choice that genuinely requires a different Foundation v1 architecture. The remaining unresolved values are finite retention duration, audit-record retention duration, and numeric resource thresholds; the design explicitly makes them configurable or benchmark-selected.

## 8. Accidental-guarantee audit

The design was checked for the requested overclaims. The following are explicitly qualified and are not current repository guarantees:

| Risky claim | Review result |
| --- | --- |
| Evidence is immutable today | Reworded as a future application invariant with append-only revisions and integrity verification. |
| Historical original transcript exists | Reworded as fidelity-dependent; import snapshot is not automatically upstream original. |
| Exact spans exist for every import | Reworded as conditional on captured source and verified mapping. |
| Evidence is retained forever | Reworded as finite configurable retention; exact duration is `PRODUCT DEFAULT TBD`. |
| Deleting evidence auto-deletes memory | Explicitly rejected; lineage records missing/redacted/revalidation status. |
| Evidence is encrypted at rest | No live-encryption guarantee claimed; external KMS is future scope. |
| Evidence is semantically searchable | Explicitly excluded from Foundation v1. |
| Evidence MCP tools exist | Explicitly excluded from O4. |
| Dashboard evidence UI exists | Explicitly excluded from Foundation v1. |
| Evidence export exists | Explicitly excluded from Foundation v1; manifest design remains future-compatible. |
| Models recall evidence | Explicitly prohibited; future retrieval requires separate authorization. |
| Legal-hold capability exists | Explicitly excluded from Foundation v1. |
| Fixed numeric limits exist | Explicitly left to benchmark/configuration. |
| Historical provenance is automatically matched | Explicitly rejected; only validated mapping is allowed. |
| Remember-Me bytes are copied into evidence | Explicitly prohibited; references do not change ownership. |
| Memory Contract v1 changed | Explicitly excluded unless a future public surface requires amendment. |
| Raw Evidence is deployed in production | Explicitly excluded; O4 is design-only. |

No unqualified promise in this audit should be read as implementation evidence.

## 9. Validation performed

Allowed O4 validation is limited to static inspection and documentation review:

- branch/base alignment and clean start were read-only verified;
- authoritative O3 contract and closure/audit documents were inspected;
- current import, memory, server, backup, asset, Remember-Me, dashboard, auth, and relevant test boundaries were inspected without running the test suite;
- the design and review are limited to the two requested primary documents;
- Markdown path/reference and wording checks are documentation-only;
- `git diff --check` and trailing-whitespace checks apply only to the two documentation files.

No Python suite, database migration, runtime probe, production request, or deployment action is part of O4 validation.

## 10. Ending status and final recommendation

The narrowed Foundation v1 remains compatible with Memory Contract v1, preserves sealed/privacy boundaries, preserves Remember-Me ownership, preserves `preserve_raw` semantics, avoids accidental recall coupling, supports safe rollback, avoids fabricated legacy provenance, and can be implemented incrementally. The remaining numeric/default values do not require a different architecture and are explicitly configurable or benchmark-selected.

READY_FOR_IMPLEMENTATION_PLANNING

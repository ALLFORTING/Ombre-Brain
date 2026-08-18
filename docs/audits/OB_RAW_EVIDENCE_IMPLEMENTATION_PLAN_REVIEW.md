# O5 Raw Evidence Implementation Plan v1 — Independent Review

Review status: planning-only review.

Reviewed baseline: `0b2b3dac387facc05e71e66bfaa5aadd982b4b08` (`origin/main`).

Reviewed planning artifacts:

- `docs/plans/OB_RAW_EVIDENCE_IMPLEMENTATION_PLAN_v1.md`
- `docs/design/OB_RAW_EVIDENCE_DESIGN_v1.md`
- `docs/audits/OB_RAW_EVIDENCE_DESIGN_REVIEW.md`
- `docs/OB_MEMORY_LAYER_CONTRACT_v1.md`
- `docs/audits/OB_MEMORY_LAYER_CONTRACT_V1_CLOSURE_REVIEW.md`

Reviewed repository evidence by static inspection:

- `import_memory.py`, `bucket_manager.py`, `server.py`, and `utils.py`;
- `asset_store.py`, `asset_storage_layout.py`, `asset_backend.py`, and Remember-Me adapters;
- `backup_export.py`, `offline_backup_bundle.py`, `production_backup_capture.py`;
- `maintenance_write_gate.py`, `maintenance_write_coverage.py`, and existing schema/version patterns;
- `tests/conftest.py`, import security-gate tests, backup/persistence tests, RM boundary tests, and production-write coverage tests.

No runtime code, test, schema, migration, configuration, dependency, production-data, commit, push, or PR operation is part of the planning artifacts.

Classifications used below:

- `CURRENT REPOSITORY FACT` — supported by inspected repository evidence;
- `PLANNED IMPLEMENTATION` — proposed future change, not current code;
- `REQUIRED INVARIANT` — must remain true across implementation;
- `DEFERRED` — explicitly outside O5A–O5E foundation;
- `NEEDS IMPLEMENTATION VALIDATION` — requires future tests, deployment checks, or benchmark evidence.

## 1. Authority and O4 conformity

The plan conforms to the adopted O4 design:

- Source, Evidence, Derived Memory, Lineage, and Import/Processing Run remain distinct.
- Foundation uses the recommended SQLite registry plus content-addressed filesystem blobs.
- Evidence uses explicit fidelity and integrity metadata rather than the existing `preserve_raw` flag.
- Evidence is not ordinary memory and is excluded from automatic recall, model context, search, and embeddings.
- Evidence and derived memory have independent default lifecycles.
- Legal holds, Dashboard UI, export, LLM retrieval, search, embeddings, and KMS remain deferred.
- Remember-Me remains authoritative for RM image blobs.
- Historical provenance requires validated mapping and cannot be inferred by similarity.

Assessment: `REQUIRED INVARIANT` conformity is complete.

## 2. Current repository/file mapping

| Planning statement | Classification | Review assessment |
| --- | --- | --- |
| Server startup initializes ordinary stores eagerly | CURRENT REPOSITORY FACT | O5A correctly avoids `server.py` startup wiring. |
| Import decodes before current extraction | CURRENT REPOSITORY FACT | O5B correctly places evidence capture before lossy decoding. |
| `preserve_raw` is post-extraction memory behavior | CURRENT REPOSITORY FACT | The plan preserves it as an independent dimension. |
| Bucket files/history are ordinary memory storage | CURRENT REPOSITORY FACT | The plan rejects them for Raw Evidence. |
| General backup recursively covers `buckets_dir` | CURRENT REPOSITORY FACT | The plan keeps O5A storage outside that root and defers backup wiring to O5E. |
| SQLite/write-gate/atomic/path patterns exist | CURRENT REPOSITORY FACT | The plan reuses patterns conceptually without reusing RM/asset authority. |
| `raw_evidence_store.py` is a suitable minimal module | PLANNED IMPLEMENTATION | One module avoids premature package structure; implementation must validate cohesion. |
| `maintenance_write_coverage.py` must register new writers | REQUIRED INVARIANT | Existing AST coverage would otherwise detect an unregistered production writer. |

No plan item depends on an imaginary existing API. Proposed O5B/O5C modules are explicitly marked future names.

## 3. O5A minimality review

O5A is deliberately limited to an isolated registry/CAS store, integrity verification, path safety, bounded writes, immutable content boundaries, and tests/coverage registration.

The plan excludes import integration, server startup, memory creation/update/delete, recall, Dashboard, MCP, backup, migration, RM, and public APIs. This satisfies the critical O5A constraint.

The proposed O5A file list is minimal:

1. `raw_evidence_store.py` — required runtime boundary.
2. `tests/test_raw_evidence_store.py` — required focused coverage.
3. `maintenance_write_coverage.py` — required production-write registration.

The review finds no need for `raw_evidence_models.py`, `raw_evidence_paths.py`, configuration changes, or a new package in O5A.

Assessment: `REQUIRED INVARIANT` minimality is satisfied, subject to implementation review if the single runtime module becomes materially incohesive.

## 4. Persistence, schema, and Windows compatibility

The plan maps O5A to current repository patterns without reusing current memory or RM databases. A dedicated registry and CAS root provide clean ownership and avoid exposing raw content through the existing recursive bucket backup.

The proposed O5A schema-version singleton and fail-closed unknown-version rule are compatible with current versioned SQLite state patterns. O5A creates a new schema version 1 only; no migration of existing databases is required. Future migrations must be separate reviewed work.

The plan correctly identifies Windows risks: absolute-root validation, symlink/reparse rejection, case-insensitive path behavior, private temporary files, no-replace publication, cleanup after `os.replace`/fsync failures, and isolated `tmp_path` tests. Exact host permission behavior remains `NEEDS IMPLEMENTATION VALIDATION`.

The plan does not claim that POSIX `chmod` alone proves Windows privacy. Deployment-specific ACL verification remains required before production capture.

## 5. O5A storage and integrity review

The proposed ordering—bounded staged write, hash/size verification, fsync, no-replace CAS publication, then SQLite metadata commit—makes a metadata row unable to reference unverified content. A crash can leave an unreferenced inert blob, which O5E can detect.

The plan distinguishes:

- logical evidence identity from physical content hash;
- immutable content fields from mutable operational state;
- ordinary memory storage from evidence storage;
- content corruption from duplicate content;
- quarantine/orphan state from available evidence.

The review accepts that O5A does not provide cross-store transactions. The explicit reconciliation requirement is sufficient for planning, but crash and fsync behavior are `NEEDS IMPLEMENTATION VALIDATION`.

## 6. Failure atomicity, rollback, and feature gating

The plan gives ordinary memory no dependency on evidence availability. Disabled open is inert, server startup is untouched, and store failures return stable errors without changing bucket behavior.

The rollback model is sound:

- no import or recall dependency exists in O5A;
- an unused Raw Evidence root may remain inert;
- older binaries ignore the dedicated root;
- unsupported evidence schema fails closed without blocking ordinary memory;
- no existing memory deletion is needed for rollback.

The review identifies one future validation point: O5B’s deployment path must prove that the chosen production evidence root remains outside ordinary backup and RM roots under real configuration. This is not an O5A blocker because O5A receives an explicit isolated root and adds no production wiring.

## 7. O5A security/privacy review

The plan preserves adopted sealed/privacy boundaries by excluding user-facing access entirely in O5A and by requiring non-enumerating metadata behavior inside the store.

Required security properties are present:

- no raw content in logs/errors/metrics;
- no arbitrary local path references;
- no path traversal or reparse escape;
- no content overwrite;
- no hidden sealed counts/hashes through default store enumeration;
- no Dashboard/MCP/model access;
- production-write coverage for every persistence boundary.

The absence of external KMS is consistent with adopted O4 policy. The plan does not claim live at-rest encryption and requires supported encrypted backups only in later operational scope.

Assessment: `REQUIRED INVARIANT` security boundaries are complete; permissions and fault-injection behavior are `NEEDS IMPLEMENTATION VALIDATION`.

## 8. O5A testability and merge gate

The test matrix covers the requested behaviors: identity, fidelity, hashing, corruption, logical/physical deduplication, append-only content, path safety, limits, unwritable storage, crash boundaries, sealed non-enumeration, feature-disabled baseline, no recall interaction, RM ownership, and production-write coverage.

The proposed gate is proportionate:

- focused store/security tests;
- relevant import security tests without changing import code;
- production-write coverage;
- full offline suite;
- AST/static validation;
- diff/whitespace/sensitive-content scans;
- no paid providers, external model generation, or production data.

The plan explicitly says not to write tests during O5 planning. This is correct.

## 9. O5B–O5E stage review

| Stage | Classification | Review assessment |
| --- | --- | --- |
| O5B opt-in capture | PLANNED IMPLEMENTATION | Correctly begins at the raw upload boundary, preserves fidelity, adds run/idempotency, and remains recall-independent. |
| O5C lineage | PLANNED IMPLEMENTATION | Correctly identifies import, merge/dehydrate, user-edit, and historical re-import boundaries; does not reuse bucket history or permit similarity attribution. |
| O5D lifecycle | PLANNED IMPLEMENTATION | Correctly separates tombstone/restriction from purge and evidence/memory deletion; legal holds remain out of scope. |
| O5E operations | PLANNED IMPLEMENTATION | Correctly owns backup/restore, orphan/corruption checks, audit, quotas, and safe diagnostics without becoming export or retrieval. |
| Dashboard/export/LLM/search/embedding/MCP/KMS/legal hold | DEFERRED | Explicitly excluded from the required foundation path. |

The stages remain small and sequential. No stage collapses later capabilities into O5A.

## 10. Cross-stage invariants and dependencies

The dependency graph `O5A -> O5B -> O5C -> O5D -> O5E` is appropriate. O5B depends on verified storage, O5C depends on run/evidence identity, O5D depends on lineage/reference status, and O5E depends on all lifecycle states.

Documentation and static analysis may be parallelized, but implementation stages should not be parallelized across these dependencies.

The invariant table is complete for the adopted design: default-off, ordinary-memory independence, no recall/model coupling, unchanged `preserve_raw`, independent deletion, sealed privacy, no fabricated legacy provenance, RM ownership, and no unauthorized production migration.

## 11. Migration and compatibility realism

The plan performs no migration of historical memories. Existing memories remain explicitly without evidence. Historical source re-import is allowed only after O5B/O5C and only with structural validation.

The plan’s older-binary behavior is realistic because the proposed evidence root is separate and no current server startup path is modified in O5A. Unknown Raw Evidence schema is fail-closed for the feature, not a reason to block ordinary memory.

Assessment: `REQUIRED INVARIANT` compatibility is satisfied; multi-version deployment behavior remains `NEEDS IMPLEMENTATION VALIDATION` during O5A/O5B rollout testing.

## 12. Accidental scope-creep and guarantee audit

The planning documents do not claim:

- Raw Evidence exists today;
- existing memories have provenance;
- O5A captures imports;
- fixed retention or quota numbers;
- instant deletion or cryptographic erasure;
- legal holds;
- Dashboard evidence UI;
- export capability;
- LLM or MCP evidence access;
- search or embeddings;
- external-KMS encryption;
- RM image duplication;
- production deployment.

The documents explicitly state that O5A is isolated and that only O5A may begin after this review. O5B–O5E are plans, not implementation authorization.

## 13. Implementation stop-condition review

The required stop conditions are present and sufficient. In particular, implementation must stop for any unexpected dependency on ordinary recall, `preserve_raw`, RM ownership, sealed enumeration, historical migration, production data, ambiguous deletion, unregistered writes, or deferred capabilities.

## 14. Remaining implementation validation

No subjective product decision remains that changes the O5A architecture. The following are expected implementation/benchmark checks, not planning blockers:

- Windows ACL/private-root behavior;
- exact SQLite locking and fsync semantics on supported deployment filesystems;
- benchmark-selected resource limits;
- crash injection around CAS/SQLite commit boundaries;
- production-write AST registration;
- older-binary coexistence with an O5A-created root;
- backup-root separation under deployment configuration during O5B/O5E.

## 15. Planning recommendation

The plan is sufficiently concrete to begin O5A under a separate explicit implementation instruction. This recommendation authorizes O5A planning-to-implementation transition only; it does not authorize O5B, O5C, O5D, or O5E implementation, and it does not authorize any deferred capability.

READY_TO_START_O5A

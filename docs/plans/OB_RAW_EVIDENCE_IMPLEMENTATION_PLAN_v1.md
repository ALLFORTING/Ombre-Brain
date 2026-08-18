# Ombre-Brain Raw Evidence Implementation Plan v1

Status: implementation planning only. No Raw Evidence implementation is included in this plan.

Planning baseline: `0b2b3dac387facc05e71e66bfaa5aadd982b4b08` (`origin/main`).

Planning branch: `plan/raw-evidence-implementation-v1`.

Authority:

- adopted Raw Evidence design: `docs/design/OB_RAW_EVIDENCE_DESIGN_v1.md`;
- independent O4 review: `docs/audits/OB_RAW_EVIDENCE_DESIGN_REVIEW.md`;
- adopted Memory Contract v1: `docs/OB_MEMORY_LAYER_CONTRACT_v1.md`;
- Memory Contract v1 closure evidence: `docs/audits/OB_MEMORY_LAYER_CONTRACT_V1_CLOSURE_REVIEW.md`.

Every implementation statement below is `PLANNED IMPLEMENTATION`, `REQUIRED INVARIANT`, `CURRENT REPOSITORY FACT`, `DEFERRED`, or `NEEDS IMPLEMENTATION VALIDATION`. Proposed module names and configuration names are not current repository contracts.

## 1. Planning goal and authorization boundary

O5 converts the adopted O4 architecture into small, reversible implementation stages. It does not authorize implementation of all stages.

The only stage that may begin after this plan is independently reviewed is O5A. O5B, O5C, O5D, and O5E remain plans until their own implementation instructions, reviews, and merge gates are approved.

O5 must not modify or imply a change to:

- current memory creation, recall, Breath, Dream, archive/session, bucket scoring, or decay;
- current `preserve_raw` semantics;
- the public MCP surface;
- Dashboard behavior;
- Remember-Me ownership or asset cutover;
- production data, existing memory files, or existing databases;
- Memory Contract v1 without a separately adopted contract change.

## 2. Current repository facts used by the plan

### Import and memory

`CURRENT REPOSITORY FACT`: `server.py` eagerly loads configuration and initializes `AssetStore`, embedding, `BucketManager`, `Dehydrator`, `DecayEngine`, and `ImportEngine` at module import. O5A must not add Raw Evidence initialization to that path.

`CURRENT REPOSITORY FACT`: `/api/import/upload` reads the uploaded body, decodes it as UTF-8 with replacement, then passes decoded text, filename, `preserve_raw`, and `resume` to `ImportEngine.start()`.

`CURRENT REPOSITORY FACT`: `ImportEngine` calculates a short source hash, parses and chunks normalized turns, persists progress in `buckets/import_state.json`, calls model extraction, and creates or merges Markdown bucket memories. `preserve_raw` preserves selected post-extraction content and skips the secondary merge/dehydration path; it is not source evidence.

`CURRENT REPOSITORY FACT`: `BucketManager` stores ordinary memories as Markdown files under `permanent`, `dynamic`, `archive`, and `feel`, and uses `bucket_history.sqlite3` for write-ahead memory snapshots. `bucket_history` is not a provenance ledger and must not be reused as Raw Evidence storage.

### Persistence and paths

`CURRENT REPOSITORY FACT`: current persistence uses SQLite files with `BEGIN IMMEDIATE`, foreign keys where applicable, explicit initialization, and in-place schema changes in some existing stores. The Remember-Me/cutover state code also has versioned singleton metadata, fail-closed unknown-version handling, and path/reparse-point validation.

`CURRENT REPOSITORY FACT`: current atomic-write patterns use a temporary file in the target parent, private permissions where supported, `fsync`, `os.replace`, and cleanup on failure. `offline_backup_bundle.py`, `server.py`, `asset_store.py`, and the cutover modules provide relevant patterns.

`CURRENT REPOSITORY FACT`: `utils.load_config()` creates ordinary bucket subdirectories during configuration load. It does not currently define a Raw Evidence root or feature flag.

`CURRENT REPOSITORY FACT`: `backup_export.py` recursively serializes files below the configured `buckets_dir`. A Raw Evidence root placed there would be exposed by the existing general backup route unless that route is deliberately changed in a later stage. O5A therefore must not place production Raw Evidence under the current ordinary bucket backup tree.

### Security and write boundaries

`CURRENT REPOSITORY FACT`: Dashboard writes use cookie sessions, same-origin/CSRF checks, and maintenance write gates. MCP authentication is separate. Dashboard authentication is not sealed-evidence authorization.

`CURRENT REPOSITORY FACT`: `maintenance_write_coverage.py` statically registers production persistence boundaries and `tests/test_stage8h_g1c_quiesced_capture.py` asserts that the registration is complete. A future Raw Evidence writer must be registered and covered.

### Backup and Remember-Me

`CURRENT REPOSITORY FACT`: offline and production backup modules provide bounded staging, SHA-256 verification, no-replace publication, SQLite snapshot inspection, safe path/reparse validation, and encrypted-backup patterns. They do not currently define a Raw Evidence backup category.

`CURRENT REPOSITORY FACT`: Remember-Me has a separate authority/backend boundary. Raw Evidence must never copy Remember-Me-owned image bytes or use the RM store as a blob authority.

## 3. O5 foundation scope

The planned foundation sequence is:

| Stage | Authorized scope |
| --- | --- |
| O5A | Isolated Raw Evidence registry/blob storage and integrity foundation only |
| O5B | Explicit opt-in import capture, source snapshot, run identity, and retry/resume |
| O5C | Evidence-to-memory lineage, transformation records, and validated provenance |
| O5D | Finite retention, expiry, tombstone, redaction, deletion, purge, and independent memory lifecycle |
| O5E | Backup/restore consistency, corruption/orphan audits, metadata-only security audit, quotas, and safe operations |

Foundation behavior is default-off. Raw Evidence is not ordinary memory and is never automatically included in recall or model context.

## 4. Permanent cross-stage boundaries

These are `REQUIRED INVARIANT` after every stage:

1. Raw Evidence remains default-off until explicitly enabled by an adopted integration policy.
2. Ordinary memory works when Raw Evidence is disabled, absent, unavailable, corrupt, or rolled back.
3. Raw Evidence never automatically participates in Breath, ordinary recall, Dream, resonance, no-query surfacing, or model context.
4. `preserve_raw` remains post-extraction memory preservation and secondary-dehydration bypass; it is independent from evidence capture.
5. Evidence content and derived memory content remain separate objects with separate lifecycle policy.
6. Evidence deletion, expiry, redaction, or loss never automatically deletes derived memory.
7. Ordinary memory deletion never automatically deletes Raw Evidence.
8. Sealed evidence is not enumerable through default metadata, counts, hashes, errors, diagnostics, or future indexes.
9. Historical memories do not receive provenance without explicit structural validation.
10. Remember-Me remains authoritative for Remember-Me image blobs; Raw Evidence never becomes a second RM asset authority.
11. No production-data migration occurs without an explicitly authorized later stage.
12. No future capability is smuggled into “observability”; Dashboard UI, export, LLM retrieval, MCP retrieval, search, embeddings, legal holds, and external KMS remain deferred.

## 5. O5A — storage and integrity foundation

### 5.1 O5A objective and non-goals

O5A establishes a future internal store that can create, read, verify, and safely quarantine evidence metadata/content in isolated test or explicitly enabled workspaces.

O5A must not:

- import or capture any real conversation;
- modify `import_memory.py`, `server.py`, `BucketManager`, `Dehydrator`, recall, Breath, Dream, archive/session, or Dashboard;
- create a lineage edge, import-run record, memory reference, or production migration;
- expose an MCP tool, REST endpoint, Dashboard surface, or backup export route;
- initialize a Raw Evidence store during ordinary server startup;
- use `bucket_history.sqlite3`, `assets.sqlite3`, `embeddings.db`, Remember-Me storage, or ordinary Markdown buckets;
- change `preserve_raw`, RM routing, or current production memory lifecycle.

The strongest O5A baseline is: a normal server process does not import or construct the store, and the feature-disabled path is byte-for-byte behaviorally equivalent to the current baseline except for ordinary planning/build artifacts.

### 5.2 Proposed internal module boundary

`PLANNED IMPLEMENTATION`: add one small runtime module:

`raw_evidence_store.py`

Responsibilities:

- immutable logical evidence/revision registration;
- dedicated SQLite registry initialization and version verification;
- bounded staged content writes;
- SHA-256 hashing and read-time verification;
- content-addressed blob publication without overwrite;
- metadata-only state/classification updates allowed by O5A;
- path, root, filename, reparse-point, and permission checks;
- stable redacted error classes;
- explicit disabled/no-op opening behavior without filesystem side effects.

Do not add `raw_evidence_models.py` or a package hierarchy in O5A. Small immutable records/dataclasses may live inside `raw_evidence_store.py`. Split the module only if implementation review demonstrates a real boundary rather than anticipating one.

O5A must not import `server.py`, network clients, model clients, asset backends, Remember-Me adapters, or Dashboard modules. Importing `raw_evidence_store.py` must have no network, server, directory-creation, or database side effect.

### 5.3 Persistent storage boundary

`RECOMMENDED V1 DESIGN`: use a dedicated absolute `evidence_root` supplied to the store constructor by a future integration layer. O5A does not add a production config key or environment variable and does not choose a deployment-specific path.

The intended production shape for later integration is a dedicated root outside the ordinary configured `buckets_dir` and outside the Remember-Me root, for example a deployment-owned sibling data root. The exact production path and configuration name remain `IMPLEMENTATION DETAIL` for the O5B deployment integration.

Within an explicitly supplied root, the planned layout is:

```text
<evidence_root>/
  registry.sqlite3
  blobs/sha256/<prefix>/<digest>
  .tmp/
  .quarantine/
```

The exact two-level prefix and filename layout are implementation details. The store must own all paths below `evidence_root`; source filenames, source IDs, upstream paths, and user strings must never become blob paths.

Metadata storage:

- `registry.sqlite3` owned only by `RawEvidenceStore`;
- SQLite connection configured with foreign keys and a bounded busy timeout;
- initialization uses a transaction and a singleton schema/version record;
- unknown or unsupported schema versions fail closed;
- O5A creates only schema version 1 in a new dedicated registry;
- no in-place migration of an existing repository database is part of O5A.

Content storage:

- staged in `.tmp` below the owned root;
- hashed while written or streamed;
- published to a content-addressed destination only after size/hash validation;
- published destination is never overwritten;
- failed temporary content is removed; an unreferenced published blob is inert and later detectable by O5E orphan audit.

Writable-directory behavior:

- O5A accepts an explicit test/isolated root and creates only that root’s owned subdirectories when the store is explicitly opened enabled;
- disabled opening returns without creating a directory, file, SQLite database, or log containing a path;
- a root that is absent but valid may be created by explicit store initialization;
- a root that is unreadable, unwritable, a file, a symlink/reparse path, or overlaps a forbidden ordinary/RM root fails with a stable storage error;
- ordinary memory remains unaffected because O5A is not on the server startup path.

Backup inclusion:

- O5A performs no backup wiring;
- the dedicated root must not fall under the existing recursive `backup_export.py` bucket root;
- O5E later adds a separately classified, policy-aware backup/restore category;
- no O5A change may make raw content appear in the existing ordinary backup JSON.

Temporary/test isolation:

- tests pass `tmp_path / "raw-evidence"` explicitly;
- tests do not use the repository `buckets` directory or production data;
- test fixtures can construct ordinary bucket storage and Raw Evidence storage as separate siblings;
- tests assert that disabled store construction leaves no path behind.

### 5.4 Minimal O5A data model

O5A needs only the following logical entities. Field names are proposed implementation names, not public API.

#### `evidence_objects`

| Field | Classification | O5A purpose |
| --- | --- | --- |
| `evidence_id` | Required in O5A | Ombre-Brain logical identity for one source occurrence; generated locally and never presented as an upstream ID |
| `source_system` | Required in O5A | Qualified origin namespace, such as an importer/source family |
| `source_kind` | Required in O5A | Conversation, document, file, item, or other source class |
| `source_scope` | Required in O5A | Conversation/account/tenant scope; empty is not a global identifier |
| `upstream_source_id` | Required in O5A, nullable | Upstream source ID when supplied; nullable when absent |
| `upstream_item_id` | Required in O5A, nullable | Upstream item/message/record ID when supplied |
| `source_occurrence_key` | Required in O5A, nullable | Local occurrence key when upstream identity is unavailable |
| `identity_origin` | Required in O5A | `upstream`, `local`, or `unknown`; prevents local IDs becoming false upstream facts |
| `privacy_class` | Required in O5A | `ordinary`, `sealed`, or `restricted_admin`; future redaction/tombstone lifecycle remains separate |
| `lifecycle_state` | Required in O5A | Minimum states: `captured`, `available`, `quarantined`, `integrity_failed`, `tombstoned` |
| `captured_at` | Required in O5A | Ombre-Brain capture time |
| `created_at` / `updated_at` | Required in O5A | Registry lifecycle timestamps |
| `record_schema_version` | Required in O5A | Per-record compatibility marker if needed by the store |

#### `evidence_revisions`

| Field | Classification | O5A purpose |
| --- | --- | --- |
| `revision_id` | Required in O5A | Immutable content-revision identity |
| `evidence_id` | Required in O5A | Foreign-key relation to the logical evidence object |
| `fidelity_level` | Required in O5A | One adopted O4 level, including `IMPORT_SNAPSHOT` or `ORIGINAL_BYTES` only when justified by the caller |
| `media_type` | Required in O5A | Content interpretation boundary |
| `hash_algorithm` | Required in O5A | Versioned identifier such as `sha256-v1` |
| `content_hash` | Required in O5A | Full lowercase digest of canonical stored bytes |
| `content_size_bytes` | Required in O5A | Integrity and resource-bound check |
| `blob_relpath` | Required in O5A | Store-generated relative path only |
| `created_at` | Required in O5A | Immutable revision creation time |
| `verification_state` | Required in O5A | Operational state such as `verified`, `quarantined`, or `failed` |
| `revision_schema_version` | Required in O5A | Content metadata compatibility marker |

#### `store_schema`

| Field | Classification | O5A purpose |
| --- | --- | --- |
| singleton key | Required in O5A | Ensures one schema record |
| `schema_version` | Required in O5A | Exact supported registry version; unknown versions fail closed |
| `created_at` / `updated_at` | Implementation detail | Schema audit metadata |

The following remain out of O5A:

- `import_run_id`, parser/importer version, retry key, and run status: O5B;
- `memory_id`, transform type, model/extractor version, spans, and lineage state: O5C;
- parent revision/correction reason, retention policy, purge audit, and redaction reason: O5D;
- backup manifest, orphan reports, access audit, and repair status: O5E;
- model, MCP, Dashboard, export, index, and embedding fields: deferred capabilities.

### 5.5 Integrity model

`REQUIRED INVARIANT`: O5A uses SHA-256 with an explicit algorithm/version field. Hashes are integrity identifiers, not authorization credentials and not default-visible metadata.

Hashing sequence:

1. validate the explicit root, limits, media metadata, and caller-supplied identity fields;
2. stream bounded bytes into a private temporary file while calculating size and SHA-256;
3. reject content exceeding the configured bound before formal publication;
4. flush and verify the temporary file;
5. publish to the CAS destination without replacing an existing file;
6. if the destination already exists, hash it and require an exact size/hash match;
7. insert evidence metadata only after the content is verified and available;
8. commit SQLite metadata in a transaction;
9. return only safe IDs/state, never content or unrestricted paths.

Same-content/different-source behavior:

- each source occurrence gets a distinct `evidence_id` and `revision_id`;
- physical bytes may share one CAS blob by content hash;
- deduplication never merges source identity or privacy classification;
- a hash collision or existing-path mismatch is an integrity failure, not a deduplication success;
- a retry with the same logical identity may be idempotent, but O5A itself does not invent a retry key or import-run relation.

Read verification:

- `get_content(revision_id)` resolves only a store-owned relative path;
- the file must be regular, within the root, and free of symlink/reparse escape;
- size and SHA-256 are rechecked before bytes are returned to an authorized internal caller;
- mismatch changes the object/revision to a quarantined/integrity-failed state and fails closed;
- the exception contains a stable code such as `integrity_failed`, not a path or content excerpt.

### 5.6 Append-only and content immutability

The store separates:

- immutable content identity: revision ID, hash algorithm, hash, size, media type, fidelity, and blob reference;
- mutable operational metadata: privacy classification, lifecycle/verification state, timestamps, and future policy markers;
- future lifecycle operations: tombstone, redaction, purge, correction, and retention transitions.

O5A must expose no `update_content` operation. New content is a new revision inserted through a create-only path; O5A does not yet expose correction workflows.

The registry should use database constraints/triggers or an equivalent repository-level invariant so ordinary metadata update code cannot rewrite the immutable revision columns. The implementation must test direct SQL mutation attempts as well as public method misuse. Physical CAS files are write-once from the application perspective; no replace-in-place operation is permitted.

### 5.7 Resource bounds

O5A accepts a limits object or constructor settings with bounded values supplied by the future integration/tests. Exact production numbers are `IMPLEMENTATION DEFAULT TO BENCHMARK`.

Bounds include:

- maximum individual evidence bytes;
- maximum metadata/source-identity field lengths;
- maximum temporary/staging bytes;
- maximum operation time or retry count where the host can enforce it;
- maximum store quota or available-space safety margin.

Enforcement occurs before allocation where possible, while streaming, before CAS publication, and before metadata commit. Limit failures use a stable `limit_exceeded` class, leave no referenced partial record, remove temporary files, and do not retry indefinitely. O5A must not attempt to solve large export limits; export is deferred.

### 5.8 Path safety

The store must:

- require an absolute, explicitly authorized root;
- reject the root if it is a symlink/reparse path or contains a symlink/reparse component;
- reject overlap with the ordinary bucket root, its owned descendants, the Remember-Me root, or any other caller-declared forbidden root;
- construct every blob path from validated lowercase hexadecimal hash components;
- reject path separators, NULs, drive prefixes, UNC paths, absolute paths, and traversal segments in any external string that is not an identity field;
- treat source filenames and upstream paths as metadata only;
- verify containment and node type before reading or publishing;
- avoid following symlinks/junctions on Windows and fail closed when inspection is inconclusive;
- reject destination collisions where an existing file does not match the expected hash/size;
- keep temporary and quarantine paths below the owned root.

The store owns content paths. No caller may provide a trusted local blob path as a substitute for a store reference.

### 5.9 Failure semantics

| Failure | O5A result |
| --- | --- |
| SQLite open/init/schema write fails | Return `storage_unavailable`/`schema_unsupported`; no ordinary memory impact; no partial formal record |
| Blob temp write fails | Remove temp content, return `content_write_failed`, no metadata row |
| Hash/size verification fails | Quarantine or remove the temp content; return `integrity_failed`, no available evidence |
| CAS destination exists with matching hash | Reuse physical blob, insert a distinct logical object/revision, mark deduplicated internally |
| CAS destination exists with mismatch | Return `integrity_conflict`; never overwrite |
| SQLite metadata insert/commit fails after CAS publish | Roll back metadata; leave an inert unreferenced CAS blob for O5E orphan detection or safely remove it when ownership is certain |
| Crash after CAS publish before metadata commit | No visible evidence object; O5E detects the unreferenced blob |
| Crash after metadata commit | Metadata points only to already published verified content; read verification remains mandatory |
| Root becomes unwritable | Store operation fails closed; ordinary memory remains operational |
| Startup/explicit initialization fails | Disabled/unavailable store; no server startup dependency in O5A |
| Partial evidence row exists | Reconciliation/quarantine path; never return it as available without verified blob |

The plan intentionally does not claim cross-store atomicity. It uses verified-before-reference publication plus explicit orphan/quarantine handling.

### 5.10 Feature gate

O5A does not add a server or importer feature flag. The store boundary itself has an explicit disabled/default-off open path:

- disabled open returns a no-op/disabled handle and performs no filesystem or SQLite work;
- enabled open is possible only through an explicit constructor/factory call in a future internal integration or isolated test;
- `server.py` does not construct the store in O5A;
- no O5A configuration key or environment variable is added;
- O5B owns the eventual import-capture flag and configuration naming, with exact names treated as a future implementation detail.

This avoids changing startup behavior and guarantees that enabling the future store cannot silently change import behavior during O5A.

### 5.11 Security boundary

O5A security requirements:

- private root/database/blob permissions where the host supports them;
- fail closed if the explicitly enabled root cannot meet required access boundaries;
- no raw content, source excerpts, hashes, or paths in logs, metrics, traces, exception strings, or test diagnostics;
- stable redacted error codes for callers;
- sealed/restricted records excluded from default enumeration and count APIs even though O5A has no user-facing API;
- no Dashboard, MCP, model, or backup access;
- an internal audit hook interface may accept event type and safe IDs, but O5A must not create a public audit surface;
- maintenance write coordinator/gate coverage for every production persistence boundary;
- no import of `server.py` or network-capable modules.

## 6. O5A exact future file-scope proposal

This is the minimal expected O5A implementation scope. These files are not created or changed by O5 planning.

| Path | Type | Reason necessary in O5A |
| --- | --- | --- |
| `raw_evidence_store.py` | New runtime module | Isolated registry/CAS store, integrity, path safety, immutable revision writes, limits, disabled open, and redacted errors |
| `tests/test_raw_evidence_store.py` | New test module | Unit, persistence, path/security, integrity, failure, disabled-baseline, and no-recall tests using isolated temporary roots |
| `maintenance_write_coverage.py` | Existing validation support | Register the new store’s SQLite/file write boundaries so the existing production-write AST audit cannot silently miss them |

No O5A change is planned for `utils.py`, `config.example.yaml`, `server.py`, `import_memory.py`, `bucket_manager.py`, `asset_storage_layout.py`, `asset_store.py`, `backup_export.py`, `offline_backup_bundle.py`, any Remember-Me module, or any public contract.

### DO NOT TOUCH IN O5A

- `import_memory.py` and all import routes;
- `server.py` and server startup wiring;
- bucket recall/composition/scoring/decay code;
- `BucketManager` memory CRUD and history schema;
- MCP registration, schemas, prompts, and public contracts;
- Dashboard HTML, routes, auth behavior, and diagnostics;
- Remember-Me adapters, authority, cutover, blobs, and asset routing;
- `backup_export.py` and backup endpoint behavior;
- production bucket files, existing SQLite databases, and configuration files;
- schema migration scripts for existing stores;
- dependencies and requirements.

If implementation discovers that a listed file must change to satisfy an O5A invariant, stop and revise the O5A plan/review before coding.

## 7. O5A test plan

No tests are written in O5 planning. The future O5A test module should use `tmp_path`, explicit separate ordinary/RM-forbidden roots, no network, no paid provider, and no production data.

### Unit and integration matrix

| Test area | Future test intent |
| --- | --- |
| Disabled open | Store remains inert; no root/database/blob is created; ordinary memory fixture remains usable |
| Create object/revision | Metadata and verified content persist under an isolated root |
| Stable evidence identity | Caller-provided local identity is retained; generated evidence/revision IDs remain stable after reload |
| Source identity scope | Same upstream item ID in different source scopes remains distinct; local identity is not reported as upstream |
| Fidelity | Accepted O4 levels persist; invalid/unsupported level fails closed; no upgrade is inferred by normalization |
| SHA-256 | Stored algorithm/hash/size match bytes; full digest is used, not the current import short hash |
| Read verification | Valid content reads; changed bytes or changed size raises `integrity_failed` and becomes unavailable |
| Corrupted content | Corrupt CAS content is quarantined/not served; error has no path/content leak |
| Same content/different source | Two logical evidence objects point to one physical blob only when content matches; source/privacy identity remains distinct |
| CAS collision | Existing mismatched destination is never overwritten |
| Append-only content | Public update cannot alter content identity; direct SQL mutation of immutable revision columns is rejected |
| Metadata lifecycle | Allowed metadata/state changes do not rewrite content or blob; disallowed fields fail closed |
| Path traversal | `..`, separators, absolute paths, UNC/drive paths, NULs, and source filenames cannot control blob location |
| Symlink/reparse | Root, intermediate, temp, and blob paths reject symlink/junction/reparse escapes where supported; unsupported symlink tests skip only with an explicit reason |
| Resource bounds | Oversized bytes, metadata, and quota conditions fail before formal publication and clean temporary data |
| Unwritable storage | Store returns a stable error and ordinary memory fixture remains operational |
| Partial-write recovery | Injected temp-write, CAS-publish, SQLite-commit, and fsync failures leave no available partial record |
| Crash boundary | Simulated crash after CAS/before metadata leaves an inert orphan; crash after metadata leaves verifiable content |
| Privacy/sealed | Default list/count APIs do not enumerate sealed/restricted records or reveal hashes/content |
| Import isolation | No `server`/`ImportEngine` import or behavior change; feature-disabled baseline remains identical |
| Recall isolation | Construct ordinary BucketManager and verify O5A objects are not candidates, searched, touched, or injected |
| Remember-Me ownership | Forbidden RM root overlap is rejected; no RM adapter/store is imported or written |

### Security-gate tests

The future O5A tests should be marked `security` where they cover:

- root ownership and reparse/path escape;
- no raw content in errors/log capture;
- sealed non-enumeration;
- immutable content enforcement;
- unwritable/partial-write fail-closed behavior;
- no public endpoint or server import side effect.

### Production-write coverage

The future `maintenance_write_coverage.py` registration must cover every `raw_evidence_store.py` function that creates directories, writes temporary files, replaces/publishes CAS content, executes SQLite DDL/DML, removes/quarantines files, or performs cleanup. The existing production-write coverage test must remain green. No bare write primitive may be added without a registered boundary or explicit isolated/test-only classification.

### Backup expectation

O5A has no backup wiring. Its tests must prove the store root is not a descendant of the ordinary `buckets_dir` when an ordinary root is supplied for validation. Backup inclusion/restore tests belong to O5E.

## 8. O5A validation gate

Before an O5A PR may merge, the future implementation must pass:

1. focused store tests, including integrity, path, failure, disabled, sealed, and immutability cases;
2. relevant persistence/security tests and the import security-gate tests to prove no import route changed;
3. production-write coverage scan and its registered-coverage test;
4. full offline suite with external/paid/provider tests excluded according to repository policy;
5. AST/static validation for changed Python files;
6. `git diff --check`;
7. changed-file trailing-whitespace scan;
8. sensitive path/content scan proving no raw evidence appears in logs, diagnostics, fixtures, snapshots, or ordinary backup payloads;
9. exact changed-file scope review against the O5A proposal;
10. no production-data access, no external model generation, and no paid provider use.

O5A stop conditions:

- any change to import, recall, Breath, Dream, Dashboard, MCP, RM, `preserve_raw`, or existing memory lifecycle;
- any ordinary startup dependency on the Raw Evidence store;
- any raw content or hidden sealed metadata in logs, errors, diagnostics, counts, or backup output;
- any direct use of existing memory/asset/RM databases for evidence content;
- any unknown schema version silently accepted;
- any content overwrite or path escape;
- any unregistered production write;
- any migration or production-data touch;
- any need for Dashboard/export/LLM/search/embedding/legal-hold/KMS capability to make O5A work.

## 9. O5A rollback and compatibility

Rollback is safe because O5A is not imported or constructed by `server.py`, `ImportEngine`, or any recall path.

- Disable/remove the future O5A construction path; ordinary memory continues to use the current bucket/asset/import stores.
- Do not delete existing ordinary memory or databases to roll back O5A.
- A Raw Evidence root and registry created by O5A may remain inert and isolated; purge is a later lifecycle decision, not a rollback prerequisite.
- If an O5A binary created a registry and an older binary runs later, the older binary must ignore the separate root and continue ordinary behavior.
- A newer binary encountering an unsupported Raw Evidence schema must fail the Raw Evidence operation closed and must not block ordinary memory startup/recall.
- Remember-Me storage and authority remain untouched.

## 10. O5B — opt-in import evidence capture

O5B is the first stage allowed to connect Raw Evidence to imports. It is a separate PR and implementation instruction.

### Capture boundary

The current route decodes the uploaded body before `ImportEngine.start()`. To preserve an honest fidelity claim, O5B must capture permitted raw bytes at the upload boundary before lossy UTF-8 replacement/normalization, then pass the decoded text through the existing parser/extraction path.

The planned flow is:

1. dashboard write authorization and request/resource preflight;
2. explicit `raw_evidence_capture` opt-in/default-off decision;
3. source/run identity creation;
4. bounded raw-byte staging and O5A evidence capture;
5. decode/parse/chunk as current import requires;
6. extraction from the captured snapshot or explicitly identified derivative;
7. ordinary memory behavior unchanged;
8. run checkpoint and retry state.

O5B may need a small capture coordinator module, for example `raw_evidence_import.py`, plus narrowly scoped changes to the upload/import boundary. That module name is proposed, not current.

### Run, identity, and retry

O5B introduces `ImportRun` identity and idempotency metadata. It must record source descriptors, importer/parser version, capture mode, source digest, actor, timestamps, status, counts, and a retry key without placing raw content in logs.

Resume/retry must distinguish:

- the same source/run/item retry, which must not duplicate evidence or memory;
- a new run of the same source, which is a new run and may reuse a physical blob without inventing provenance;
- evidence capture succeeded but extraction failed;
- extraction succeeded but memory write failed;
- memory write succeeded but lineage is not yet present, which O5C owns.

`preserve_raw` remains an independent boolean/dimension. `raw_evidence_capture=yes` must not imply `preserve_raw=yes`, and `preserve_raw=yes` must not imply evidence capture.

### O5B failure semantics

- capture failure: import follows the explicitly adopted failure policy; it must not claim evidence exists;
- evidence succeeds/extraction fails: preserve evidence with a failed/retryable run;
- extraction succeeds/memory fails: preserve evidence and retry state; no false memory-success claim;
- evidence is unavailable: ordinary memory operations remain available; an evidence-required import may fail closed;
- no recall, search, embedding, Dashboard, MCP, or model access is added.

## 11. O5C — lineage and provenance integration

O5C adds append-only transformation records without making Raw Evidence recallable.

### Planned lineage records

`PLANNED IMPLEMENTATION`: a separate lineage module/store, such as `raw_evidence_lineage.py`, may own append-only records. It must not reuse `bucket_history` as a provenance API.

Minimum relations:

- evidence revision → derived memory identifier;
- derived memory → evidence revision inputs;
- import run → capture and transformation;
- transformation kind: extraction, summarize, merge, dehydrate, edit, re-extract, correction, or redaction;
- importer/extractor/model version;
- source span/offset only when the captured fidelity and mapping support it;
- one-to-many and many-to-one transformations;
- status: complete, partial, evidence-missing, source-redacted, needs-revalidation, provenance-broken, or superseded.

### Repository integration points

The future implementation must inspect and instrument the actual success boundaries:

- `ImportEngine._process_single_chunk()` after extracted items and memory outcomes are known;
- `ImportEngine._merge_or_create_item()` for merge/dehydration transformations;
- the central `BucketManager.create()`/`update()`/`delete()` boundaries for user or maintenance edits;
- Dashboard import-review updates only if they are an actual supported memory-edit path;
- future validated historical re-import workflow.

Lineage write failure must not fail ordinary memory creation. It produces a recoverable `lineage_pending`/`needs_reconcile` state. Memory operations must remain successful and independently observable.

### Legacy and historical provenance

Existing memories remain `legacy_memory_without_evidence` unless a later explicit re-import creates a structurally validated mapping. Text similarity, timestamps, embeddings, or model judgment alone cannot create historical provenance.

## 12. O5D — retention, deletion, redaction, and purge

O5D implements the adopted lifecycle without legal holds.

Scope:

- finite/configurable policy-class retention and expiry;
- immediate tombstone/restriction for redaction and deletion;
- policy-controlled physical purge;
- shared CAS blob garbage collection only when no live revision references remain;
- source-deletion review/confirmation unless identity and authority are proven;
- explicit memory provenance statuses after evidence loss;
- independent ordinary memory deletion and evidence deletion;
- staged full-data/account tombstone-and-purge across applicable live stores;
- explicit backup lifecycle handoff to O5E.

Required behavior:

- evidence disappearance never silently deletes derived memory;
- memory deletion does not delete evidence by default;
- redaction removes normal evidence access immediately;
- physical purge is not promised to be instantaneous across backups;
- cryptographic erasure is not claimed;
- legal holds remain `OUT OF SCOPE`.

The implementation must model shared blobs separately from logical evidence and must not physically remove a blob while a live revision references it.

## 13. O5E — backup, restore, and observability hardening

O5E owns operational consistency after lifecycle and lineage states exist.

Scope:

- dedicated evidence backup category and versioned manifest;
- registry/blob/lineage/run consistency checks;
- staging-first restore with hash/size/schema verification;
- orphan CAS blob detection;
- metadata rows with missing blobs;
- broken lineage detection and safe repair status;
- storage usage/quota accounting;
- restricted metadata-only security/access audit;
- safe diagnostics that never reveal raw content or sealed existence;
- corruption quarantine and recovery runbooks;
- feature flag, rollback, and backup lifecycle observability.

O5E must not add Dashboard evidence UI, export, LLM retrieval, MCP retrieval, search, embeddings, legal holds, or external KMS.

Existing `backup_export.py` must not be reused as an unfiltered raw-content export. O5E must define a separate policy-aware path and prove ordinary backup consumers cannot receive Raw Evidence accidentally.

## 14. Explicitly deferred capabilities

These remain outside O5A–O5E unless a later separately reviewed design changes scope:

- Dashboard Raw Evidence browser, detail, sealed viewer, or lineage graph;
- evidence export UI/API/tool and user/operator export capability;
- explicit LLM evidence retrieval;
- MCP evidence retrieval;
- semantic evidence search;
- full-text evidence search;
- evidence embeddings;
- legal hold authority/release workflows;
- external-KMS/envelope encryption;
- automatic historical provenance matching;
- automatic Raw Evidence participation in recall, Breath, Dream, resonance, or model context.

These are not hidden inside observability, backup, or diagnostics.

## 15. Cross-stage invariant table

| Invariant | O5A | O5B | O5C | O5D | O5E |
| --- | --- | --- | --- | --- | --- |
| Default-off until explicitly enabled | Required | Required | Required | Required | Required |
| Ordinary memory works without evidence | Required | Required | Required | Required | Required |
| No automatic Breath participation | Required | Required | Required | Required | Required |
| No automatic model-context participation | Required | Required | Required | Required | Required |
| `preserve_raw` unchanged | Required | Required | Required | Required | Required |
| Evidence deletion does not auto-delete memory | N/A but reserved | Required | Required | Required | Required |
| Memory deletion does not auto-delete evidence | N/A but reserved | Required | Required | Required | Required |
| Sealed boundaries and non-enumeration | Required | Required | Required | Required | Required |
| No fabricated historical provenance | Required for stored identity | Required | Required | Required | Required |
| Remember-Me owns RM image blobs | Required | Required | Required | Required | Required |
| No production-data migration without authorization | Required | Required | Required | Required | Required |

`N/A but reserved` means O5A does not implement the lifecycle operation, but its data model and rollback contract must not prevent the later invariant.

## 16. Dependency graph and parallelization

```text
O5A storage/integrity
  -> O5B opt-in import capture
    -> O5C lineage/provenance
      -> O5D retention/deletion/redaction/purge
        -> O5E backup/restore/observability hardening
```

The required implementation path is sequential because:

- O5B needs the verified O5A store and failure states;
- O5C needs O5B run/source/evidence identity;
- O5D needs O5C lineage statuses and shared-reference knowledge;
- O5E needs O5D lifecycle states to distinguish live, tombstoned, purged, orphaned, and broken records.

Documentation, fixture preparation, and static repository discovery may proceed in parallel outside implementation PRs. No implementation stage should be parallelized across these dependency boundaries.

## 17. Future implementation PR strategy

Prefer one independently reviewable PR per stage.

| Stage | Branch | Commit granularity | PR title | Expected change type | Review focus | Merge prerequisites |
| --- | --- | --- | --- | --- | --- | --- |
| O5A | `impl/raw-evidence-o5a-storage` | One focused implementation commit plus tests/coverage registration in the same PR | `Implement Raw Evidence O5A storage foundation` | New isolated runtime store, tests, write-coverage registration | No startup/import/recall coupling; integrity/path/failure safety | O5A gate, full offline suite, production-write coverage |
| O5B | `impl/raw-evidence-o5b-capture` | Capture coordinator and import integration kept reviewable; no unrelated cleanup | `Implement opt-in Raw Evidence import capture` | Import-boundary/runtime/test changes | Raw-byte fidelity, opt-in, retry, `preserve_raw` independence | O5A merged; O5B-specific gate |
| O5C | `impl/raw-evidence-o5c-lineage` | Lineage store and each transformation boundary reviewable together | `Implement Raw Evidence lineage integration` | Lineage/runtime/test changes | Accurate edges, no recall coupling, legacy truthfulness | O5B merged; transform coverage |
| O5D | `impl/raw-evidence-o5d-lifecycle` | Lifecycle policy/state and purge/GC tests together | `Implement Raw Evidence lifecycle controls` | Retention/deletion/redaction/runtime/test changes | Independent deletion, tombstone/purge, shared blobs | O5C merged; deletion/recovery gate |
| O5E | `impl/raw-evidence-o5e-operations` | Backup/restore and observability changes split only if independently safe | `Harden Raw Evidence backup and observability` | Backup/restore/audit/ops runtime/test changes | No raw leakage, integrity, orphan/broken-lineage handling | O5D merged; restore/corruption gate |

O5A is the only stage authorized by this planning review. Each later PR requires a new explicit implementation instruction and review.

## 18. Implementation stop conditions

Future Codex implementation instructions must stop and return for review if:

- a stage needs to weaken or silently amend Memory Contract v1;
- `preserve_raw` semantics change or become evidence capture;
- ordinary recall, Breath, Dream, or model context becomes dependent on evidence storage;
- Remember-Me ownership, cutover, adapter, or blob authority changes;
- sealed evidence becomes enumerable by default;
- historical memories require automatic or similarity-based provenance;
- production data, existing memory files, or existing databases would be touched unexpectedly;
- deletion, redaction, retention, purge, or shared-blob semantics become ambiguous;
- a later capability becomes necessary for Foundation/O5A;
- a new writer is not covered by the production-write audit;
- a path, schema, or integrity failure would expose content or overwrite existing data;
- an older binary cannot safely ignore a newer Raw Evidence store;
- implementation scope exceeds the stage file proposal without an updated plan/review.

## 19. Remaining defaults and implementation validation

The following are intentionally not numeric product decisions in O5:

- default evidence retention duration: `PRODUCT DEFAULT TBD`;
- audit-record retention duration: `PRODUCT DEFAULT TBD`;
- evidence-item maximum size: `IMPLEMENTATION DEFAULT TO BENCHMARK`;
- import batch maximum: `IMPLEMENTATION DEFAULT TO BENCHMARK`;
- storage quota: `IMPLEMENTATION DEFAULT TO BENCHMARK`;
- future export-size limit: deferred with export capability.

The architecture requires them to be configurable, bounded, and fail closed. It does not require a specific number to begin O5A storage planning.

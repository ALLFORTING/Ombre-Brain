# RM Production Cutover - Final Execution Runbook

Status: documentation and command-planning artifact only. This document does
not authorize or execute a production cutover, access Render, change
production configuration, restart production, migrate production data,
reindex production, or release a production freeze.

## Source and accepted implementation

- merged main: `6670efa6b3be5dad1f0ce98707604574b3044dfb`
- merged-main tree: `b7eab186a5ce145e9a05cb287d5ad19b19e99b36`
- service: `Ombre-Brain`
- service ID: `srv-d7sqj128qa3s73essoug`
- Remember-Me: `0.1.0.dev7`
- Remember-Me source contract: `a00ea991442d7581a3856b178525a8e77da833fe`
- legacy root: `/opt/render/project/src/buckets`
- RM root: `/opt/render/project/src/buckets/remember-me`
- state root: `/opt/render/project/src/buckets/state`

Implementation E is `4037508ceb5c200dd9215bb814c330602aabfa92`. The exact
merged-main object was unavailable locally and the bounded GitHub SSH fetch was
blocked by network permission. The accepted handoff mechanically verified that
local `90f3824a3986978080279a4c5b0209f21191a46c` has the exact merged-main
tree above. The integration branch is based on that tree-equivalent source and
cherry-picks E without patch changes.

## Safety rules

1. Stop on any failed, unknown, stale, mismatched, or expired gate.
2. Never print, paste, persist in evidence, screenshot, or commit a lease
   token, API key, password, cookie, private key, or user content.
3. Do not import `server.py` for backup, initialization, migration, or state
   preparation.
4. Do not instantiate `CutoverStateStore` to inspect an absent state database;
   its normal constructor creates the state directory/database.
5. Do not run D2 `status` until `STATE_DB` already exists.
6. Do not delete RM data, RM evidence, checkpoints, or the legacy source during
   rollback.
7. Do not use public MCP/Dashboard writes for migration or reindexing.
8. No command below authorizes production; owner approval remains separate.

## Actual command surfaces

Parser inspection confirms:

```text
operations: backup verify-backup restore preflight readiness-gate acceptance-checks
migration: preflight-local initialize-cutover acquire-freeze migrate reconcile
           verify reindex inspect abort
transition: status prepare-rm switch-to-rm accept-rm release-freeze-to-rm
            recover-expired-rm class-a-rollback accept-legacy release-freeze-to-legacy
```

There is no `validate-restart` CLI. `switch-to-rm --restart-validated`
performs restart validation internally.

## Operator variables

These are path patterns only; they do not authorize production execution.

```sh
LEGACY_ROOT=/opt/render/project/src/buckets
RM_ROOT=/opt/render/project/src/buckets/remember-me
STATE_ROOT=/opt/render/project/src/buckets/state
STATE_DB=/opt/render/project/src/buckets/state/migration.sqlite3
LEASE_CAPABILITY_FILE=/opt/render/project/src/buckets/state/operator/lease-token.json
D2_TRANSITION_IDENTITY=<exact value from D2 status>
MIGRATION_IDENTITY=ombre-rm-production-cutover
MIGRATION_VERSION=1
LEASE_TTL_SECONDS=60
```

The state and checkpoint databases are distinct:

```text
STATE_DB=/opt/render/project/src/buckets/state/migration.sqlite3
/opt/render/project/src/buckets/state/migration-progress.sqlite3
```

### Capability secret handling

`acquire-freeze` creates `STATE_ROOT/operator/lease-token.json` only after the
explicit initialization phase. The target is mode `0600`; publication is
atomic and unsafe/symlink replacement is rejected. The file contains the only
local plaintext lease credential needed for rehydration. Never print it, place
the token in chat, shell history, screenshots, reports, logs, evidence, or
commits; never copy it into the repository. D1 backup excludes it with the
explicit `lease_capability` reason and verification fails if it appears as an
entry. The state database stores only the token hash. C/D2 output is redacted.
RM release, legacy release, and active pre-D2 abort remove the capability file.
Expired D2 recovery atomically replaces the capability with mode `0600` and
updates the durable lease hash and D2 binding in one SQLite transaction; it
never revives the old token.

## Phase 0 - Identity and preflight

The owner/operator must first confirm authenticated Render metadata: service
ID/name, running source, intended instance, persistent disk, and actual runtime
service rather than an ephemeral instance. Then run the read-only C preflight:

```sh
python -m remember_me_cutover_migration preflight-local \
  --legacy-root "$LEGACY_ROOT" --rm-root "$RM_ROOT" --state-db "$STATE_DB" \
  --report /approved/rm-preflight-local.json \
  --migration-identity "$MIGRATION_IDENTITY" --migration-version "$MIGRATION_VERSION"
```

It must succeed without creating RM/state. Do not run D1 `preflight` or D2
`status` while `STATE_DB` is absent.

## Phase 1 - Bootstrap backup and verification

E closes the former pre-existing-state blocker. The legacy-authoritative
profile accepts absent RM/state roots, records explicit absence, snapshots
SQLite through the backup API, hashes managed blobs, and verifies the manifest.
It does not create production RM/state roots.

```sh
python -m remember_me_cutover_operations backup \
  --profile legacy-authoritative --legacy-root "$LEGACY_ROOT" \
  --rm-root "$RM_ROOT" --state-db "$STATE_DB" \
  --destination /approved/rm-backup-<run-id> \
  --report /approved/rm-backup-<run-id>.json

python -m remember_me_cutover_operations verify-backup \
  --backup /approved/rm-backup-<run-id>
```

Require `status=PASS`, no missing/hash/SQLite/blob failures, and capability
exclusion PASS when capability material exists. Isolated restore verification:

```sh
python -m remember_me_cutover_operations restore \
  --backup /approved/rm-backup-<run-id> \
  --legacy-root /isolated/restore/legacy \
  --rm-root /isolated/restore/legacy/remember-me \
  --state-root /isolated/restore/legacy/state \
  --report /isolated/restore/report.json
```

When the source RM/state roots were absent, the restored roots must remain
absent.

## Phase 2 - Explicit initialization

Only this explicit command creates the RM root and state database. It validates
Design A ownership, the exact dev7 contract, and the expected start shape:

```text
state=legacy_authority_rm_ready
authority=legacy
rm_available=true
freeze=open
```

```sh
python -m remember_me_cutover_migration initialize-cutover \
  --legacy-root "$LEGACY_ROOT" --rm-root "$RM_ROOT" --state-db "$STATE_DB" \
  --report /approved/rm-initialize.json
```

It is idempotent only for the identical initialized shape. Unexpected
pre-existing RM/state contents fail closed. No migration, reindex, authority
switch, or public contract change occurs here.

## Phase 3 - Acquire the one retained lease

```sh
python -m remember_me_cutover_migration acquire-freeze \
  --legacy-root "$LEGACY_ROOT" --rm-root "$RM_ROOT" --state-db "$STATE_DB" \
  --report /approved/rm-acquire-freeze.json \
  --migration-identity "$MIGRATION_IDENTITY" --migration-version "$MIGRATION_VERSION" \
  --lease-ttl-seconds "$LEASE_TTL_SECONDS" \
  --lease-capability-file "$LEASE_CAPABILITY_FILE"
```

Required durable state: `FROZEN_LEGACY_MIGRATION`, legacy authority, active
freeze, and identity bound to source generation/target identity. Output is
redacted status/identity only.

## Phase 4 - Migrate with retained lease

```sh
python -m remember_me_cutover_migration migrate \
  --legacy-root "$LEGACY_ROOT" --rm-root "$RM_ROOT" --state-db "$STATE_DB" \
  --report /approved/rm-migrate.json \
  --migration-identity "$MIGRATION_IDENTITY" --migration-version "$MIGRATION_VERSION" \
  --batch-size 100 --lease-ttl-seconds "$LEASE_TTL_SECONDS" \
  --lease-capability-file "$LEASE_CAPABILITY_FILE"
```

The capability is hash/generation/identity checked, renewed, and converted to
`rm-migration-write`; no second lease is acquired. Use `--resume` only after
reviewing a paused/blocked checkpoint.

## Phase 5 - Reconcile, verify, and D1 preflight

```sh
python -m remember_me_cutover_migration reconcile \
  --legacy-root "$LEGACY_ROOT" --rm-root "$RM_ROOT" --state-db "$STATE_DB" \
  --report /approved/rm-reconcile.json \
  --migration-identity "$MIGRATION_IDENTITY" --migration-version "$MIGRATION_VERSION" \
  --lease-ttl-seconds "$LEASE_TTL_SECONDS" \
  --lease-capability-file "$LEASE_CAPABILITY_FILE"

python -m remember_me_cutover_migration verify \
  --legacy-root "$LEGACY_ROOT" --rm-root "$RM_ROOT" --state-db "$STATE_DB" \
  --report /approved/rm-verify.json \
  --migration-identity "$MIGRATION_IDENTITY" --migration-version "$MIGRATION_VERSION" \
  --lease-ttl-seconds "$LEASE_TTL_SECONDS" \
  --lease-capability-file "$LEASE_CAPABILITY_FILE"

python -m remember_me_cutover_operations preflight \
  --legacy-root "$LEGACY_ROOT" --rm-root "$RM_ROOT" --state-db "$STATE_DB" \
  --report /approved/rm-d1-preflight.json \
  --backup-root /approved/rm-backup-<run-id> \
  --embedding-enabled <true|false|unknown> \
  --expected-model-id <approved-model-id-or-omit> \
  --estimated-vector-bytes <approved-estimate> --headroom-bytes 536870912 \
  --worker-count 1 --multiprocess false --shared-state true --service-instances 1
```

Reconcile and verify retain and renew the same lease. Reconciliation must
advance to `FROZEN_READY_FOR_RM_SWITCH` only on an exact pass.

## Phase 6 - Reindex with retained lease

```sh
python -m remember_me_cutover_migration reindex \
  --legacy-root "$LEGACY_ROOT" --rm-root "$RM_ROOT" --state-db "$STATE_DB" \
  --report /approved/rm-reindex.json \
  --migration-identity "$MIGRATION_IDENTITY" --migration-version "$MIGRATION_VERSION" \
  --lease-ttl-seconds "$LEASE_TTL_SECONDS" --max-new-index-work 100 \
  --lease-capability-file "$LEASE_CAPABILITY_FILE"
```

Provider-enabled work requires separate owner approval for external/paid
calls. Provider-disabled keyword-only readiness must have zero external calls.

## Phase 7 - Readiness gate

Create sanitized evidence with no token/capability material:

```sh
python -m remember_me_cutover_operations readiness-gate \
  --evidence /approved/rm-readiness-evidence.json \
  --report /approved/rm-readiness-gate.json
```

Require `READY_FOR_AUTHORITY_SWITCH=YES`, `status=PASS`, all hard gates PASS,
`blocking_gates=[]`, and `production_access_occurred=false`. D2 evidence must
identify Remember-Me `0.1.0.dev7` and source contract
`a00ea991442d7581a3856b178525a8e77da833fe`.

## Phase 8 - D2 prepare, external coordination, restart, and switch

```sh
python -m remember_me_cutover_transition prepare-rm \
  --state-db "$STATE_DB" --configured-authority legacy \
  --lease-capability-file "$LEASE_CAPABILITY_FILE" \
  --evidence /approved/rm-readiness-evidence.json
```

After PASS and explicit owner approval, coordinate externally:

```text
OMBRE_ASSET_AUTHORITY=rm
OMBRE_RM_RUNTIME_ENABLED=true
OMBRE_RM_DATA_ROOT=/opt/render/project/src/buckets/remember-me
```

Perform the controlled restart through the authenticated Render control plane.
The real server boot must read the verified D2 `RM_PREPARED` coordination
record and enter `COORDINATION_PENDING`; this is the required restart seam,
not an optional rehearsal detail. During this boot:

```text
configured authority = rm
boot mode            = COORDINATION_PENDING
selected backend     = rm
durable authority    = legacy
durable state        = FROZEN_READY_FOR_RM_SWITCH
public mutations     = blocked
legacy fallback      = forbidden
```

Health/startup may remain available so the operator can complete the handoff,
but the runtime must not change durable authority or open writes. The boot
proof is accepted only when the D2 phase, frozen state, active lease identity,
and migration identity all match; any ordinary authority mismatch remains
fail-closed. Keep the legacy mount, public contracts, service, disk, instance
count, and deployment source unchanged. Only after that fresh process has
started successfully, run:

```sh
python -m remember_me_cutover_transition switch-to-rm \
  --state-db "$STATE_DB" --configured-authority rm \
  --lease-capability-file "$LEASE_CAPABILITY_FILE" \
  --restart-validated --rm-available true
```

Required: `phase=RM_FROZEN_ACCEPTANCE`, RM authority, active freeze, and public
mutations blocked.

Do not run `switch-to-rm` before the restart as a workaround. That bypasses
the restart-validation and frozen-coordination safety model. If a pre-D2 abort
or expired recovery returns the durable state to
`LEGACY_AUTHORITY_RM_READY`, D2 status must show no active `RM_PREPARED`
phase; reacquire a fresh lease and repeat the migration/readiness/prepare
sequence. Never edit SQLite manually or reuse the old lease/transition record.

## Phase 9 - Frozen RM acceptance and point of no return

Run approved authenticated RM health/persistence, reads, auth/privacy, and
public-write-rejection checks. Save only sanitized booleans/statuses:

```sh
python -m remember_me_cutover_operations acceptance-checks \
  --evidence /approved/rm-acceptance-evidence.json \
  --report /approved/rm-d1-acceptance.json

python -m remember_me_cutover_transition accept-rm \
  --state-db "$STATE_DB" --configured-authority rm \
  --lease-capability-file "$LEASE_CAPABILITY_FILE" \
  --checks /approved/rm-checks.json
```

Require D2 acceptance PASS and `LOSSLESS_ROLLBACK_WINDOW_OPEN=YES`. Obtain
explicit owner point-of-no-return confirmation immediately before:

```sh
python -m remember_me_cutover_transition release-freeze-to-rm \
  --state-db "$STATE_DB" --configured-authority rm \
  --lease-capability-file "$LEASE_CAPABILITY_FILE"
```

Required after release: `phase=RM_OPEN`, `cutover_state=rm_authority_open`,
freeze inactive, and `LOSSLESS_ROLLBACK_WINDOW_OPEN=NO`. Successful release
cleans the capability file; Class A is then forbidden.

## Expired retained lease recovery

An expired retained lease is not an implicit rollback, an acceptance result,
or permission to open legacy. Public mutations remain blocked while the lease
is expired. Never edit SQLite, call an internal Python method, reuse the old
token, or invoke pre-D2 recovery against `FROZEN_RM_ACCEPTANCE`.

### Pre-D2 expired recovery

When the durable phase is `FROZEN_LEGACY_MIGRATION` or
`FROZEN_READY_FOR_RM_SWITCH`, the supported recovery remains the migration
`abort` command. It verifies the migration/source identity, preserves RM data
and evidence, returns to `LEGACY_AUTHORITY_RM_READY`, and removes the
capability. This path requires the configured runtime to remain legacy during
the controlled recovery.

### D2 `RM_FROZEN_ACCEPTANCE` expired recovery

When the durable state is `FROZEN_RM_ACCEPTANCE`, authority is `rm`, the
retained lease is expired, RM is available, and the D2 transition/readiness/
migration identities still match, run the explicit recovery command below:

```sh
python -m remember_me_cutover_transition recover-expired-rm \
  --state-db "$STATE_DB" --configured-authority rm \
  --lease-capability-file "$LEASE_CAPABILITY_FILE" \
  --transition-identity "$D2_TRANSITION_IDENTITY" \
  --lease-ttl-seconds "$LEASE_TTL_SECONDS" --rm-available true
```

The command requires the existing capability file as an in-memory continuity
proof, then issues a new lease ID/token/generation. The old lease remains
invalid. It atomically publishes the new `0600` capability and binds the new
lease to the existing D2 transition record while preserving RM authority,
`FROZEN_RM_ACCEPTANCE`, readiness evidence, migration identity, transition
identity, and any legitimate acceptance result. It does not release the
freeze, mark acceptance PASS, enter `RM_OPEN`, or enable public writes.

After PASS, verify with the supported `status` command:

```text
phase=RM_FROZEN_ACCEPTANCE
authority=rm
freeze_status=active
freeze_active=true
lease_healthy=true
LOSSLESS_ROLLBACK_WINDOW_OPEN=YES
rollback_class_currently_available=CLASS_A
acceptance_status=null (unless it legitimately passed already)
```

The operator may then run the normal frozen RM acceptance and separately
obtain owner confirmation before `release-freeze-to-rm`. Any active lease,
wrong phase/authority, missing or corrupt transition record, stale or
mismatched migration/transition identity, unavailable RM, or unsafe
capability path fails closed.

### Active Class A rollback

Class A rollback is a different workflow. It is available only while
`FROZEN_RM_ACCEPTANCE` has an active retained lease. An expired lease must be
recovered with `recover-expired-rm` first; it must not be treated as a usable
Class A capability. After Class A has been prepared or the runtime reaches
`FROZEN_RM_ROLLBACK`, use only the existing coordinated rollback commands.

### Point of no return / `RM_OPEN`

After `release-freeze-to-rm`, the phase is `RM_OPEN`, the lossless rollback
window is closed, and expired retained-lease recovery is rejected. Any future
reverse reconciliation requires a separately approved Class B design.

## Phase 10 - Post-cutover verification

Only after state exists:

```sh
python -m remember_me_cutover_transition status \
  --state-db "$STATE_DB" --configured-authority rm --rm-available true
```

Confirm RM authority/open state, acceptance PASS, persistence, reads/writes,
auth/privacy, public write rejection, no legacy fallback, and unchanged public
contracts through approved authenticated checks without recording user data.

## Pre-D2 active-lease rollback

E closes the former active-lease blocker. From either
`FROZEN_LEGACY_MIGRATION` or `FROZEN_READY_FOR_RM_SWITCH`, use:

```sh
python -m remember_me_cutover_migration abort \
  --legacy-root "$LEGACY_ROOT" --rm-root "$RM_ROOT" --state-db "$STATE_DB" \
  --report /approved/rm-abort.json \
  --migration-identity "$MIGRATION_IDENTITY" --migration-version "$MIGRATION_VERSION" \
  --reason "<sanitized failure reason>" \
  --lease-capability-file "$LEASE_CAPABILITY_FILE"
```

It verifies source generation/identity, marks the checkpoint aborted, returns
to `LEGACY_AUTHORITY_RM_READY`, preserves RM data/evidence, and removes the
capability. Verify legacy with a new `preflight-local` process and confirm the
capability path is absent. There is no subsequent D2 release command in this
pre-D2 open state: `abort` is the valid release to legacy. Expired recovery is
separate; never force expiry or edit SQLite manually.

## D2 Class A rollback

Class A is available only in `FROZEN_RM_ACCEPTANCE` with the same active
retained lease. It preserves RM data/evidence.

```sh
python -m remember_me_cutover_transition class-a-rollback \
  --state-db "$STATE_DB" --configured-authority rm \
  --lease-capability-file "$LEASE_CAPABILITY_FILE" \
  --reason "<sanitized failure reason>" --mode prepare --rm-available true
```

Required: `phase=ROLLBACK_PREPARED`, frozen rollback state, retained freeze,
and preserved RM target. The owner then coordinates:

```text
OMBRE_ASSET_AUTHORITY=legacy
```

and performs the controlled restart. Finalize:

```sh
python -m remember_me_cutover_transition class-a-rollback \
  --state-db "$STATE_DB" --configured-authority legacy \
  --lease-capability-file "$LEASE_CAPABILITY_FILE" \
  --reason "<same sanitized failure reason>" --mode finalize \
  --restart-validated --rm-available true

python -m remember_me_cutover_transition accept-legacy \
  --state-db "$STATE_DB" --configured-authority legacy \
  --lease-capability-file "$LEASE_CAPABILITY_FILE" \
  --checks /approved/legacy-checks.json

python -m remember_me_cutover_transition release-freeze-to-legacy \
  --state-db "$STATE_DB" --configured-authority legacy \
  --lease-capability-file "$LEASE_CAPABILITY_FILE"
```

Require `phase=LEGACY_OPEN`, legacy authority, inactive freeze, preserved RM
target/evidence, and capability cleanup. After RM authority is open, Class A
is forbidden; use a separately approved Class B design.

## Local command-level rehearsals and restart boundaries

The integration fixture starts with legacy present and RM/state absent and
invokes the actual module CLIs in separate Python processes. The restart
rehearsal also starts the actual server boot path in a fresh Python process
with the external RM environment, so module cache cannot hide a boot error.
It covers:

```text
backup -> verify-backup -> initialize-cutover -> acquire-freeze -> migrate
-> reconcile -> verify -> reindex -> readiness-gate -> prepare-rm
-> external RM authority + fresh server boot (coordination-pending)
-> switch-to-rm -> frozen acceptance
-> explicit point-of-no-return -> release -> RM status/read/write checks
```

Separate CLI rehearsals cover active pre-D2 abort from both frozen states and
D2 Class A rollback. Each process boundary checks state/capability survival;
reports and logs are scanned for token material. The fixture uses Remember-Me
dev7, makes no external/paid calls, and never accesses Render.

## Production-write inventory

| Phase | Writes | Scope |
|---|---:|---|
| preflight-local, verify-backup, readiness-gate, status | no | read/evidence evaluation |
| backup | yes | approved backup destination only |
| initialize/acquire/renew/release | yes | explicit state/capability lifecycle |
| migrate/reconcile/verify/reindex | yes | RM/checkpoint/state evidence |
| D2 prepare/switch/accept/release | yes | transition/authority/freeze state |
| external config/restart | yes | owner-controlled runtime state |
| rollback | yes | state/checkpoint/evidence; RM data preserved |

The production-write coverage scanner must report zero issues.

## Final verdicts

```text
PRE_EXECUTION_GATES_COMPLETE = YES
PRODUCTION_RUNBOOK_COMPLETE = YES
CLASS_A_ROLLBACK_RUNBOOK_COMPLETE = YES
POINT_OF_NO_RETURN_EXPLICIT = YES
READY_FOR_OWNER_CUTOVER_AUTHORIZATION = NO
PRODUCTION_CUTOVER_AUTHORIZED = NO
```

E is integrated and the runbook is updated for the actual E commands. This
local milestone does not publish, deploy, or authorize the production cutover.

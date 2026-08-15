# Remember-Me Production Cutover — Implementation D2

Implementation D2 is an offline operator/control-plane package. It does not
execute a production cutover, edit Render configuration, deploy, or import
the controller from `server.py`.

## Coordination protocol

The operator supplies the local state database, active freeze lease, and
sanitized gate evidence. `prepare-rm` requires the legacy authority, an active
matching lease, the D1 readiness evaluator to return
`READY_FOR_AUTHORITY_SWITCH=YES`, and every D2 hard gate to pass. It persists a
`RM_PREPARED` record and does not change `OMBRE_ASSET_AUTHORITY`.

The operator then changes `OMBRE_ASSET_AUTHORITY=rm` outside this tool and
performs a controlled restart. A restart with the new configuration while the
persisted state is still legacy is reported as `COORDINATION_PENDING`; writes
are blocked and legacy fallback is forbidden. `switch-to-rm` requires an
explicit restart-validation acknowledgement and atomically moves the durable
state to `FROZEN_RM_ACCEPTANCE` while retaining the freeze lease.

Frozen acceptance records only allowlisted check names, statuses, evidence
identities, timestamps, and failure codes. It emits no tokens, user content,
asset metadata, or secrets. Only a `PASS` acceptance can call
`release-freeze-to-rm`; that operation persists `RM_AUTHORITY_OPEN` and
removes the lease in the same durable state transition. Normal RM mutation
access is therefore not available during any acceptance boundary.

## Class A rollback

While `FROZEN_RM_ACCEPTANCE` is active, `class-a-rollback --mode prepare`
enters `FROZEN_RM_ROLLBACK` and preserves RM data and evidence. The operator
coordinates the external authority back to legacy, restarts, and finalizes to
`FROZEN_LEGACY_ACCEPTANCE`. Legacy acceptance must pass before the freeze can
be released to `LEGACY_AUTHORITY_RM_READY`. Direct rollback from
`RM_AUTHORITY_OPEN` is rejected. After RM freeze release,
`LOSSLESS_ROLLBACK_WINDOW_OPEN=NO`; future reversal is explicitly Class B and
is not implemented here.

## Local command examples

```text
python -m remember_me_cutover_transition status --state-db <path> --configured-authority legacy
python -m remember_me_cutover_transition prepare-rm --state-db <path> --configured-authority legacy --lease-id <id> --lease-token <token> --evidence <json>
python -m remember_me_cutover_transition switch-to-rm --state-db <path> --configured-authority rm --lease-id <id> --lease-token <token> --restart-validated
python -m remember_me_cutover_transition accept-rm --state-db <path> --configured-authority rm --lease-id <id> --lease-token <token> --checks <json>
python -m remember_me_cutover_transition release-freeze-to-rm --state-db <path> --configured-authority rm --lease-id <id> --lease-token <token>
```

Lease tokens are command inputs only. They are never persisted in D2 evidence
or printed by the tool.

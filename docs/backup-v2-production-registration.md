# Backup v2 Production Registration

Stage 8H-G1D-B adds code-only preparation for the encrypted backup-v2 production
channel. The route factory remains unavailable unless production explicitly opts
in with `OMBRE_BACKUP_V2_ENABLED=true`. This PR does not configure Render,
GitHub secrets, GitHub variables, or any endpoint, and it does not start a
server, dispatch a workflow, generate a real key, capture data, rehearse a
restore, migrate, reindex, deploy, or cut over production.

## Default-Disabled Registration

Backup-v2 is disabled when `OMBRE_BACKUP_V2_ENABLED` is unset, empty, or exactly
`false`. Disabled startup does not import or initialize the capture controller,
does not prepare or inspect a backup workspace, does not instantiate a JWK
client, does not register backup-v2 routes, and performs no network access.

When the value is any other non-empty string except exact lowercase `true`,
startup fails closed with a stable configuration error. Invalid enabled
configuration also aborts startup; the service must not silently fall back to
disabled mode after an attempted enable.

## Environment Contract

Enabled mode requires these values:

- `OMBRE_BACKUP_V2_ENABLED`: exact lowercase `true`.
- `OMBRE_BACKUP_V2_PUBLIC_KEY_B64`: canonical base64 for the 32 raw X25519
  recipient public-key bytes.
- `OMBRE_BACKUP_V2_RECIPIENT_FINGERPRINT`: `x25519-sha256:` followed by 64
  lowercase hexadecimal characters, matching the configured public key.
- `OMBRE_BACKUP_V2_REPOSITORY_ID`: decimal GitHub repository ID for the
  approved transport repository.
- `OMBRE_BACKUP_V2_REPOSITORY_OWNER_ID`: decimal GitHub owner ID for the
  approved transport owner.
- `OMBRE_BACKUP_V2_WORKSPACE_ROOT`: absolute backup-v2 workspace directory,
  outside the source tree and not containing it.
- `OMBRE_BACKUP_V2_FREEZE_TIMEOUT_SECONDS`: positive integer, 1 through 600.
- `OMBRE_BACKUP_V2_MAX_FREEZE_SECONDS`: positive integer, 2 through 1800, and
  greater than the freeze timeout.
- `OMBRE_BACKUP_V2_MAX_SOURCE_BYTES`: positive integer, at most 10737418240.
- `OMBRE_BACKUP_V2_MAX_BUNDLE_BYTES`: positive integer, at most 10737418240.
- `OMBRE_BACKUP_V2_MINIMUM_FREE_BYTES`: positive integer, at most 10737418240.
- `OMBRE_BACKUP_V2_READY_TTL_SECONDS`: positive integer, 1 through 86400.
- `RENDER_GIT_COMMIT`: exact 40-character lowercase Git commit SHA. There is no
  separate production override for the runtime commit.

Numeric values reject whitespace, signs, booleans, decimals, zero, negatives,
overflow, scientific notation, and trailing junk. Invalid values are not
clamped.

The source root always comes from the normal Ombre-Brain configuration,
`server.config["buckets_dir"]`. There is no second environment variable for the
source root.

## Workspace Boundary

The workspace root must be absolute, must not use traversal, must not equal the
source root, must not sit inside the source root, and must not contain the
source root. Existing workspace validation also rejects symlink or reparse-point
escapes.

When enabled, an absent workspace is created only by the existing
`prepare_backup_workspace` implementation. An existing workspace is loaded only
by `load_backup_workspace`. A partial, non-empty, invalid, or unsafe workspace
fails startup and is not repaired or deleted automatically.

## Process Guard

Backup-v2 production registration is supported only for `streamable-http`, one
Python process, one Uvicorn worker, and no reload mode. Known worker count
environment variables such as `WEB_CONCURRENCY` and `UVICORN_WORKERS` must be
unset, empty, or exact integer `1`.

Render scaling must remain one instance during capture and restore rehearsal.
This stage does not add distributed locks or multi-instance coordination.

## OIDC Verification

Backup-v2 uses the GitHub Actions issuer
`https://token.actions.githubusercontent.com`, the exact audience
`ombre-brain-backup-v2`, and the issuer's official JWKS endpoint. The JWK client
is lazy: no OIDC or JWKS network request occurs at startup.

Authenticated requests must supply exactly one `Authorization: Bearer ...`
header. Query-string, cookie, and request-body tokens are rejected. Tokens are
length-bounded, are not logged, are not decoded for debugging, are not written
to disk, and underlying JWT or JWK exception text is not exposed.

The cryptographic verifier accepts only `RS256`, validates the signature,
issuer, exact audience, expiry, and required temporal claims, then passes the
validated claims to the existing strict backup-v2 OIDC policy. That policy keeps
the approved repository, owner, branch, workflow path, dispatch event, audience,
run ID, and run attempt contract unchanged. Legacy v1 workflow claims do not
authorize v2 endpoints.

## Route Lifecycle

`backup_entry.run()` evaluates backup-v2 registration after it knows the
transport and before constructing the streamable HTTP app. Disabled mode returns
without registering anything. Enabled mode validates the complete configuration,
constructs the controller and verifier, builds the existing four v2 routes from
the G1C route factory, and registers them only after construction succeeds.

The exact paths are:

- `POST /api/backup/v2/captures`
- `GET /api/backup/v2/captures/{request_id}`
- `GET /api/backup/v2/captures/{request_id}/bundle`
- `POST /api/backup/v2/captures/{request_id}/ack`

Registration is idempotent within one process and rejects duplicate or partial
backup-v2 route state. It is unavailable for stdio and SSE transports.

## Legacy Preservation

This stage does not remove or redesign `/api/backup/export`. The
`/api/embeddings/backfill`, `/api/aliases/clean`, `/mcp`, and `/health`
behaviors are preserved. Legacy v1 OIDC tokens are not routed into v2 endpoints,
and a v2 token does not authorize legacy operations through shared mutable claim
state.

## Offline Key Workflow

Production uses X25519 recipient keys. Only the public key and its fingerprint
may later enter Render. The private key must never enter Render, GitHub Actions,
Git, logs, artifacts, job summaries, environment exports, ChatGPT, Codex,
GitHub issue or PR text, shell history, or production configuration.

Future production key preparation should be:

1. Create a trusted directory outside this repository.
2. Run the offline key tool locally.
3. Make at least one independently protected recovery copy.
4. Verify the keyset.
5. Compare fingerprints.
6. Only later copy the public key and fingerprint into production settings.
7. Retain the private key offline.
8. Keep old private keys until every artifact encrypted to them has expired or
   been independently preserved.

Rollback is achieved by setting `OMBRE_BACKUP_V2_ENABLED` to exact lowercase
`false` and redeploying. Do not delete keys or artifacts as part of rollback.

This PR does not generate a real production key. Tests create only ephemeral
synthetic keys in temporary directories.

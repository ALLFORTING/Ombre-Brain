# RM Production Cutover Foundation

This package adds control-plane primitives only:

- strict `OMBRE_ASSET_AUTHORITY=legacy|rm` parsing, defaulting to `legacy`;
- a durable SQLite authority/state model;
- explicit state-transition validation;
- persistent freeze leases with expiry, renewal, recovery inspection, and
  ephemeral migration capabilities;
- reusable owned-subpath validation for the accepted Design A layout; and
- an injection boundary for later shared legacy/RM mutation gating.

Implementation A does not import these modules from `server.py`.  It does not
activate Remember-Me authority, change MCP or Dashboard routing, start a
migration, create a production freeze, change Render configuration, or access
production data.  The existing v1.4.0 default behavior therefore remains
legacy-authoritative.

Later implementation packages must wire both AssetStore and Remember-Me Core
through `AssetMutationGate`, then add the explicit migration, acceptance,
rollback, backup, and production-preflight workflows.

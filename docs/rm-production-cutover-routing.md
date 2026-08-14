# RM Production Cutover - Implementation B

Implementation B adds runtime-facing backend routing on top of the persistent
Implementation A control plane.

The Remember-Me runtime being initialized is not sufficient to select it as
the asset authority.  The runtime registry validates `OMBRE_ASSET_AUTHORITY`,
the persisted cutover state, RM availability, and the owned storage layout.
With the default or explicit `legacy` authority, Dashboard and MCP asset
operations remain on the legacy `AssetStore` even when RM is initialized.
With `rm` authority, the existing Remember-Me Core and MCP presenter are
selected.  RM failures never fall back to legacy.

Dashboard and RM/legacy MCP mutation paths share the persistent Implementation
A mutation gate.  An active or ambiguous freeze rejects public durable writes
from either backend; reads remain available when the selected backend is
healthy.  RM image responses use verified bytes, while legacy responses retain
their safe filesystem `FileResponse` path.

This package does not perform migration, reindex orchestration, reconciliation,
backup redesign, preflight, Render changes, production authority switching, or
legacy retirement.  The default remains legacy and production cutover remains
unauthorized.  Privileged migration-owned writes and the production migration
workflow are deferred to Implementation C.

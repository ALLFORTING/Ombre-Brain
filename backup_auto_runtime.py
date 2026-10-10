"""Default-disabled, separate OIDC authority for unattended plaintext captures."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import logging
import os
from pathlib import Path

from backup_v2_oidc import GitHubActionsBackupV2OidcVerifier, _log_oidc_denial
from backup_v2_runtime import (
    BackupV2RuntimeConfigError, _custom_route_signatures, _parse_bounded_int,
    _require_single_worker, _validate_workspace_root, require_runtime_coordinator,
    resolve_runtime_commit,
)
from offline_backup_bundle import (
    PLAIN_FORMAT, ProductionDirectoryPolicy, load_backup_workspace, prepare_backup_workspace,
    _path_contains_reparse_point,
)
from production_backup_capture import (
    CaptureChannelError, CaptureLimits, ProductionBackupCaptureController,
    _RUN_ID_PATTERN, _json, _route_error, build_backup_v2_routes,
)

AUTO_AUDIENCE = "ombre-brain-backup-auto-v1"
AUTO_REPOSITORY = "ALLFORTING/ob-backup"
AUTO_REPOSITORY_ID = "1266342286"
AUTO_OWNER_ID = "281855397"
AUTO_WORKFLOW_REF = f"{AUTO_REPOSITORY}/.github/workflows/backup-auto.yml@refs/heads/main"
PREFIX = "/api/backup/auto/v1"
logger = logging.getLogger("ombre_brain.backup_auto")


class StrictBackupAutoOidcPolicy:
    def verify(self, claims):
        exact = {"repository": AUTO_REPOSITORY, "repository_id": AUTO_REPOSITORY_ID,
                 "repository_owner": "ALLFORTING", "repository_owner_id": AUTO_OWNER_ID,
                 "repository_visibility": "private", "ref": "refs/heads/main",
                 "workflow_ref": AUTO_WORKFLOW_REF, "aud": AUTO_AUDIENCE}
        if (not isinstance(claims, dict) or any(claims.get(k) != v for k, v in exact.items())
                or claims.get("event_name") not in {"schedule", "workflow_dispatch"}
                or any(not isinstance(claims.get(k), str) or _RUN_ID_PATTERN.fullmatch(claims[k]) is None
                       for k in ("run_id", "run_attempt"))
                or "job_workflow_ref" in claims or "environment" in claims):
            if not isinstance(claims, dict):
                mismatches = ["claims"]
            else:
                mismatches = [k for k, v in exact.items() if claims.get(k) != v]
                event = claims.get("event_name")
                if not isinstance(event, str) or event not in {"schedule", "workflow_dispatch"}:
                    mismatches.append("event_name")
                mismatches.extend(
                    k for k in ("run_id", "run_attempt")
                    if not isinstance(claims.get(k), str) or _RUN_ID_PATTERN.fullmatch(claims[k]) is None
                )
                mismatches.extend(k for k in ("job_workflow_ref", "environment") if k in claims)
            _log_oidc_denial("auto_policy", fields=mismatches)
            raise CaptureChannelError("oidc_denied")
        return {k: claims[k] for k in ("run_id", "run_attempt")}


def register_backup_auto_if_enabled(server_module, transport, *, environ=None):
    env = os.environ if environ is None else environ
    flag = env.get("OMBRE_BACKUP_AUTO_ENABLED")
    if flag in (None, "", "false"):
        return None
    if flag != "true" or transport != "streamable-http":
        raise BackupV2RuntimeConfigError("backup_auto_config_invalid")
    _require_single_worker(env)
    commit = resolve_runtime_commit(env)
    # Shares the already-guarded runtime initialization and the one write coordinator.
    initializer = getattr(server_module, "_get_runtime_components", None)
    if initializer is not None:
        initializer()
    coordinator = require_runtime_coordinator(server_module)
    source = Path(server_module.config["buckets_dir"])
    raw_workspace = env.get("OMBRE_BACKUP_AUTO_WORKSPACE_ROOT", "")
    if not raw_workspace or _path_contains_reparse_point(source) or _path_contains_reparse_point(Path(raw_workspace)):
        raise BackupV2RuntimeConfigError("backup_auto_workspace_invalid")
    source = source.resolve(strict=True)
    workspace_root = _validate_workspace_root(raw_workspace, source)
    # A separate owned workspace is required; never reuse v2 or a recovery workspace.
    old_root = env.get("OMBRE_BACKUP_V2_WORKSPACE_ROOT")
    from backup_v2_runtime import _paths_overlap
    if old_root and _paths_overlap(workspace_root, Path(old_root).resolve(), case_sensitive=os.name != "nt"):
        raise BackupV2RuntimeConfigError("backup_auto_workspace_invalid")
    translated = {k: env.get(k.replace("OMBRE_BACKUP_V2_", "OMBRE_BACKUP_AUTO_"), "")
                  for k in (
                      "OMBRE_BACKUP_V2_FREEZE_TIMEOUT_SECONDS", "OMBRE_BACKUP_V2_MAX_FREEZE_SECONDS",
                      "OMBRE_BACKUP_V2_MAX_SOURCE_BYTES", "OMBRE_BACKUP_V2_MAX_BUNDLE_BYTES",
                      "OMBRE_BACKUP_V2_MINIMUM_FREE_BYTES", "OMBRE_BACKUP_V2_READY_TTL_SECONDS")}
    numbers = {k.removeprefix("OMBRE_BACKUP_V2_").lower(): _parse_bounded_int(k, translated)
               for k in translated}
    if numbers["freeze_timeout_seconds"] >= numbers["max_freeze_seconds"]:
        raise BackupV2RuntimeConfigError("backup_auto_config_invalid")
    limits = CaptureLimits(**numbers)
    signatures = {(method, PREFIX + suffix) for method, suffix in (
        ("GET", "/metadata"), ("POST", "/captures"), ("GET", "/captures/{request_id}"),
        ("GET", "/captures/{request_id}/bundle"), ("POST", "/captures/{request_id}/ack"))}
    existing = _custom_route_signatures(server_module.mcp)
    config = (commit, str(source), str(workspace_root), limits)
    if existing.intersection(signatures):
        previous = vars(server_module).get("_backup_auto_controller")
        if (not signatures.issubset(existing) or previous is None
                or vars(server_module).get("_backup_auto_config") != config):
            raise BackupV2RuntimeConfigError("backup_auto_route_conflict")
        require_runtime_coordinator(server_module, previous)
        return previous
    if workspace_root.exists():
        # Marker from prepare_backup_workspace alone does not prove auto ownership.
        owner = workspace_root / ".ob-auto-v1-owner.json"
        import json
        if owner.is_symlink() or not owner.is_file() or owner.stat().st_size > 1024:
            raise BackupV2RuntimeConfigError("backup_auto_workspace_invalid")
        record = json.loads(owner.read_text(encoding="utf-8"))
        workspace = load_backup_workspace(workspace_root)
        if record != {"format": PLAIN_FORMAT, "workspace_id": workspace.workspace_id, "nonce": workspace.nonce}:
            raise BackupV2RuntimeConfigError("backup_auto_workspace_invalid")
    else:
        workspace = prepare_backup_workspace(workspace_root,
            directory_policy=ProductionDirectoryPolicy(source, workspace_root))
        from offline_backup_bundle import _atomic_write_json
        _atomic_write_json(workspace.root / ".ob-auto-v1-owner.json",
                           {"format": PLAIN_FORMAT, "workspace_id": workspace.workspace_id, "nonce": workspace.nonce})
    policy = ProductionDirectoryPolicy(source, workspace_root)

    def current_commit():
        if vars(server_module).get("_backup_auto_controller") is not controller:
            raise CaptureChannelError("capture_runtime_unavailable")
        try:
            require_runtime_coordinator(server_module, controller, published_only=True)
            return resolve_runtime_commit(env)
        except BackupV2RuntimeConfigError:
            raise CaptureChannelError("capture_runtime_unavailable") from None

    controller = ProductionBackupCaptureController(
        enabled=True, worker_count=1, coordinator=coordinator, source_root=source,
        workspace_root=workspace.root, recipient_public_key=None, recipient_fingerprint="none",
        runtime_commit=commit, limits=limits, oidc_policy=StrictBackupAutoOidcPolicy(),
        directory_policy=policy, plaintext=True, runtime_commit_resolver=current_commit)
    verifier = GitHubActionsBackupV2OidcVerifier(audience=AUTO_AUDIENCE)

    async def verify(request):
        controller._check_runtime_commit()
        claims = await verifier.verify_request(request)
        controller._check_runtime_commit()
        controller.oidc_policy.verify(claims)
        return claims

    async def metadata(request):
        try:
            claims = await verify(request)
            return _json({"format": PLAIN_FORMAT, "runtime_commit": current_commit(),
                          "ready_ttl_seconds": controller.limits.ready_ttl_seconds,
                          **controller.oidc_policy.verify(claims)})
        except Exception as exc:
            return _route_error(exc)

    from starlette.routing import Route
    routes = [Route(PREFIX + "/metadata", metadata, methods=["GET"])]
    routes += build_backup_v2_routes(controller, verify, prefix=PREFIX, plaintext=True)
    for route in routes:
        methods = sorted(route.methods - {"HEAD", "OPTIONS"})
        server_module.mcp.custom_route(route.path, methods=methods, include_in_schema=False)(route.endpoint)
    server_module._backup_auto_controller = controller
    server_module._backup_auto_config = config
    return controller


def install_backup_auto_lifespan(app, server_module):
    controller = vars(server_module).get("_backup_auto_controller")
    if controller is None:
        return
    original = app.router.lifespan_context

    async def cleanup_loop():
        while True:
            try:
                await controller.cleanup_stale()
            except Exception:
                # No paths or exception strings; preserve uncertain material.
                logger.warning("backup-auto cleanup failed; retained materials require review")
            await asyncio.sleep(min(60, controller.limits.ready_ttl_seconds))

    @asynccontextmanager
    async def lifespan(application):
        async with original(application) as state:
            # Restart loses job/task provenance. Count and retain everything unknown.
            unknown = sum(1 for root in (controller.workspace.bundles_root, controller.workspace.temp_root)
                          for _ in root.iterdir())
            if unknown:
                logger.warning("backup-auto unproven materials retained: %d", unknown)
            cleaner = asyncio.create_task(cleanup_loop(), name="backup-auto-ttl")
            try:
                yield state
            finally:
                import anyio
                with anyio.CancelScope(shield=True):
                    cleaner.cancel()
                    await asyncio.gather(cleaner, return_exceptions=True)
                    # HTTP disconnect never cancels capture; shutdown waits for workers/finally.
                    await asyncio.gather(*tuple(controller._tasks.values()), return_exceptions=True)
    app.router.lifespan_context = lifespan

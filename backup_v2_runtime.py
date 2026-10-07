"""Default-disabled backup-v2 production registration."""

from __future__ import annotations

from dataclasses import dataclass
import json
import logging
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
from typing import Any, Mapping


logger = logging.getLogger("ombre_brain.backup_v2")

ENABLE_ENV = "OMBRE_BACKUP_V2_ENABLED"
REQUIRED_ENV = (
    "OMBRE_BACKUP_V2_PUBLIC_KEY_B64",
    "OMBRE_BACKUP_V2_RECIPIENT_FINGERPRINT",
    "OMBRE_BACKUP_V2_REPOSITORY_ID",
    "OMBRE_BACKUP_V2_REPOSITORY_OWNER_ID",
    "OMBRE_BACKUP_V2_WORKSPACE_ROOT",
    "OMBRE_BACKUP_V2_FREEZE_TIMEOUT_SECONDS",
    "OMBRE_BACKUP_V2_MAX_FREEZE_SECONDS",
    "OMBRE_BACKUP_V2_MAX_SOURCE_BYTES",
    "OMBRE_BACKUP_V2_MAX_BUNDLE_BYTES",
    "OMBRE_BACKUP_V2_MINIMUM_FREE_BYTES",
    "OMBRE_BACKUP_V2_READY_TTL_SECONDS",
)
V2_ROUTE_SIGNATURES = frozenset({
    ("POST", "/api/backup/v2/captures"),
    ("GET", "/api/backup/v2/captures/{request_id}"),
    ("GET", "/api/backup/v2/captures/{request_id}/bundle"),
    ("POST", "/api/backup/v2/captures/{request_id}/ack"),
    ("GET", "/api/backup/v2/operator-status/{request_id}"),
})
NUMERIC_BOUNDS = {
    "OMBRE_BACKUP_V2_FREEZE_TIMEOUT_SECONDS": (1, 600),
    "OMBRE_BACKUP_V2_MAX_FREEZE_SECONDS": (2, 1800),
    "OMBRE_BACKUP_V2_MAX_SOURCE_BYTES": (1, 10 * 1024 * 1024 * 1024),
    "OMBRE_BACKUP_V2_MAX_BUNDLE_BYTES": (1, 10 * 1024 * 1024 * 1024),
    "OMBRE_BACKUP_V2_MINIMUM_FREE_BYTES": (1, 10 * 1024 * 1024 * 1024),
    "OMBRE_BACKUP_V2_READY_TTL_SECONDS": (1, 86_400),
}
_GIT_SHA = re.compile(r"[0-9a-f]{40}")
_POSITIVE_ID = re.compile(r"[1-9][0-9]{0,19}")


class BackupV2RuntimeConfigError(RuntimeError):
    """Stable backup-v2 runtime configuration failure."""

    def __init__(self, code: str = "backup_v2_config_invalid") -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class BackupV2RegistrationResult:
    enabled: bool
    registered: bool
    route_count: int = 0


def register_backup_v2_if_enabled(
    server_module: Any,
    transport: str,
    *,
    environ: Mapping[str, str] | None = None,
    log: logging.Logger | None = None,
) -> BackupV2RegistrationResult:
    env = os.environ if environ is None else environ
    active_logger = log or logger
    flag = env.get(ENABLE_ENV)
    if flag in (None, "", "false"):
        active_logger.info("backup-v2 registration disabled")
        return BackupV2RegistrationResult(enabled=False, registered=False)
    if flag != "true":
        raise BackupV2RuntimeConfigError()
    if transport != "streamable-http":
        raise BackupV2RuntimeConfigError("backup_v2_transport_unsupported")

    # Resolve provenance before initialization can write to the source.
    runtime_commit = resolve_runtime_commit(env)
    initializer = getattr(server_module, "_get_runtime_components", None)
    if initializer is not None:
        initializer()
    coordinator = require_runtime_coordinator(server_module)
    config = _parse_enabled_config(server_module, env)
    _require_single_worker(env)
    existing = _custom_route_signatures(server_module.mcp)
    if existing.intersection(V2_ROUTE_SIGNATURES):
        previous = getattr(server_module, "_backup_v2_controller", None)
        if (not V2_ROUTE_SIGNATURES.issubset(existing)
                or previous is None
                or getattr(server_module, "_backup_v2_config", None) != config):
            raise BackupV2RuntimeConfigError("backup_v2_route_conflict")
        require_runtime_coordinator(server_module, previous)
        return BackupV2RegistrationResult(enabled=True, registered=True, route_count=5)

    from backup_v2_oidc import GitHubActionsBackupV2OidcVerifier
    from offline_backup_bundle import ProductionDirectoryPolicy, load_backup_workspace, prepare_backup_workspace
    from production_backup_capture import (
        CaptureLimits,
        ProductionBackupCaptureController,
        StrictBackupV2OidcPolicy,
        build_backup_v2_routes,
        parse_public_key_b64,
        public_key_fingerprint,
    )

    public_key = parse_public_key_b64(config["public_key_b64"])
    fingerprint = public_key_fingerprint(public_key)
    if fingerprint != config["recipient_fingerprint"]:
        raise BackupV2RuntimeConfigError("backup_v2_key_invalid")

    workspace_root = Path(config["workspace_root"])
    directory_policy = ProductionDirectoryPolicy(Path(config["source_root"]), workspace_root)
    if workspace_root.exists():
        workspace = load_backup_workspace(workspace_root, directory_policy=directory_policy)
    else:
        workspace = prepare_backup_workspace(workspace_root, directory_policy=directory_policy)

    policy = StrictBackupV2OidcPolicy(
        expected_repository_id=config["repository_id"],
        expected_repository_owner_id=config["repository_owner_id"],
    )
    limits = CaptureLimits(
        freeze_timeout_seconds=config["freeze_timeout_seconds"],
        max_freeze_seconds=config["max_freeze_seconds"],
        max_source_bytes=config["max_source_bytes"],
        max_bundle_bytes=config["max_bundle_bytes"],
        minimum_free_bytes=config["minimum_free_bytes"],
        ready_ttl_seconds=config["ready_ttl_seconds"],
    )
    class RuntimeCaptureController(ProductionBackupCaptureController):
        def _preflight(self, abort_signal=None):
            require_runtime_coordinator(server_module, self)
            if resolve_runtime_commit(env) != runtime_commit:
                raise BackupV2RuntimeConfigError("backup_v2_commit_conflict")
            return super()._preflight(abort_signal)

    controller = RuntimeCaptureController(
        enabled=True,
        worker_count=1,
        coordinator=coordinator,
        source_root=config["source_root"],
        workspace_root=workspace.root,
        recipient_public_key=public_key,
        recipient_fingerprint=fingerprint,
        runtime_commit=config["runtime_commit"],
        limits=limits,
        oidc_policy=policy,
        directory_policy=directory_policy,
    )
    verifier = GitHubActionsBackupV2OidcVerifier()

    async def verify_runtime_request(request):
        require_runtime_coordinator(server_module, controller)
        if resolve_runtime_commit(env) != runtime_commit:
            raise BackupV2RuntimeConfigError("backup_v2_commit_conflict")
        return await verifier.verify_request(request)

    routes = build_backup_v2_routes(controller, verify_runtime_request)
    routes.append(_build_operator_status_route(server_module, controller, env))
    _register_routes_once(server_module.mcp, routes)
    server_module._backup_v2_controller = controller
    server_module._backup_v2_config = config
    active_logger.info(
        "backup-v2 registration enabled for commit %s fingerprint %s",
        config["runtime_commit"],
        fingerprint,
    )
    return BackupV2RegistrationResult(enabled=True, registered=True, route_count=len(routes))


def _build_operator_status_route(server_module, controller, env):
    from starlette.routing import Route
    from production_backup_capture import CaptureChannelError, _request_id, _json
    from maintenance_write_gate import MaintenanceWriteError

    def validate_registered():
        if vars(server_module).get("_backup_v2_controller") is not controller:
            raise BackupV2RuntimeConfigError("status_unavailable")
        require_runtime_coordinator(server_module, controller, published_only=True)

    async def endpoint(request):
        try:
            # Inject only the running module's purpose-limited auth function.
            denied = server_module._require_backup_v2_status_auth(request)
            if denied is not None:
                return denied
            if request.method != "GET":
                return _json({"status": "method_not_allowed"}, 405)
            request_id = _request_id(request.path_params["request_id"])
            pairs = request.query_params.multi_items()
            if (len(pairs) != 2 or {key for key, value in pairs} !=
                    {"original_run_id", "original_run_attempt"}
                    or any(_POSITIVE_ID.fullmatch(value) is None for key, value in pairs)):
                raise CaptureChannelError("request_invalid")
            identity = dict(pairs)
            validate_registered()
            # Provenance can perform file I/O: always outside synchronous locks.
            if resolve_runtime_commit(env) != controller.runtime_commit:
                raise BackupV2RuntimeConfigError("status_unavailable")
            payload = await controller.operator_status(
                request_id, identity["original_run_id"], identity["original_run_attempt"],
                validate_registered=validate_registered)
            return _json(payload)
        except CaptureChannelError:
            return _json({"status": "request_invalid"}, 400)
        except BackupV2RuntimeConfigError:
            return _json({"status": "status_unavailable"}, 503)
        except MaintenanceWriteError:
            return _json({"status": "snapshot_conflict"}, 409)
        except Exception:
            return _json({"status": "internal_error"}, 500)

    class OperatorStatusRoute(Route):
        async def handle(self, scope, receive, send):
            # Also authenticate/rate-limit rejected verbs and give stable no-store errors.
            # Router still advertises GET/HEAD; endpoint never returns status for other verbs.
            await self.app(scope, receive, send)

    return OperatorStatusRoute("/api/backup/v2/operator-status/{request_id}", endpoint, methods=["GET"])


def _parse_enabled_config(server_module: Any, env: Mapping[str, str]) -> dict[str, Any]:
    missing = [name for name in REQUIRED_ENV if env.get(name) in (None, "")]
    if missing:
        raise BackupV2RuntimeConfigError()
    from offline_backup_bundle import _path_contains_reparse_point
    source_candidate = Path(str(server_module.config["buckets_dir"]))
    workspace_candidate = Path(env["OMBRE_BACKUP_V2_WORKSPACE_ROOT"])
    if (_path_contains_reparse_point(source_candidate)
            or _path_contains_reparse_point(workspace_candidate)):
        raise BackupV2RuntimeConfigError("backup_v2_workspace_invalid")
    source_root = source_candidate.resolve(strict=True)
    workspace_root = _validate_workspace_root(env["OMBRE_BACKUP_V2_WORKSPACE_ROOT"], source_root)
    freeze_timeout = _parse_bounded_int(
        "OMBRE_BACKUP_V2_FREEZE_TIMEOUT_SECONDS", env
    )
    max_freeze = _parse_bounded_int("OMBRE_BACKUP_V2_MAX_FREEZE_SECONDS", env)
    if freeze_timeout >= max_freeze:
        raise BackupV2RuntimeConfigError()
    repository_id = _parse_repository_id(env["OMBRE_BACKUP_V2_REPOSITORY_ID"])
    owner_id = _parse_repository_id(env["OMBRE_BACKUP_V2_REPOSITORY_OWNER_ID"])
    runtime_commit = resolve_runtime_commit(env)
    return {
        "public_key_b64": env["OMBRE_BACKUP_V2_PUBLIC_KEY_B64"],
        "recipient_fingerprint": env["OMBRE_BACKUP_V2_RECIPIENT_FINGERPRINT"],
        "repository_id": repository_id,
        "repository_owner_id": owner_id,
        "workspace_root": str(workspace_root),
        "source_root": str(source_root),
        "freeze_timeout_seconds": freeze_timeout,
        "max_freeze_seconds": max_freeze,
        "max_source_bytes": _parse_bounded_int("OMBRE_BACKUP_V2_MAX_SOURCE_BYTES", env),
        "max_bundle_bytes": _parse_bounded_int("OMBRE_BACKUP_V2_MAX_BUNDLE_BYTES", env),
        "minimum_free_bytes": _parse_bounded_int(
            "OMBRE_BACKUP_V2_MINIMUM_FREE_BYTES", env
        ),
        "ready_ttl_seconds": _parse_bounded_int("OMBRE_BACKUP_V2_READY_TTL_SECONDS", env),
        "runtime_commit": runtime_commit,
    }


def _parse_bounded_int(name: str, env: Mapping[str, str]) -> int:
    value = env.get(name, "")
    if not isinstance(value, str) or value != value.strip():
        raise BackupV2RuntimeConfigError()
    if not re.fullmatch(r"[1-9][0-9]*", value):
        raise BackupV2RuntimeConfigError()
    parsed = int(value)
    lower, upper = NUMERIC_BOUNDS[name]
    if not lower <= parsed <= upper:
        raise BackupV2RuntimeConfigError()
    return parsed


def _parse_repository_id(value: str) -> str:
    if not isinstance(value, str) or _POSITIVE_ID.fullmatch(value) is None:
        raise BackupV2RuntimeConfigError()
    return value


def _require_single_worker(env: Mapping[str, str]) -> None:
    for name in ("WEB_CONCURRENCY", "UVICORN_WORKERS"):
        value = env.get(name)
        if value in (None, "", "1"):
            continue
        raise BackupV2RuntimeConfigError("backup_v2_multi_worker_unsupported")


def _validate_workspace_root(value: str, source_root: Path) -> Path:
    candidate = Path(value)
    if (
        not candidate.is_absolute()
        or any(part == ".." for part in candidate.parts)
        or any(part in ("", ".") for part in candidate.parts[1:])
    ):
        raise BackupV2RuntimeConfigError()
    workspace_root = candidate.resolve(strict=False)
    if _paths_overlap(
        workspace_root,
        source_root,
        case_sensitive=os.name != "nt",
    ):
        raise BackupV2RuntimeConfigError("backup_v2_workspace_invalid")
    return workspace_root


def _paths_overlap(left: Path, right: Path, *, case_sensitive: bool) -> bool:
    left_path = _comparison_path(left, case_sensitive=case_sensitive)
    right_path = _comparison_path(right, case_sensitive=case_sensitive)
    return _is_equal_or_descendant(left_path, right_path) or _is_equal_or_descendant(
        right_path,
        left_path,
    )


def _comparison_path(path: Path, *, case_sensitive: bool) -> Path:
    if case_sensitive:
        return path
    path_text = str(path).casefold()
    if isinstance(path, PureWindowsPath) or os.name == "nt":
        return PureWindowsPath(path_text)  # type: ignore[return-value]
    return PurePosixPath(path_text)  # type: ignore[return-value]


def _is_equal_or_descendant(candidate: Path, ancestor: Path) -> bool:
    try:
        candidate.relative_to(ancestor)
    except ValueError:
        return False
    return True


def _register_routes_once(mcp: Any, routes: list[Any]) -> None:
    existing = _custom_route_signatures(mcp)
    if V2_ROUTE_SIGNATURES.issubset(existing):
        return
    if existing.intersection(V2_ROUTE_SIGNATURES):
        raise BackupV2RuntimeConfigError("backup_v2_route_conflict")
    for route in routes:
        methods = sorted(method for method in route.methods if method not in {"HEAD", "OPTIONS"})
        for method in methods:
            signature = (method, route.path)
            if signature not in V2_ROUTE_SIGNATURES:
                raise BackupV2RuntimeConfigError("backup_v2_route_conflict")
        decorator = mcp.custom_route(
            route.path,
            methods=methods,
            name=getattr(route, "name", None),
            include_in_schema=False,
        )
        decorator(route.endpoint)
        if route.path == "/api/backup/v2/operator-status/{request_id}":
            # FastMCP constructs a plain Route. Preserve this endpoint's rejection handler.
            registered = next(item for item in mcp._custom_starlette_routes
                              if item.path == route.path and item.endpoint is route.endpoint)
            registered.handle = route.handle


def _custom_route_signatures(mcp: Any) -> set[tuple[str, str]]:
    signatures: set[tuple[str, str]] = set()
    for route in getattr(mcp, "_custom_starlette_routes", ()):
        for method in getattr(route, "methods", ()) or ():
            if method not in {"HEAD", "OPTIONS"}:
                signatures.add((method, route.path))
    return signatures


BUILD_METADATA_PATH = Path(__file__).resolve().parent / ".backup-v2-build.json"


def resolve_runtime_commit(env: Mapping[str, str], *, metadata_path: Path | None = None) -> str:
    """Use image build provenance or Render's provider-issued runtime SHA only."""
    path = BUILD_METADATA_PATH if metadata_path is None else metadata_path
    commits = []
    build_valid = False
    if path.exists() or path.is_symlink():
        try:
            if path.is_symlink() or path.stat().st_size > 512:
                raise ValueError()
            record = json.loads(path.read_text(encoding="ascii"))
            if set(record) != {"source", "status", "commit"} or record["source"] != "zeabur-build":
                raise ValueError()
            if record["status"] == "valid" and isinstance(record["commit"], str) and _GIT_SHA.fullmatch(record["commit"]):
                commits.append(record["commit"])
                build_valid = True
            elif record["status"] != "missing" or record["commit"] is not None:
                raise ValueError()
        except (OSError, ValueError, TypeError, KeyError):
            raise BackupV2RuntimeConfigError("backup_v2_commit_invalid") from None
    # On Zeabur an arbitrary RENDER_GIT_COMMIT cannot stand in for build metadata.
    if env.get("ZEABUR_SERVICE_ID") and not build_valid:
        raise BackupV2RuntimeConfigError("backup_v2_commit_missing")
    zeabur_runtime = env.get("ZEABUR_GIT_COMMIT_SHA")
    if zeabur_runtime is not None:
        if not build_valid:
            raise BackupV2RuntimeConfigError("backup_v2_commit_missing")
        if not isinstance(zeabur_runtime, str) or _GIT_SHA.fullmatch(zeabur_runtime) is None:
            raise BackupV2RuntimeConfigError("backup_v2_commit_invalid")
        commits.append(zeabur_runtime)
    render_commit = env.get("RENDER_GIT_COMMIT")
    if render_commit is not None:
        if not isinstance(render_commit, str) or _GIT_SHA.fullmatch(render_commit) is None:
            raise BackupV2RuntimeConfigError("backup_v2_commit_invalid")
        commits.append(render_commit)
    if not commits:
        raise BackupV2RuntimeConfigError("backup_v2_commit_missing")
    if len(set(commits)) != 1:
        raise BackupV2RuntimeConfigError("backup_v2_commit_conflict")
    return commits[0]


def require_runtime_coordinator(server_module: Any, controller: Any = None, *, published_only=False):
    """Reject a partially initialized or split write boundary on every request."""
    from maintenance_write_gate import DEFAULT_WRITE_COORDINATOR
    if published_only:
        from types import SimpleNamespace
        published = vars(server_module)
        if "_runtime_components" in published:
            if published["_runtime_components"] is None:
                raise BackupV2RuntimeConfigError("backup_v2_coordinator_mismatch")
            # Resolve only known lazy proxies from already published components.
            # Explicit module overrides must still undergo identity checks.
            runtime = published["_runtime_components"]
            proxy_type = published.get("_LazyRuntimeComponent")
            published = dict(published)
            for name, component in runtime.items():
                if (name not in published or
                        (isinstance(proxy_type, type) and isinstance(published[name], proxy_type))):
                    published[name] = component
        server_module = SimpleNamespace(**published)
    try:
        components = {
            name: getattr(server_module, name)
            for name in ("bucket_mgr", "asset_store", "embedding_engine",
                         "asset_embedding_index", "dehydrator")
        }
        manager = components["bucket_mgr"]
        coordinator = manager.write_coordinator
        if coordinator is not DEFAULT_WRITE_COORDINATOR:
            raise ValueError()
        components["relations"] = manager.relation_store
        state_store = server_module.asset_backend_registry.state_store
        if state_store is not None:
            components["migration_state"] = state_store
        bundle = getattr(server_module, "remember_me_host_bundle", None)
        if bundle is not None:
            components["remember_me"] = bundle.core_adapter
        if any(item.write_coordinator is not coordinator for item in components.values()):
            raise ValueError()
        if controller is not None and controller.coordinator is not coordinator:
            raise ValueError()
        if getattr(server_module.decay_engine.bucket_mgr, "write_coordinator", None) is not coordinator:
            raise ValueError()
        importer = server_module.import_engine
        for item in (importer.bucket_mgr, importer.dehydrator, importer.embedding_engine):
            if item.write_coordinator is not coordinator:
                raise ValueError()
        return coordinator
    except (AttributeError, ValueError):
        raise BackupV2RuntimeConfigError("backup_v2_coordinator_mismatch") from None

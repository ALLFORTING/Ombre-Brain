# ============================================================
# Module: MCP Server Entry Point (server.py)
# 模块：MCP 服务器主入口
#
# Starts the Ombre Brain MCP service and registers memory
# operation tools for Claude to call.
# 启动 Ombre Brain MCP 服务，注册记忆操作工具供 Claude 调用。
#
# Core responsibilities:
# 核心职责：
#   - Initialize config, bucket manager, dehydrator, decay engine
#     初始化配置、记忆桶管理器、脱水器、衰减引擎
#   - Expose the current MCP tool and resource surface:
#     注册当前 MCP 工具与资源表面：
#       25 default tools; 15 optional diagnostic tools
#                25 个默认工具；15 个可选诊断工具
#       memory/session, Remember-Me, and maintenance operations
#                记忆/会话、Remember-Me 与维护操作
#       one viewer resource; diagnostics via OMBRE_DIAG_TOOLS
#                一个 viewer resource；诊断由 OMBRE_DIAG_TOOLS 控制
#   - Exact inventory: docs/mcp-public-contract.json
#     详细清单：docs/mcp-public-contract.json
#   - User onboarding: README.md and CLAUDE_PROMPT.md
#     用户 onboarding：README.md 与 CLAUDE_PROMPT.md
#   - Runtime behavior remains in the registered handlers below.
#     运行时行为由下方已注册的 handler 保持。
#
# Startup:
# 启动方式：
#   Local:  python server.py
#   Remote: OMBRE_TRANSPORT=streamable-http python server.py
#   Docker: docker-compose up
# ============================================================

import os
import sys
import base64
import binascii
import errno
import io
import random
import logging
import asyncio
import weakref
import copy
import hashlib
import hmac
import html
import secrets
import stat
import time
import threading
import tempfile
import struct
import zlib
import re
import json as _json_lib
import httpx
import frontmatter
from bucket_write_lock import BucketWriteLockError
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfoNotFoundError
from boot_todos import active_display_candidates, todo_page, fit_todos, shanghai_date, TodoPage
from pathlib import Path
from urllib.parse import unquote_plus, urlparse
from typing import Union
from functools import wraps
import inspect
from contextlib import asynccontextmanager, nullcontext
from typing_extensions import Annotated, Literal
from pydantic import Field
from PIL import Image, UnidentifiedImageError


TodoItem = Annotated[
    dict,
    Field(json_schema_extra={
        "properties": {
            "id": {"type": "string", "pattern": "^todo_[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"},
            "text": {"type": "string"},
            "said_by": {"type": "string", "enum": ["ting", "model", "system", "unknown"]},
            "said_at": {"anyOf": [{"type": "string"}, {"type": "null"}]},
            "source_bucket": {"anyOf": [{"type": "string"}, {"type": "null"}]},
        },
        "required": ["text"],
        "additionalProperties": False,
    }),
]


# This identity is process-local evidence only.  It is never used for
# authentication, capability lookup, or durable state.
_RM_PROCESS_BOOT_ID = secrets.token_hex(16)
_RM_PROCESS_STARTED_AT = datetime.now(timezone.utc).isoformat(timespec="seconds")
_BREATH_CURSOR_TTL_SECONDS = 15 * 60
_BREATH_CURSOR_MAX_STATES = 256
_BREATH_CURSOR_STATES: dict[str, dict] = {}
_MUTATION_CONFIRM_TTL_SECONDS = 5 * 60
_MUTATION_CONFIRM_MAX_TOKENS = 256
_DIGEST_SOURCE_LIMIT_PER_GROUP = 20
_mutation_confirm_tokens: dict[str, dict] = {}
_mutation_confirm_lock = threading.Lock()
_confirmed_delete_locks = weakref.WeakValueDictionary()
_merge_running_operations: set[str] = set()
_digest_running_operations: set[str] = set()


# --- Ensure same-directory modules can be imported ---
# --- 确保同目录下的模块能被正确导入 ---
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from mcp.server.fastmcp import Context, FastMCP
from mcp.types import CallToolResult, ImageContent, TextContent

from bucket_manager import (
    BucketManager,
    BucketIdempotencyError,
    SupersessionError,
    automatic_todo_provenance,
    active_todo_projection,
    canonicalize_todos,
    merge_todo_provenance,
    normalize_provenance_kind,
    prepare_todo_provenance,
    reconcile_todo_provenance,
    _merge_todo_terminal_state,
    _todo_is_terminal,
)
from asset_store import (
    MAX_IMAGE_PIXELS as RM_ASSET_MAX_IMAGE_PIXELS,
    AssetStore,
    AssetStoreError,
    InvalidAssetImage,
)
from asset_backend import AssetBackendError, RuntimeAssetBackendRegistry
from asset_dashboard import AssetDashboardError, AssetDashboardService
from asset_embedding_index import AssetEmbeddingIndex
from asset_viewer import (
    ASSET_VIEWER_HTML,
    ASSET_VIEWER_MIME_TYPE,
    ASSET_VIEWER_RESOURCE_META,
    ASSET_VIEWER_TOOL_META,
    ASSET_VIEWER_URI,
)
from dehydrator import AnalysisParseError, Dehydrator
from openai import APIConnectionError, APITimeoutError
from decay_engine import DecayEngine
from bucket_write_lock import bucket_write_scope
from archive_session_operations import (
    ArchiveSessionError, ArchiveSessionOperations, canonical_payload,
    validate_operation_id, short_step, execute_archive_operation,
)
from related_integrity import (RelatedError, parse_related, scan_relation_store,
                               automatic_eligible, digest as related_digest, plan_mutation, mutation_request, read_vectors)
from confirmed_delete_admission import DeleteAdmissionError
from embedding_engine import EmbeddingEngine
from digest_dedupe import run_dedupe_scan
from import_memory import ImportEngine, ImportState
from maintenance_write_gate import (
    MaintenanceWriteError,
    guarded_async_mutation,
    guarded_http_mutation,
    guarded_mutation,
    optional_async_writer_scope,
)
from utils import (
    DISPLAY_ALIASES,
    apply_display_aliases,
    ensure_bucket_storage,
    load_config,
    setup_logging,
    strip_wikilinks,
    count_tokens_approx,
)

# --- Load config & init logging / 加载配置 & 初始化日志 ---
config = load_config()
setup_logging(config.get("log_level", "INFO"))
logger = logging.getLogger("ombre_brain")
_runtime_components: dict[str, object] | None = None
_runtime_components_lock = threading.RLock()
_MISSING_RUNTIME_OVERRIDE = object()

def _apply_display_aliases(text: str) -> str:
    return apply_display_aliases(text)

# --- Runtime env vars (port + webhook) / 运行时环境变量 ---
# OMBRE_PORT: HTTP/SSE 监听端口，默认 8000
try:
    OMBRE_PORT = int(os.environ.get("OMBRE_PORT", "8000") or "8000")
except ValueError:
    logger.warning("OMBRE_PORT 不是合法整数，回退到 8000")
    OMBRE_PORT = 8000

# OMBRE_HOOK_URL: 在 breath/dream 被调用后推送事件到该 URL（POST JSON）。
# OMBRE_HOOK_SKIP: 设为 true/1/yes 跳过推送。
# 详见 ENV_VARS.md。
OMBRE_HOOK_URL = os.environ.get("OMBRE_HOOK_URL", "").strip()
OMBRE_HOOK_SKIP = os.environ.get("OMBRE_HOOK_SKIP", "").strip().lower() in ("1", "true", "yes", "on")


def _response_seal() -> str:
    """Read the verification phrase from runtime env; never persist it."""
    return os.environ.get("OMBRE_RESPONSE_SEAL", "").strip()


def _with_response_seal(text: str) -> str:
    return f"{str(text).rstrip()}\n\nseal: {_response_seal()}"


def _mcp_auth_token() -> str:
    return os.environ.get("OMBRE_AUTH_TOKEN", "").strip()


_ACCESS_LOG_TICKET_PATH_PATTERN = re.compile(
    r"^(/rm/(?:upload|asset-upload|asset-download|vision-download)/)(.+?)(/?)$",
)


def _redact_uvicorn_access_path(path_with_query: str) -> str:
    """Sanitize the log copy of a Uvicorn path; never change ASGI scope.

    Uvicorn logs a quoted, already decoded scope path. Hide the entire
    capability suffix, including malformed requests, independently of status.
    """
    path, separator, query = path_with_query.partition("?")
    path = _ACCESS_LOG_TICKET_PATH_PATTERN.sub(r"\1[redacted]\3", path)
    segments = query.split("&")
    for index, segment in enumerate(segments):
        name, equals, _value = segment.partition("=")
        # Match Starlette's single parse_qsl name decoding; retain literal
        # case-insensitive redaction compatibility without changing auth.
        if equals and unquote_plus(name).casefold() == "token":
            segments[index] = name + equals + "[redacted]"
    return path + separator + "&".join(segments)


class _UvicornAccessTokenRedactionFilter(logging.Filter):
    """Redact path tickets and query tokens without changing request scope."""

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, tuple) and len(args) >= 3 and isinstance(args[2], str):
            redacted_args = list(args)
            redacted_args[2] = _redact_uvicorn_access_path(args[2])
            record.args = tuple(redacted_args)
        return True


def install_uvicorn_access_log_redaction() -> None:
    """Install the idempotent server-side path-ticket/query-token log filter.

    This relies on uvicorn keeping the raw path/query string in record.args[2];
    revisit the filter whenever uvicorn or the access-log formatter changes.
    """
    access_logger = logging.getLogger("uvicorn.access")
    if not any(
        isinstance(item, _UvicornAccessTokenRedactionFilter)
        for item in access_logger.filters
    ):
        access_logger.addFilter(_UvicornAccessTokenRedactionFilter())


def _mcp_session_hash(session_id: bytes | str) -> str:
    raw = session_id if isinstance(session_id, bytes) else session_id.encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:12]


class _MCPRequestDiagnosticMiddleware:
    """Log response headers immediately, without consuming or altering streams."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        path = scope.get("path", "")
        if scope["type"] != "http" or not (path == "/mcp" or path.startswith("/mcp/")):
            await self.app(scope, receive, send)
            return

        session_id = next(
            (value for name, value in scope.get("headers", [])
             if name.lower() == b"mcp-session-id"),
            None,
        )
        has_session = "true" if session_id is not None else "false"
        session_field = (
            f" session_hash={_mcp_session_hash(session_id)}"
            if session_id is not None else ""
        )

        async def diagnostic_send(message):
            if message["type"] == "http.response.start":
                logging.getLogger("ombre_brain.mcp").info(
                    "mcp_request method=%s status=%s has_session=%s%s",
                    scope["method"], message["status"], has_session, session_field,
                )
            await send(message)

        await self.app(scope, receive, diagnostic_send)


class _MCPSDKSessionRedactionFilter(logging.Filter):
    """Sanitize only known MCP 1.29.1 session messages; keep errors intact."""

    _FULL_ID_MESSAGES = {
        "mcp.server.streamable_http_manager": (
            ("Created new transport with session ID: ", ""),
            ("Session ", " idle timeout"),
            ("Session ", " crashed"),
            ("Cleaning up crashed session ", " from active instances."),
        ),
        "mcp.server.streamable_http": (("Terminating session: ", ""),),
        "mcp.server.sse": (
            ("Received invalid session ID: ", ""),
            ("Could not find session for ID: ", ""),
        ),
    }
    _REDACTED_MESSAGES = {
        "mcp.server.streamable_http_manager": (
            "Rejecting request for session %s: credential does not match the one that created the session",
        ),
        "mcp.server.sse": (
            "Rejecting message for session %s: credential does not match",
        ),
    }

    def filter(self, record: logging.LogRecord) -> bool:
        if record.msg in self._REDACTED_MESSAGES.get(record.name, ()):
            # The HTTP manager passes only [:64], so a full-ID hash is impossible.
            prefix, _, suffix = record.msg.partition("%s")
            record.msg = f"{prefix}[redacted]{suffix}"
            record.args = ()
        elif isinstance(record.msg, str) and not record.args:
            for prefix, suffix in self._FULL_ID_MESSAGES.get(record.name, ()):
                if record.msg.startswith(prefix) and record.msg.endswith(suffix):
                    end = len(record.msg) - len(suffix) if suffix else len(record.msg)
                    session_id = record.msg[len(prefix):end]
                    record.msg = f"{prefix}session_hash={_mcp_session_hash(session_id)}{suffix}"
                    break
        return True


def add_mcp_diagnostic_middleware(app):
    """Shared HTTP-entrypoint installation; auth and transport stay unchanged."""
    for name in _MCPSDKSessionRedactionFilter._FULL_ID_MESSAGES:
        sdk_logger = logging.getLogger(name)
        if not any(isinstance(item, _MCPSDKSessionRedactionFilter)
                   for item in sdk_logger.filters):
            sdk_logger.addFilter(_MCPSDKSessionRedactionFilter())
    if not any(item.cls is _MCPRequestDiagnosticMiddleware for item in app.user_middleware):
        app.add_middleware(_MCPRequestDiagnosticMiddleware)
    return app


_HOOK_OBVIOUS_TOKENS = frozenset({
    "changeme",
    "password",
    "token",
    "secret",
    "example",
    "test",
})


def _hook_token() -> str:
    return os.environ.get("OMBRE_HOOK_TOKEN", "")


def _validate_hook_token() -> None:
    token = _hook_token()
    if not token:
        return
    if len(token) < 32 or any(ord(char) < 0x21 or ord(char) > 0x7E for char in token):
        raise RuntimeError("OMBRE_HOOK_TOKEN invalid")
    if token.casefold() in _HOOK_OBVIOUS_TOKENS:
        raise RuntimeError("OMBRE_HOOK_TOKEN invalid")


def _hook_unauthorized_response():
    from starlette.responses import JSONResponse

    return JSONResponse({"error": "hook_unauthorized"}, status_code=401)


def _require_hook_auth(request):
    from starlette.responses import JSONResponse

    expected = _hook_token()
    if not expected:
        return JSONResponse({"error": "hook_not_configured"}, status_code=503)

    authorization = request.headers.get("authorization", "")
    scheme, separator, candidate = authorization.partition(" ")
    if scheme.casefold() != "bearer" or not separator:
        return _hook_unauthorized_response()
    candidate = candidate.strip()
    if not candidate:
        return _hook_unauthorized_response()
    if any(ord(char) > 0x7F for char in candidate):
        return _hook_unauthorized_response()
    try:
        candidate_bytes = candidate.encode("utf-8")
        expected_bytes = expected.encode("utf-8")
        matched = hmac.compare_digest(candidate_bytes, expected_bytes)
    except (UnicodeError, TypeError):
        matched = False
    return None if matched else _hook_unauthorized_response()


_validate_hook_token()


def _constant_time_token_match(candidate: str, expected: str) -> bool:
    if not candidate or not expected:
        return False
    return secrets.compare_digest(
        candidate.encode("utf-8"),
        expected.encode("utf-8"),
    )


def add_mcp_auth_middleware(app):
    """
    Protect only the supported MCP HTTP transport endpoints.
    HTTP MCP is deny-by-default when OMBRE_AUTH_TOKEN is unset. Anonymous access
    requires an explicit OMBRE_MCP_ALLOW_ANONYMOUS_HTTP opt-in. Query-token
    compatibility is separately opt-in and uses only OMBRE_MCP_QUERY_TOKEN.
    """
    expected = _mcp_auth_token()
    query_token = os.environ.get("OMBRE_MCP_QUERY_TOKEN", "").strip()
    query_token_opt_in = _env_flag_enabled(os.environ.get("OMBRE_MCP_ALLOW_QUERY_TOKEN", ""))
    anonymous_opt_in = _env_flag_enabled(os.environ.get("OMBRE_MCP_ALLOW_ANONYMOUS_HTTP", ""))
    if not expected: logger.warning("SECURITY WARNING: anonymous HTTP MCP enabled by OMBRE_MCP_ALLOW_ANONYMOUS_HTTP" if anonymous_opt_in else "OMBRE_AUTH_TOKEN not set; /mcp is disabled")
    if query_token_opt_in: logger.warning("SECURITY WARNING: MCP query-token compatibility enabled; URL/query credentials may be retained by clients, proxies, or access logs")
    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.responses import PlainTextResponse
    class MCPAuthMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            path = request.url.path or ""
            if not _is_mcp_http_path(path):
                return await call_next(request)

            query_auth_configured = query_token_opt_in and bool(query_token)
            if not expected and not query_auth_configured and not anonymous_opt_in: return PlainTextResponse("Unauthorized", status_code=401)
            authorization = request.headers.get("authorization", "")
            bearer = authorization[7:].strip() if authorization.lower().startswith("bearer ") else ""

            if anonymous_opt_in and not expected and not query_auth_configured: return await call_next(request)

            if _constant_time_token_match(bearer, expected): return await call_next(request)

            if query_token_opt_in and _constant_time_token_match(request.query_params.get("token", ""), query_token): return await call_next(request)

            return PlainTextResponse("Unauthorized", status_code=401)

    app.add_middleware(MCPAuthMiddleware)
    logger.info("OMBRE_AUTH_TOKEN set, /mcp authentication enabled" if expected else "HTTP MCP authentication not configured")
    return app



async def _fire_webhook(event: str, payload: dict) -> None:
    """
    Fire-and-forget POST to OMBRE_HOOK_URL with the given event payload.
    Failures are logged at WARNING level only — never propagated to the caller.
    """
    if OMBRE_HOOK_SKIP or not OMBRE_HOOK_URL:
        return
    try:
        body = {
            "event": event,
            "timestamp": time.time(),
            "payload": payload,
        }
        async with httpx.AsyncClient(timeout=5.0) as client:
            await client.post(OMBRE_HOOK_URL, json=body)
    except Exception as e:
        logger.warning(f"Webhook push failed ({event} → {OMBRE_HOOK_URL}): {e}")

# --- Create MCP server instance / 创建 MCP 服务器实例 ---
# host="0.0.0.0" so Docker container's SSE is externally reachable
# stdio mode ignores host (no network)
def _todo_drop_argument_error(arguments: dict) -> str | None:
    """Check explicit drop arguments before SDK defaults are expanded."""
    if arguments.get('operation_id') is not None and 'todo_drop' in arguments:
        return 'unsupported_combination: operation_id supports ordinary single-bucket trace only.'
    # Explicit None has exactly the legacy meaning, including raw presence checks.
    arguments = {key: value for key, value in arguments.items() if key != 'operation_id'}
    if "todo_drop" not in arguments:
        return None
    if set(arguments) - {"bucket_id", "todo_drop", "confirm_token"}:
        return "todo_drop must be called alone with bucket_id, todo_drop, and optional confirm_token."
    if arguments["todo_drop"] is None:
        return "todo_drop requires a stable todo_<uuid> ID. 该 todo 为旧格式，无稳定 ID，当前不能单条放弃。"
    return None


class _TodoDropGuardFastMCP(FastMCP):
    async def call_tool(self, name: str, arguments: dict):
        if name == "trace":
            error = _todo_drop_argument_error(arguments)
            if error:
                return CallToolResult(isError=True, content=[TextContent(type="text", text=error)])
        return await super().call_tool(name, arguments)


def _guard_todo_drop_presence(fn):
    """Direct Python calls retain presence; MCP registers the original fn."""
    signature = inspect.signature(fn)
    @wraps(fn)
    async def guarded(*args, **kwargs):
        supplied = signature.bind_partial(*args, **kwargs).arguments
        error = _todo_drop_argument_error(supplied)
        if error:
            return error
        return await fn(*args, **kwargs)
    return guarded


mcp = _TodoDropGuardFastMCP(
    "Ombre Brain",
    host="0.0.0.0",
    port=OMBRE_PORT,
)


def _backup_v2_initialization_scope():
    # Disabled mode retains the existing lazy initialization behavior.
    if os.environ.get("OMBRE_BACKUP_V2_ENABLED") != "true":
        return nullcontext()
    from maintenance_write_gate import DEFAULT_WRITE_COORDINATOR
    return DEFAULT_WRITE_COORDINATOR.writer_scope("backup_v2_runtime_initialization")


def _register_backup_v2(transport):
    from backup_v2_runtime import register_backup_v2_if_enabled
    # __main__ is the actual running module; never import a second server.
    return register_backup_v2_if_enabled(sys.modules[__name__], transport)


def _get_runtime_components() -> dict[str, object]:
    """Create durable runtime services on first use, never at module import."""
    global _runtime_components
    if _runtime_components is not None:
        return _runtime_components
    with _runtime_components_lock:
        if _runtime_components is not None:
            return _runtime_components
        with _backup_v2_initialization_scope():
            components: dict[str, object] = {}
            ensure_bucket_storage(config)
            store = AssetStore(config["buckets_dir"])
            embedding = EmbeddingEngine(config)
            index = AssetEmbeddingIndex(store, embedding)
            manager = BucketManager(config, embedding_engine=embedding)
            dehydrator_instance = Dehydrator(config)
            decay = DecayEngine(config, manager)
            importer = ImportEngine(config, manager, dehydrator_instance, embedding)
            components.update({
                "asset_store": store,
                "embedding_engine": embedding,
                "asset_embedding_index": index,
                "bucket_mgr": manager,
                "dehydrator": dehydrator_instance,
                "decay_engine": decay,
                "import_engine": importer,
            })
            bundle = _bootstrap_remember_me_host(store, embedding)
            components["remember_me_host_bundle"] = bundle
            registry = RuntimeAssetBackendRegistry.from_runtime(
                legacy_store=store,
                bundle_provider=lambda: components["remember_me_host_bundle"],
                embedding_index=index,
            )
            components["asset_backend_registry"] = registry
            components["asset_dashboard"] = AssetDashboardService(
                store,
                backend_provider=lambda: registry.selected_backend(),
                max_asset_bytes=RM_ASSET_MAX_UPLOAD_BYTES,
                max_image_pixels=RM_ASSET_MAX_IMAGE_PIXELS,
            )
            _runtime_components = components
            return components


def _get_runtime_component(name: str):
    return _get_runtime_components()[name]


def _get_remember_me_host_bundle():
    override = globals().get("remember_me_host_bundle", _MISSING_RUNTIME_OVERRIDE)
    if override is not _MISSING_RUNTIME_OVERRIDE:
        return override
    if _runtime_components is None and not _env_flag_enabled(
        os.environ.get("OMBRE_RM_RUNTIME_ENABLED", "")
    ):
        return None
    return _get_runtime_component("remember_me_host_bundle")


def __getattr__(name: str):
    if name == "remember_me_host_bundle":
        return _get_remember_me_host_bundle()
    raise AttributeError(name)


class _LazyRuntimeComponent:
    """Preserve module-level component access without import-time storage IO."""

    __slots__ = ("_name",)

    def __init__(self, name: str) -> None:
        object.__setattr__(self, "_name", name)

    def __getattr__(self, attribute: str):
        return getattr(_get_runtime_component(self._name), attribute)

    def __setattr__(self, attribute: str, value) -> None:
        if attribute == "_name":
            object.__setattr__(self, attribute, value)
            return
        setattr(_get_runtime_component(self._name), attribute, value)

    def __delattr__(self, attribute: str) -> None:
        delattr(_get_runtime_component(self._name), attribute)


asset_store = _LazyRuntimeComponent("asset_store")
embedding_engine = _LazyRuntimeComponent("embedding_engine")
asset_embedding_index = _LazyRuntimeComponent("asset_embedding_index")
bucket_mgr = _LazyRuntimeComponent("bucket_mgr")
dehydrator = _LazyRuntimeComponent("dehydrator")
decay_engine = _LazyRuntimeComponent("decay_engine")
import_engine = _LazyRuntimeComponent("import_engine")
asset_dashboard = _LazyRuntimeComponent("asset_dashboard")
asset_backend_registry = _LazyRuntimeComponent("asset_backend_registry")

DIAGNOSTIC_TOOL_NAMES = frozenset({
    "asset_attachment_context_probe",
    "asset_ingest_probe",
    "asset_ingest_begin",
    "asset_ingest_chunk",
    "asset_ingest_finish",
    "asset_ingest_abort",
    "asset_browser_upload_link",
    "asset_browser_upload_status",
    "asset_render_probe",
    "asset_export_probe",
    "asset_vision_challenge",
    "asset_vision_verify",
    "asset_vision_export",
    "asset_vision_download_link",
    "asset_vision_upload_challenge",
})


def build_streamable_http_app():
    """Build once at startup; MCP 1.29.1 reads this setting, not a method kwarg.

    The OB flag controls only this HTTP builder. SSE and stdio do not use it.
    Authentication, CORS and diagnostics are installed by the entrypoints.
    """
    mcp.settings.stateless_http = _env_flag_enabled(
        os.getenv("OMBRE_MCP_STATELESS_HTTP", "false")
    )
    app = mcp.streamable_http_app()
    original_lifespan = app.router.lifespan_context
    @asynccontextmanager
    async def lifespan(application):
        async with original_lifespan(application) as state:
            try:
                yield state
            finally:
                # Drain while the SDK and provider services are still available.
                # Forced loop/process termination is outside this guarantee.
                import anyio
                with anyio.CancelScope(shield=True):
                    pending = [task for task in _LEGACY_POST_EFFECT_TASKS
                               if task.get_loop() is asyncio.get_running_loop()]
                    if pending:
                        await asyncio.gather(*pending, return_exceptions=True)
    app.router.lifespan_context = lifespan
    return app


def _env_flag_enabled(value: str) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _conflict_detection_enabled() -> bool:
    """Keep conflict detection independently switchable and default-on."""
    return _env_flag_enabled(
        os.environ.get("OMBRE_CONFLICT_DETECTION_ENABLED", "true")
    )


DIAGNOSTIC_TOOLS_ENABLED = _env_flag_enabled(
    os.environ.get("OMBRE_DIAG_TOOLS", "")
)
logger.info(
    "diagnostic tools enabled"
    if DIAGNOSTIC_TOOLS_ENABLED
    else "diagnostic tools disabled"
)


def diagnostic_tool(*args, **kwargs):
    """Register a diagnostic MCP tool only when its server profile is enabled."""
    if DIAGNOSTIC_TOOLS_ENABLED:
        return mcp.tool(*args, **kwargs)

    def preserve_function(function):
        return function

    return preserve_function


# =============================================================
# Dashboard Auth — simple cookie-based session auth
# Dashboard 认证 —— 基于 Cookie 的会话认证
#
# Env var OMBRE_DASHBOARD_PASSWORD overrides file-stored password.
# First visit with no password set → forced setup wizard.
# Sessions stored in memory (lost on restart, 7-day expiry).
# =============================================================
_sessions: dict[str, dict] = {}  # {token: {expires_at, csrf_token}}
_setup_token: str | None = None
_setup_lock = asyncio.Lock()

_AUTH_STORE_MISSING = "missing"
_AUTH_STORE_VALID = "valid"
_AUTH_STORE_CORRUPT = "corrupt"
_AUTH_STORE_UNREADABLE = "unreadable"


def _get_auth_file() -> str:
    return os.path.join(config["buckets_dir"], ".dashboard_auth.json")


def _log_auth_store_error(state: str) -> None:
    logger.error("auth_store_%s", state)


def _valid_password_hash(value) -> bool:
    if not isinstance(value, str):
        return False
    salt, separator, digest = value.partition(":")
    if not separator or len(salt) != 32 or len(digest) != 64:
        return False
    return all(char in "0123456789abcdefABCDEF" for char in salt + digest)


def _auth_store_state() -> tuple[str, str | None]:
    """Return the auth file state without treating abnormal nodes as missing."""
    auth_file = _get_auth_file()
    try:
        try:
            file_stat = os.lstat(auth_file)
        except FileNotFoundError:
            try:
                has_node = os.path.lexists(auth_file)
            except OSError:
                _log_auth_store_error(_AUTH_STORE_UNREADABLE)
                return _AUTH_STORE_UNREADABLE, None
            if not has_node:
                return _AUTH_STORE_MISSING, None
            _log_auth_store_error(_AUTH_STORE_UNREADABLE)
            return _AUTH_STORE_UNREADABLE, None
    except OSError:
        _log_auth_store_error(_AUTH_STORE_UNREADABLE)
        return _AUTH_STORE_UNREADABLE, None

    reparse_point = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    if (
        stat.S_ISLNK(file_stat.st_mode)
        or not stat.S_ISREG(file_stat.st_mode)
        or (
            reparse_point
            and getattr(file_stat, "st_file_attributes", 0) & reparse_point
        )
    ):
        _log_auth_store_error(_AUTH_STORE_UNREADABLE)
        return _AUTH_STORE_UNREADABLE, None

    try:
        with open(auth_file, "r", encoding="utf-8") as handle:
            payload = _json_lib.load(handle)
    except (OSError, UnicodeError):
        _log_auth_store_error(_AUTH_STORE_UNREADABLE)
        return _AUTH_STORE_UNREADABLE, None
    except (_json_lib.JSONDecodeError, TypeError, ValueError):
        _log_auth_store_error(_AUTH_STORE_CORRUPT)
        return _AUTH_STORE_CORRUPT, None

    if not isinstance(payload, dict):
        _log_auth_store_error(_AUTH_STORE_CORRUPT)
        return _AUTH_STORE_CORRUPT, None
    stored = payload.get("password_hash")
    if not _valid_password_hash(stored):
        _log_auth_store_error(_AUTH_STORE_CORRUPT)
        return _AUTH_STORE_CORRUPT, None
    return _AUTH_STORE_VALID, stored


def _initialize_setup_token() -> None:
    global _setup_token
    state, _stored = _auth_store_state()
    if state == _AUTH_STORE_MISSING:
        _setup_token = (os.environ.get("OMBRE_DASHBOARD_SETUP_TOKEN") or "").strip() or None
        # Setup credentials are operator-provided and are never logged.


_initialize_setup_token()


def _load_password_hash() -> str | None:
    state, stored = _auth_store_state()
    return stored if state == _AUTH_STORE_VALID else None


def _fsync_directory(path: str) -> None:
    """Flush directory metadata where the platform exposes directory fsync."""
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_write_auth_payload(payload: dict) -> None:
    auth_file = _get_auth_file()
    parent = os.path.dirname(auth_file) or "."
    os.makedirs(parent, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=".dashboard_auth.",
        suffix=".tmp",
        dir=parent,
    )
    try:
        os.chmod(temporary, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            descriptor = None
            _json_lib.dump(payload, handle, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, auth_file)
        _fsync_directory(parent)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if os.path.lexists(temporary):
            os.unlink(temporary)
            _fsync_directory(parent)


def _password_hash_record(password: str) -> str:
    salt = secrets.token_hex(16)
    h = hashlib.sha256(f"{salt}:{password}".encode("utf-8")).hexdigest()
    return f"{salt}:{h}"


@guarded_mutation("dashboard_auth_write")
def _save_password_hash(password: str) -> None:
    _atomic_write_auth_payload({"password_hash": _password_hash_record(password)})


_AUTH_PUBLISH_CREATED = "created"
_AUTH_PUBLISH_EXISTS = "exists"
_LINK_COMPAT_ERRNOS = frozenset(
    errno_value
    for errno_value in (
        getattr(errno, "EOPNOTSUPP", None),
        getattr(errno, "ENOTSUP", None),
        getattr(errno, "EXDEV", None),  # cross-device link: use conservative fallback
        # EINVAL is intentionally not treated as link incompatibility.
        getattr(errno, "ENOSYS", None),
    )
    if errno_value is not None
)


def _write_fd_bytes(descriptor: int, payload: bytes) -> None:
    offset = 0
    while offset < len(payload):
        offset += os.write(descriptor, payload[offset:])


def _publish_auth_payload_exclusive(
    auth_file: str, parent: str, payload: bytes
) -> str:
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    try:
        descriptor = os.open(auth_file, flags, 0o600)
    except FileExistsError:
        return _AUTH_PUBLISH_EXISTS
    except OSError as exc:
        if exc.errno == errno.EEXIST:
            return _AUTH_PUBLISH_EXISTS
        raise

    try:
        os.chmod(auth_file, 0o600)
        _write_fd_bytes(descriptor, payload)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    _fsync_directory(parent)
    return _AUTH_PUBLISH_CREATED


def _create_auth_file_if_absent(password_hash: str) -> str:
    """Publish the initial auth file without ever replacing an existing target."""
    auth_file = _get_auth_file()
    parent = os.path.dirname(auth_file) or "."
    os.makedirs(parent, exist_ok=True)
    payload = _json_lib.dumps(
        {"password_hash": password_hash}, ensure_ascii=False
    ).encode("utf-8")
    descriptor, temporary = tempfile.mkstemp(
        prefix=".dashboard_auth.setup.",
        suffix=".tmp",
        dir=parent,
    )
    try:
        os.chmod(temporary, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = None
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())

        try:
            os.link(temporary, auth_file)
        except FileExistsError:
            return _AUTH_PUBLISH_EXISTS
        except OSError as exc:
            if exc.errno == errno.EEXIST:
                return _AUTH_PUBLISH_EXISTS
            if exc.errno not in _LINK_COMPAT_ERRNOS:
                raise
            return _publish_auth_payload_exclusive(auth_file, parent, payload)

        _fsync_directory(parent)
        os.unlink(temporary)
        _fsync_directory(parent)
        return _AUTH_PUBLISH_CREATED
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if os.path.lexists(temporary):
            os.unlink(temporary)
            _fsync_directory(parent)


def _verify_password_hash(password: str, stored: str) -> bool:
    if not isinstance(password, str) or not _valid_password_hash(stored):
        return False
    salt, h = stored.split(":", 1)
    try:
        actual = hashlib.sha256(f"{salt}:{password}".encode("utf-8")).hexdigest()
        return hmac.compare_digest(h.encode("ascii"), actual.encode("ascii"))
    except (UnicodeError, TypeError):
        return False


def _is_setup_needed() -> bool:
    """Only a truly missing auth file permits setup."""
    state, _stored = _auth_store_state()
    return state == _AUTH_STORE_MISSING


def _verify_any_password(password: str) -> bool:
    """Check password against env var (first) or stored hash."""
    if not isinstance(password, str):
        return False
    env_pwd = os.environ.get("OMBRE_DASHBOARD_PASSWORD", "")
    if env_pwd:
        try:
            return hmac.compare_digest(
                password.encode("utf-8"), env_pwd.encode("utf-8")
            )
        except (UnicodeError, TypeError):
            return False
    stored = _load_password_hash()
    if not stored:
        return False
    return _verify_password_hash(password, stored)


def _create_session() -> str:
    token = secrets.token_urlsafe(32)
    _sessions[token] = {
        "expires_at": time.time() + 86400 * 7,
        "csrf_token": secrets.token_urlsafe(32),
    }
    return token


def _session_data(request) -> dict | None:
    token = request.cookies.get("ombre_session")
    if not token:
        return None
    session = _sessions.get(token)
    if not session or time.time() > session["expires_at"]:
        _sessions.pop(token, None)
        return None
    return session


def _is_authenticated(request) -> bool:
    return _session_data(request) is not None


def _normalize_origin(value: str) -> str | None:
    value = (value or "").strip()
    if not value or "," in value:
        return None
    try:
        parsed = urlparse(value)
        port = parsed.port
    except ValueError:
        return None
    scheme = parsed.scheme.casefold()
    hostname = (parsed.hostname or "").casefold()
    if (
        scheme not in {"http", "https"}
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.params
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        return None
    if any(char.isspace() or ord(char) < 32 for char in hostname):
        return None
    host = f"[{hostname}]" if ":" in hostname else hostname
    if port is not None and port != (443 if scheme == "https" else 80):
        host = f"{host}:{port}"
    return f"{scheme}://{host}"


def _single_forwarded_header(request, name: str) -> tuple[bool, str | None]:
    raw = request.headers.get(name)
    if raw is None:
        return False, None
    value = raw.strip()
    if not value or "," in value or any(char.isspace() for char in value):
        return True, None
    return True, value


def _dashboard_external_origin(request) -> str | None:
    """Resolve the external origin after a trusted proxy emits one header value.

    Deployments must strip client-supplied X-Forwarded-* headers before adding
    their own values. Empty or comma-separated proxy chains are rejected here.
    """
    proto_present, forwarded_proto = _single_forwarded_header(
        request, "x-forwarded-proto"
    )
    host_present, forwarded_host = _single_forwarded_header(
        request, "x-forwarded-host"
    )
    if (proto_present and forwarded_proto is None) or (
        host_present and forwarded_host is None
    ):
        return None

    scheme = forwarded_proto.casefold() if proto_present else request.url.scheme
    if scheme not in {"http", "https"}:
        return None
    host = forwarded_host if host_present else request.headers.get("host", "")
    if not host or "," in host:
        return None
    return _normalize_origin(f"{scheme}://{host}")


def _dashboard_write_error(route: str, status_code: int, code: str):
    from starlette.responses import JSONResponse

    logger.warning(
        "Dashboard write rejected route=%s status=%d code=%s",
        route,
        status_code,
        code,
    )
    return JSONResponse({"error": code}, status_code=status_code)


def _require_same_origin(request, route: str):
    origin = _normalize_origin(request.headers.get("origin", ""))
    expected_origin = _dashboard_external_origin(request)
    if origin is None or expected_origin is None or origin != expected_origin:
        return _dashboard_write_error(route, 403, "same_origin_required")
    return None


def _require_dashboard_write(request, route: str):
    session = _session_data(request)
    if session is None:
        logger.warning(
            "Dashboard write rejected route=%s status=401 code=unauthorized",
            route,
        )
        return _require_auth(request)
    supplied = request.headers.get("x-ombre-csrf", "")
    try:
        csrf_valid = bool(supplied) and hmac.compare_digest(
            supplied.encode("utf-8"), session["csrf_token"].encode("utf-8")
        )
    except (UnicodeError, TypeError, AttributeError):
        csrf_valid = False
    if not csrf_valid:
        return _dashboard_write_error(route, 403, "csrf_required")
    origin = _normalize_origin(request.headers.get("origin", ""))
    expected_origin = _dashboard_external_origin(request)
    if origin is None or expected_origin is None or origin != expected_origin:
        return _dashboard_write_error(route, 403, "same_origin_required")
    return None


def _require_auth(request):
    """Return JSONResponse(401) if not authenticated, else None."""
    from starlette.responses import JSONResponse
    if not _is_authenticated(request):
        return JSONResponse(
            {"error": "Unauthorized", "setup_needed": _is_setup_needed()},
            status_code=401,
        )
    return None


# --- Auth endpoints ---
@mcp.custom_route("/auth/status", methods=["GET"])
async def auth_status(request):
    """Return auth state plus a session-bound CSRF token when authenticated."""
    from starlette.responses import JSONResponse
    session = _session_data(request)
    return JSONResponse({
        "authenticated": session is not None,
        "setup_needed": _is_setup_needed(),
        "csrf_token": session["csrf_token"] if session else "",
    })


async def _auth_setup_impl(request):
    from starlette.responses import JSONResponse
    global _setup_token

    err = _require_same_origin(request, "/auth/setup")
    if err:
        return err

    async with _setup_lock:
        auth_state, _stored = _auth_store_state()
        if auth_state in {_AUTH_STORE_CORRUPT, _AUTH_STORE_UNREADABLE}:
            return JSONResponse({"error": "auth_store_unreadable"}, status_code=503)
        if auth_state != _AUTH_STORE_MISSING:
            return JSONResponse({"error": "Already configured"}, status_code=400)

        supplied_token = request.headers.get("x-ombre-setup-token", "")
        expected_token = _setup_token
        try:
            token_valid = (
                isinstance(supplied_token, str)
                and isinstance(expected_token, str)
                and hmac.compare_digest(
                    supplied_token.encode("utf-8"), expected_token.encode("utf-8")
                )
            )
        except (UnicodeError, TypeError, AttributeError):
            token_valid = False
        if not token_valid:
            return JSONResponse({"error": "setup_token_invalid" if _setup_token is not None else "setup_token_not_configured"}, status_code=403 if _setup_token is not None else 503)

        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "Invalid JSON"}, status_code=400)
        if not isinstance(body, dict):
            return JSONResponse({"error": "password_invalid"}, status_code=400)
        password = body.get("password")
        if not isinstance(password, str):
            return JSONResponse({"error": "password_invalid"}, status_code=400)
        if password != password.strip():
            return JSONResponse({"error": "password_whitespace"}, status_code=400)
        if len(password) < 6:
            return JSONResponse({"error": "password_too_short"}, status_code=400)

        try:
            publish_result = _create_auth_file_if_absent(
                _password_hash_record(password)
            )
        except Exception:
            logger.error("dashboard_auth_setup_write_failed")
            return JSONResponse({"error": "setup_failed"}, status_code=500)
        if publish_result == _AUTH_PUBLISH_EXISTS:
            return JSONResponse({"error": "setup_conflict"}, status_code=409)

        _setup_token = None

    try:
        token = _create_session()
    except Exception:
        logger.error("dashboard_auth_setup_session_failed")
        return JSONResponse(
            {"error": "setup_completed_login_required"}, status_code=500
        )
    resp = JSONResponse({"ok": True})
    resp.set_cookie(
        "ombre_session", token, httponly=True, samesite="lax", max_age=86400 * 7, secure=(_dashboard_external_origin(request) or "").startswith("https://")
    )
    return resp


@mcp.custom_route("/auth/setup", methods=["POST"])
@guarded_http_mutation("dashboard_auth_setup", methods=("POST",))
async def auth_setup_endpoint(request):
    return await _auth_setup_impl(request)


@mcp.custom_route("/auth/login", methods=["POST"])
async def auth_login(request):
    """Login with password."""
    from starlette.responses import JSONResponse
    err = _require_same_origin(request, "/auth/login")
    if err:
        return err
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"error": "password_invalid"}, status_code=400)
    password = body.get("password", "")
    if not isinstance(password, str):
        return JSONResponse({"error": "password_invalid"}, status_code=400)
    if _verify_any_password(password):
        token = _create_session()
        resp = JSONResponse({"ok": True})
        resp.set_cookie("ombre_session", token, httponly=True, samesite="lax", max_age=86400 * 7, secure=(_dashboard_external_origin(request) or "").startswith("https://"))
        return resp
    return JSONResponse({"error": "密码错误"}, status_code=401)


@mcp.custom_route("/auth/logout", methods=["POST"])
async def auth_logout(request):
    """Invalidate session."""
    from starlette.responses import JSONResponse
    token = request.cookies.get("ombre_session")
    if token:
        _sessions.pop(token, None)
    resp = JSONResponse({"ok": True})
    resp.delete_cookie("ombre_session")
    return resp


@mcp.custom_route("/auth/change-password", methods=["POST"])
@guarded_http_mutation("dashboard_auth_change", methods=("POST",))
async def auth_change_password(request):
    """Change dashboard password (requires current password)."""
    from starlette.responses import JSONResponse
    err = _require_dashboard_write(request, "/auth/change-password")
    if err:
        return err
    auth_state, _stored = _auth_store_state()
    env_password = os.environ.get("OMBRE_DASHBOARD_PASSWORD", "")
    if env_password and auth_state not in {
        _AUTH_STORE_CORRUPT,
        _AUTH_STORE_UNREADABLE,
    }:
        return JSONResponse({"error": "当前使用环境变量密码，请直接修改 OMBRE_DASHBOARD_PASSWORD"}, status_code=400)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)
    current = body.get("current", "")
    new_pwd = body.get("new", "")
    if not isinstance(current, str) or not isinstance(new_pwd, str):
        return JSONResponse({"error": "密码必须是字符串"}, status_code=400)
    if not _verify_any_password(current):
        return JSONResponse({"error": "当前密码错误"}, status_code=401)
    if len(new_pwd) < 6:
        return JSONResponse({"error": "新密码不能少于6位"}, status_code=400)
    _save_password_hash(new_pwd)
    if env_password and auth_state in {
        _AUTH_STORE_CORRUPT,
        _AUTH_STORE_UNREADABLE,
    }:
        logger.info("auth_store_recovered state=%s", auth_state)
    _sessions.clear()
    token = _create_session()
    resp = JSONResponse({"ok": True})
    resp.set_cookie("ombre_session", token, httponly=True, samesite="lax", max_age=86400 * 7, secure=(_dashboard_external_origin(request) or "").startswith("https://"))
    return resp


# =============================================================
# /health endpoint: lightweight keepalive
# 轻量保活接口
# For Cloudflare Tunnel or reverse proxy to ping, preventing idle timeout
# 供 Cloudflare Tunnel 或反代定期 ping，防止空闲超时断连
# =============================================================
@mcp.custom_route("/", methods=["GET"])
async def root_redirect(request):
    from starlette.responses import RedirectResponse
    return RedirectResponse(url="/dashboard")


@mcp.custom_route("/health", methods=["GET"])
async def health_check(request):
    from starlette.responses import JSONResponse
    try:
        await decay_engine.ensure_started()
        stats = await bucket_mgr.get_stats()
        return JSONResponse({
            "status": "ok",
            "buckets": stats["permanent_count"] + stats["dynamic_count"],
            "decay_engine": "running" if decay_engine.is_running else "stopped",
        })
    except Exception: logger.exception("Health check failed"); return JSONResponse({"status": "error", "detail": "service_unavailable"}, status_code=500)
# HTTP error response hardening keeps this status surface bounded.


# =============================================================
# /breath-hook endpoint: Dedicated hook for SessionStart
# 会话启动专用挂载点
# =============================================================
@mcp.custom_route("/breath-hook", methods=["GET"])
async def breath_hook(request):
    from starlette.responses import PlainTextResponse
    auth_error = _require_hook_auth(request)
    if auth_error:
        return auth_error
    try:
        all_buckets = await bucket_mgr.list_all(include_archive=False)
        # pinned
        pinned = [
            b for b in all_buckets
            if (b["metadata"].get("pinned") or b["metadata"].get("protected"))
            and not _is_sealed(b)
        ]
        # top 2 unresolved by score
        unresolved = [b for b in all_buckets
                      if not b["metadata"].get("resolved", False)
                      and b["metadata"].get("type") not in ("permanent", "feel")
                      and not b["metadata"].get("dormant", False)
                      and not b["metadata"].get("pinned")
                      and not b["metadata"].get("protected")
                      and not _is_sealed(b)]
        scored = sorted(unresolved, key=lambda b: decay_engine.calculate_score(b["metadata"]), reverse=True)

        parts = []
        touch_ids = []
        token_budget = 10000
        for b in pinned:
            summary = await dehydrator.dehydrate(strip_wikilinks(b["content"]), {k: v for k, v in b["metadata"].items() if k != "tags"})
            marker = "📌 " if b["metadata"].get("pinned", False) else ""
            parts.append(f"{marker}[核心准则] {summary}")
            token_budget -= count_tokens_approx(summary)

        # Diversity: top-1 fixed + shuffle rest from top-20
        candidates = list(scored)
        if len(candidates) > 1:
            top1 = [candidates[0]]
            pool = candidates[1:min(20, len(candidates))]
            random.shuffle(pool)
            candidates = top1 + pool + candidates[min(20, len(candidates)):]
        # Hard cap: max 20 surfacing buckets in hook
        candidates = candidates[:20]

        for b in candidates:
            if token_budget <= 0:
                break
            summary = await dehydrator.dehydrate(strip_wikilinks(b["content"]), {k: v for k, v in b["metadata"].items() if k != "tags"})
            summary_tokens = count_tokens_approx(summary)
            if summary_tokens > token_budget:
                break
            parts.append(summary)
            touch_ids.append(b["id"])
            token_budget -= summary_tokens
        if not parts:
            await _fire_webhook("breath_hook", {"surfaced": 0})
            return PlainTextResponse("")
        body_text = "[Ombre Brain - 记忆浮现]\n" + "\n---\n".join(parts)
        if touch_ids:
            async with optional_async_writer_scope("breath_hook_touch") as entered:
                if entered:
                    for bucket_id in touch_ids:
                        await bucket_mgr.touch(bucket_id)
        await _fire_webhook("breath_hook", {"surfaced": len(parts), "chars": len(body_text)})
        return PlainTextResponse(body_text)
    except Exception as e:
        logger.warning(f"Breath hook failed: {e}")
        return PlainTextResponse("")


# =============================================================
# /dream-hook endpoint: Dedicated hook for Dreaming
# Dreaming 专用挂载点
# =============================================================
@mcp.custom_route("/dream-hook", methods=["GET"])
async def dream_hook(request):
    from starlette.responses import PlainTextResponse
    auth_error = _require_hook_auth(request)
    if auth_error:
        return auth_error
    try:
        all_buckets = await bucket_mgr.list_all(include_archive=False)
        candidates = [
            b for b in all_buckets
            if b["metadata"].get("type") not in ("permanent", "feel")
            and not b["metadata"].get("dormant", False)
            and not b["metadata"].get("pinned", False)
            and not b["metadata"].get("protected", False)
            and not _is_sealed(b)
        ]
        candidates.sort(key=lambda b: b["metadata"].get("created", ""), reverse=True)
        recent = candidates[:10]

        if not recent:
            return PlainTextResponse("")

        parts = []
        for b in recent:
            meta = b["metadata"]
            resolved_tag = "[已解决]" if meta.get("resolved", False) else "[未解决]"
            parts.append(
                f"{meta.get('name', b['id'])} {resolved_tag} "
                f"V{meta.get('valence', 0.5):.1f}/A{meta.get('arousal', 0.3):.1f}\n"
                f"{strip_wikilinks(b['content'][:200])}"
            )

        body_text = "[Ombre Brain - Dreaming]\n" + "\n---\n".join(parts)
        async with optional_async_writer_scope("dream_hook_touch") as entered:
            if entered:
                for bucket_id in (b["id"] for b in recent):
                    await bucket_mgr.touch(bucket_id)
        await _fire_webhook("dream_hook", {"surfaced": len(parts), "chars": len(body_text)})
        return PlainTextResponse(body_text)
    except Exception as e:
        logger.warning(f"Dream hook failed: {e}")
        return PlainTextResponse("")


# =============================================================
# Internal helper: deduplicate-or-create
# 内部辅助：仅复用确定性重复，否则新建
# Shared by hold and grow to avoid duplicate logic
# hold 和 grow 共用，避免重复逻辑
# =============================================================
async def _merge_or_create(
    content: str,
    tags: list,
    importance: int,
    domain: list,
    valence: float,
    arousal: float,
    name: str = "",
    trigger_date: str = "",
    todos: list | None = None,
    provenance_kind: str | None = None,
    source_id_out: list[str] | None = None,
    outcome_out: dict | None = None,
) -> tuple[str, bool]:
    """
    Reuse a deterministic duplicate if found; otherwise create a new bucket.
    Returns (bucket_id_or_name, is_merged), where True means an existing record
    was reused.
    仅复用确定性重复，否则新建；返回 (桶ID或名称, 是否复用已有记录)。
    """
    incoming_todos = list(dict.fromkeys(
        _apply_display_aliases(text) for text in _canonical_todos(todos)
    ))
    incoming_provenance = automatic_todo_provenance(incoming_todos)
    try:
        existing = await bucket_mgr.search(content, limit=1, domain_filter=domain or None, include_sealed=False)
    except Exception as e:
        logger.warning(f"Search for merge failed, creating new / 合并搜索失败，新建: {e}")
        existing = []

    if existing:
        bucket = existing[0]
        metadata = bucket.get("metadata", {})
        normalized_existing = BucketManager._normalize_search_text(bucket.get("content", ""))
        normalized_content = BucketManager._normalize_search_text(content)
        if (
            not _is_sealed(bucket)
            and metadata.get("type") != "feel"
            and normalized_existing == normalized_content
            and not (metadata.get("pinned") or metadata.get("protected"))
        ):
            # Deterministic duplicate only: reuse the existing record without
            # invoking the lossy LLM merge path.
            writes = {}
            if trigger_date:
                old_date = str(metadata.get("trigger_date", "") or "").strip()
                if old_date and old_date != trigger_date:
                    raise ValueError(
                        f"duplicate bucket {bucket['id']} has trigger_date={old_date}; "
                        f"requested {trigger_date} was rejected without writing"
                    )
                if old_date != trigger_date:
                    writes.update(trigger_date=trigger_date, trigger_last_seen="")
            if incoming_todos:
                existing_todos = _canonical_todos(metadata.get("todos"))
                merged_todos, merged_provenance = merge_todo_provenance(
                    existing_todos, metadata.get("todo_provenance"),
                    incoming_todos, incoming_provenance,
                )
                if (
                    merged_todos != existing_todos
                    or not isinstance(metadata.get("todos"), list)
                ):
                    writes["todos"] = merged_todos
                if merged_provenance != reconcile_todo_provenance(
                    existing_todos, metadata.get("todo_provenance")
                ):
                    writes["todo_provenance"] = merged_provenance
            if writes and not await bucket_mgr.update(bucket["id"], **writes):
                raise RuntimeError(f"failed to update duplicate bucket {bucket['id']}")
            if outcome_out is not None:
                outcome_out.update(bucket_id=bucket["id"], reused=True,
                                   written_fields=sorted(writes),
                                   ignored_fields=["tags", "importance", "domain",
                                                   "valence", "arousal", "name",
                                                   "provenance_kind"])
            if source_id_out is not None:
                source_id_out.append(bucket["id"])
            return metadata.get("name", bucket["id"]), True

    bucket_id = await bucket_mgr.create(
        content=content,
        tags=tags,
        importance=importance,
        domain=domain,
        valence=valence,
        arousal=arousal,
        name=(name.strip() if isinstance(name, str) else "") or _canonical_body_name(content),
        todos=incoming_todos,
        todo_provenance=incoming_provenance,
        provenance_kind=provenance_kind,
    )
    await _auto_link_related(bucket_id)
    if trigger_date:
        if not await bucket_mgr.update(bucket_id, trigger_date=trigger_date,
                                       trigger_last_seen=""):
            raise RuntimeError(f"trigger_date was not written to new bucket {bucket_id}")
    if source_id_out is not None:
        source_id_out.append(bucket_id)
    if outcome_out is not None:
        outcome_out.update(bucket_id=bucket_id, reused=False,
                           written_fields=["content", "tags", "importance", "domain",
                                           "valence", "arousal", "name", "todos",
                                           *(["todo_provenance"] if incoming_provenance else []),
                                           *(["trigger_date"] if trigger_date else [])],
                           ignored_fields=[])
    return bucket_id, False


def _canonical_body_name(content: str) -> str:
    """Use at most 20 Unicode characters from the stored body as a title."""
    return " ".join(str(content).split())[:20]


def _provider_failure_category(exc: BaseException) -> str:
    """Map an exception chain to a small public reason without exposing details."""
    current: BaseException | None = exc
    while current is not None:
        if isinstance(current, AnalysisParseError):
            return "parse_error"
        if getattr(current, "status_code", None) == 429:
            return "rate_limited"
        if isinstance(current, (APIConnectionError, APITimeoutError)):
            return "connection_error"
        current = current.__cause__
    if "API 不可用" in str(exc) or "OMBRE_API_KEY" in str(exc):
        return "provider_unconfigured"
    return "provider_error"


def _bucket_date(meta: dict, *keys: str) -> str:
    """Return the first available bucket date as YYYY-MM-DD."""
    for key in keys:
        value = meta.get(key)
        if not value:
            continue
        try:
            return datetime.fromisoformat(str(value)).date().isoformat()
        except (ValueError, TypeError):
            continue
    return ""


def _bucket_topic(meta: dict) -> str:
    domains = meta.get("domain", []) or meta.get("domains", [])
    if isinstance(domains, list) and domains:
        return ",".join(str(d) for d in domains if d)
    if isinstance(domains, str):
        return domains
    return "未分类"


def _bucket_emotion(meta: dict) -> str:
    try:
        val = float(meta.get("valence", 0.5))
        aro = float(meta.get("arousal", 0.3))
    except (ValueError, TypeError):
        val, aro = 0.5, 0.3
    return f"V{val:.1f}/A{aro:.1f}"


def _superseded_by_id(metadata: dict) -> str:
    """Return the normalized successor marker; absent/empty metadata means active."""
    value = metadata.get("superseded_by", "") if isinstance(metadata, dict) else ""
    return str(value or "").strip()


def _supersedes_ids(metadata: dict) -> list[str]:
    """Read legacy-tolerant reverse supersession metadata as unique bucket IDs."""
    value = metadata.get("supersedes", []) if isinstance(metadata, dict) else []
    if isinstance(value, str):
        values = _parse_csv_ids(value)
    elif isinstance(value, list):
        values = [str(item).strip() for item in value if str(item).strip()]
    else:
        values = []
    return list(dict.fromkeys(values))


async def _superseded_marker(bucket: dict) -> str:
    """Render a successor marker without leaking a sealed successor."""
    successor_id = _superseded_by_id(bucket.get("metadata", {}))
    if not successor_id or successor_id == "none":
        return " ⊘已作废" if successor_id else ""
    successor = await bucket_mgr.get(successor_id)
    if not successor or _is_sealed(successor):
        return " ⊘已作废"
    successor_name = successor.get("metadata", {}).get("name", successor_id)
    return f" ⊘已作废→{successor_id}({successor_name})"


async def _current_successor_lines(
    obsolete_buckets: list[dict],
    returned_ids: set[str],
) -> list[str]:
    """List visible successors without loading successor bodies or using budget."""
    lines = []
    seen = set()
    for bucket in obsolete_buckets:
        old_id = str(bucket.get("id", ""))
        successor_id = _superseded_by_id(bucket.get("metadata", {}))
        if (
            not old_id
            or not successor_id
            or successor_id == "none"
            or successor_id in returned_ids
            or (old_id, successor_id) in seen
        ):
            continue
        successor = await bucket_mgr.get(successor_id)
        if not successor or _is_sealed(successor):
            continue
        successor_name = successor.get("metadata", {}).get("name", successor_id)
        lines.append(f"当前有效：[{successor_id}] {successor_name}（取代了 {old_id}）")
        seen.add((old_id, successor_id))
    return lines


async def _dream_superseded_notice(bucket: dict) -> str:
    """Explain supersession before a Dream detail body without sealed leakage."""
    metadata = bucket.get("metadata", {})
    successor_id = _superseded_by_id(metadata)
    if not successor_id:
        return ""
    if successor_id == "none":
        return "此桶已作废。"
    successor = await bucket_mgr.get(successor_id)
    if not successor or _is_sealed(successor):
        return "此桶已作废。"
    successor_name = successor.get("metadata", {}).get("name", successor_id)
    timestamp = metadata.get("superseded_at", "未知时间")
    return f"此桶已被 {successor_id}({successor_name}) 取代于 {timestamp}"


def _bucket_display_icon(meta: dict, *, protected_as_pinned: bool = False) -> str:
    """Share the pulse type/status icons while keeping query pins literal."""
    if int(meta.get("sealed", 0) or 0) == 1:
        return "🔒"
    if meta.get("pinned") or (protected_as_pinned and meta.get("protected")):
        return "📌"
    if meta.get("type") == "permanent":
        return "📦"
    if meta.get("type") == "feel":
        return "🫧"
    if meta.get("type") == "archived":
        return "🗄️"
    if meta.get("resolved", False):
        return "✅"
    return "💭"


async def _bucket_summary_line(
    bucket: dict, score: float | None = None, pinned: bool = False,
    *, importance_only: bool = False,
) -> str:
    meta = bucket.get("metadata", {})
    label = meta.get("name", bucket["id"])
    superseded_marker = await _superseded_marker(bucket)
    topic = _bucket_topic(meta)
    emotion = _bucket_emotion(meta)
    updated = _bucket_date(meta, "updated_at", "last_active", "created")
    icon = _bucket_display_icon(meta)
    if importance_only:
        importance = meta.get("importance", "?")
        return f"{icon} [bucket_id:{bucket['id']}] {label}{superseded_marker} | 主题:{topic} | {emotion} | 重要:{importance} | 更新:{updated}"
    if pinned:
        importance = meta.get("importance", "?")
        return f"{icon} [bucket_id:{bucket['id']}] {label}{superseded_marker} | 主题:{topic} | {emotion} | 重要:{importance} | 更新:{updated}"
    weight = f"{score:.2f}" if score is not None else "0.00"
    return f"{icon} [bucket_id:{bucket['id']}] {label}{superseded_marker} | 主题:{topic} | {emotion} | 权重:{weight} | 更新:{updated}"


async def _dream_summary_line(bucket: dict) -> str:
    meta = bucket.get("metadata", {})
    label = meta.get("name", bucket["id"])
    superseded_marker = await _superseded_marker(bucket)
    topic = _bucket_topic(meta)
    emotion = _bucket_emotion(meta)
    updated = _bucket_date(meta, "updated_at", "last_active", "created")
    content = strip_wikilinks(bucket.get("content", "")).replace("\n", " ").strip()
    one_line = content[:80] + ("…" if len(content) > 80 else "")
    return f"[{label}]{superseded_marker} 主题:{topic} | {one_line} | {emotion} | 更新:{updated} | bucket_id:{bucket['id']}"


def _recent_cutoff(recent_days: int) -> str | None:
    if recent_days == -1:
        return None
    if recent_days < -1:
        raise ValueError("recent_days must be -1 or non-negative.")
    return (datetime.now().date() - timedelta(days=recent_days)).isoformat()


def _is_recent_bucket(bucket: dict, cutoff: str | None, *, exact_day: bool = False) -> bool:
    if not cutoff:
        return True
    updated = _bucket_date(bucket.get("metadata", {}), "updated_at", "last_active", "created")
    return bool(updated and (updated == cutoff if exact_day else updated >= cutoff))


def _parse_date_filter(value: str, parameter: str) -> str:
    value = (value or "").strip()
    if not value:
        return ""
    try:
        return datetime.strptime(value, "%Y-%m-%d").date().isoformat()
    except ValueError as exc:
        raise ValueError(f"{parameter} must use YYYY-MM-DD format.") from exc


def _parse_optional_date(value: str, parameter: str) -> str | None:
    value = (value or "").strip()
    if not value:
        return ""
    return _parse_date_filter(value, parameter)


def _is_in_date_range(
    bucket: dict,
    date_from: str = "",
    date_to: str = "",
) -> bool:
    if not date_from and not date_to:
        return True
    updated = _bucket_date(
        bucket.get("metadata", {}),
        "updated_at",
        "last_active",
        "created",
    )
    if not updated:
        return False
    return (not date_from or updated >= date_from) and (
        not date_to or updated <= date_to
    )


def _parse_resonance(value: str) -> tuple[float, float] | None:
    value = (value or "").strip()
    if not value:
        return None
    try:
        raw_v, raw_a = [part.strip() for part in value.split(",", 1)]
        target = (float(raw_v), float(raw_a))
    except (ValueError, TypeError) as exc:
        raise ValueError("resonance must use 'v,a' format, both between 0 and 1.") from exc
    if not (0 <= target[0] <= 1 and 0 <= target[1] <= 1):
        raise ValueError("resonance values must be between 0 and 1.")
    return target


def _resonance_distance(bucket: dict, target: tuple[float, float]) -> float:
    meta = bucket.get("metadata", {})
    raw_valence = meta.get("valence")
    raw_arousal = meta.get("arousal")
    valence = float(0.5 if raw_valence is None else raw_valence)
    arousal = float(0.3 if raw_arousal is None else raw_arousal)
    return ((valence - target[0]) ** 2 + (arousal - target[1]) ** 2) ** 0.5


def _last_access_days(meta: dict) -> float:
    value = meta.get("last_active") or meta.get("updated_at") or meta.get("created")
    try:
        last_access = datetime.fromisoformat(str(value))
        return max(0.0, (datetime.now() - last_access).total_seconds() / 86400)
    except (ValueError, TypeError):
        return 999.0


async def _mark_dormant_buckets(buckets: list[dict]) -> int:
    marked = 0
    for bucket in buckets:
        meta = bucket.get("metadata", {})
        if (
            meta.get("type", "dynamic") == "dynamic"
            and not meta.get("pinned", False)
            and not meta.get("protected", False)
            and not meta.get("dormant", False)
            and int(meta.get("importance", 5)) < 3
            and _last_access_days(meta) > 30
        ):
            if await bucket_mgr.set_dormant(bucket["id"], True):
                meta["dormant"] = True
                marked += 1
    return marked


def _parse_csv_ids(value: str) -> list[str]:
    return [part.strip() for part in (value or "").split(",") if part.strip()]


def _normalize_archive_topics(topics: list[str] | None) -> list[str]:
    """Normalize structured archive topics without changing their labels."""
    if topics is None:
        return []
    if not isinstance(topics, list) or any(not isinstance(item, str) for item in topics):
        raise ValueError("topics must be a list of strings.")

    normalized = []
    seen = set()
    for item in topics:
        topic = item.strip()
        if not topic or topic in seen:
            continue
        seen.add(topic)
        normalized.append(topic)
    return normalized


def _normalize_breath_filter(
    value: list[str] | None,
    parameter: str,
    *,
    apply_aliases: bool = False,
) -> list[str]:
    """Normalize one structured breath filter without broadening its match."""
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError(f"{parameter} must be a list of strings or null.")

    normalized = []
    seen = set()
    for item in value:
        if not isinstance(item, str):
            raise ValueError(f"{parameter} must contain only strings.")
        item = item.strip()
        if not item:
            raise ValueError(f"{parameter} cannot contain blank values.")
        if apply_aliases:
            item = _apply_display_aliases(item)
        if item in seen:
            continue
        seen.add(item)
        normalized.append(item)
    return normalized


def _structured_metadata_values(metadata: dict, field: str) -> list[str]:
    """Return safe structured values without stringifying malformed metadata."""
    raw = metadata.get(field, [])
    if field == "tags" and isinstance(raw, str):
        raw = [part.strip() for part in raw.split(",") if part.strip()]
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, str)]


def _is_test_bucket(bucket: dict) -> bool:
    """Use the existing exact tag identity only at delivery entry points."""
    return "test" in _structured_metadata_values(bucket.get("metadata", {}), "tags")


def _matches_any_structured_filter(
    bucket: dict,
    field: str,
    values: list[str],
) -> bool:
    if not values:
        return True
    metadata = bucket.get("metadata", {})
    stored_values = _structured_metadata_values(metadata, field)
    wanted = set(values)
    return any(item in wanted for item in stored_values)


def _filter_breath_candidates(
    buckets: list[dict],
    *,
    domain_values: list[str] | None = None,
    recent_cutoff: str | None = None,
    include_dormant: bool = False,
    include_sealed: bool = False,
    date_from: str = "",
    date_to: str = "",
    tags_filter: list[str] | None = None,
    topic_filter: list[str] | None = None,
    apply_domain: bool = True,
    apply_dormant: bool = True,
    importance_min: int = -1,
    recent_days: int = -1,
) -> list[dict]:
    """Apply the shared privacy and structured Breath candidate gates."""
    domain_set = {
        str(value).casefold()
        for value in (domain_values or [])
        if str(value).strip()
    }
    tags_filter = tags_filter or []
    topic_filter = topic_filter or []
    candidates = [
        bucket
        for bucket in buckets
        if include_sealed or not _is_sealed(bucket)
    ]
    if apply_domain and domain_set:
        candidates = [
            bucket
            for bucket in candidates
            if domain_set
            & {
                str(value).casefold()
                for value in bucket.get("metadata", {}).get("domain", [])
            }
        ]
    if apply_dormant and not include_dormant:
        candidates = [
            bucket
            for bucket in candidates
            if not bucket.get("metadata", {}).get("dormant", False)
        ]
    return [
        bucket
        for bucket in candidates
        if _is_recent_bucket(bucket, recent_cutoff, exact_day=recent_days == 0)
        and _is_in_date_range(bucket, date_from, date_to)
        and _breath_importance_matches(bucket, importance_min)
        and _matches_any_structured_filter(bucket, "tags", tags_filter)
        and _matches_any_structured_filter(bucket, "topics", topic_filter)
    ]


def _breath_recency_key(bucket: dict) -> tuple[str, str]:
    """Return the canonical deterministic breath recency key."""
    metadata = bucket.get("metadata", {})
    return (
        _bucket_date(
            metadata,
            "updated_at",
            "last_active",
            "created_at",
            "created",
        ),
        str(bucket.get("id", "")),
    )


def _normalize_todos(raw) -> list[str]:
    if isinstance(raw, list):
        return [str(item).strip() for item in raw if str(item).strip()]
    if isinstance(raw, dict):
        return [
            f"{key}: {value}".strip()
            for key, value in raw.items()
            if str(value).strip()
        ]
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return []
        try:
            parsed = _json_lib.loads(text)
        except (ValueError, TypeError):
            parsed = None
        if parsed is not None and parsed != raw:
            return _normalize_todos(parsed)
        return [
            line.strip().lstrip("-* ").strip()
            for line in text.replace(",", "\n").splitlines()
            if line.strip().lstrip("-* ").strip()
        ]
    return [str(raw).strip()] if raw is not None and str(raw).strip() else []


def _canonical_todos(raw) -> list[str]:
    """Normalize legacy todo shapes and return an ordered, deduplicated list."""
    return canonicalize_todos(_normalize_todos(raw))


def _parse_explicit_provenance_kind(value) -> str | None:
    """Validate a public assertion without treating an omitted value as one."""
    if value is None or value == "":
        return None
    return normalize_provenance_kind(value, strict=True)


def _structured_todo_items(todo_items) -> tuple[list[str], list[dict]]:
    """Validate MCP todo_items without changing legacy todos semantics."""
    if not isinstance(todo_items, list):
        raise ValueError("todo_items 必须是数组。")
    raw_todos = []
    for item in todo_items:
        if not isinstance(item, dict):
            raise ValueError("todo_items 的每一项必须是对象。")
        text = item.get("text")
        if not isinstance(text, str) or not text.strip():
            raise ValueError("todo_items.text 必须是非空字符串。")
        for field in ("done_at", "dropped_at"):
            if field in item:
                raise ValueError(f"todo_items.{field} is server-generated and cannot be supplied.")
        raw_todos.append(text.strip())
    todos = _canonical_todos(raw_todos)
    try:
        provenance = reconcile_todo_provenance(
            todos,
            todo_items,
            strict=True,
        )
    except ValueError as exc:
        raise ValueError(f"todo_items 无效：{exc}") from exc
    return todos, provenance


def _parse_emotion_history(raw) -> list[dict]:
    if isinstance(raw, list):
        history = raw
    elif isinstance(raw, str) and raw.strip():
        try:
            history = _json_lib.loads(raw)
        except Exception:
            history = []
    else:
        history = []
    return [item for item in history if isinstance(item, dict)]


def _encode_emotion_history(history: list[dict]) -> str:
    return _json_lib.dumps(history[-20:], ensure_ascii=False, separators=(",", ":"))


def _append_emotion_history(meta: dict, valence: float, arousal: float) -> str:
    history = _parse_emotion_history(meta.get("emotion_history", ""))
    history.append({
        "date": datetime.now().date().isoformat(),
        "v": round(float(valence), 3),
        "a": round(float(arousal), 3),
    })
    return _encode_emotion_history(history)


def _emotion_timeline_path() -> str:
    return os.path.join(config["buckets_dir"], ".emotion_timeline.json")


def _load_emotion_timeline() -> list[dict]:
    path = _emotion_timeline_path()
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = _json_lib.load(handle)
        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict)]
    except (OSError, ValueError, TypeError):
        pass
    return []


@guarded_mutation("emotion_timeline_write")
def _record_emotion_snapshot(
    valence: float, arousal: float, source: str, bucket_id: str = "",
    *, _expected_entry: dict | None = None, _strict: bool = False, _verify_only: bool = False,
    _effect_key: str | None = None, _s4_context: dict | None = None,
    _expected_source: dict | None = None,
) -> None:
    if not (0 <= valence <= 1 and 0 <= arousal <= 1):
        return
    entry = dict(_expected_entry) if _expected_entry is not None else {
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "valence": round(float(valence), 3),
        "arousal": round(float(arousal), 3),
        "source": source,
    }
    if bucket_id:
        entry["bucket_id"] = bucket_id
    if _effect_key is not None:
        entry['_s4_effect'] = _effect_key
    path = _emotion_timeline_path()
    try:
        with bucket_write_scope(config["buckets_dir"]):
            if _s4_context is not None:
                bucket_mgr._trace_fence(_s4_context)
            if bucket_id and not _verify_only:
                from confirmed_delete_admission import DeleteAdmissionError
                try:
                    bucket_mgr.delete_admission.admit(bucket_id,expected_source=_expected_source,
                        kind='emotion',allow_missing=_expected_source is None)
                except DeleteAdmissionError as exc:
                    raise BucketIdempotencyError(exc.code) from exc
            timeline = _read_emotion_timeline_for_write(path)
            if _effect_key is not None:
                existing = [item for item in timeline if item.get('_s4_effect') == _effect_key]
                if existing:
                    if existing != [entry]:
                        raise BucketIdempotencyError('operation_emotion_conflict')
                    BucketManager._sync_directory(os.path.dirname(path))
                    return
            if source == "archive" and bucket_id:
                existing = [item for item in timeline if item.get("source") == source
                            and item.get("bucket_id") == bucket_id]
                if existing:
                    if existing != [entry]:
                        raise ArchiveSessionError("archive_emotion_evidence_conflict")
                    BucketManager._sync_directory(os.path.dirname(path))
                    return
                if _verify_only:
                    raise ArchiveSessionError("archive_emotion_receipt_conflict")
            timeline.append(entry)
            payload = _json_lib.dumps(timeline, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            BucketManager._write_bytes_atomic(path, payload)
            if _read_emotion_timeline_for_write(path) != timeline:
                raise ArchiveSessionError("archive_emotion_evidence_conflict")
    except BucketIdempotencyError:
        raise
    except (OSError, ValueError) as exc:
        if _strict:
            if isinstance(exc, ArchiveSessionError):
                raise
            raise ArchiveSessionError("archive_emotion_write_failed") from None
        logger.warning("Failed to persist emotion timeline: emotion_timeline_write_failed")


def _read_emotion_timeline_for_write(path: str) -> list[dict]:
    def invalid_constant(_value):
        raise ValueError()
    def unique_keys(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError()
            result[key] = value
        return result
    try:
        with open(path, "r", encoding="utf-8") as handle:
            timeline = _json_lib.load(handle, parse_constant=invalid_constant, object_pairs_hook=unique_keys)
    except FileNotFoundError:
        if os.path.lexists(path):
            raise ArchiveSessionError("archive_emotion_timeline_invalid") from None
        return []
    except (OSError, ValueError):
        raise ArchiveSessionError("archive_emotion_timeline_invalid") from None
    if (os.path.islink(path) or not isinstance(timeline, list)
            or any(not isinstance(item, dict) for item in timeline)):
        raise ArchiveSessionError("archive_emotion_timeline_invalid")
    return timeline


def _with_emotion_timeline(text: str, enabled: bool, max_tokens: int = 10000) -> str:
    if not enabled:
        return text
    visibility: dict[str, bool] = {}
    visible = []
    for item in _load_emotion_timeline():
        # Legacy entries have no reliable provenance. Keep them readable without
        # guessing from timestamps; historical sealed cleanup is Data Repair.
        if "bucket_id" in item:
            bucket_id = item.get("bucket_id")
            if not isinstance(bucket_id, str) or not bucket_id:
                continue
            if bucket_id not in visibility:
                try:
                    path = bucket_mgr._find_bucket_file(bucket_id)
                    post = frontmatter.load(path) if path else None
                    visibility[bucket_id] = bool(
                        post is not None and int(post.get("sealed", 0) or 0) != 1
                    )
                except (OSError, TypeError, ValueError):
                    visibility[bucket_id] = False
            if not visibility[bucket_id]:
                continue
        visible.append({key: value for key, value in item.items() if key != '_s4_effect'})
    timeline = sorted(
        visible,
        key=lambda item: str(item.get("timestamp", "")),
    )
    prefix = f"{text}\n\nemotion_history: "
    def encode(items: list[dict]) -> str:
        return _json_lib.dumps(items, ensure_ascii=False, separators=(",", ":"))
    budget = max(0, int(max_tokens))
    if count_tokens_approx(prefix + "[]") > budget:
        return text
    if count_tokens_approx(prefix + encode(timeline)) <= budget:
        return prefix + encode(timeline)
    # Keep the newest complete records, preserving chronological raw-array order.
    chosen: list[dict] = []
    notice = "\nemotion_history_truncated: true"
    if count_tokens_approx(prefix + "[]" + notice) > budget:
        return text
    for item in reversed(timeline):
        candidate = [item, *chosen]
        if count_tokens_approx(prefix + encode(candidate) + notice) > budget:
            break
        chosen = candidate
    return prefix + encode(chosen) + notice


def _related_ids(meta: dict) -> list[str]:
    return parse_related(meta).require_safe()


async def _unlink_related(source: dict, relation_ids: list[str]) -> bool:
    bucket_mgr.mutate_related(str(source['id']), remove=relation_ids)
    return True


def _metadata_restore_value(metadata: dict, field: str):
    """Return a BucketManager.update-compatible value that restores field presence."""
    return metadata.get(field) if field in metadata else None


async def _apply_supersession(
    source: dict,
    superseded_by: str,
    *,
    preserve_superseded_at: bool = False,
) -> tuple[bool, str]:
    """Delegate the current forward/reverse mutation to one storage lock."""
    source_id = str(source.get("id", ""))
    source_meta = source.get("metadata", {})
    requested = str(superseded_by).strip()

    if requested and requested != "none" and requested == source_id:
        return False, "superseded_by 不能指向自身。"

    target = None
    if requested and requested != "none":
        target = await bucket_mgr.get(requested)
        if not target:
            return False, f"未找到 superseded_by 目标桶: {requested}"
        if _is_sealed(target):
            return False, f"superseded_by 目标桶已封存，不能作为取代桶: {requested}"

    source_update = {
        "superseded_by": requested or None,
        "superseded_at": (
            _metadata_restore_value(source_meta, "superseded_at")
            if preserve_superseded_at and requested
            else (datetime.now().isoformat() if requested else None)
        ),
    }
    try:
        success = await bucket_mgr.update(source_id, **source_update, _supersession_reverse=True)
    except SupersessionError as exc:
        return False, f"superseded_by rejected: {exc.code}"
    if not success:
        return False, "superseded_by 修改失败，未完成关系写入。"

    if requested == "none":
        return True, "none"
    if not requested:
        return True, ""
    target_name = target.get("metadata", {}).get("name", requested) if target else requested
    return True, f"{requested} ({target_name})"


async def _clear_outgoing_supersession_for_delete(bucket: dict) -> bool:
    """Remove a soon-to-be-deleted bucket from its successor's reverse list."""
    successor_id = _superseded_by_id(bucket.get("metadata", {}))
    if not successor_id or successor_id == "none":
        return True
    successor = await bucket_mgr.get(successor_id)
    if not successor:
        return True
    successor_meta = successor.get("metadata", {})
    return await bucket_mgr.update(
        successor_id,
        supersedes=[
            item for item in _supersedes_ids(successor_meta)
            if item != bucket.get("id")
        ],
    )


async def _inbound_supersession_buckets(target_id: str) -> list[dict]:
    """Find forward supersession pointers without trusting stale reverse lists."""
    buckets = await bucket_mgr.list_all(include_archive=True)
    return [
        bucket for bucket in buckets
        if _superseded_by_id(bucket.get("metadata", {})) == target_id
    ]


async def _format_related_line(bucket: dict) -> str:
    related = _related_ids(bucket.get("metadata", {}))
    if not related:
        return ""
    parts = []
    for related_id in related:
        related_bucket = await bucket_mgr.get(related_id)
        if related_bucket:
            if _is_sealed(related_bucket):
                continue
            name = related_bucket.get("metadata", {}).get("name", related_id)
            parts.append(f"[{related_id}] {name}")
        else:
            parts.append(f"[{related_id}]")
    return "关联: " + ", ".join(parts)


async def _with_related_line(text: str, bucket: dict) -> str:
    related_line = await _format_related_line(bucket)
    return f"{text}\n{related_line}" if related_line else text


async def _append_bucket_extras(text: str, bucket: dict, emotion_trend: bool = False) -> str:
    # This helper is used by current-time Breath composition only. Historical
    # ``as_of`` output intentionally bypasses it because history has no
    # provenance snapshots.
    provenance_kind = normalize_provenance_kind(
        bucket.get("metadata", {}).get("provenance_kind")
    )
    lines = [f"[prov={provenance_kind}] {text}"]
    meta = bucket.get("metadata", {})
    current_todos, _ = active_todo_projection(
        meta.get("todos"), meta.get("todo_provenance")
    )
    if current_todos:
        lines.append(
            "=== 当前 todos（以 metadata 为准）===\n"
            + "\n".join(f"- {item}" for item in current_todos)
        )
    related_line = await _format_related_line(bucket)
    if related_line:
        lines.append(related_line)
    return "\n".join(lines)


async def _merge_bucket_into_target(
    target_id: str, source_id: str, confirm_token: str = "",
) -> str:
    if not source_id or source_id == target_id:
        return "merge 必须指定另一个有效的 bucket_id。"
    for pending in bucket_mgr.read_merge_operations(open_only=True):
        if (pending["target_id"], pending["source_id"]) == (target_id, source_id):
            return await _execute_merge_operation(pending)
        if {target_id, source_id} & {pending["target_id"], pending["source_id"]}:
            return ("merge blocked by unfinished operation_id: "
                    f"{pending['operation_id']}; resume its original target/source pair first.")
    target = await bucket_mgr.get(target_id)
    source = await bucket_mgr.get(source_id)
    if not target:
        return f"未找到目标记忆桶: {target_id}"
    if not source:
        return f"未找到源记忆桶: {source_id}"

    source_meta = source.get("metadata", {})
    if source_meta.get("pinned") or source_meta.get("protected"):
        return f"源桶 {source_id} 是钉选/保护桶，不能被合并删除。"

    target_meta = target.get("metadata", {})
    if target_meta.get("pinned") or target_meta.get("protected"):
        return f"目标桶 {target_id} 是钉选/保护桶，不能作为合并目标。"
    if source_meta.get("type") == "feel" or target_meta.get("type") == "feel":
        return "合并失败：feel 桶不能参与普通记忆合并。"
    source_sealed = _is_sealed(source)
    target_sealed = _is_sealed(target)
    if source_sealed != target_sealed: return "合并失败：不允许跨隐私边界合并（sealed 状态不匹配）。"
    try:
        related_plan = bucket_mgr.preview_related_delete(source_id, target_id=target_id)
    except RelatedError as exc:
        return f"merge blocked: {exc.code}; repair the unsafe relation inventory first."
    all_buckets = await bucket_mgr.list_all(include_archive=True)
    inbound = [
        bucket for bucket in all_buckets
        if _superseded_by_id(bucket.get("metadata", {})) == source_id
    ]
    if any(bucket.get("id") == target_id for bucket in inbound):
        return "合并失败：目标桶不能同时被源桶取代。"
    target_content = target.get("content", "").rstrip()
    source_content = source.get("content", "").strip()
    merged_content = (
        f"{target_content}\n\n{source_content}"
        if target_content and source_content
        else target_content or source_content
    )
    target_tags = target_meta.get("tags", []) or []
    source_tags = source_meta.get("tags", []) or []
    if isinstance(target_tags, str):
        target_tags = _parse_csv_ids(target_tags)
    if isinstance(source_tags, str):
        source_tags = _parse_csv_ids(source_tags)
    merged_tags = list(dict.fromkeys([*target_tags, *source_tags]))
    try:
        merged_todos, merged_todo_provenance = merge_todo_provenance(
            _canonical_todos(target_meta.get("todos")),
            target_meta.get("todo_provenance"),
            _canonical_todos(source_meta.get("todos")),
            source_meta.get("todo_provenance"),
            source_is_persisted=True,
        )
    except ValueError as exc:
        return f"merge todo identity conflict: {exc}"
    merged_importance = max(
        int(target_meta.get("importance", 5)),
        int(source_meta.get("importance", 5)),
    )
    merged_valence = (
        float(target_meta.get("valence", 0.5))
        + float(source_meta.get("valence", 0.5))
    ) / 2
    merged_arousal = (
        float(target_meta.get("arousal", 0.3))
        + float(source_meta.get("arousal", 0.3))
    ) / 2

    relation_operations: list[tuple[str, dict, dict]] = []
    inbound_ids = [
        str(bucket.get("id", "")) for bucket in inbound
        if str(bucket.get("id", "")) and bucket.get("id") != source_id
    ]
    for bucket in all_buckets:
        holder_id = str(bucket.get("id", ""))
        holder_meta = bucket.get("metadata", {})
        reverse_ids = _supersedes_ids(holder_meta)
        if holder_id == source_id or source_id not in reverse_ids:
            continue
        desired_reverse = [item for item in reverse_ids if item != source_id]
        if holder_id == target_id:
            desired_reverse = list(dict.fromkeys(desired_reverse + inbound_ids))
        relation_operations.append(
            (
                holder_id,
                {"supersedes": desired_reverse},
                {"supersedes": _metadata_restore_value(holder_meta, "supersedes")},
            )
        )
    if not any(operation[0] == target_id for operation in relation_operations) and inbound_ids:
        relation_operations.append(
            (
                target_id,
                {"supersedes": list(dict.fromkeys(
                    _supersedes_ids(target_meta) + inbound_ids
                ))},
                {"supersedes": _metadata_restore_value(target_meta, "supersedes")},
            )
        )
    for inbound_bucket in inbound:
        inbound_id = str(inbound_bucket.get("id", ""))
        if not inbound_id or inbound_id == source_id:
            continue
        inbound_meta = inbound_bucket.get("metadata", {})
        relation_operations.append(
            (
                inbound_id,
                {
                    "superseded_by": target_id,
                    "superseded_at": _metadata_restore_value(
                        inbound_meta, "superseded_at"
                    ),
                },
                {
                    "superseded_by": _metadata_restore_value(
                        inbound_meta, "superseded_by"
                    ),
                    "superseded_at": _metadata_restore_value(
                        inbound_meta, "superseded_at"
                    ),
                },
            )
        )

    try:
        bucket_mgr.validate_supersession_rewire(source_id, target_id,
            {identity: target_id for identity in inbound_ids}, planning=True)
    except SupersessionError as exc:
        return f"merge blocked: {exc.code}"

    target_update = {
        "content": merged_content, "tags": merged_tags,
        "importance": merged_importance, "valence": merged_valence,
        "arousal": merged_arousal, "todos": merged_todos,
        "todo_provenance": merged_todo_provenance,
        "provenance_kind": "unknown",
    }
    plan = {
        "target_id": target_id, "source_id": source_id,
        "source_sealed": source_sealed,
        "related_inventory": related_plan['inventory'],
        "target_content_sha256": hashlib.sha256(
            str(target.get("content", "")).encode("utf-8")
        ).hexdigest(),
        "source_content_sha256": hashlib.sha256(
            str(source.get("content", "")).encode("utf-8")
        ).hexdigest(),
        "target_metadata_sha256": _merge_metadata_digest(target_meta),
        "source_metadata_sha256": _merge_metadata_digest(source_meta),
        "target_update": target_update,
        "relations": [
            {"bucket_id": bucket_id, "updates": update,
             "before_sha256": _merge_metadata_digest(next(
                 bucket["metadata"] for bucket in all_buckets
                 if bucket["id"] == bucket_id
             ))}
            for bucket_id, update, _ in relation_operations
        ],
    }
    payload = {
        "target_id": target_id, "source_id": source_id,
        "target_content_sha256": plan["target_content_sha256"],
        "source_content_sha256": plan["source_content_sha256"],
        "target_metadata_sha256": plan["target_metadata_sha256"],
        "source_metadata_sha256": plan["source_metadata_sha256"],
        "plan_sha256": _confirmation_payload_digest(plan),
    }
    token = (confirm_token or "").strip()
    if not token:
        issued = _issue_mutation_confirmation("trace.merge", payload)
        return (
            "merge preview: no changes made. "
            f"trace(bucket_id={target_id}, merge={source_id}) appends source into target, "
            f"rewires {len(relation_operations)} relation steps, then deletes source. "
            f"target_sha256={payload['target_content_sha256']} "
            f"source_sha256={payload['source_content_sha256']}\n"
            f"confirm_token: {issued}"
        )
    with _mutation_confirm_lock:
        entry = _mutation_confirm_tokens.get(token)
        if (not entry or float(entry.get("expires_at", 0)) <= time.monotonic()
                or entry.get("operation") != "trace.merge"
                or entry.get("payload_digest") != _confirmation_payload_digest(payload)):
            return "merge confirmation invalid, expired, used, or stale; preview again."
        operation_id = secrets.token_hex(12)
        try:
            plan["target_update"]["todo_provenance"] = prepare_todo_provenance(
                merged_todos, merged_todo_provenance,
            )
            bucket_mgr.write_merge_operation(
                operation_id, plan, status="running", completed=[], create=True,
            )
        except ValueError as exc:
            return f"merge blocked: {exc}"
        _mutation_confirm_tokens.pop(token, None)
    operation = next(
        record for record in bucket_mgr.read_merge_operations()
        if record["operation_id"] == operation_id
    )
    return await _execute_merge_operation(operation)


def _merge_metadata_digest(metadata: dict) -> str:
    encoded = _json_lib.dumps(metadata, ensure_ascii=False, sort_keys=True,
                              default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _todo_resume_matches(expected: list, actual: list | None, *, allow_legacy_assigned_ids: bool = False) -> bool:
    # Validate intact records before the existing legacy journal ID exception.
    for records in (expected, actual or []):
        reconcile_todo_provenance(
            [r.get("text") for r in records if isinstance(r, dict)], records, strict=True)
    remaining = list(actual or [])
    for planned in expected:
        found = next((r for r in remaining if
                      (r.get("id") == planned["id"] if "id" in planned else r.get("text") == planned.get("text"))), None)
        if found is None:
            return False
        merged = _merge_todo_terminal_state(planned, found)
        if any(merged.get(k) != found.get(k) for k in ("done_at", "dropped_at")):
            return False
        ignored = {"done_at", "dropped_at"}
        actual_fields = {k: v for k, v in found.items() if k not in ignored}
        if allow_legacy_assigned_ids and "id" not in planned:
            actual_fields.pop("id", None)
        if actual_fields != {k: v for k, v in planned.items() if k not in ignored}:
            return False
        remaining.remove(found)
    return all(_todo_is_terminal(r) for r in remaining)


@guarded_async_mutation("merge_resume")
async def _execute_merge_operation(operation: dict) -> str:
    """Resume a confirmed merge from durable steps without rebuilding its body."""
    operation_id = operation["operation_id"]
    plan = operation["plan"]
    target_id, source_id = plan["target_id"], plan["source_id"]
    with _mutation_confirm_lock:
        if operation_id in _merge_running_operations:
            return f"merge operation already running: operation_id: {operation_id}"
        _merge_running_operations.add(operation_id)
    completed = list(operation["completed"])
    steps = ["target", *[f"relation:{index}" for index in range(len(plan["relations"]))],
             "delete_started", "delete_source"]

    def persist(status: str) -> None:
        bucket_mgr.write_merge_operation(operation_id, plan, status=status,
                                         completed=completed)

    async def update_step(step: str, bucket_id: str, changes: dict) -> None:
        key = f"merge:{operation_id}:{step}"
        if step not in completed:
            await bucket_mgr.apply_import_operation(
                key, operation_kind="update", target_bucket_id=bucket_id,
                payload={"kwargs": changes},
            )
        inspected = bucket_mgr.inspect_import_operation(key)
        if not inspected or not inspected["marker"] or not inspected["memory_exists"]:
            raise RuntimeError(f"merge step {step} has no durable write marker")
        written = await bucket_mgr.get(bucket_id)
        if not written:
            raise RuntimeError(f"merge step {step} target missing")
        for field, expected in changes.items():
            if field == "content":
                actual = written["content"]
            else:
                actual = written["metadata"].get(field)
            if field == "todo_provenance":
                legacy = bool(expected) and all("id" not in r for r in expected)
                if _todo_resume_matches(expected, actual, allow_legacy_assigned_ids=legacy):
                    continue
            if actual != expected:
                raise RuntimeError(f"merge step {step} changed after write: {field}")
        if step not in completed:
            completed.append(step)
            persist("running")

    try:
        pending_rewires = {}
        for index, relation in enumerate(plan["relations"]):
            if "superseded_by" not in relation["updates"]:
                continue
            step = f"relation:{index}"
            marker = bucket_mgr.inspect_import_operation(f"merge:{operation_id}:{step}")
            if step not in completed and not (marker and marker["marker"]):
                pending_rewires[relation["bucket_id"]] = relation["updates"]["superseded_by"]
        if pending_rewires:
            bucket_mgr.validate_supersession_rewire(source_id, target_id, pending_rewires)
        relation_child_key = f"merge:{operation_id}:related-delete"
        relation_child = bucket_mgr.relation_store.lookup(relation_child_key)
        current_source = await bucket_mgr.get(source_id)
        if current_source is None and relation_child is None:
            raise RelatedError('legacy_merge_relation_unrecoverable')
        if relation_child is None:
            relation_preview = bucket_mgr.preview_related_delete(source_id, target_id=target_id)
            if (plan.get('related_inventory') is not None
                    and relation_preview['inventory'] != plan['related_inventory']):
                raise RelatedError('related_plan_stale')
        persist("running")
        target_step = bucket_mgr.inspect_import_operation(f"merge:{operation_id}:target")
        if "target" not in completed and not (target_step and target_step["marker"]):
            original_target = await bucket_mgr.get(target_id)
            if (not original_target or
                hashlib.sha256(str(original_target["content"]).encode("utf-8")).hexdigest()
                    != plan["target_content_sha256"] or
                _merge_metadata_digest(original_target["metadata"])
                    != plan["target_metadata_sha256"]):
                raise RuntimeError("target changed after confirmation")
        await update_step("target", target_id, plan["target_update"])
        for index, relation in enumerate(plan["relations"]):
            step = f"relation:{index}"
            marker = bucket_mgr.inspect_import_operation(f"merge:{operation_id}:{step}")
            if (step not in completed and not (marker and marker["marker"])
                    and relation["bucket_id"] != target_id):
                current = await bucket_mgr.get(relation["bucket_id"])
                if (not current or _merge_metadata_digest(current["metadata"])
                        != relation["before_sha256"]):
                    raise RuntimeError(f"relation target changed: {relation['bucket_id']}")
            await update_step(f"relation:{index}", relation["bucket_id"],
                              relation["updates"])
        if "delete_source" not in completed:
            source = await bucket_mgr.get(source_id)
            if source is not None:
                if (hashlib.sha256(str(source.get("content", "")).encode("utf-8")).hexdigest()
                        != plan["source_content_sha256"]):
                    raise RuntimeError("source changed after confirmation")
                if _merge_metadata_digest(source["metadata"]) != plan["source_metadata_sha256"]:
                    raise RuntimeError("source metadata changed after confirmation")
                if "delete_started" not in completed:
                    completed.append("delete_started")
                    persist("running")
                if not await bucket_mgr.delete(source_id,
                        _allow_sealed=plan["source_sealed"],
                        _relation_target=target_id,
                        _relation_operation_key=relation_child_key,
                        _relation_expected_inventory=plan.get('related_inventory'),
                        _expected_todo_state=(source["metadata"].get("todos"),
                                              source["metadata"].get("todo_provenance"))):
                    raise RuntimeError("source deletion failed")
            elif relation_child is None:
                raise RelatedError('legacy_merge_relation_unrecoverable')
            else:
                bucket_mgr.relation_store.commit(lambda inv: None,
                    operation_key=relation_child_key,
                    request_digest=related_digest({'source': source_id, 'target': target_id}))
            completed.append("delete_source")
            persist("running")
        persist("complete")
        return (f"operation_id: {operation_id}\n已合并 {source_id} → {target_id}: "
                f"importance={plan['target_update']['importance']}, "
                f"valence={plan['target_update']['valence']:.3f}, "
                f"arousal={plan['target_update']['arousal']:.3f}, "
                f"tags={','.join(str(tag) for tag in plan['target_update']['tags'])}")
    except Exception as exc:
        try:
            persist("failed")
        except Exception:
            logger.exception("Could not persist merge failure state for %s", operation_id)
        logger.error("Merge operation %s failed: %s", operation_id, exc)
        remaining = [step for step in steps if step not in completed]
        return (f"operation_id: {operation_id}\nmerge partial failure: {exc}\n"
                f"completed: {', '.join(completed) or '(none)'}\n"
                f"remaining: {', '.join(remaining) or '(none)'}\n"
                f"Retry trace(bucket_id={target_id}, merge={source_id}) to resume this operation.")
    finally:
        with _mutation_confirm_lock:
            _merge_running_operations.discard(operation_id)


def _split_search_results(matches: list[dict], max_results: int) -> tuple[list[dict], list[dict], int]:
    """Return all pinned matches plus a separately limited non-pinned result set."""
    pinned = [
        bucket for bucket in matches
        if bucket.get("metadata", {}).get("pinned")
        or bucket.get("metadata", {}).get("protected")
    ]
    regular = [
        bucket for bucket in matches
        if bucket not in pinned
    ]
    pinned.sort(key=lambda bucket: float(bucket.get("score", 0)), reverse=True)
    regular.sort(key=lambda bucket: float(bucket.get("score", 0)), reverse=True)
    hidden_count = max(0, len(regular) - max_results)
    return pinned, regular[:max_results], hidden_count


def _is_sealed(bucket: dict) -> bool:
    """Return True when a bucket is manually sealed."""
    return int(bucket.get("metadata", {}).get("sealed", 0) or 0) == 1


def _confirmation_payload_digest(payload: dict) -> str:
    """Return a canonical digest for an in-memory mutation confirmation plan."""
    encoded = _json_lib.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _issue_mutation_confirmation(operation: str, payload: dict) -> str:
    """Create a short-lived, one-shot confirmation token for one exact plan."""
    now = time.monotonic()
    with _mutation_confirm_lock:
        expired = [
            token
            for token, entry in _mutation_confirm_tokens.items()
            if float(entry.get("expires_at", 0)) <= now
        ]
        for token in expired:
            _mutation_confirm_tokens.pop(token, None)
        while len(_mutation_confirm_tokens) >= _MUTATION_CONFIRM_MAX_TOKENS:
            _mutation_confirm_tokens.pop(next(iter(_mutation_confirm_tokens)), None)
        token = secrets.token_urlsafe(18)
        _mutation_confirm_tokens[token] = {
            "operation": operation,
            "payload_digest": _confirmation_payload_digest(payload),
            "expires_at": now + _MUTATION_CONFIRM_TTL_SECONDS,
        }
    return token


def _consume_mutation_confirmation(operation: str, payload: dict, token: str) -> bool:
    """Consume a confirmation only when its operation, plan, and TTL still match."""
    candidate = (token or "").strip()
    if not candidate:
        return False
    now = time.monotonic()
    expected_digest = _confirmation_payload_digest(payload)
    with _mutation_confirm_lock:
        entry = _mutation_confirm_tokens.get(candidate)
        if not entry or float(entry.get("expires_at", 0)) <= now:
            _mutation_confirm_tokens.pop(candidate, None)
            return False
        if not (
            hmac.compare_digest(str(entry.get("operation", "")), operation)
            and hmac.compare_digest(str(entry.get("payload_digest", "")), expected_digest)
        ):
            return False
        _mutation_confirm_tokens.pop(candidate, None)
    return True


def _delete_confirmation_payload(buckets: list[dict], plans: list[dict]) -> dict:
    """Bind a delete confirmation to the exact target state shown in its preview."""
    targets = []
    for bucket in buckets:
        metadata = bucket.get("metadata", {})
        targets.append(
            {
                "bucket_id": str(bucket.get("id", "")),
                "name": str(metadata.get("name", "")),
                "importance": int(metadata.get("importance", 0) or 0),
                "updated_at": str(metadata.get("updated_at", "")),
                "content_sha256": hashlib.sha256(
                    str(bucket.get("content", "")).encode("utf-8")
                ).hexdigest(),
                "pinned": bool(metadata.get("pinned")),
                "protected": bool(metadata.get("protected")),
                "sealed": bool(_is_sealed(bucket)),
                "superseded_by": _superseded_by_id(metadata),
            }
        )
    return {"targets": targets, "plan": plans}


def _delete_preview_excerpt(bucket: dict, limit: int = 80) -> str:
    text = re.sub(r"\s+", " ", strip_wikilinks(str(bucket.get("content", "")))).strip()
    return text[:limit]


def _format_delete_confirmation(buckets: list[dict], plans: list[dict], token: str) -> str:
    lines = ["删除确认：本次不会删除。"]
    for bucket, plan in zip(buckets, plans):
        metadata = bucket.get("metadata", {})
        lines.append(
            f"- bucket_id:{bucket['id']} name:{metadata.get('name', bucket['id'])} "
            f"importance:{int(metadata.get('importance', 0) or 0)} "
            f"preview:{_delete_preview_excerpt(bucket)}"
        )
        if plan["outgoing_supersession"]:
            lines.append(
                f"  将清理 successor {plan['outgoing_supersession']} 的反向 supersedes 记录。"
            )
        if plan["incoming_related"]:
            lines.append(
                "  将清理 incoming related_buckets 引用: "
                + ", ".join(plan["incoming_related"])
            )
        if plan.get('sealed_backlink_count'):
            lines.append(f"  存在 {plan['sealed_backlink_count']} 个 sealed backlink 需要清理。")
    lines.append(f"confirm_token: {token}")
    lines.append(
        f"确认有效期: {_MUTATION_CONFIRM_TTL_SECONDS} 秒；请使用相同 bucket_id 和 delete=True 重试。"
    )
    return "\n".join(lines)


async def _prepare_trace_delete(
    bucket_ids: list[str],
) -> tuple[list[dict] | None, str, list[dict]]:
    """Validate every delete target before issuing or consuming any confirmation."""
    buckets = []
    plans = []
    inventory = scan_relation_store(config['buckets_dir'])
    try:
        inventory.require_complete()
    except RelatedError as exc:
        kinds = ','.join(sorted({b['kind'] for b in inventory.blockers}))
        return None, f"delete blocked: {exc.code} ({kinds}); backlink integrity unknown.", []
    all_buckets = await bucket_mgr.list_all(include_archive=True)
    for bucket_id in bucket_ids:
        bucket = await bucket_mgr.get(bucket_id)
        if not bucket:
            return None, f"未找到记忆桶: {bucket_id}", plans
        try:
            bucket_mgr.assert_confirmed_delete_writable(bucket_id)
        except BucketIdempotencyError as exc:
            return None, 'delete blocked: '+exc.code, plans
        metadata = bucket.get("metadata", {})
        protections = [
            name for name, active in (
                ("sealed", _is_sealed(bucket)),
                ("pinned", metadata.get("pinned")),
                ("protected", metadata.get("protected")),
            ) if active
        ]
        inbound = [
            item for item in all_buckets
            if _superseded_by_id(item.get("metadata", {})) == bucket_id
        ]
        related = sorted(
            str(item.get("id", "")) for item in all_buckets
            if not _is_sealed(item)
            and bucket_id in _related_ids(item.get("metadata", {}))
        )
        plan = {
            "bucket_id": bucket_id,
            "name": str(metadata.get("name", bucket_id)),
            "importance": int(metadata.get("importance", 0) or 0),
            "protections": protections,
            "incoming_supersession": sorted(str(item.get("id", "")) for item in inbound),
            "incoming_related": related,
            "outgoing_supersession": _superseded_by_id(metadata),
            "related_cleanup": True,
            "sealed_backlink_count": sum(1 for endpoint in inventory.endpoints.values()
                if endpoint.metadata.get('sealed') and bucket_id in endpoint.related.ids),
            "related_plan_digest": related_digest(bucket_mgr.preview_related_delete(bucket_id)),
        }
        plans.append(plan)
        if protections:
            return None, f"删除失败：记忆桶 {bucket_id} 受到保护。", plans
        if inbound:
            inbound_ids = ", ".join(str(item.get("id", "")) for item in inbound)
            return None, (
                f"删除失败：记忆桶 {bucket_id} 仍被以下作废关系引用: {inbound_ids}。"
                "请先用 trace(superseded_by='') 解除或改指向。"
            ), plans
        try:
            bucket_mgr.confirmed_delete_supersession_guard(inventory,bucket_id,_superseded_by_id(metadata))
        except RelatedError as exc:
            return None,'delete blocked: '+exc.code,plans
        buckets.append(bucket)
    return buckets, "", plans


async def _execute_trace_delete(delete_id: str) -> tuple[bool, str]:
    """Only a durably accepted child may execute this destructive path."""
    if not isinstance(delete_id,str):
        raise RelatedError('confirmed_delete_evidence_missing')
    message = await bucket_mgr.execute_confirmed_delete(delete_id)
    return bucket_mgr._confirmed_row(delete_id)['status'] == 'completed', message


def _confirmed_delete_projection(bucket_ids, rows, admission_error=None):
    completed = [r['bucket_id'] for r in rows if r['status']=='completed']
    started = [r['bucket_id'] for r in rows]
    not_started = [i for i in bucket_ids if i not in started]
    inventory = scan_relation_store(config['buckets_dir'])
    remaining = None if inventory.blockers else [i for i in bucket_ids if i in inventory.endpoints]
    blocked = [{'bucket_id':r['bucket_id'],'error_code':r['last_error_code'],'accepted':True}
               for r in rows if r['status']=='blocked']
    if admission_error:
        blocked.append({'bucket_id':not_started[0] if not_started else '',
                        'error_code':admission_error,'accepted':False})
    projection = {'attempt_id':rows[0]['attempt_id'] if rows else None,'requested_ids':bucket_ids,
                  'started_ids':started,'completed_ids':completed,
                  'pending_ids':[r['bucket_id'] for r in rows if r['status']=='pending'],
                  'blocked':blocked,'not_started_ids':not_started,'remaining_existing_ids':remaining,
                  'absent_without_completion_evidence_ids':None if remaining is None else
                    [i for i in bucket_ids if i not in remaining and i not in completed],
                  'fresh_confirmation_required':bool(not_started)}
    if len(completed)==len(bucket_ids):
        return {'status':'deleted','message':'\n'.join(r['result_text'] for r in rows),'plan':[],
                'deleted_ids':completed}
    message = '删除失败：delete incomplete; accepted children only.\n'+_json_lib.dumps(projection,ensure_ascii=False,sort_keys=True)
    if not_started:
        message += '\nRemaining items require a fresh preview and confirmation token.'
    return {'status':'incomplete','message':message,'plan':[],'deleted_ids':completed,**projection}


def _confirmed_delete_token_entry(token):
    with _mutation_confirm_lock:
        entry = _mutation_confirm_tokens.get(token)
        if (not entry or entry['operation'] != 'trace.delete' or entry['expires_at'] <= time.monotonic()):
            return None
        return entry


async def _delete_with_confirmation(bucket_ids: list[str], confirm_token: str) -> dict:
    """One-shot admission; consumed tokens can locate existing children only."""
    supplied = (confirm_token or '').strip()
    invalid = {'status':'invalid','message':'删除确认无效、已过期或与当前目标不匹配；请重新预览后确认。','plan':[]}
    token_hash = bucket_mgr.confirmed_token_hash(supplied) if supplied else None
    if supplied:
        lock_key = (os.getpid(),asyncio.get_running_loop(),'s4e-attempt',str(Path(config['buckets_dir']).resolve()),token_hash)
        lock = _confirmed_delete_locks.setdefault(lock_key,asyncio.Lock())
        async with lock:
            return await _confirmed_delete_confirm(bucket_ids,supplied,token_hash,invalid)
    buckets,error,plans = await _prepare_trace_delete(bucket_ids)
    if buckets is None:
        return {'status':'blocked','message':error,'plan':plans}
    payload = _delete_confirmation_payload(buckets,plans)
    with bucket_write_scope(config['buckets_dir']):
        inventory = scan_relation_store(config['buckets_dir']); inventory.require_complete()
        incarnations = {i:bucket_mgr._confirmed_file_guard(e.path)['incarnation'] for i,e in inventory.endpoints.items()}
        frozen = [bucket_mgr.freeze_confirmed_delete(inventory,i,incarnations) for i in bucket_ids]
        if any(related_digest(p['relation_plan']) != shown['related_plan_digest'] for p,shown in zip(frozen,plans)):
            return invalid
        current = [bucket_mgr._load_bucket(str(Path(config['buckets_dir'])/inventory.endpoint(i).path)) for i in bucket_ids]
        if _delete_confirmation_payload(current,plans) != payload:
            return invalid
        token = _issue_mutation_confirmation('trace.delete',payload)
        with _mutation_confirm_lock:
            _mutation_confirm_tokens[token].update(delete_inventory=copy.deepcopy(inventory),
                delete_incarnations=incarnations,delete_frozen=frozen,delete_requested=list(bucket_ids))
    return {'status':'preview','message':_format_delete_confirmation(buckets,plans,token),'plan':plans,
            'confirm_token':token,'expires_in_seconds':_MUTATION_CONFIRM_TTL_SECONDS}


async def _confirmed_delete_confirm(bucket_ids,supplied,token_hash,invalid):
    with bucket_write_scope(config['buckets_dir']):
        rows = bucket_mgr.confirmed_delete_rows(token_hash=token_hash)
    if rows:
        digest = rows[0]['confirmation_payload_digest']
        if any(r['requested_ids'] != bucket_ids or r['confirmation_payload_digest'] != digest
               or r['bucket_id'] != bucket_ids[r['ordinal']] for r in rows):
            return invalid
        for row in rows:
            if row['status'] != 'completed':
                await _execute_trace_delete(row['delete_id'])
        return _confirmed_delete_projection(bucket_ids,bucket_mgr.confirmed_delete_rows(token_hash=token_hash))
    entry = _confirmed_delete_token_entry(supplied)
    if entry is None or entry.get('delete_requested') != bucket_ids:
        return invalid
    buckets,error,plans = await _prepare_trace_delete(bucket_ids)
    if buckets is None:
        return {'status':'blocked','message':error,'plan':plans}
    payload = _delete_confirmation_payload(buckets,plans)
    if entry['payload_digest'] != _confirmation_payload_digest(payload):
        return invalid
    inventory = copy.deepcopy(entry['delete_inventory'])
    incarnations = copy.deepcopy(entry['delete_incarnations'])
    first = entry['delete_frozen'][0]
    # No await or business effect between final guard checks, consumption and acceptance.
    with bucket_write_scope(config['buckets_dir']):
        live = scan_relation_store(config['buckets_dir']); live.require_complete()
        if live.fingerprint() != inventory.fingerprint():
            return invalid
        for identity in bucket_ids:
            endpoint = live.endpoint(identity)
            guard = bucket_mgr._confirmed_file_guard(endpoint.path)
            if guard['file_hash'] != inventory.endpoint(identity).file_hash or guard['incarnation'] != incarnations[identity]:
                return invalid
            bucket_mgr.assert_confirmed_delete_writable(identity)
            try:
                bucket_mgr.confirmed_delete_supersession_guard(live,identity,entry['delete_frozen'][bucket_ids.index(identity)]['successor_id'])
            except RelatedError:
                return invalid
        if not _consume_mutation_confirmation('trace.delete',payload,supplied):
            return invalid
        bucket_mgr.confirmed_delete_checkpoint('token_consumed',{})
        try:
            delete_id = bucket_mgr.accept_confirmed_delete(token_hash,entry['payload_digest'],bucket_ids,0,first)
        except Exception:
            accepted = bucket_mgr.confirmed_delete_rows(token_hash=token_hash)
            if not accepted:
                return _confirmed_delete_projection(bucket_ids,[], 'confirmed_delete_authorization_lost')
            delete_id = accepted[0]['delete_id']
    for ordinal,identity in enumerate(bucket_ids):
        if ordinal:
            plan = bucket_mgr.freeze_confirmed_delete(inventory,identity,incarnations)
            try:
                delete_id = bucket_mgr.accept_confirmed_delete(token_hash,entry['payload_digest'],bucket_ids,ordinal,plan)
            except Exception:
                return _confirmed_delete_projection(bucket_ids,bucket_mgr.confirmed_delete_rows(token_hash=token_hash),
                                                    'confirmed_delete_accept_pending')
        await bucket_mgr.confirmed_delete_pause('child_accepted',{'delete_id':delete_id})
        ok,_ = await _execute_trace_delete(delete_id)
        if not ok:
            break
        row = bucket_mgr._confirmed_row(delete_id)
        try:
            bucket_mgr.project_confirmed_delete(inventory,incarnations,row)
        except RelatedError:
            # The current call cannot prove a later child plan; it admits no later item.
            return _confirmed_delete_projection(bucket_ids,bucket_mgr.confirmed_delete_rows(token_hash=token_hash),
                                                'confirmed_delete_projection_unproven')
        await bucket_mgr.confirmed_delete_pause('between_children',{'delete_id':delete_id})
    return _confirmed_delete_projection(bucket_ids,bucket_mgr.confirmed_delete_rows(token_hash=token_hash))


async def _trace_delete_with_confirmation(bucket_ids: list[str], confirm_token: str) -> str:
    """Preserve the MCP text contract while using the shared delete plan."""
    outcome = await _delete_with_confirmation(bucket_ids, confirm_token)
    return outcome["message"]


def _dashboard_delete_response(bucket_id: str, outcome: dict, *, review: bool = False):
    from starlette.responses import JSONResponse

    status = outcome["status"]
    if status == "deleted":
        body = {"id": bucket_id, "deleted": True}
        if review:
            body.update(applied=1, errors=0)
        return JSONResponse(body)
    body = {
        "id": bucket_id,
        "deleted": False,
        "status": status,
        "plan": outcome.get("plan", []),
        "message": outcome["message"],
    }
    if status == "incomplete":
        for key in ('attempt_id','requested_ids','started_ids','completed_ids','pending_ids','blocked',
                    'not_started_ids','remaining_existing_ids','absent_without_completion_evidence_ids','fresh_confirmation_required'):
            body[key] = outcome[key]
    if status == "preview":
        body["confirm_token"] = outcome["confirm_token"]
        body["expires_in_seconds"] = outcome["expires_in_seconds"]
    else:
        body["error"] = "bucket_delete_failed" if status == "failed" else "delete_confirmation_required"
    if review:
        body.update(applied=0, errors=0 if status == "preview" else 1)
    return JSONResponse(body, status_code=200 if status == "preview" else 500 if status == "failed" else 409)


def _extract_session_summary(content: str, max_chars: int | None = 700) -> str:
    """Extract the Summary section from an archived session bucket."""
    text = strip_wikilinks(content or "").strip()
    marker = "## Summary"
    if marker in text:
        text = text.split(marker, 1)[1].strip()
        if "\n## " in text:
            text = text.split("\n## ", 1)[0].strip()
    return text[:max_chars].strip() if max_chars is not None else text


def _format_mailbox(
    limit: int = 1, include_sealed: bool = False, *, exclude_session_ids: set[str] | None = None,
) -> str:
    letters = bucket_mgr.get_letters(
        limit, include_sealed=include_sealed,
        **({"exclude_session_ids": exclude_session_ids} if exclude_session_ids else {}),
    )
    if not letters:
        return "=== 信箱 ===\n（暂无信件）"
    parts = ["=== 信箱 ==="]
    for letter in letters:
        parts.append(
            f"[letter_id:{letter.get('id')}] "
            f"created_at:{letter.get('created_at')} "
            f"session_id:{letter.get('session_id')}\n"
            f"{letter.get('content', '')}"
        )
    return "\n---\n".join(parts)


def _format_note_preview(text: str, limit: int = 80) -> str:
    """Make a bounded one-line preview without changing the stored note body."""
    compact = " ".join((text or "").split())
    return compact[:limit] + ("…" if len(compact) > limit else "")


def _parse_note_open_at(value: str) -> str:
    """Normalize an optional local open_at timestamp for note visibility."""
    value = (value or "").strip()
    if not value:
        return ""
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("open_at must use ISO local date/time format.") from exc
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone().replace(tzinfo=None)
    return parsed.isoformat(timespec="seconds")


def _note_delivery_state(note: dict) -> str:
    if note.get("dismissed_at"):
        return "dismissed"
    if note.get("skipped_at"):
        return "skipped_for_delivery"
    if note.get("boot_delivered_at"):
        return "boot_delivered"
    return "pending"


def _format_ting_note_for_boot(now: str, max_tokens: int) -> tuple[str, int | None]:
    """Prepare the first boot section without claiming delivery before full text fits."""
    candidate = bucket_mgr.get_latest_note_delivery_candidate(available_at=now)
    if candidate:
        full_text = (
            "=== boot: 婷留言 ===\n"
            f"[note_id:{candidate['note_id']}] {candidate['created_at']} "
            f"{candidate['author']}\n{candidate['text']}"
        )
        if count_tokens_approx(full_text) <= max_tokens - BOOT_TRUNCATION_NOTICE_TOKENS:
            return full_text, int(candidate["note_id"])
        return (
            "=== boot: 婷留言 ===\n"
            f"婷有新留言 #{candidate['note_id']}，全文请用 "
            f"get_note(note_id={candidate['note_id']}) 读取",
            None,
        )

    latest = bucket_mgr.get_latest_visible_note(available_at=now)
    if latest is None:
        return "=== boot: 婷留言 ===\n婷留言：无（暂无历史留言）", None
    return (
        "=== boot: 婷留言 ===\n"
        f"婷留言：无（上次留言 #{latest['note_id']}，{latest['created_at']}）",
        None,
    )


def _format_bucket_truncation_notice(bucket_id: str, shown: int, total: int) -> str:
    return (
        f"[…已截断：bucket {bucket_id}，显示 {shown} / {total} 字符；"
        f"完整内容可用 dream(detail_ids=\"{bucket_id}\") 读取]"
    )


def _format_boot_preview(
    bucket: dict,
    max_chars: int,
    *,
    show_truncation: bool = False,
) -> str:
    """Return a display-safe bucket preview with an optional truncation marker."""
    content = strip_wikilinks(bucket.get("content", "")).strip()
    preview = content[:max_chars]
    if show_truncation and len(content) > len(preview):
        bucket_id = str(bucket.get("id", ""))
        return f"{preview}\n{_format_bucket_truncation_notice(bucket_id, len(preview), len(content))}"
    return preview


def _format_tg_summary_refresh_notice(bucket_id: str, state: str, source_hash: str) -> str:
    """Tell the caller how to refresh a missing or stale TG summary safely."""
    label = "尚未生成" if state == "missing" else "已过期"
    return (
        f"[…TG summary {label}：bucket {bucket_id}；当前原文 source_hash:{source_hash}；"
        f"请先用 dream(detail_ids=\"{bucket_id}\") 读取全文，再按 refresh_tg_summary "
        "的 generation contract 生成并保存压缩版]"
    )


def _format_tg_summary_preview(bucket: dict, source_hash: str, summary: str) -> str:
    """Render a valid caller-generated TG summary with a source-of-truth reminder."""
    bucket_id = str(bucket.get("id", ""))
    return (
        f"[TG 压缩版：bucket {bucket_id}；source_hash:{source_hash}]\n"
        f"{summary}\n"
        f"[这是压缩版；原 bucket 是唯一真实来源；需要细节时用 "
        f"dream(detail_ids=\"{bucket_id}\") 读取全文]"
    )


async def _format_due_triggers(
    active_buckets: list[dict],
    max_items: int = 10,
    *,
    show_preview_truncation: bool = False,
) -> tuple[str, list[tuple[str, int]]]:
    today = datetime.now().date().isoformat()
    due = []
    for bucket in active_buckets:
        meta = bucket.get("metadata", {})
        trigger_date = str(meta.get("trigger_date", "") or "").strip()
        if not trigger_date or trigger_date > today:
            continue
        if meta.get("resolved", False) or _is_sealed(bucket):
            continue
        if str(meta.get("trigger_last_seen", "") or "") == today:
            continue
        due.append(bucket)
    due.sort(key=lambda b: (str(b["metadata"].get("trigger_date", "")), -int(b["metadata"].get("importance", 0) or 0)))
    shown = due[:max_items]
    lines = []
    for bucket in shown:
        meta = bucket.get("metadata", {})
        preview = _format_boot_preview(
            bucket,
            300,
            show_truncation=show_preview_truncation,
        )
        lines.append(
            f"[bucket_id:{bucket['id']}] {meta.get('name', bucket['id'])} "
            f"trigger_date:{meta.get('trigger_date')}\n{preview}"
        )
    header = "=== boot: 今日浮现 ===\n"
    text = header + ("\n---\n".join(lines) if lines else "（今日无到期提醒）")
    end = len(header)
    complete_items = []
    for bucket, line in zip(shown, lines):
        end += len(line)
        complete_items.append((bucket["id"], end))
        end += len("\n---\n")
    return text, complete_items


def _format_feel_echo(active_buckets: list[dict]) -> str:
    feels = [
        bucket for bucket in active_buckets
        if bucket.get("metadata", {}).get("type") == "feel"
        and not _is_sealed(bucket)
        and not _is_test_bucket(bucket)
    ]
    if not feels:
        return "=== boot: 回声 ===\n（暂无可见 feel）"
    bucket = random.choice(feels)
    meta = bucket.get("metadata", {})
    created = _bucket_date(meta, "created_at", "created")
    content = strip_wikilinks(bucket.get("content", "")).strip()
    return (
        "=== boot: 回声 ===\n"
        f"[bucket_id:{bucket['id']}] {meta.get('name', bucket['id'])} created_at:{created}\n"
        f"{content}"
    )


BOOT_TRUNCATION_NOTICE_TOKENS = 160
BOOT_TG_TRUNCATION_NOTICE_TOKENS = 600
BOOT_DELTA_MAX_TOKENS = 600
TG_SUMMARY_MAX_CHARS = 1200
TG_SUMMARY_GENERATION_CONTRACT = """\
TG summary 是 pinned bucket 的紧凑开机上下文，不是新的事实来源；原 bucket 正文永远是 source of truth。
调用方必须先用 dream(detail_ids=...) 阅读当前原文，再忠实压缩，绝不补充原文没有的信息。
若原文含行首中文编号 part（如 一、/二、/三、），必须覆盖每个 part 的核心内容，不能因前面较长而遗漏后面部分。
优先保留当前状态、重要关系或身份、明确约束、协作规则、婷的稳定偏好和仍有效的重要事实；删除重复、铺垫、例子及低价值措辞。
摘要最多 1200 个 Unicode 字符，适合 TG 紧凑上下文；summary 正文只写压缩后的有效内容，不要附加“这是压缩版”、原 bucket 是 source of truth 或 dream 全文指引——这些由 TG boot 统一追加。
"""
TG_SUMMARY_TOOL_DESCRIPTION = (
    "Store a caller-generated TG pinned-summary; no external model is called.\n\n"
    "TG summary generation contract:\n"
    + TG_SUMMARY_GENERATION_CONTRACT
)
BOOT_SECTION_MINIMUM_CHARS = {
    "mailbox": 1000,
    "todos": 1500,
    "sessions": 1200,
    "pinned": 4000,
}
BOOT_PROFILE_CONFIG = {
    "talk": {
        "max_tokens": 16000,
        "pinned_chars": 5000,
        "delta_tokens": 600,
        "trigger_items": 10,
        "include_mailbox": True,
        "include_sessions": True,
        "include_echo": True,
        "section_minimums": BOOT_SECTION_MINIMUM_CHARS,
    },
    "code": {
        "max_tokens": 12000,
        "pinned_chars": 2500,
        "delta_tokens": 400,
        "trigger_items": 10,
        "include_mailbox": True,
        "include_sessions": True,
        "include_echo": False,
        "section_minimums": {
            "mailbox": 700,
            "todos": 1200,
            "sessions": 800,
            "pinned": 2500,
        },
    },
    "tg": {
        "max_tokens": 4000,
        "pinned_chars": 600,
        "delta_tokens": 220,
        "trigger_items": 5,
        "include_mailbox": True,
        "include_sessions": False,
        "include_echo": False,
        "section_minimums": {
            "mailbox": 600,
            "todos": 600,
            "pinned": 600,
        },
    },
}
BOOT_PROFILE_CODE_ROOTS = frozenset({"项目", "工程", "工具", "环境", "部署"})
BOOT_PROFILE_TG_MIN_IMPORTANCE = 8
BOOT_PROFILE_NAMES = frozenset(BOOT_PROFILE_CONFIG)


def _profile_metadata_labels(bucket: dict) -> set[str]:
    """Return normalized structured labels without inspecting bucket prose."""
    meta = bucket.get("metadata", {})
    labels = []
    for key in ("domain", "tags", "topics"):
        value = meta.get(key, [])
        if isinstance(value, str):
            value = [value]
        if isinstance(value, list):
            labels.extend(str(item).strip() for item in value if str(item).strip())
    return {label.split("/", 1)[0].casefold() for label in labels}


def _profile_is_code_context(bucket: dict) -> bool:
    return bool(_profile_metadata_labels(bucket) & BOOT_PROFILE_CODE_ROOTS)


def _profile_is_global_constraint(bucket: dict) -> bool:
    meta = bucket.get("metadata", {})
    return bool(
        meta.get("pinned")
        or meta.get("protected")
        or int(meta.get("importance", 0) or 0) >= 9
    )


def _profile_allows_bucket(bucket: dict, profile: str) -> bool:
    """Conservatively filter display-only profile sections from metadata."""
    if _is_sealed(bucket):
        return False
    if profile == "talk":
        return True
    if profile == "code":
        return _profile_is_global_constraint(bucket) or _profile_is_code_context(bucket)
    return _profile_is_global_constraint(bucket) or (
        int(bucket.get("metadata", {}).get("importance", 0) or 0)
        >= BOOT_PROFILE_TG_MIN_IMPORTANCE
    )


def _prefix_within_token_budget(text: str, token_budget: int) -> str:
    """Return the longest text prefix that fits the approximate token budget."""
    if token_budget <= 0:
        return ""
    if count_tokens_approx(text) <= token_budget:
        return text
    low, high = 0, len(text)
    while low < high:
        middle = (low + high + 1) // 2
        if count_tokens_approx(text[:middle]) <= token_budget:
            low = middle
        else:
            high = middle - 1
    return text[:low].rstrip()


def _fit_sections_to_budget(
    sections: list[tuple[str, str, str]],
    max_tokens: int,
    *,
    minimum_chars: dict[str, int] | None = None,
    atomic_sections: set[str] | None = None,
    omission_item_refs: dict[str, list[str]] | None = None,
    omission_item_ends: dict[str, list[tuple[str, int]]] | None = None,
    truncation_notice_tokens: int = BOOT_TRUNCATION_NOTICE_TOKENS,
    return_sections: bool = False,
    todo_display: TodoPage | None = None,
) -> str | tuple[str, dict[str, str]]:
    """Fit named sections in priority order and report every omitted block."""
    if not sections:
        return ("", {}) if return_sections else ""
    minimum_chars = minimum_chars or {}
    atomic_sections = atomic_sections or set()
    omission_item_refs = omission_item_refs or {}
    omission_item_ends = omission_item_ends or {}
    total_tokens = (
        count_tokens_approx("\n\n".join(text for _, _, text in sections))
        if todo_display is not None
        else sum(count_tokens_approx(text) for _, _, text in sections)
    )
    if total_tokens <= max_tokens:
        body = "\n\n".join(text for _, _, text in sections)
        return (body, {key: text for key, _, text in sections}) if return_sections else body

    # Per-section rounding and separators must fit too. The existing notice
    # reserve also protects a complete todo summary if no content slot survives.
    content_budget = max(0, max_tokens - truncation_notice_tokens - (
        len(sections) if todo_display is not None else 0
    ))
    requested_tokens = {}
    for key, _, text in sections:
        minimum = max(0, minimum_chars.get(key, 0))
        if key == "triggers":
            requested_tokens[key] = count_tokens_approx(text)
        elif minimum > 0:
            requested_tokens[key] = count_tokens_approx(text[:minimum])

    reserved_tokens = {}
    remaining_reserve = content_budget
    # If every guarantee fits, later sections keep their full reservation.
    # Otherwise the same pass assigns the available budget in output order.
    for key, _, _ in sections:
        requested = requested_tokens.get(key, 0)
        reserved = min(requested, remaining_reserve)
        if key in requested_tokens:
            reserved_tokens[key] = reserved
        remaining_reserve -= reserved
    output = []
    emitted_sections: dict[str, str] = {}
    complete = []
    partial = []
    omitted = []

    def _omission_detail(
        key: str,
        display_name: str,
        emitted_text: str,
        *,
        partial_output: bool,
    ) -> str:
        refs = omission_item_refs.get(key, [])
        if not refs:
            return display_name
        if key in omission_item_ends:
            ends = omission_item_ends[key]
            emitted_refs = [ref for ref, end in ends if end <= len(emitted_text)]
            omitted_refs = [ref for ref, end in ends if end > len(emitted_text)]
            complete_count = len(emitted_refs)
        else:
            emitted_refs = [ref for ref in refs if ref in emitted_text]
            omitted_refs = [ref for ref in refs if ref not in emitted_text]
            complete_count = len(emitted_refs)
            if partial_output and emitted_refs:
                last_emitted = emitted_refs[-1]
                if last_emitted not in omitted_refs:
                    omitted_refs.append(last_emitted)
                    complete_count -= 1
        omitted_label = "、".join(omitted_refs) or "正文尾部"
        continuation = ""
        if key == "mailbox":
            letter_ids = [
                ref.split(":", 1)[1]
                for ref in omitted_refs
                if ref.startswith("letter_id:")
            ]
            if letter_ids:
                continuation = (
                    "；完整内容请用 "
                    + "、".join(
                        f"get_letter(letter_id={letter_id})"
                        for letter_id in letter_ids
                    )
                    + " 读取"
                )
        return (
            f"{display_name}（原 {len(refs)} 项，完整输出 {complete_count} 项；"
            f"省略/截断：{omitted_label}{continuation}）"
        )

    used = 0
    priority_exhausted = False
    todo_index = None
    todo_summary_tokens = 0

    for index, (key, display_name, text) in enumerate(sections):
        later_reserve = sum(
            reserved_tokens.get(later_key, 0)
            for later_key, _, _ in sections[index + 1 :]
        )
        is_reserved = key in reserved_tokens
        if priority_exhausted and not is_reserved:
            omitted.append(
                _omission_detail(key, display_name, "", partial_output=False)
            )
            continue

        available = max(0, content_budget - used - later_reserve)
        if key == "todos" and todo_display is not None:
            fitted = fit_todos(todo_display, available)
            todo_index = len(output)
            output.append(fitted.text)
            emitted_sections[key] = fitted.text
            if count_tokens_approx(fitted.text) <= available:
                used += count_tokens_approx(fitted.text)
            else:
                todo_summary_tokens = count_tokens_approx(fitted.text) + 1
            if fitted.hidden:
                priority_exhausted = True
            continue
        section_tokens = count_tokens_approx(text)
        if section_tokens <= available:
            output.append(text)
            emitted_sections[key] = text
            complete.append(display_name)
            used += section_tokens
            continue

        if key in atomic_sections:
            omitted.append(
                _omission_detail(key, display_name, "", partial_output=False)
            )
            priority_exhausted = True
            continue

        prefix = _prefix_within_token_budget(text, available)
        if prefix:
            output.append(prefix)
            emitted_sections[key] = prefix
            partial.append(
                _omission_detail(key, display_name, prefix, partial_output=True)
            )
            used += count_tokens_approx(prefix)
        else:
            omitted.append(
                _omission_detail(key, display_name, "", partial_output=False)
            )
        priority_exhausted = True

    notice_lines = ["已按 boot 预算截断："]
    if partial:
        notice_lines.append("- 部分截断：" + "、".join(partial))
    if omitted:
        notice_lines.append("- 未输出：" + "、".join(omitted))
    notice = "\n".join(notice_lines)
    notice_budget = max(0, truncation_notice_tokens - todo_summary_tokens)
    if count_tokens_approx(notice) > notice_budget:
        if omission_item_refs and (todo_display is None or notice_budget >= 100):
            overflow = (
                "\n- 截断说明的 ID 清单仅列前缀；未完整输出的钉选项"
                "另见完整 TG summary recovery receipts。"
                if "pinned" in omission_item_ends else
                "\n- 省略 ID 清单超出本次 TG 预算；仅列出前缀，"
                "未列出的稳定 ID 无法在当前紧凑输出中完整列出。"
            )
            notice = (
                _prefix_within_token_budget(
                    notice,
                    max(0, notice_budget - count_tokens_approx(overflow)),
                )
                + overflow
            )
        else:
            notice = _prefix_within_token_budget(
                notice,
                notice_budget,
            )
    output.append(notice)
    if todo_index is not None:
        def _measure_final_todos(text: str) -> int:
            candidate_output = list(output)
            candidate_output[todo_index] = text
            return count_tokens_approx("\n\n".join(candidate_output))

        # Reclaim unused lower-section/notice budget without changing any other
        # emitted text. The page was captured before fitting and CAS retries.
        fitted = fit_todos(todo_display, max_tokens, measure=_measure_final_todos)
        output[todo_index] = fitted.text
        emitted_sections["todos"] = fitted.text
    body = "\n\n".join(output)
    return (body, emitted_sections) if return_sections else body


TG_RECOVERY_HEADER = "=== boot: TG summary recovery receipts ==="
TG_RECOVERY_OVER_BUDGET = (
    "为了保证 TG summary 可恢复，本次响应超过了配置内容预算；"
    "普通 boot 内容未输出，recovery receipts 不代表其他内容已经投递。"
)


def _fit_tg_boot_sections(
    sections: list[tuple[str, str, str]],
    max_tokens: int,
    *,
    recovery_items: list[tuple[str, str, str, int]],
    **fit_options,
) -> tuple[str, dict[str, str]]:
    """Reserve unique receipts within the content budget; keep the legacy envelope."""
    unique_items = {}
    for item in recovery_items:
        unique_items.setdefault(item[0], item)
    recovery_items = list(unique_items.values())
    fit_options["omission_item_refs"] = {
        **(fit_options.get("omission_item_refs") or {}),
        "pinned": [f"bucket_id:{item[0]}" for item in recovery_items],
    }
    fit_options["omission_item_ends"] = {
        **(fit_options.get("omission_item_ends") or {}),
        "pinned": [(f"bucket_id:{item[0]}", item[3]) for item in recovery_items],
    }

    def measure(body: str) -> int:
        # Profile/seal retain their baseline envelope semantics, independent of
        # the ordinary content budget and any protected recovery overflow.
        return count_tokens_approx(body)

    def receipts(ids: set[str]) -> str:
        lines = [
            f"- {bucket_id} [{state}, source_hash:{source_hash}]"
            for bucket_id, state, source_hash, _ in recovery_items
            if bucket_id in ids
        ]
        return TG_RECOVERY_HEADER + "\n" + "\n".join(lines) if lines else ""

    def join(body: str, receipt_text: str) -> str:
        return "\n\n".join(part for part in (body, receipt_text) if part)

    omitted: set[str] = set()
    all_ids = {item[0] for item in recovery_items}
    while True:
        receipt_text = receipts(omitted)
        # Preserve the baseline 40-token ordinary-content margin. It is not a
        # length limit on the response seal. Receipts share the remaining space.
        budget = max(0, max_tokens - 40 - measure(receipt_text) - bool(receipt_text))
        body, emitted = "", {}
        if measure(receipt_text) <= max_tokens:
            while budget > 0:
                body, emitted = _fit_sections_to_budget(
                    sections, budget, return_sections=True, **fit_options,
                )
                excess = measure(join(body, receipt_text)) - max_tokens
                if excess <= 0:
                    break
                budget = max(0, budget - excess)
            else:
                # Even the ordinary minimum (e.g. the todo summary) cannot fit.
                # Only recovery metadata may exceed the configured budget.
                body, emitted = "", {}
        actual_omitted = {
            bucket_id for bucket_id, _, _, end in recovery_items
            if end > len(emitted.get("pinned", ""))
        }
        expanded = omitted | actual_omitted
        if expanded != omitted:
            omitted = expanded
            continue  # At least one new ID; at most len(recovery_items) expansions.
        receipt_text = receipts(actual_omitted)
        if measure(receipt_text) > max_tokens:
            # Zero ordinary output means every pinned item needs a receipt.
            receipt_text = receipts(all_ids)
            return join(receipt_text, TG_RECOVERY_OVER_BUDGET), {}
        return join(body, receipt_text), emitted


def _boot_delta_locator(bucket: dict) -> str:
    """Return a bounded stable locator without exposing bucket content."""
    meta = bucket.get("metadata", {})
    name = str(meta.get("name", bucket.get("id", ""))).strip()
    if len(name) > 80:
        name = name[:77].rstrip() + "..."
    return f"[bucket_id:{bucket['id']}] {name or bucket['id']}"


def _format_boot_delta(
    *,
    checkpoint: dict | None,
    high_water: int,
    visible_buckets: list[dict],
    max_tokens: int = BOOT_DELTA_MAX_TOKENS,
    include_omitted_ids: bool = False,
    return_progress: bool = False,
) -> str | tuple[str, int]:
    """Format complete, bounded boot-delta records from the durable event log."""
    header = "=== boot: 增量摘要 ==="
    if checkpoint is None:
        text = f"{header}\n（暂无上次 boot 基线）"
        return (text, high_water) if return_progress else text

    visible_by_id = {
        str(bucket.get("id", "")): bucket
        for bucket in visible_buckets
        if bucket.get("id") and not _is_sealed(bucket)
    }
    events = bucket_mgr.get_boot_delta_events(
        int(checkpoint["last_event_id"]),
        high_water,
    )
    records: list[tuple[str, int, int, str, str]] = []
    for event in events:
        bucket = visible_by_id.get(str(event.get("bucket_id", "")))
        if bucket is None:
            continue
        payload = event.get("payload", {})
        event_type = event.get("event_type")
        if event_type == "created":
            detail = "新建"
        elif event_type == "content_updated":
            detail = "正文已修改"
        elif event_type == "todos_updated":
            closed = int(payload.get("closed_count", 0) or 0)
            opened = int(payload.get("opened_count", 0) or 0)
            if closed:
                detail = f"todo 已关闭 {closed} 项"
            elif opened:
                detail = f"todo 已更新，新增 {opened} 项"
            else:
                detail = "todo 状态已更新"
        elif event_type == "superseded":
            detail = (
                "已标记 invalidated"
                if payload.get("mode") == "none"
                else "已标记 superseded"
            )
        else:
            continue
        importance = int(bucket.get("metadata", {}).get("importance", 0) or 0)
        records.append(
            (
                str(event.get("occurred_at", "")),
                importance,
                int(event.get("id", 0) or 0),
                str(bucket["id"]),
                f"- {_boot_delta_locator(bucket)}：{detail}",
            )
        )

    if not records:
        text = f"{header}\n（无新增变化）"
        return (text, high_water) if return_progress else text

    # A scalar checkpoint can only cross a contiguous prefix of eligible events.
    # Keep the existing display priority within the selected prefix.
    records.sort(key=lambda item: item[2])
    selected: list[tuple[str, int, int, str, str]] = []
    omitted_ids: list[str] = []

    def _display(items: list[tuple[str, int, int, str, str]]) -> list[str]:
        return [item[4] for item in sorted(items, key=lambda item: item[:3], reverse=True)]

    def _omitted_suffix(bucket_ids: list[str]) -> str:
        suffix = f"（还有 {len(bucket_ids)} 项未展开"
        if not include_omitted_ids:
            return suffix + "）"
        suffix += "：bucket_id:" + "、".join(bucket_ids) + "）"
        available = max_tokens - count_tokens_approx(header)
        if count_tokens_approx(suffix) <= available:
            return suffix
        prefix = f"（还有 {len(bucket_ids)} 项未展开：bucket_id:"
        overflow = "；ID 清单超出 TG delta 预算，仅列出前缀）"
        shown_ids = []
        for bucket_id in bucket_ids:
            candidate = "、".join([*shown_ids, bucket_id])
            if count_tokens_approx(prefix + candidate + overflow) > available:
                break
            shown_ids.append(bucket_id)
        return prefix + "、".join(shown_ids) + overflow

    for index, record in enumerate(records):
        remaining = len(records) - index - 1
        candidate = [header, *_display([*selected, record])]
        if remaining:
            remaining_ids = [item[3] for item in records[index + 1 :]]
            candidate.append(_omitted_suffix(remaining_ids))
        if count_tokens_approx("\n".join(candidate)) <= max_tokens:
            selected.append(record)
            continue
        omitted_ids = [item[3] for item in records[index:]]
        break

    lines = [header, *_display(selected)]
    if omitted_ids:
        lines.append(_omitted_suffix(omitted_ids))
    text = "\n".join(lines)
    safe_event_id = selected[-1][2] if omitted_ids and selected else (
        int(checkpoint["last_event_id"]) if omitted_ids else high_water
    )
    return (text, safe_event_id) if return_progress else text


def _digest_api_config() -> tuple[str, str, str]:
    api_key = os.environ.get("OMBRE_DIGEST_API_KEY", "").strip()
    base_url = os.environ.get("OMBRE_DIGEST_BASE_URL", "https://api.deepseek.com/v1").strip()
    model = os.environ.get("OMBRE_DIGEST_MODEL", "deepseek-chat").strip()
    return api_key, base_url.rstrip("/"), model


def _days_since(value: str) -> int:
    try:
        dt = datetime.fromisoformat(str(value))
        # Normalize timezone metadata before computing the age.
        return max(0, (datetime.now() - dt.replace(tzinfo=None)).days)
    except (ValueError, TypeError):
        return 9999


async def _digest_candidates() -> list[dict]:
    cutoff_days = int(os.environ.get("OMBRE_DIGEST_MIN_DAYS", "30") or "30")
    buckets = await bucket_mgr.list_all(include_archive=False)
    candidates = []
    for bucket in buckets:
        meta = bucket.get("metadata", {})
        if meta.get("type", "dynamic") != "dynamic":
            continue
        if meta.get("pinned") or meta.get("protected") or _is_sealed(bucket):
            continue
        if meta.get("digested", False) or meta.get("resolved", False):
            continue
        if int(meta.get("importance", 5) or 5) > 4:
            continue
        if _days_since(meta.get("last_active") or meta.get("created")) < cutoff_days:
            continue
        candidates.append(bucket)
    candidates.sort(key=_digest_bucket_order_key)
    return candidates


async def _importance_rebalance_candidates() -> list[dict]:
    buckets = await bucket_mgr.list_all(include_archive=False)
    candidates = []
    for bucket in buckets:
        meta = bucket.get("metadata", {})
        if meta.get("type") == "permanent" or meta.get("pinned") or meta.get("protected") or _is_sealed(bucket):
            continue
        importance = int(meta.get("importance", 0) or 0)
        if importance < 8:
            continue
        if _days_since(meta.get("created") or meta.get("created_at")) <= 30:
            continue
        candidates.append(bucket)
    candidates.sort(
        key=lambda b: (
            -int(b.get("metadata", {}).get("importance", 0) or 0),
            str(b.get("metadata", {}).get("created") or b.get("metadata", {}).get("created_at") or ""),
        )
    )
    return candidates


def _digest_bucket_state(bucket: dict) -> dict:
    metadata = bucket.get("metadata", {})
    return {
        "bucket_id": str(bucket.get("id", "")),
        "importance": int(metadata.get("importance", 0) or 0),
        "type": str(metadata.get("type", "dynamic")),
        "pinned": bool(metadata.get("pinned")),
        "protected": bool(metadata.get("protected")),
        "sealed": _is_sealed(bucket),
        "digested": bool(metadata.get("digested")),
        "resolved": bool(metadata.get("resolved")),
        "created": str(metadata.get("created") or metadata.get("created_at") or ""),
        "last_active": str(metadata.get("last_active", "")),
        "updated_at": str(metadata.get("updated_at", "")),
        "content_sha256": hashlib.sha256(str(bucket.get("content", "")).encode("utf-8")).hexdigest(),
    }


def _digest_timestamp_sort_key(bucket: dict) -> tuple[int, tuple[int, ...]]:
    """Sort parseable activity/creation timestamps oldest-first, then missing values."""
    metadata = bucket.get("metadata", {})
    for field in ("last_active", "created", "created_at"):
        value = metadata.get(field)
        if not value:
            continue
        try:
            parsed = datetime.fromisoformat(str(value))
        except (TypeError, ValueError):
            continue
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone(timezone.utc)
        return 0, (
            parsed.year,
            parsed.month,
            parsed.day,
            parsed.hour,
            parsed.minute,
            parsed.second,
            parsed.microsecond,
        )
    return 1, ()


def _digest_bucket_order_key(
    bucket: dict,
) -> tuple[int, tuple[int, tuple[int, ...]], str]:
    metadata = bucket.get("metadata", {})
    return (
        int(metadata.get("importance", 0) or 0),
        _digest_timestamp_sort_key(bucket),
        str(bucket.get("id", "")),
    )


def _digest_group_order_key(
    item: tuple[str, list[dict]],
) -> tuple[int, tuple[int, tuple[int, ...]], str]:
    domain, buckets = item
    return (
        min(int(bucket.get("metadata", {}).get("importance", 0) or 0) for bucket in buckets),
        min(_digest_timestamp_sort_key(bucket) for bucket in buckets),
        str(domain),
    )


def _digest_confirmation_payload(selected: list[tuple[str, list[dict]]], rebalance_candidates: list[dict]) -> dict:
    """Retain the prior plan shape while issuing a token for each kind separately."""
    return {
        "groups": [
            {"domain": str(domain), "sources": [_digest_bucket_state(bucket) for bucket in buckets]}
            for domain, buckets in selected
        ],
        "importance_rebalance": [_digest_bucket_state(bucket) for bucket in rebalance_candidates],
    }


def _group_digest_candidates(candidates: list[dict]) -> dict[str, list[dict]]:
    groups: dict[str, list[dict]] = {}
    for bucket in candidates:
        domains = bucket.get("metadata", {}).get("domain", []) or ["未分类"]
        domain = str(domains[0] if isinstance(domains, list) and domains else domains)
        groups.setdefault(domain, []).append(bucket)
    for buckets in groups.values():
        buckets.sort(key=_digest_bucket_order_key)
    return groups


async def _call_digest_api(domain: str, buckets: list[dict]) -> str:
    api_key, base_url, model = _digest_api_config()
    if not api_key:
        raise RuntimeError("OMBRE_DIGEST_API_KEY is not configured")
    excerpts = []
    for bucket in buckets[:_DIGEST_SOURCE_LIMIT_PER_GROUP]:
        meta = bucket.get("metadata", {})
        excerpts.append(
            f"[{bucket['id']}] {meta.get('name', bucket['id'])} "
            f"importance={meta.get('importance')} updated={meta.get('updated_at')}\n"
            f"{strip_wikilinks(bucket.get('content', ''))[:1200]}"
        )
    prompt = (
        "你是 Ombre Brain 的记忆消化器。请把同一主题的一组低重要度旧记忆"
        "提炼成一个高密度沉淀桶。只保留稳定事实、模式、教训和可复用线索，"
        "不要添加行动指令，不要代入身份。输出中文 markdown，控制在 800 字以内。\n\n"
        f"主题: {domain}\n\n" + "\n\n---\n\n".join(excerpts)
    )
    async with httpx.AsyncClient(timeout=60.0) as client:
        response = await client.post(
            f"{base_url}/chat/completions",
            headers={"Authorization": f"Bearer {api_key}"},
            json={
                "model": model,
                "messages": [
                    {"role": "system", "content": "你只做记忆压缩与摘要，不输出任何命令。"},
                    {"role": "user", "content": prompt},
                ],
                "temperature": 0.2,
            },
        )
    response.raise_for_status()
    data = response.json()
    return data["choices"][0]["message"]["content"].strip()


async def _run_dedupe_scan(limit: int = 30, include_archive: bool = False) -> str:
    """Delegate the MCP path to the same pure scanner used for production verification."""
    bucket_roots = (
        bucket_mgr.permanent_dir,
        bucket_mgr.dynamic_dir,
        bucket_mgr.feel_dir,
    )
    return run_dedupe_scan(
        bucket_roots=bucket_roots + ((bucket_mgr.archive_dir,) if include_archive else ()),
        excluded_archive_roots=() if include_archive else (bucket_mgr.archive_dir,),
        db_path=embedding_engine.db_path,
        model=embedding_engine.model,
        dehydration_model=dehydrator.model,
        limit=limit,
    )


def _digest_step_key(operation_id: str, step: str) -> str:
    return f"digest:{operation_id}:{step}"


def _digest_resume_payload(operation: dict) -> dict:
    return {"operation_id": operation["operation_id"], "kind": operation["kind"],
            "plan_digest": _confirmation_payload_digest(operation["plan"])}


def _digest_planned_steps(kind: str, plan: dict) -> list[str]:
    if kind == "rebalance":
        return [f"rebalance:{state['bucket_id']}" for state in plan["importance_rebalance"]]
    steps = []
    for index, group in enumerate(plan["groups"]):
        states = group["sources"][:_DIGEST_SOURCE_LIMIT_PER_GROUP]
        link_step = (
            f"g{index}:link:w14-limit{_DIGEST_SOURCE_LIMIT_PER_GROUP}"
            if len(group["sources"]) > _DIGEST_SOURCE_LIMIT_PER_GROUP
            else f"g{index}:link"
        )
        steps.extend([f"g{index}:create", link_step])
        steps.extend(f"g{index}:source:{state['bucket_id']}" for state in states)
    return [*steps, "log:create"]


def _claim_digest_token(token: str, kind: str, plan: dict, *, operation: dict | None = None) -> dict | None:
    """Create/claim the durable record before consuming a process-local token."""
    candidate = (token or "").strip()
    expected = _digest_resume_payload(operation) if operation else plan
    operation_name = "digest.resume" if operation else f"digest.{kind}"
    now = time.monotonic()
    with _mutation_confirm_lock:
        entry = _mutation_confirm_tokens.get(candidate)
        if (not entry or float(entry.get("expires_at", 0)) <= now or
                entry.get("operation") != operation_name or
                entry.get("payload_digest") != _confirmation_payload_digest(expected)):
            return None
        operation_id = operation["operation_id"] if operation else secrets.token_hex(12)
        if operation_id in _digest_running_operations:
            return None
        if operation:
            bucket_mgr.write_digest_operation(
                operation_id, kind, plan, owner=_RM_PROCESS_BOOT_ID,
                status="running", outputs=operation["outputs"], completed=operation["completed"],
                recover=operation["owner"] != _RM_PROCESS_BOOT_ID,
            )
        else:
            bucket_mgr.write_digest_operation(operation_id, kind, plan, owner=_RM_PROCESS_BOOT_ID)
        records = bucket_mgr.read_digest_operations()
        claimed = next(row for row in records if row["operation_id"] == operation_id)
        _mutation_confirm_tokens.pop(candidate, None)
        _digest_running_operations.add(operation_id)
    return claimed


async def _digest_apply_step(operation: dict, step: str, operation_kind: str,
                             payload: dict, target_id: str | None = None) -> str:
    key = _digest_step_key(operation["operation_id"], step)
    result = await bucket_mgr.apply_import_operation(
        key, operation_kind=operation_kind, target_bucket_id=target_id, payload=payload,
    )
    inspected = bucket_mgr.inspect_import_operation(key)
    if not inspected or not inspected["marker"]:
        raise RuntimeError(f"digest step {step} has no durable write marker")
    written = await bucket_mgr.get(result["result_id"])
    if not written:
        raise RuntimeError(f"digest step {step} has no resulting bucket")
    if operation_kind == "update":
        for field, expected in payload["kwargs"].items():
            if written["metadata"].get(field) != expected:
                raise RuntimeError(f"digest step {step} did not write {field}")
    if step not in operation["completed"]:
        operation["completed"].append(step)
        bucket_mgr.write_digest_operation(
            operation["operation_id"], operation["kind"], operation["plan"],
            owner=_RM_PROCESS_BOOT_ID, outputs=operation["outputs"],
            completed=operation["completed"],
        )
    return result["result_id"]


async def _digest_require_source(state: dict, step: str, operation: dict) -> dict:
    bucket = await bucket_mgr.get(state["bucket_id"])
    if not bucket:
        raise RuntimeError(f"digest source missing: {state['bucket_id']}")
    if step not in operation["completed"] and _digest_bucket_state(bucket) != state:
        marker = bucket_mgr.inspect_import_operation(_digest_step_key(operation["operation_id"], step))
        if not marker or not marker["marker"]:
            raise RuntimeError(f"digest source changed: {state['bucket_id']}")
    return bucket


async def _execute_digest_operation(operation: dict) -> str:
    operation_id, kind, plan = operation["operation_id"], operation["kind"], operation["plan"]
    try:
        if kind == "consolidation":
            digested_total = 0
            log_entries = []
            for index, group in enumerate(plan["groups"]):
                domain = group["domain"]
                all_states = group["sources"]
                states = all_states[:_DIGEST_SOURCE_LIMIT_PER_GROUP]
                legacy_omitted = all_states[_DIGEST_SOURCE_LIMIT_PER_GROUP:]
                if legacy_omitted:
                    unsafe_steps = [
                        f"g{index}:source:{state['bucket_id']}"
                        for state in legacy_omitted
                    ]
                    for unsafe_step in unsafe_steps:
                        marker = bucket_mgr.inspect_import_operation(
                            _digest_step_key(operation_id, unsafe_step)
                        )
                        if unsafe_step in operation["completed"] or (marker and marker["marker"]):
                            raise RuntimeError(
                                "legacy digest plan already marked a source beyond the safe per-group limit"
                            )
                    logger.warning(
                        "Digest operation %s group %s has %s legacy sources; limiting execution to %s",
                        operation_id,
                        index,
                        len(all_states),
                        _DIGEST_SOURCE_LIMIT_PER_GROUP,
                    )
                source_ids = [state["bucket_id"] for state in states]
                digest_step = f"g{index}:create"
                buckets = [await _digest_require_source(state, f"g{index}:source:{state['bucket_id']}", operation)
                           for state in states]
                output_key = f"g{index}:provider"
                if output_key not in operation["outputs"]:
                    operation["outputs"][output_key] = await _call_digest_api(domain, buckets)
                    bucket_mgr.write_digest_operation(
                        operation_id, kind, plan, owner=_RM_PROCESS_BOOT_ID,
                        outputs=operation["outputs"], completed=operation["completed"],
                    )
                for state in states:
                    await _digest_require_source(state, f"g{index}:source:{state['bucket_id']}", operation)
                digest_id = await _digest_apply_step(operation, digest_step, "create", {
                    "content": operation["outputs"][output_key], "tags": ["digest", "auto-digested"],
                    "importance": 6, "domain": [domain, "digest"], "valence": 0.5,
                    "arousal": 0.3, "provenance_kind": "summary",
                    "name": f"digest_{domain}_{plan['date']}",
                })
                link_step = (
                    f"g{index}:link:w14-limit{_DIGEST_SOURCE_LIMIT_PER_GROUP}"
                    if legacy_omitted
                    else f"g{index}:link"
                )
                await _digest_apply_step(operation, link_step, "update",
                                         {"kwargs": {"source_bucket": ",".join(source_ids)}}, digest_id)
                for state in states:
                    source_id = state["bucket_id"]
                    step = f"g{index}:source:{source_id}"
                    await _digest_require_source(state, step, operation)
                    await _digest_apply_step(operation, step, "update",
                                             {"kwargs": {"digested": True, "source_bucket": digest_id}}, source_id)
                    digested_total += 1
                log_entries.append(f"[{digest_id}] {domain}: {', '.join(source_ids)}")
            log_content = ("# 自动消化日志\n\n" + f"- 时间: {plan['date']}\n"
                           + f"- 消化桶数: {digested_total}\n\n" + "\n".join(log_entries))
            log_id = await _digest_apply_step(operation, "log:create", "create", {
                "content": log_content, "tags": ["digest-log"], "importance": 5,
                "domain": ["system", "digest"], "valence": 0.5, "arousal": 0.3,
                "provenance_kind": "system",
                "name": f"digest_log_{plan['date']}",
            })
            result = f"已消化: {digested_total} 个桶\ndigest log bucket: {log_id}"
        else:
            for state in plan["importance_rebalance"]:
                bucket_id = state["bucket_id"]
                step = f"rebalance:{bucket_id}"
                bucket = await _digest_require_source(state, step, operation)
                meta = bucket["metadata"]
                if step not in operation["completed"] and (meta.get("type") == "permanent" or
                        meta.get("pinned") or meta.get("protected") or _is_sealed(bucket)):
                    raise RuntimeError(f"rebalance source protected: {bucket_id}")
                await _digest_apply_step(operation, step, "update",
                                         {"kwargs": {"importance": state["importance"] - 1}}, bucket_id)
            result = f"importance rebalanced: {len(plan['importance_rebalance'])}"
        bucket_mgr.write_digest_operation(
            operation_id, kind, plan, owner=_RM_PROCESS_BOOT_ID, status="complete",
            outputs=operation["outputs"], completed=operation["completed"],
        )
        return f"operation_id: {operation_id}\n{result}"
    except Exception as exc:
        bucket_mgr.write_digest_operation(
            operation_id, kind, plan, owner=_RM_PROCESS_BOOT_ID, status="failed",
            outputs=operation["outputs"], completed=operation["completed"],
        )
        token = _issue_mutation_confirmation("digest.resume", _digest_resume_payload(operation))
        remaining = [step for step in _digest_planned_steps(kind, plan)
                     if step not in operation["completed"]]
        logger.error("Digest operation %s failed: %s", operation_id, exc)
        return (f"operation_id: {operation_id}\npartial failure: {type(exc).__name__}: {exc}\n"
                f"completed steps: {len(operation['completed'])}\n"
                f"remaining steps ({len(remaining)}): {', '.join(remaining[:20])}"
                f"{' ...' if len(remaining) > 20 else ''}\nresume_confirm_token: {token}")
    finally:
        with _mutation_confirm_lock:
            _digest_running_operations.discard(operation_id)


async def _run_digest(dry_run: bool = True, max_groups: int = 10, confirm_token: str = "",
                      limit: int = 30) -> str:
    if not isinstance(limit, int) or limit < 0:
        return "limit 必须是非负整数。"
    display_limit = min(limit, 500)
    open_operations = bucket_mgr.read_digest_operations(open_only=True)
    supplied = (confirm_token or "").strip()
    if supplied and not dry_run:
        for pending in open_operations:
            claimed = _claim_digest_token(supplied, pending["kind"], pending["plan"], operation=pending)
            if claimed:
                return await _execute_digest_operation(claimed)

    candidates = await _digest_candidates()
    rebalance_candidates = await _importance_rebalance_candidates()
    groups = _group_digest_candidates(candidates)
    effective_group_limit = max(1, max_groups)
    ordered_groups = sorted(groups.items(), key=_digest_group_order_key)
    selected_groups = ordered_groups[:effective_group_limit]
    selected = [
        (domain, buckets[:_DIGEST_SOURCE_LIMIT_PER_GROUP])
        for domain, buckets in selected_groups
    ]
    omitted_groups = len(ordered_groups) - len(selected_groups)
    lines = [
        "=== 自动消化 dry-run ===",
        f"候选桶数（全部 maintenance consolidation 候选）: {len(candidates)}",
        (
            f"主题组: 总数 {len(ordered_groups)} / 选中 {len(selected_groups)} / "
            f"因 max_groups={max_groups} 省略 {omitted_groups}"
            f"（legacy 有效下限 1；仅限制 consolidation，不影响 importance rebalance）"
        ),
    ]
    for (domain, all_group_buckets), (_, planned_buckets) in zip(selected_groups, selected):
        deferred = len(all_group_buckets) - len(planned_buckets)
        lines.append(
            f"- {domain}: 候选 {len(all_group_buckets)} / 本次计划 {len(planned_buckets)} / "
            f"留待后续 {deferred} -> {', '.join(bucket['id'] for bucket in planned_buckets)}"
        )
    if rebalance_candidates:
        lines.append("=== importance rebalance dry-run ===")
        total = len(rebalance_candidates)
        lines.append(f"总候选 {total} 项 / 当前显示 {min(total, display_limit)} 项 / 确认后实际执行 {total} 项")
        distribution: dict[str, int] = {}
        for bucket in rebalance_candidates:
            importance = int(bucket.get("metadata", {}).get("importance", 0) or 0)
            key = f"{importance}->{importance - 1}"
            distribution[key] = distribution.get(key, 0) + 1
        lines.append("分布: " + ", ".join(f"{key}: {count}" for key, count in sorted(distribution.items(), reverse=True)))
        for bucket in rebalance_candidates[:display_limit]:
            meta = bucket.get("metadata", {})
            importance = int(meta.get("importance", 0) or 0)
            created = meta.get("created") or meta.get("created_at") or ""
            lines.append(f"- bucket_id:{bucket['id']} importance:{importance}->{importance - 1} created:{created}")

    payloads = {
        "consolidation": {"groups": _digest_confirmation_payload(selected, [])["groups"],
                          "date": datetime.now().date().isoformat()},
        "rebalance": {"importance_rebalance": _digest_confirmation_payload([], rebalance_candidates)["importance_rebalance"]},
    }
    pending_by_kind = {item["kind"]: item for item in open_operations}
    if not selected and not rebalance_candidates and not open_operations:
        return "\n".join(lines + ["No digest or importance rebalance candidates."])
    for kind in ("consolidation", "rebalance"):
        if kind in pending_by_kind:
            pending = pending_by_kind[kind]
            remaining = [step for step in _digest_planned_steps(kind, pending["plan"])
                         if step not in pending["completed"]]
            lines.append(f"unfinished {kind} operation_id: {pending['operation_id']}; "
                         f"completed steps: {len(pending['completed'])}; remaining steps: {len(remaining)}")
            if not supplied:
                token = _issue_mutation_confirmation("digest.resume", _digest_resume_payload(pending))
                lines.append(f"resume_confirm_token: {token}")
            continue
        if kind == "consolidation" and not selected or kind == "rebalance" and not rebalance_candidates:
            continue
        payload = payloads[kind]
        if supplied and not dry_run:
            claimed = _claim_digest_token(supplied, kind, payload)
            if claimed:
                return await _execute_digest_operation(claimed)
        if not supplied:
            token = _issue_mutation_confirmation(f"digest.{kind}", payload)
            label = "confirm_token" if kind == "consolidation" or not selected else "rebalance_confirm_token"
            lines.append(f"{label}: {token}")
    if not dry_run:
        lines.append("confirmation required: supply the matching, unexpired confirm_token for exactly one plan.")
    return "\n".join(lines)


async def _auto_link_related(bucket_id: str, threshold: float | None = None, top_k: int = 3, *, _expected_source=None) -> list[tuple[str, float]]:
    """Bidirectionally link a new bucket to its closest non-sealed semantic neighbors."""
    def empty_result():
        if _expected_source is not None:
            with bucket_write_scope(config['buckets_dir']):
                bucket_mgr.admit_delayed_effect(bucket_id, _expected_source, 'related')
        return []
    if threshold is None:
        threshold = float(os.environ.get("OMBRE_RELATED_THRESHOLD", "0.75") or "0.75")
    if not embedding_engine or not embedding_engine.enabled:
        return empty_result()
    inventory = scan_relation_store(config['buckets_dir'])
    bucket = await bucket_mgr.get(bucket_id)
    if not bucket or not automatic_eligible(inventory, bucket_id):
        return empty_result()
    target_embedding = await embedding_engine.get_embedding(bucket_id)
    if target_embedding is None:
        # BucketManager owns lifecycle generation; related-linking never retries it.
        # A provider failure therefore remains best-effort without replay retries.
        # Sealed targets already returned above and never reach this path.
        return empty_result()
    all_buckets = await bucket_mgr.list_all(include_archive=False)
    scored = []
    for other in all_buckets:
        other_id = other["id"]
        if other_id == bucket_id or not automatic_eligible(inventory, other_id):
            continue
        other_embedding = await embedding_engine.get_embedding(other_id)
        if other_embedding is None:
            continue
        score = embedding_engine._cosine_similarity(target_embedding, other_embedding)
        if score >= threshold:
            scored.append((other_id, score))
    scored.sort(key=lambda item: item[1], reverse=True)
    selected = scored[:max(1, top_k)]
    if not selected:
        return empty_result()

    with bucket_write_scope(config['buckets_dir']):
        if _expected_source is not None:
            bucket_mgr.admit_delayed_effect(bucket_id, _expected_source, 'related')
        bucket_mgr.mutate_related(bucket_id, add=[identity for identity, _ in selected], origin='inferred')
    return selected


async def _run_related_backfill(dry_run: bool = True, limit: int = 100, threshold: float | None = None) -> str:
    if threshold is None:
        threshold = float(os.environ.get("OMBRE_RELATED_THRESHOLD", "0.75") or "0.75")
    # No lazy proxy access on dry-run: no runtime, provider, decay or recovery.
    from related_integrity import read_vectors
    runtime = _runtime_components
    if runtime is not None:
        engine = runtime['embedding_engine']
        enabled, db_path, model = engine.enabled, engine.db_path, engine.model
    else:
        embedding = config.get('embedding', {})
        dehy = config.get('dehydration', {})
        key = embedding.get('api_key') or ('' if embedding.get('independent') else dehy.get('api_key')) or ''
        enabled = bool(str(key).strip()) and embedding.get('enabled', True)
        db_path = os.path.join(config['buckets_dir'], 'embeddings.db')
        model = embedding.get('model', 'gemini-embedding-001')
    if not enabled:
        return "自动 related 回填不可用：embedding 未启用。"
    inventory = scan_relation_store(config['buckets_dir'])
    ids = [i for i in inventory.order if automatic_eligible(inventory, i)][:max(1, limit)]
    vectors = read_vectors(db_path, model)
    planned = []
    for identity in ids:
        target_embedding = vectors.get(identity)
        if target_embedding is None:
            continue
        scored = []
        for other_id in ids:
            if other_id == identity or other_id not in vectors:
                continue
            score = EmbeddingEngine._cosine_similarity(target_embedding, vectors[other_id])
            if score >= threshold:
                scored.append((other_id, score))
        scored.sort(key=lambda item: item[1], reverse=True)
        top = scored[:3]
        if top:
            planned.append((identity, top))
    lines = ["=== 自动 related dry-run ===" if dry_run else "=== 自动 related 回填 ===",
             f"扫描桶数: {len(ids)}", f"计划关联: {len(planned)} 个桶"]
    for identity, links in planned[:50]:
        lines.append(f"- {identity}: " + ', '.join(f"{i}({score:.3f})" for i, score in links))
    if dry_run:
        return '\n'.join(lines)
    applied, unchanged = 0, 0
    for identity, links in planned:
        try:
            result = bucket_mgr.mutate_related(identity, add=[i for i, _ in links], origin='inferred')
        except Exception as exc:
            detail = str(exc) if isinstance(exc, RelatedError) else 'related_commit_failed'
            lines.append(f"partial failure: {detail}; committed: {applied}; unchanged: {unchanged}; later operations not started; failing relation not reported as successful.")
            return '\n'.join(lines)
        applied += int(result['changed'])
        unchanged += int(not result['changed'])
    lines.append(f"committed: {applied}; unchanged: {unchanged}")
    return '\n'.join(lines)


async def _call_conflict_api(new_content: str, old_buckets: list[dict]) -> str:
    api_key, base_url, model = _digest_api_config()
    if not api_key or not old_buckets:
        return ""
    old_parts = []
    for bucket in old_buckets[:3]:
        meta = bucket.get("metadata", {})
        old_parts.append(
            f"[{bucket['id']}] {meta.get('name', bucket['id'])}\n"
            f"{strip_wikilinks(bucket.get('content', ''))[:1200]}"
        )
    prompt = (
        "判断新内容和旧记忆之间是否存在日期、数字或事实上的直接矛盾。"
        "只返回一个 JSON 对象，不要使用 Markdown 代码块或附加文字。"
        "对象必须包含且仅表达以下字段："
        '{"same_fact":布尔值,"conflict":布尔值,"bucket_id":"旧记忆ID",'
        '"evidence_new":"新内容中的原句","evidence_old":"旧记忆中的原句"}。'
        "无冲突时两个布尔值至少一个为 false，其余字符串可为空。"
        "判定冲突时 bucket_id 必须来自给出的旧记忆，且两段 evidence 必须直接支持判断。"
        "必须遵守以下硬规则：同一天发生的不同事件不构成矛盾；"
        "必须先确认描述的是同一主体、同一事实槽位，再判断两个值是否互斥；"
        "不同时间点的状态通常是历史演变，不能仅因值不同而判为冲突；"
        "只要主体、事实槽位或互斥关系有任何不确定，same_fact 或 conflict 必须为 false。"
        "\n\n# 新内容\n"
        f"{strip_wikilinks(new_content)[:1500]}"
        "\n\n# 旧记忆\n"
        + "\n\n---\n\n".join(old_parts)
    )
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post(
            f"{base_url}/chat/completions",
            headers={"Authorization": f"Bearer {api_key}"},
            json={
                "model": model,
                "messages": [
                    {"role": "system", "content": "你只做事实矛盾检测，不输出建议或行动指令。"},
                    {"role": "user", "content": prompt},
                ],
                "temperature": 0,
                "response_format": {"type": "json_object"},
            },
        )
    response.raise_for_status()
    data = response.json()
    return data["choices"][0]["message"]["content"].strip()


def _parse_conflict_response(
    response: str,
    allowed_bucket_ids: set[str],
    *,
    new_content: str,
    old_buckets: list[dict],
) -> dict | None:
    """Parse the strict conflict verdict; every invalid shape fails closed."""
    try:
        payload = _json_lib.loads(response)
    except (TypeError, ValueError, _json_lib.JSONDecodeError):
        return None
    required = {
        "same_fact",
        "conflict",
        "bucket_id",
        "evidence_new",
        "evidence_old",
    }
    if not isinstance(payload, dict) or not required.issubset(payload):
        return None
    if not isinstance(payload["same_fact"], bool) or not isinstance(payload["conflict"], bool):
        return None
    for key in ("bucket_id", "evidence_new", "evidence_old"):
        if not isinstance(payload[key], str):
            return None
        payload[key] = payload[key].strip()
    if payload["same_fact"] and payload["conflict"]:
        old_content_by_id = {
            str(bucket.get("id", "")): strip_wikilinks(bucket.get("content", ""))
            for bucket in old_buckets
        }

        def normalized(value: str) -> str:
            return " ".join(strip_wikilinks(value).lower().split())

        if (
            payload["bucket_id"] not in allowed_bucket_ids
            or not payload["evidence_new"]
            or not payload["evidence_old"]
            or normalized(payload["evidence_new"]) not in normalized(new_content)
            or normalized(payload["evidence_old"])
            not in normalized(old_content_by_id.get(payload["bucket_id"], ""))
        ):
            return None
        payload["evidence"] = {
            "new": payload["evidence_new"],
            "old": payload["evidence_old"],
        }
    return payload


def _format_conflict_warning(verdict: dict) -> str:
    def evidence(value: str) -> str:
        return " ".join(value.split())[:200]

    return (
        f"bucket {verdict['bucket_id']} 同一事实冲突："
        f"新内容「{evidence(verdict['evidence_new'])}」；"
        f"旧记忆「{evidence(verdict['evidence_old'])}」"
    )


def _conflict_tokens(text: str) -> set[str]:
    normalized = strip_wikilinks(_apply_display_aliases(text or "")).lower()
    calendar_dates = {
        f"{match.group(1)}{int(match.group(2)):02d}{int(match.group(3)):02d}"
        for match in re.finditer(
            r"((?:19|20)\d{2})[./-](\d{1,2})[./-](\d{1,2})",
            normalized,
        )
    }
    lexical_tokens = {
        token
        for token in re.findall(r"[a-z0-9_]{3,}|[\u4e00-\u9fff]{2,}", normalized)
        if len(token.strip()) >= 2
        and token not in bucket_mgr.wikilink_stopwords
    }
    return lexical_tokens | calendar_dates


def _is_conflict_temporal_token(token: str) -> bool:
    """Return whether a lexical token only identifies a year or calendar date."""
    normalized = str(token or "").strip().lower()
    return bool(
        re.fullmatch(r"(?:19|20)\d{2}", normalized)
        or re.fullmatch(r"(?:19|20)\d{6}", normalized)
    )


def _candidate_haystack(bucket: dict) -> str:
    meta = bucket.get("metadata", {})
    return " ".join([
        str(meta.get("name", "")),
        str(meta.get("summary", "")),
        " ".join(map(str, meta.get("tags", []) or [])),
        strip_wikilinks(bucket.get("content", "")),
    ])


def _candidate_overlap(query_tokens: set[str], bucket: dict) -> set[str]:
    return query_tokens & _conflict_tokens(_candidate_haystack(bucket))


def _is_visible_recall_bucket(bucket: dict) -> bool:
    """Match normal memory-search visibility without exposing sealed content."""
    metadata = bucket.get("metadata", {})
    return not _is_sealed(bucket) and not bool(metadata.get("dormant", False))


async def _recall_memory_candidates(content: str, limit: int = 8) -> dict:
    """Broad, read-only candidate recall shared by doorbell and conflict checks."""
    candidates = []
    seen = set()
    trace = {}

    def add_bucket(bucket: dict) -> None:
        bucket_id = str(bucket.get("id", "") or "")
        if not bucket_id or bucket_id in seen or not _is_visible_recall_bucket(bucket):
            return
        seen.add(bucket_id)
        candidates.append(bucket)

    try:
        ranked = await bucket_mgr.search(
            content,
            limit=max(limit, 8),
            include_sealed=False,
            trace=trace,
        )
        for bucket in ranked:
            add_bucket(bucket)
    except Exception as exc:
        logger.warning("Shared memory candidate search failed: %s", exc)

    # Keep recall broad enough for conflict detection if hybrid ranking does not
    # admit a lexical candidate. This remains read-only and applies the same
    # archive, sealed, and dormant visibility rules as normal search.
    query_tokens = _conflict_tokens(content)
    if len(candidates) < limit and query_tokens:
        try:
            all_buckets = await bucket_mgr.list_all(include_archive=False)
        except Exception as exc:
            logger.warning("Shared lexical candidate recall failed: %s", exc)
        else:
            lexical = []
            for bucket in all_buckets:
                if not _is_visible_recall_bucket(bucket) or str(bucket.get("id", "")) in seen:
                    continue
                overlap = _candidate_overlap(query_tokens, bucket)
                if overlap:
                    lexical.append((len(overlap), bucket))
            lexical.sort(key=lambda item: item[0], reverse=True)
            for _, bucket in lexical:
                add_bucket(bucket)
                if len(candidates) >= limit:
                    break

    semantic = trace.get("semantic", {"enabled": False, "status": "not_run"})
    if semantic.get("status") == "not_run":
        semantic = {
            "enabled": bool(embedding_engine and getattr(embedding_engine, "enabled", False)),
            "status": (
                "unavailable_or_empty_index"
                if embedding_engine and getattr(embedding_engine, "enabled", False)
                else "disabled"
            ),
        }
    return {
        "candidates": candidates[:limit],
        "semantic": semantic,
    }


async def _conflict_candidate_buckets(content: str, limit: int = 3) -> list[dict]:
    recall = await _recall_memory_candidates(content, limit=max(limit, 8))
    candidates = []
    query_tokens = _conflict_tokens(content)

    for bucket in recall["candidates"]:
        overlap = _candidate_overlap(query_tokens, bucket)
        temporal_overlap = {
            token for token in overlap if _is_conflict_temporal_token(token)
        }
        non_temporal_overlap = overlap - temporal_overlap
        strong_non_temporal_overlap = [
            token for token in non_temporal_overlap
            if len(token) >= 4 or any(ch.isdigit() for ch in token)
        ]
        has_non_temporal_context = bool(non_temporal_overlap)
        qualifies = bool(
            strong_non_temporal_overlap
            or len(non_temporal_overlap) >= 2
            or (temporal_overlap and has_non_temporal_context)
        )
        if qualifies:
            candidates.append(bucket)
        if len(candidates) >= limit:
            break
    return candidates[:limit]


async def _detect_conflict_verdict(content: str) -> dict:
    """Return a structured, fail-closed conflict verdict without mutating memory."""
    if not _conflict_detection_enabled():
        return {"status": "disabled", "same_fact": False, "conflict": False}
    api_key, _base_url, _model = _digest_api_config()
    if not api_key:
        return {
            "status": "unavailable",
            "reason": "digest_api_not_configured",
            "same_fact": False,
            "conflict": False,
        }
    try:
        old_buckets = await _conflict_candidate_buckets(content, limit=3)
        if not old_buckets:
            return {"status": "checked", "same_fact": False, "conflict": False}
        response = await _call_conflict_api(content, old_buckets)
    except Exception as exc:
        logger.warning("Conflict detection failed: %s", exc)
        return {
            "status": "unavailable",
            "reason": "detector_error",
            "same_fact": False,
            "conflict": False,
        }
    verdict = _parse_conflict_response(
        response,
        {str(bucket.get("id", "")) for bucket in old_buckets},
        new_content=content,
        old_buckets=old_buckets,
    )
    if not verdict:
        return {
            "status": "unavailable",
            "reason": "invalid_detector_response",
            "same_fact": False,
            "conflict": False,
        }
    verdict["status"] = "checked"
    return verdict


async def _detect_conflict_warning(content: str) -> str:
    verdict = await _detect_conflict_verdict(content)
    if verdict.get("status") == "unavailable":
        return f"检查未执行：{verdict['reason']}"
    if not (verdict.get("same_fact") and verdict.get("conflict")):
        return ""
    return _format_conflict_warning(verdict)


async def _similarity_doorbell(content: str, threshold: float = 0.80) -> str:
    """Return a pre-write similarity reminder, never a write or a merge decision."""
    recall = await _recall_memory_candidates(content, limit=8)
    semantic = recall.get("semantic", {})
    if semantic.get("status") != "available":
        return f"相似检查未执行：embedding {semantic.get('status', 'unavailable')}"
    scored = []
    for bucket in recall["candidates"]:
        try:
            score = float(bucket.get("semantic_score", 0.0) or 0.0)
        except (TypeError, ValueError):
            continue
        if score >= threshold:
            scored.append((score, bucket))
    if not scored:
        return ""
    score, bucket = max(scored, key=lambda item: item[0])
    metadata = bucket.get("metadata", {})
    name = str(metadata.get("name") or bucket.get("id"))
    return f"与 {name} 相似 {score:.2f}，确定要新开一个桶吗"


async def _digest_scheduler_loop() -> None:
    enabled = os.environ.get("OMBRE_DIGEST_SCHEDULER", "").strip().lower() in ("1", "true", "yes", "on")
    if not enabled:
        return
    dry_run = os.environ.get("OMBRE_DIGEST_DRY_RUN", "true").strip().lower() not in ("0", "false", "no", "off")
    await asyncio.sleep(30)
    last_key = ""
    while True:
        now = datetime.now()
        key = now.strftime("%Y-%m-%d-%H")
        if now.weekday() == 6 and now.hour == 3 and key != last_key:
            last_key = key
            try:
                result = await _run_digest(dry_run=dry_run)
                logger.info("Scheduled digest completed: %s", result[:1000])
            except Exception as exc:
                logger.warning("Scheduled digest failed: %s", exc)
        await asyncio.sleep(600)


# =============================================================
# Tool 1: breath — Breathe
# 工具 1：breath — 呼吸
#
# No args: surface highest-weight unresolved memories (active push)
# 无参数：浮现权重最高的未解决记忆
# With args: search by keyword + emotion coordinates
# 有参数：按关键词+情感坐标检索记忆
# =============================================================
_BREATH_ALLOWED = {
    "ordinary_query": "query, domain, importance_min, tags_filter, recent_days, date_from, date_to, include_dormant, include_sealed, valence/arousal, resonance, min_score, max_results, max_tokens, mode, emotion_trend, touch, wake_dormant; cursor only without tags_filter",
    "session": "query, domain=session, importance_min, tags_filter, topic_filter, recent_days, date_from, date_to, include_sealed, max_results, max_tokens, emotion_trend, touch; mode=full only with query",
    "feel": "query, domain=feel or feels=True, importance_min, tags_filter, recent_days, date_from, date_to, include_sealed, max_results, max_tokens, emotion_trend, touch; mode=full only with query and tags_filter",
    "resonance": "resonance, domain, importance_min, tags_filter, recent_days, date_from, date_to, include_dormant, include_sealed, max_results, max_tokens, emotion_trend, touch, wake_dormant; mode=summary",
    "tags_only": "tags_filter, domain, importance_min, recent_days, date_from, date_to, include_dormant, include_sealed, valence (presentation only), max_results, max_tokens, emotion_trend, touch, wake_dormant; mode=summary",
    "importance_only": "importance_min, domain, recent_days, date_from, date_to, include_dormant, include_sealed, max_results, max_tokens, emotion_trend, touch, wake_dormant; mode=summary",
    "default_emergence": "domain, recent_days, date_from, date_to, include_dormant, include_sealed, max_results, max_tokens, mode, emotion_trend, touch, wake_dormant",
    "historical_query": "as_of, query, domain (normal values), include_dormant, include_sealed, valence+arousal, min_score, max_results, max_tokens, cursor, mode (fixed historical body), touch (always read-only)",
    "mailbox": "mailbox, mailbox_limit, include_sealed",
}


def _breath_parameter_error(selector: str, parameter: str, reason: str) -> str:
    return (
        f"breath mode={selector} 不支持参数 {parameter}：{reason}。\n"
        f"该模式可使用：{_BREATH_ALLOWED[selector]}。"
    )


def _breath_importance_matches(bucket: dict, importance_min: int) -> bool:
    if importance_min == -1:
        return True
    try:
        return int(bucket.get("metadata", {}).get("importance", 0)) >= importance_min
    except (TypeError, ValueError, OverflowError):
        return False


def _prepare_breath_request(**arguments) -> dict:
    """Resolve selectors and reject ignored arguments before reads or writes."""
    request = {
        name: parameter.default
        for name, parameter in inspect.signature(breath).parameters.items()
    }
    request.update(arguments)
    request["mode"] = (request["mode"] or "").strip().lower()
    request["as_of"] = (request["as_of"] or "").strip()
    filter_errors = []
    for name in ("tags_filter", "topic_filter"):
        try:
            request[name] = _normalize_breath_filter(
                request[name], name, apply_aliases=name == "tags_filter"
            )
        except ValueError as exc:
            filter_errors.append((name, str(exc)))
    domains = sorted({part.strip().casefold() for part in (request["domain"] or "").split(",") if part.strip()})
    request["domain"] = ",".join(domains)
    reserved = set(domains) & {"session", "feel"}
    selector = (
        "mailbox" if request["mailbox"] else
        "historical_query" if request["as_of"] else
        "feel" if request["feels"] or reserved == {"feel"} else
        "session" if request["topic_filter"] or reserved == {"session"} else
        "ordinary_query" if request["query"].strip() else
        "resonance" if request["resonance"].strip() else
        "tags_only" if request["tags_filter"] else
        "importance_only" if request["importance_min"] != -1 else
        "default_emergence"
    )
    request["selector"] = selector

    def reject(parameter, reason):
        raise ValueError(_breath_parameter_error(selector, parameter, reason))

    for name, reason in filter_errors:
        reject(name, reason)
    for field in ("valence", "arousal"):
        value = request[field]
        if value != -1 and not 0 <= value <= 1:
            reject(field, f"{field} must be -1 or within 0.0-1.0")
    if request["importance_min"] != -1 and not 1 <= request["importance_min"] <= 10:
        reject("importance_min", "importance_min must be -1 or within 1-10")
    if request["recent_days"] < -1:
        reject("recent_days", "recent_days must be -1 or non-negative")
    if request["mode"] not in ("summary", "full"):
        reject("mode", "mode must be summary or full")
    if request["min_score"] != -1 and not 0 <= request["min_score"] <= 1:
        reject("min_score", "min_score 必须是 -1 或 0 到 1 之间的数字")
    if reserved and (len(reserved) != 1 or len(domains) != 1):
        reject("domain", "reserved session/feel 必须单独使用，不能混合 selector 或普通 domain")
    if selector == "mailbox":
        defaults = inspect.signature(breath).parameters
        for name in defaults:
            if name in ("mailbox", "mailbox_limit", "include_sealed"):
                continue
            value = request[name]
            disabled = not value if name in ("tags_filter", "topic_filter") else value == defaults[name].default
            if not disabled:
                reject(name, "mailbox 是独立信件模型，不支持 bucket retrieval 参数")
        return request
    if request["mailbox_limit"] != 1:
        reject("mailbox_limit", "仅 mailbox 支持非默认 mailbox_limit")
    if request["feels"] and (domains and domains != ["feel"]):
        reject("domain", "feels=True 只能与空 domain 或纯 feel domain 同用")
    if request["feels"] and (request["topic_filter"] or request["as_of"]):
        reject("topic_filter" if request["topic_filter"] else "feels", "feel selector 与 session/historical selector 冲突")
    if request["topic_filter"] and domains and domains != ["session"]:
        reject("domain", "topic_filter 是 session selector，只兼容空 domain 或纯 session domain")
    if selector == "historical_query" and reserved:
        reject("domain", "historical mode 不提供 reserved session/feel historical selector")
    if request["cursor"] and (selector not in ("ordinary_query", "historical_query") or request["tags_filter"]):
        reject("cursor", "cursor 仅适用于不带 tags_filter/topic_filter 的 ordinary query 或 historical query")
    if selector == "historical_query":
        if not request["query"].strip():
            reject("query", "as_of 历史检索需要提供 query，且不支持历史浮现模式")
        for field in ("importance_min", "recent_days"):
            if request[field] != -1:
                reject(field, "historical mode 不支持该当前 metadata/time filter")
        for field in ("date_from", "date_to", "resonance", "tags_filter", "topic_filter", "wake_dormant", "emotion_trend"):
            if request[field]:
                reason = (
                    "as_of 历史检索是只读的，不能 wake_dormant" if field == "wake_dormant" else
                    "as_of 历史检索不支持 tags_filter/topic_filter" if field in ("tags_filter", "topic_filter") else
                    "historical mode 不支持该当前 filter/ranking/attachment"
                )
                reject(field, reason)
    if selector in ("session", "feel"):
        for field in ("min_score", "valence", "arousal", "resonance", "include_dormant", "wake_dormant"):
            active = request[field] != -1 if field in ("min_score", "valence", "arousal") else bool(request[field])
            if active:
                reject(field, "该模式保留子串匹配和 recency 排序，没有 query relevance score、emotion ranking 或 dormant touch/gate")
    if selector not in ("ordinary_query", "historical_query") and request["min_score"] != -1:
        reject("min_score", "该模式没有 _breath_score，不能应用 strong/weak 展示阈值")
    v, a = request["valence"], request["arousal"]
    if a != -1 and v == -1:
        reject("arousal", "arousal 没有独立 ranking 语义，必须同时提供 valence")
    if selector == "historical_query" and ((v == -1) != (a == -1)):
        reject("valence/arousal", "historical emotion ranking 必须成对提供 valence 和 arousal")
    if selector not in ("ordinary_query", "historical_query", "tags_only") and (v != -1 or a != -1):
        reject("valence/arousal", "该模式不支持 emotion ranking 或 valence presentation")
    if selector == "tags_only" and a != -1:
        reject("arousal", "tags-only 只保留 valence presentation，不支持 emotion ranking")
    if selector == "ordinary_query" and v != -1 and a == -1 and request["mode"] == "full":
        reject("valence", "valence-only 只影响 summary presentation，full canonical body 不使用该参数")
    if request["mode"] == "full" and (
        selector in ("resonance", "tags_only", "importance_only")
        or selector == "session" and not request["query"].strip()
        or selector == "feel" and not (request["query"].strip() and request["tags_filter"])
    ):
        reject("mode", "该路径使用固定 summary/preview 格式，不支持 full")
    # Read-only calls retain the existing override: wake never writes without touch.
    if request["wake_dormant"] and request["touch"] and not request["include_dormant"]:
        reject("wake_dormant", "显式唤醒需要 include_dormant=True")
    for name in ("date_from", "date_to"):
        try:
            request[name] = _parse_date_filter(request[name], name)
        except ValueError as exc:
            reject(name, str(exc))
    if request["date_from"] and request["date_to"] and request["date_from"] > request["date_to"]:
        reject("date_from/date_to", "date_from cannot be later than date_to")
    try:
        request["resonance_target"] = _parse_resonance(request["resonance"])
    except ValueError as exc:
        reject("resonance", str(exc))
    if request["feels"]:
        request["domain"] = "feel"
    return request


def _breath_side_effect_warning(failures: int) -> str:
    return (
        f"\n\n[side-effect/accounting warning] {failures} 个已显示桶的 direct touch 记账失败；"
        "检索结果及 displayed/omitted/remaining/total 保持不变，未重试 touch。"
        if failures else ""
    )


def _breath_listing_accounting(total: int, selected: int, displayed: int, failed: int = 0) -> str:
    return (
        f"\n共匹配 {total} / 本次显示 {displayed} / "
        f"后续剩余 {max(0, total - displayed - failed)} / 因组装失败省略 {failed} / "
        f"因结果上限省略 {max(0, total - selected)} / "
        f"因 token 预算省略 {max(0, selected - displayed - failed)}"
    )


async def _breath_filtered_impl(
    *,
    query: str,
    max_tokens: int,
    domain: str,
    valence: float,
    arousal: float,
    max_results: int,
    mode: str,
    recent_cutoff: str | None,
    include_dormant: bool,
    wake_dormant: bool,
    touch: bool,
    include_sealed: bool,
    date_from: str,
    date_to: str,
    resonance_target: tuple[float, float] | None,
    emotion_trend: bool,
    tags_filter: list[str],
    topic_filter: list[str],
    min_score: float,
    importance_min: int = -1,
    recent_days: int = -1,
) -> str:
    """Retrieve exact-filtered candidates without changing old breath paths."""
    domain_values = [part.strip() for part in (domain or "").split(",") if part.strip()]
    domain_set = {part.casefold() for part in domain_values}
    query_text = query.strip()

    def empty_result(message: str) -> str:
        return _with_emotion_timeline(message, emotion_trend)

    def is_session(bucket: dict) -> bool:
        domains = bucket.get("metadata", {}).get("domain", [])
        return isinstance(domains, list) and "session" in domains

    def apply_common_filters(
        buckets: list[dict],
        *,
        apply_domain: bool = True,
        apply_dormant: bool = True,
    ) -> list[dict]:
        return _filter_breath_candidates(
            buckets,
            domain_values=domain_values,
            recent_cutoff=recent_cutoff,
            include_dormant=include_dormant,
            include_sealed=include_sealed,
            date_from=date_from,
            date_to=date_to,
            tags_filter=tags_filter,
            topic_filter=topic_filter,
            apply_domain=apply_domain,
            apply_dormant=apply_dormant,
            importance_min=importance_min,
            recent_days=recent_days,
        )

    # A topic filter is an archived-session constraint. A session domain is
    # also allowed to select this route when only tags_filter is supplied.
    session_route = bool(topic_filter) or domain_set == {"session"}
    if session_route:
        if domain_set and domain_set != {"session"}:
            return empty_result("没有找到对话归档。")
        try:
            all_buckets = await bucket_mgr.list_all(include_archive=True)
            sessions = apply_common_filters(
                [bucket for bucket in all_buckets if is_session(bucket)],
                apply_domain=False,
                apply_dormant=False,
            )
            if query_text:
                q = query_text.lower()
                sessions = [
                    bucket
                    for bucket in sessions
                    if q in str(bucket.get("metadata", {}).get("name", "")).lower()
                    or q in bucket.get("content", "").lower()
                ]
            sessions.sort(key=_breath_recency_key, reverse=True)
            total_sessions = len(sessions)
            sessions = sessions[:max_results]
            if not sessions:
                return empty_result("没有找到对话归档。")

            results = []
            for bucket in sessions:
                metadata = bucket.get("metadata", {})
                body = str(bucket.get("content", ""))
                if query_text and mode == "full":
                    available = max_tokens - count_tokens_approx("\n---\n".join(results))
                    body = _prefix_within_token_budget(body, available)
                    display = "原文·已截断" if body != str(bucket.get("content", "")) else "原文"
                else:
                    preview = body[:1200]
                    body = strip_wikilinks(preview)
                    display = "原文节选·已截断" if len(str(bucket.get("content", ""))) > 1200 else "原文"
                    if body != preview:
                        display += "·双链标记已省略"
                    if not query_text and len(str(bucket.get("content", ""))) > 1200:
                        body += "\n" + _format_bucket_truncation_notice(
                            str(bucket["id"]), len(body), len(str(bucket.get("content", "")))
                        )
                text = (
                    f"[session] [bucket_id:{bucket['id']}] "
                    f"{metadata.get('name', bucket['id'])}\n"
                    f"{f'[显示={display}] ' if query_text else ''}{body}"
                )
                result = await _append_bucket_extras(text, bucket, emotion_trend)
                if not body or count_tokens_approx(body if query_text and mode == "full" else "\n---\n".join(results + [result])) > max_tokens:
                    break
                results.append(result)
            text = "\n---\n".join(results)
            text += _breath_listing_accounting(total_sessions, len(sessions), len(results))
            return _with_emotion_timeline(text, emotion_trend)
        except Exception as exc:
            logger.error(f"Filtered session retrieval failed: {exc}")
            return "读取对话归档失败。"

    if domain_set == {"feel"}:
        try:
            all_buckets = await bucket_mgr.list_all(include_archive=False)
            feels = apply_common_filters(
                [
                    bucket
                    for bucket in all_buckets
                    if bucket.get("metadata", {}).get("type") == "feel"
                ],
                apply_domain=False,
                apply_dormant=False,
            )
            if query_text:
                q = query_text.lower()
                feels = [
                    bucket
                    for bucket in feels
                    if q in str(bucket.get("metadata", {}).get("name", "")).lower()
                    or q in bucket.get("content", "").lower()
                    or any(
                        q in str(tag).lower()
                        for tag in _structured_metadata_values(
                            bucket.get("metadata", {}), "tags"
                        )
                    )
                ]
            feels.sort(key=_breath_recency_key, reverse=True)
            total_feels = len(feels)
            feels = feels[:max_results]
            if not feels:
                return empty_result("没有留下过 feel。")

            results = []
            for bucket in feels:
                metadata = bucket["metadata"]
                created = _bucket_date(metadata, "created_at", "created")
                updated = _bucket_date(metadata, "updated_at", "last_active", "created")
                body = str(bucket['content'])
                if query_text and mode == "full":
                    available = max_tokens - count_tokens_approx("\n---\n".join(results))
                    body = _prefix_within_token_budget(body, available)
                    display = "原文·已截断" if body != str(bucket['content']) else "原文"
                else:
                    raw = body
                    body = strip_wikilinks(raw)
                    display = "原文·双链标记已省略" if body != raw else "原文"
                entry = (
                    f"[{created}] [bucket_id:{bucket['id']}] "
                    f"name:{metadata.get('name', bucket['id'])} updated_at:{updated} "
                    f"tags:{','.join(_structured_metadata_values(metadata, 'tags'))}\n"
                    f"{f'[显示={display}] ' if query_text else ''}{body}"
                )
                entry = await _append_bucket_extras(entry, bucket, emotion_trend)
                if not body or count_tokens_approx(body if query_text and mode == "full" else "\n---\n".join(results + [entry])) > max_tokens:
                    break
                results.append(entry)
            text = "=== 你留下的 feel ===\n" + "\n---\n".join(results)
            text += _breath_listing_accounting(total_feels, len(feels), len(results))
            return _with_emotion_timeline(text, emotion_trend)
        except Exception as exc:
            logger.error(f"Filtered feel retrieval failed: {exc}")
            return "读取 feel 失败。"

    try:
        all_buckets = await bucket_mgr.list_all(include_archive=False)
        candidates = apply_common_filters(all_buckets)
        # Feel has a dedicated route. The historical no-query route also
        # excludes feel buckets, so keep them out of deterministic tag-only
        # retrieval unless domain="feel" explicitly selected that route.
        if not query_text:
            candidates = [
                bucket
                for bucket in candidates
                if bucket.get("metadata", {}).get("type") != "feel"
            ]
    except Exception as exc:
        logger.error(f"Filtered active retrieval failed: {exc}")
        return "记忆系统暂时无法访问。"

    async def format_active_matches(
        matches: list[dict],
        hidden_count: int,
        downgraded_count: int = 0,
    ) -> str:
        results = []
        returned_ids = set()
        emitted = []
        failed = 0
        token_used = 0
        token_budget_omitted = 0
        strong_matches = [
            bucket for bucket in matches if not bucket.get("_breath_weak", False)
        ]
        weak_matches = [
            bucket for bucket in matches if bucket.get("_breath_weak", False)
        ]
        for index, bucket in enumerate(strong_matches):
            if token_used >= max_tokens:
                token_budget_omitted += len(strong_matches) - index
                break
            try:
                clean_meta = {
                    key: value
                    for key, value in bucket["metadata"].items()
                    if key != "tags"
                }
                if 0 <= valence <= 1 and "valence" in clean_meta:
                    original_v = float(clean_meta.get("valence", 0.5))
                    shift = (valence - 0.5) * 0.2
                    clean_meta["valence"] = max(0.0, min(1.0, original_v + shift))
                content = strip_wikilinks(bucket["content"])
                if touch:
                    summary = await dehydrator.dehydrate(content, clean_meta)
                else:
                    summary = await dehydrator.dehydrate(
                        content,
                        clean_meta,
                        cache_read=True,
                        cache_write=False,
                    )
                summary_tokens = count_tokens_approx(summary)
                if token_used + summary_tokens > max_tokens:
                    token_budget_omitted += len(strong_matches) - index
                    break
                summary = await _format_breath_query_summary(bucket, summary)
                results.append(await _append_bucket_extras(summary, bucket, emotion_trend))
                emitted.append(bucket)
                token_used += summary_tokens
            except Exception as exc:
                logger.warning(f"Failed to format filtered search result: {exc}")
                failed += 1
                continue

        weak_lines = [
            f"[bucket_id:{bucket['id']}] "
            f"{_bucket_display_icon(bucket.get('metadata', {}))} "
            f"{bucket.get('metadata', {}).get('name', bucket['id'])} "
            f"{_breath_retrieval_score_label(bucket)}"
            f"{' [休眠]' if bucket.get('metadata', {}).get('dormant', False) else ''}"
            for bucket in weak_matches
        ]
        if not results and not weak_lines and not matches and not hidden_count:
            if touch:
                await _fire_webhook("breath", {"mode": "empty", "matches": 0})
            return empty_result("未找到相关记忆。")
        final_text = "\n---\n".join(results)
        if weak_lines:
            weak_section = "--- 弱匹配（仅列名） ---\n" + "\n".join(weak_lines)
            final_text = "\n\n".join(
                part for part in (final_text, weak_section) if part
            )
        if hidden_count:
            final_text += f"\n\n还有{hidden_count}个相关桶未显示"
        final_text += _breath_listing_accounting(
            len(matches) + hidden_count, len(matches), len(results) + len(weak_lines), failed
        ) + f" / 因低于阈值降级 {downgraded_count}"
        if touch:
            await _fire_webhook(
                "breath",
                {
                    "mode": "ok",
                    "matches": len(results) + len(weak_lines),
                    "chars": len(final_text),
                },
            )
        final_text = _with_emotion_timeline(final_text, emotion_trend)
        touch_failures = 0
        if touch:
            for bucket in emitted:
                returned_ids.add(bucket["id"])
                try:
                    await bucket_mgr.touch(
                        bucket["id"], ripple_ids=returned_ids, wake_dormant=wake_dormant
                    )
                except Exception:
                    logger.warning("Breath tag listing direct touch failed", exc_info=True)
                    touch_failures += 1
        return final_text + _breath_side_effect_warning(touch_failures)

    if not query_text:
        candidates.sort(key=_breath_recency_key, reverse=True)
        hidden_count = max(0, len(candidates) - max_results)
        return await format_active_matches(candidates[:max_results], hidden_count)

    q_valence = valence if 0 <= valence <= 1 else None
    q_arousal = arousal if 0 <= arousal <= 1 else None
    try:
        search_trace = {}
        matches = await bucket_mgr.search(
            query,
            limit=max(1000, len(candidates)),
            domain_filter=None,
            query_valence=q_valence,
            query_arousal=q_arousal,
            include_dormant=True, include_sealed=include_sealed,
            candidate_buckets=candidates,
            trace=search_trace,
        )
    except Exception as exc:
        logger.error(f"Filtered search failed: {exc}")
        return "搜索过程出错，请稍后重试。"

    if resonance_target:
        matches.sort(key=lambda bucket: _resonance_distance(bucket, resonance_target))
    trace_by_id = {
        str(entry.get("id", "")): entry
        for entry in search_trace.get("candidates", [])
    }
    matches = _annotate_breath_query_matches(
        matches,
        query=query,
        trace_by_id=trace_by_id,
        min_score=min_score,
    )
    ordered_matches = _order_breath_query_matches(matches)
    hidden_count = max(0, len(ordered_matches) - max_results)
    selected = ordered_matches[:max_results]
    final_text, _composition = await _compose_breath_query_matches(
        selected,
        max_tokens=max_tokens,
        q_valence=q_valence,
        emotion_trend=emotion_trend,
        hidden_count=hidden_count,
        total_matches=len(ordered_matches),
        touch=touch,
        wake_dormant=wake_dormant,
        downgraded_count=sum(1 for bucket in ordered_matches if bucket.get("_breath_weak", False)),
        mode=mode,
        touch_ripple=True,
    )
    return final_text or empty_result("未找到相关记忆。")


def _filter_breath_query_matches(
    matches: list[dict],
    *,
    recent_cutoff: str | None,
    date_from: str,
    date_to: str,
    include_sealed: bool,
) -> list[dict]:
    """Apply the post-search gates used by the ordinary query Breath path."""
    return [
        bucket
        for bucket in matches
        if _is_recent_bucket(bucket, recent_cutoff)
        and _is_in_date_range(bucket, date_from, date_to)
        and (include_sealed or not _is_sealed(bucket))
    ]


def _breath_cursor_scope(
    *,
    query: str,
    domain: str,
    valence: float,
    arousal: float,
    recent_cutoff: str | None,
    include_dormant: bool,
    include_sealed: bool,
    date_from: str,
    date_to: str,
    resonance: str,
    min_score: float,
    as_of: str = "",
    touch: bool = True,
    mode: str = "summary",
    selector: str = "ordinary_query",
    importance_min: int = -1,
    wake_dormant: bool = False,
    recent_days: int = -1,
) -> str:
    payload = {
        "version": 2,
        "selector": selector,
        "importance_min": importance_min,
        "wake_dormant": wake_dormant,
        "recent_days": recent_days,
        "rendering_kind": "historical_body" if selector == "historical_query" else mode,
        "query": query,
        "domain": domain,
        "valence": valence,
        "arousal": arousal,
        "recent_cutoff": recent_cutoff,
        "include_dormant": include_dormant,
        "include_sealed": include_sealed,
        "date_from": date_from,
        "date_to": date_to,
        "resonance": resonance,
        "min_score": min_score,
        "as_of": as_of,
        "touch": touch,
        "mode": mode,
    }
    encoded = _json_lib.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _encode_breath_cursor(matches: list[dict], position: int, scope: str, *, context: dict | None = None) -> str:
    now = time.monotonic()
    for token, state in list(_BREATH_CURSOR_STATES.items()):
        if float(state.get("expires_at", 0)) <= now:
            _BREATH_CURSOR_STATES.pop(token, None)
    while len(_BREATH_CURSOR_STATES) >= _BREATH_CURSOR_MAX_STATES:
        oldest = min(
            _BREATH_CURSOR_STATES,
            key=lambda token: float(_BREATH_CURSOR_STATES[token].get("created_at", 0)),
        )
        _BREATH_CURSOR_STATES.pop(oldest, None)
    token = secrets.token_urlsafe(24)
    _BREATH_CURSOR_STATES[token] = {
        "matches": [
            {
                "id": str(bucket.get("id", "")),
                "score": float(bucket.get("_breath_score", bucket.get("score", 0.0))),
                "retrieval_score": (
                    bucket.get("score") if "_breath_score" in bucket
                    else bucket.get("retrieval_score")
                ),
                "channel": str(bucket.get("_breath_channel", bucket.get("channel", "关键词"))),
                "weak": bool(bucket.get("_breath_weak", bucket.get("weak", False))),
                "vector_match": bool(bucket.get("vector_match", False)),
            }
            for bucket in matches
        ],
        "position": position,
        "scope": scope,
        "created_at": now,
        "expires_at": now + _BREATH_CURSOR_TTL_SECONDS,
    }
    if context is not None:
        _BREATH_CURSOR_STATES[token]["version"] = 2
        _BREATH_CURSOR_STATES[token]["context"] = dict(context)
    return token


def _validated_breath_cursor_state(cursor: str, expected_scope: str | None = None, *, require_context: bool = False) -> dict:
    if not isinstance(cursor, str) or not cursor or len(cursor) > 256:
        raise ValueError("invalid cursor")
    state = _BREATH_CURSOR_STATES.get(cursor)
    now = time.monotonic()
    if (
        not isinstance(state, dict)
        or not isinstance(state.get("expires_at"), (int, float))
        or not isinstance(state.get("created_at"), (int, float))
        or not state["created_at"] <= now < state["expires_at"]
        or not 0 < state["expires_at"] - state["created_at"] <= _BREATH_CURSOR_TTL_SECONDS + 1e-7
    ):
        _BREATH_CURSOR_STATES.pop(cursor, None)
        raise ValueError("invalid cursor")
    frozen_matches = state.get("matches")
    position = state.get("position")
    if (
        not isinstance(state.get("scope"), str)
        or (expected_scope is not None and state.get("scope") != expected_scope)
        or not isinstance(frozen_matches, list)
        or len(frozen_matches) > 1000
        or any(
            not isinstance(match, dict)
            or not isinstance(match.get("id"), str)
            or not match["id"]
            or not isinstance(match.get("score"), (int, float))
            or isinstance(match.get("score"), bool)
            or not 0.0 <= float(match["score"]) <= 1.0
            or not isinstance(match.get("channel"), str)
            or not match["channel"]
            or not isinstance(match.get("weak"), bool)
            or (
                "retrieval_score" in match
                and match["retrieval_score"] is not None
                and (
                    not isinstance(match["retrieval_score"], (int, float))
                    or isinstance(match["retrieval_score"], bool)
                    or not 0.0 <= float(match["retrieval_score"]) <= 100.0
                )
            )
            or ("vector_match" in match and not isinstance(match["vector_match"], bool))
            for match in frozen_matches
        )
        or len({match["id"] for match in frozen_matches}) != len(frozen_matches)
        or not isinstance(position, int)
        or isinstance(position, bool)
        or position < 0
        or position > len(frozen_matches)
    ):
        raise ValueError("invalid cursor")
    if require_context:
        # The complete token/match/TTL shape is validated before frozen values
        # can participate in interpretation of the current request.
        if state.get("version") != 2 or not isinstance(state.get("context"), dict):
            raise ValueError("invalid cursor")
        context = state["context"]
        if context.get("selector") not in ("ordinary_query", "historical_query"):
            raise ValueError("invalid cursor")
        days = context.get("recent_days")
        cutoff = context.get("recent_cutoff")
        if not isinstance(days, int) or isinstance(days, bool) or days < -1:
            raise ValueError("invalid cursor")
        if days == -1:
            if cutoff is not None:
                raise ValueError("invalid cursor")
        elif not isinstance(cutoff, str) or not cutoff or _parse_date_filter(cutoff, "cursor cutoff") != cutoff:
            raise ValueError("invalid cursor")
        if context["selector"] == "historical_query" and days != -1:
            raise ValueError("invalid cursor")
    return state


def _decode_breath_cursor(cursor: str, expected_scope: str) -> tuple[list[dict], int]:
    state = _validated_breath_cursor_state(cursor, expected_scope)
    return [dict(match) for match in state["matches"]], state["position"]


def _resolve_breath_min_score(min_score: float) -> float:
    """Resolve the Breath display threshold without changing recall admission."""
    if min_score == -1:
        configured = os.getenv("OMBRE_BREATH_MIN_SCORE")
        if configured is None or not configured.strip():
            return 0.0
        try:
            min_score = float(configured)
        except (TypeError, ValueError) as exc:
            raise ValueError("OMBRE_BREATH_MIN_SCORE 必须是 0 到 1 之间的数字。") from exc
    try:
        resolved = float(min_score)
    except (TypeError, ValueError) as exc:
        raise ValueError("min_score 必须是 -1 或 0 到 1 之间的数字。") from exc
    if not 0.0 <= resolved <= 1.0:
        raise ValueError("min_score 必须是 -1 或 0 到 1 之间的数字。")
    return resolved


def _breath_weak_score_anchor_match(query: str, bucket: dict) -> bool:
    """Preserve the existing weak-match grouping independently of labels."""
    query_text = "".join(str(query or "").casefold().split())
    if not query_text:
        return False
    meta = bucket.get("metadata", {})
    searchable = " ".join((
        str(meta.get("name", "")),
        str(bucket.get("content", "")),
    ))
    return query_text in "".join(searchable.casefold().split())


def _breath_exact_anchor_match(query: str, bucket: dict) -> bool:
    query_text = "".join(str(query or "").casefold().split())
    if not query_text:
        return False
    meta = bucket.get("metadata", {})
    normalize = lambda value: "".join(str(value or "").casefold().split())
    tags = meta.get("tags", [])
    if isinstance(tags, str):
        tags = [part.strip() for part in tags.split(",")]
    return query_text == normalize(meta.get("name", "")) or any(
        query_text == normalize(tag) for tag in (tags or [])
    )


def _breath_retrieval_score_label(bucket: dict) -> str:
    score = bucket.get("score")
    if isinstance(score, (int, float)) and not isinstance(score, bool):
        return f"检索分={score:.2f}"
    return "检索分=未记录"


def _annotate_breath_query_matches(
    matches: list[dict],
    *,
    query: str,
    trace_by_id: dict[str, dict],
    min_score: float,
) -> list[dict]:
    """Attach transient, normalized recall metadata for Breath presentation."""
    annotated = []
    for bucket in matches:
        rendered = dict(bucket)
        bid = str(rendered.get("id", ""))
        trace_scores = trace_by_id.get(bid, {}).get("scores", {})
        try:
            fuzzy_score = max(0.0, min(1.0, float(trace_scores.get("fuzzy_lexical", 0.0))))
        except (TypeError, ValueError):
            fuzzy_score = 0.0
        try:
            semantic_score = max(
                0.0,
                min(1.0, float(trace_scores.get("semantic", rendered.get("semantic_score", 0.0)))),
            )
        except (TypeError, ValueError):
            semantic_score = 0.0

        score = (
            1.0 if _breath_weak_score_anchor_match(query, rendered)
            else max(fuzzy_score, semantic_score)
        )
        if _breath_exact_anchor_match(query, rendered):
            channel = "精确"
        else:
            if fuzzy_score > 0.0 and semantic_score > 0.0:
                channel = "双"
            elif semantic_score > 0.0:
                channel = "语义"
            else:
                channel = "关键词"
        rendered["_breath_score"] = score
        rendered["_breath_channel"] = channel
        rendered["_breath_weak"] = score < min_score
        annotated.append(rendered)
    return annotated


def _order_breath_query_matches(matches: list[dict]) -> list[dict]:
    """Keep recall order within each display group, with weak matches at the tail."""
    return [
        *[bucket for bucket in matches if not bucket.get("_breath_weak", False)],
        *[bucket for bucket in matches if bucket.get("_breath_weak", False)],
    ]


async def _format_breath_query_summary(bucket: dict, summary: str) -> str:
    """Add the stable query-result header used by both Breath query paths."""
    meta = bucket.get("metadata", {})
    icon = _bucket_display_icon(meta)
    dormant_tag = " [休眠]" if meta.get("dormant", False) else ""
    channel = str(bucket.get("_breath_channel", "关键词"))
    superseded_marker = await _superseded_marker(bucket)
    header = (
        f"[bucket_id:{bucket['id']}] {icon} [{_breath_retrieval_score_label(bucket)}] "
        f"[通道:{channel}]{superseded_marker}{dormant_tag} {summary}"
    )
    return "[语义关联] " + header if bucket.get("vector_match") else header

async def _compose_breath_query_matches(
    matches: list[dict],
    *,
    max_tokens: int,
    q_valence: float | None,
    emotion_trend: bool,
    hidden_count: int,
    total_matches: int | None = None,
    trace_by_id: dict[str, dict] | None = None,
    touch: bool = True,
    wake_dormant: bool = False,
    cache: bool = True,
    next_cursor: str = "",
    downgraded_count: int = 0,
    mode: str = "summary",
    ordered_matches: list[dict] | None = None,
    start_position: int = 0,
    prior_consumed: int = 0,
    cursor_scope: str = "",
    touch_ripple: bool = False,
    cursor_context: dict | None = None,
) -> tuple[str, dict]:
    """Consume a prefix of the frozen page; budget omissions remain unconsumed."""
    del cache  # Cache reads are always allowed; only touch controls cache writes.
    results: list[str] = []
    weak_lines: list[str] = []
    shown_buckets: list[dict] = []
    direct_touch_buckets: list[dict] = []
    returned_ids: set[str] = set()
    token_used = 0
    consumed = 0
    failed_omitted = 0
    matched_count = int(total_matches) if total_matches is not None else len(matches) + hidden_count
    for bucket in matches:
        bid = str(bucket.get("id", ""))
        decision = trace_by_id.get(bid) if trace_by_id is not None else None
        remaining_budget = max_tokens - token_used
        if remaining_budget <= 0:
            break
        if bucket.get("_breath_weak", False):
            meta = bucket.get("metadata", {})
            dormant_tag = " [休眠]" if meta.get("dormant", False) else ""
            line = (
                f"[bucket_id:{bid}] {_bucket_display_icon(meta)} "
                f"{meta.get('name', bid)} {_breath_retrieval_score_label(bucket)}{dormant_tag}"
            )
            required = max(1, count_tokens_approx(line))
            if required > remaining_budget:
                break
            weak_lines.append(line)
            shown_buckets.append(bucket)
            token_used += required
            consumed += 1
            if decision is not None:
                decision["final_decision"] = "surfaced"
                decision["surfaced_token_count"] = required
            continue

        try:
            if mode == "full":
                body = str(bucket["content"])
                display = "原文"
                summary = body
            else:
                clean_meta = {
                    key: value for key, value in bucket["metadata"].items()
                    if key != "tags"
                }
                if q_valence is not None and "valence" in clean_meta:
                    original_v = float(clean_meta.get("valence", 0.5))
                    shift = (q_valence - 0.5) * 0.2
                    clean_meta["valence"] = max(0.0, min(1.0, original_v + shift))
                rendered = await dehydrator.dehydrate(
                    strip_wikilinks(bucket["content"]),
                    clean_meta,
                    cache_read=True,
                    cache_write=touch,
                    return_kind=True,
                )
                if isinstance(rendered, tuple):
                    summary, kind = rendered
                else:
                    summary, kind = rendered, "summary"
                if kind == "original":
                    summary = str(bucket["content"])
                display = "原文" if kind == "original" else "压缩摘要·非原文"
            required = count_tokens_approx(summary)
            if required > remaining_budget:
                if mode == "full" or display == "原文":
                    summary = _prefix_within_token_budget(summary, remaining_budget)
                    if not summary:
                        break
                    display = (
                        "原文·已截断" if display == "原文"
                        else "压缩摘要·已截断·非原文"
                    )
                    required = count_tokens_approx(summary)
                else:
                    break
            formatted = await _format_breath_query_summary(
                bucket, f"[显示={display}] {summary}"
            )
            entry = await _append_bucket_extras(formatted, bucket, emotion_trend)
            results.append(entry)
            shown_buckets.append(bucket)
            direct_touch_buckets.append(bucket)
            token_used += required
            consumed += 1
            if decision is not None:
                decision["final_decision"] = "surfaced"
                decision["surfaced_token_count"] = required
        except Exception as exc:
            logger.warning(
                "Breath composition failed for bucket %s (%s); using canonical body",
                bid, type(exc).__name__,
            )
            try:
                body = str(bucket["content"])
                prefix = _prefix_within_token_budget(body, remaining_budget)
                if not prefix:
                    if not body:
                        failed_omitted += 1
                        consumed += 1
                        if decision is not None:
                            decision["final_decision"] = "omitted_composition_error"
                    break
                display = (
                    "原文·已截断；摘要服务暂不可用"
                    if prefix != body else "原文；摘要服务暂不可用"
                )
                formatted = await _format_breath_query_summary(
                    bucket, f"[显示={display}] {prefix}"
                )
                entry = await _append_bucket_extras(formatted, bucket, emotion_trend)
                results.append(entry)
                shown_buckets.append(bucket)
                direct_touch_buckets.append(bucket)
                token_used += count_tokens_approx(prefix)
                consumed += 1
                if decision is not None:
                    decision["final_decision"] = "surfaced_fallback"
            except Exception as fallback_exc:
                logger.warning(
                    "Breath canonical fallback failed for bucket %s (%s)",
                    bid, type(fallback_exc).__name__,
                )
                failed_omitted += 1
                consumed += 1
                if decision is not None:
                    decision["final_decision"] = "omitted_composition_error"

    if trace_by_id is not None:
        for omitted_bucket in matches[consumed:]:
            omitted_decision = trace_by_id.get(str(omitted_bucket.get("id", "")))
            if omitted_decision is not None:
                omitted_decision["final_decision"] = "omitted_token_budget"
    displayed_count = len(results) + len(weak_lines)
    remaining = max(0, matched_count - prior_consumed - consumed)
    selected_limit = min(len(matches), max(0, matched_count - prior_consumed))
    result_limit_omitted = max(0, matched_count - prior_consumed - selected_limit)
    token_budget_omitted = max(0, selected_limit - consumed)
    if ordered_matches is not None:
        if consumed:
            next_position = int(matches[consumed - 1].get("_breath_index", start_position + consumed - 1)) + 1
        elif matches:
            next_position = int(matches[0].get("_breath_index", start_position))
        else:
            next_position = len(ordered_matches)
        next_cursor = (
            _encode_breath_cursor(ordered_matches, next_position, cursor_scope, context=cursor_context)
            if remaining and cursor_scope else ""
        )
    composition = {
        "surfaced_count": displayed_count,
        "token_used": token_used,
        "token_budget": max_tokens,
        "matched_count": matched_count,
        "prior_consumed": prior_consumed,
        "composition_failed_omitted": failed_omitted,
        "result_limit_omitted": result_limit_omitted,
        "token_budget_omitted": token_budget_omitted,
        "hidden_count": remaining,
        "remaining_count": remaining,
        "consumed_count": consumed,
        "downgraded_count": downgraded_count,
        "next_cursor": next_cursor,
    }
    if matched_count == 0:
        return "", composition
    summary_lines = []
    if not consumed and matches:
        summary_lines.append("max_tokens 过小，无法显示当前匹配项；请增大预算后重试。")
    if remaining:
        summary_lines.append(f"还有{remaining}个相关记忆未显示")
    summary_lines.extend(
        await _current_successor_lines(
            shown_buckets,
            {str(bucket.get("id", "")) for bucket in matches},
        )
    )
    summary_lines.append(
        f"共匹配 {matched_count} / 前页已消费 {prior_consumed} / "
        f"本次显示 {displayed_count} / 因组装失败省略 {failed_omitted} / "
        f"后续剩余 {remaining} / 因结果上限省略 {result_limit_omitted} / "
        f"因 token 预算省略 {token_budget_omitted} / "
        f"因低于阈值降级 {downgraded_count}"
    )
    if next_cursor:
        summary_lines.append(f"下一页 cursor: {next_cursor}")
    final_text = "\n---\n".join(results)
    if weak_lines:
        weak_section = "--- 弱匹配（仅列名） ---\n" + "\n".join(weak_lines)
        final_text = "\n\n".join(part for part in (final_text, weak_section) if part)
    final_text = "\n\n".join(part for part in (final_text, "\n".join(summary_lines)) if part)
    final_text = _with_emotion_timeline(final_text, emotion_trend)
    touch_failures = 0
    if touch:
        for bucket in direct_touch_buckets:
            if touch_ripple:
                returned_ids.add(bucket["id"])
            try:
                await bucket_mgr.touch(
                    bucket["id"],
                    wake_dormant=wake_dormant,
                    **({"ripple_ids": returned_ids} if touch_ripple else {}),
                )
            except Exception:
                logger.warning("Breath emitted result direct touch failed", exc_info=True)
                touch_failures += 1
    return final_text + _breath_side_effect_warning(touch_failures), composition


def _parse_as_of_timestamp(value: str) -> datetime | None:
    """Parse an existing local-history timestamp without inventing a value."""
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is not None:
        return parsed.astimezone().replace(tzinfo=None)
    return parsed


def _parse_breath_as_of(value: str) -> tuple[datetime, str]:
    """Parse a historical lookup time using the local-naive history convention.

    Bucket history uses ``now_iso()`` local ISO timestamps without timezone
    information. A date-only request therefore means the end of that local
    calendar day, matching the existing inclusive date-filter convention.
    """
    raw = (value or "").strip()
    if not raw:
        raise ValueError("as_of 必须是 ISO8601 日期或时间。")
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
            day = datetime.strptime(raw, "%Y-%m-%d")
            return day + timedelta(days=1, microseconds=-1), raw
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("as_of 必须是 ISO8601 日期或时间。") from exc
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone().replace(tzinfo=None)
    return parsed, raw


def _historical_bucket_at(
    bucket: dict,
    snapshots: list[dict],
    as_of: datetime,
) -> dict | None:
    """Return the body version effective at ``as_of`` without changing storage.

    ``changed_at`` is the write-ahead snapshot timestamp. It is the only
    available version boundary, so a version is treated as effective on
    ``[changed_at, next_changed_at)``; exact equality selects the newer body.
    A malformed timestamp or a legacy bucket without an exact ``created``
    timestamp is omitted rather than presented as a fabricated history.
    """
    metadata = bucket.get("metadata", {})
    created = _parse_as_of_timestamp(metadata.get("created", ""))
    if created is None or as_of < created:
        return None

    parsed_snapshots: list[tuple[datetime, dict]] = []
    for snapshot in snapshots:
        changed_at = _parse_as_of_timestamp(snapshot.get("changed_at", ""))
        if changed_at is None or changed_at < created:
            return None
        parsed_snapshots.append((changed_at, snapshot))

    content = str(bucket.get("content", ""))
    version_start = created
    version_end: datetime | None = None
    for index, (changed_at, snapshot) in enumerate(parsed_snapshots):
        if as_of < changed_at:
            content = str(snapshot.get("old_content", ""))
            version_start = (
                created if index == 0 else parsed_snapshots[index - 1][0]
            )
            version_end = changed_at
            break
        version_start = changed_at

    historical_metadata = dict(metadata)
    successor_id = _superseded_by_id(historical_metadata)
    superseded_at = _parse_as_of_timestamp(
        historical_metadata.get("superseded_at", "")
    )
    # The history table cannot reconstruct old metadata. Never project today's
    # supersession marker back before its recorded timestamp (or when absent).
    if successor_id and (superseded_at is None or as_of < superseded_at):
        historical_metadata.pop("superseded_by", None)
        historical_metadata.pop("superseded_at", None)

    return {
        "id": bucket["id"],
        "content": content,
        "metadata": historical_metadata,
        "_as_of": as_of.isoformat(timespec="seconds"),
        "_as_of_version_start": version_start.isoformat(timespec="seconds"),
        "_as_of_version_end": (
            version_end.isoformat(timespec="seconds") if version_end else ""
        ),
    }


async def _historical_breath_corpus(
    *,
    as_of: datetime,
    domain_values: list[str],
    include_dormant: bool,
    include_sealed: bool,
) -> list[dict]:
    """Build an existing-bucket historical body corpus using read-only calls."""
    all_buckets = await bucket_mgr.list_all(include_archive=False)
    visible = _filter_breath_candidates(
        all_buckets,
        domain_values=domain_values,
        include_dormant=include_dormant,
        include_sealed=include_sealed,
    )
    snapshots_by_id = bucket_mgr.get_history_for_bucket_ids(
        str(bucket.get("id", "")) for bucket in visible
    )
    return [
        historical
        for bucket in visible
        if (
            historical := _historical_bucket_at(
                bucket,
                snapshots_by_id.get(str(bucket.get("id", "")), []),
                as_of,
            )
        ) is not None
    ]


async def _format_historical_breath_summary(
    bucket: dict,
    body: str,
    *,
    requested_as_of: str,
    truncated: bool = False,
) -> str:
    """Render raw historical content without current-summary cache side effects."""
    metadata = bucket.get("metadata", {})
    icon = _bucket_display_icon(metadata)
    dormant_tag = " [休眠]" if metadata.get("dormant", False) else ""
    channel = str(bucket.get("_breath_channel", "关键词"))
    superseded_marker = await _superseded_marker(bucket)
    version_start = str(bucket.get("_as_of_version_start", ""))
    version_end = str(bucket.get("_as_of_version_end", ""))
    version_range = (
        f"有效至 {version_end}" if version_end else "此后版本"
    )
    return (
        f"[历史版本 · as_of={requested_as_of} · metadata=当前] "
        f"[bucket_id:{bucket['id']}] {icon} [{_breath_retrieval_score_label(bucket)}] "
        f"[通道:{channel}]{superseded_marker}{dormant_tag}\n"
        f"[正文版本有效: {version_start} — {version_range}]\n"
        f"[显示={'历史原文·已截断' if truncated else '历史原文'}] {body}"
    )


async def _compose_historical_breath_matches(
    matches: list[dict],
    *,
    max_tokens: int,
    hidden_count: int,
    total_matches: int,
    downgraded_count: int,
    next_cursor: str,
    requested_as_of: str,
    ordered_matches: list[dict] | None = None,
    start_position: int = 0,
    prior_consumed: int = 0,
    cursor_scope: str = "",
    cursor_context: dict | None = None,
) -> str:
    """Render only a consumed prefix of the historical frozen query page."""
    results: list[str] = []
    weak_lines: list[str] = []
    token_used = 0
    consumed = 0
    for bucket in matches:
        remaining_budget = max_tokens - token_used
        if remaining_budget <= 0:
            break
        if bucket.get("_breath_weak", False):
            meta = bucket.get("metadata", {})
            dormant_tag = " [休眠]" if meta.get("dormant", False) else ""
            line = (
                f"[历史版本 · as_of={requested_as_of}] [bucket_id:{bucket['id']}] "
                f"{_bucket_display_icon(meta)} {meta.get('name', bucket['id'])} "
                f"{_breath_retrieval_score_label(bucket)}{dormant_tag}"
            )
            required = max(1, count_tokens_approx(line))
            if required > remaining_budget:
                break
            weak_lines.append(line)
            token_used += required
            consumed += 1
            continue
        body = str(bucket.get("content", ""))
        required = count_tokens_approx(body)
        if required > remaining_budget:
            body = _prefix_within_token_budget(body, remaining_budget)
            if not body:
                break
        rendered = await _format_historical_breath_summary(
            bucket, body, requested_as_of=requested_as_of,
            truncated=body != str(bucket.get("content", "")),
        )
        results.append(rendered)
        token_used += count_tokens_approx(body)
        consumed += 1
    displayed_count = len(results) + len(weak_lines)
    remaining = max(0, total_matches - prior_consumed - consumed)
    selected_limit = min(len(matches), total_matches - prior_consumed)
    result_limit_omitted = max(0, total_matches - prior_consumed - selected_limit)
    token_budget_omitted = max(0, selected_limit - consumed)
    if ordered_matches is not None:
        if consumed:
            next_position = int(matches[consumed - 1].get("_breath_index", start_position + consumed - 1)) + 1
        elif matches:
            next_position = int(matches[0].get("_breath_index", start_position))
        else:
            next_position = len(ordered_matches)
        next_cursor = (
            _encode_breath_cursor(ordered_matches, next_position, cursor_scope, context=cursor_context)
            if remaining and cursor_scope else ""
        )
    if total_matches == 0:
        return "未找到在该时点存在的相关历史记忆。"
    parts = ["\n---\n".join(results)] if results else []
    if weak_lines:
        parts.append("--- 历史弱匹配（仅列名） ---\n" + "\n".join(weak_lines))
    summary_lines = []
    if not displayed_count and matches:
        summary_lines.append("max_tokens 过小，无法显示当前历史匹配项；请增大预算后重试。")
    if remaining:
        summary_lines.append(f"还有{remaining}个相关历史记忆未显示")
    summary_lines.append(
        f"共匹配 {total_matches} / 前页已消费 {prior_consumed} / "
        f"本次显示 {displayed_count} / 因组装失败省略 0 / "
        f"后续剩余 {remaining} / 因结果上限省略 {result_limit_omitted} / "
        f"因 token 预算省略 {token_budget_omitted} / "
        f"因低于阈值降级 {downgraded_count}"
    )
    if next_cursor:
        summary_lines.append(f"下一页 cursor: {next_cursor}")
    return "\n\n".join(parts + ["\n".join(summary_lines)])


async def _breath_as_of_impl(
    *,
    as_of: str,
    query: str,
    max_tokens: int,
    domain: str,
    valence: float,
    arousal: float,
    max_results: int,
    importance_min: int,
    recent_days: int,
    emotion_trend: bool,
    include_dormant: bool,
    include_sealed: bool,
    date_from: str,
    date_to: str,
    resonance: str,
    tags_filter: list[str],
    topic_filter: list[str],
    wake_dormant: bool,
    cursor: str,
    min_score: float,
    mode: str = "summary",
) -> str:
    """Read the historical-body corpus without activation, cache, or embeddings."""
    try:
        as_of_time, requested_as_of = _parse_breath_as_of(as_of)
        resolved_min_score = _resolve_breath_min_score(min_score)
    except ValueError as exc:
        return str(exc)
    if not query or not query.strip():
        return "as_of 历史检索需要提供 query，且不支持历史浮现模式。"
    if wake_dormant:
        return "as_of 历史检索是只读的，不能 wake_dormant。"
    if emotion_trend:
        return "as_of 历史检索不附加当前 emotion_trend。"
    if importance_min >= 1 or recent_days > 0 or date_from or date_to or resonance:
        return "as_of 历史检索不支持当前时间/权重过滤参数。"
    if tags_filter or topic_filter:
        return "as_of 历史检索暂不支持 tags_filter/topic_filter。"
    max_results = max(1, min(max_results, 50))
    max_tokens = min(max_tokens, 20000)
    domain_values = [part.strip() for part in domain.split(",") if part.strip()]
    q_valence = valence if 0 <= valence <= 1 else None
    q_arousal = arousal if 0 <= arousal <= 1 else None
    cursor_scope = _breath_cursor_scope(
        query=query,
        domain=domain,
        valence=valence,
        arousal=arousal,
        recent_cutoff=None,
        include_dormant=include_dormant,
        include_sealed=include_sealed,
        date_from="",
        date_to="",
        resonance="",
        min_score=resolved_min_score,
        as_of=as_of_time.isoformat(timespec="seconds"),
        touch=False,
        mode=mode,
        selector="historical_query",
    )
    cursor_context = {"selector": "historical_query", "recent_days": -1, "recent_cutoff": None}
    if cursor:
        try:
            _validated_breath_cursor_state(cursor, cursor_scope, require_context=True)
        except ValueError:
            return _breath_parameter_error("historical_query", "cursor", "cursor 无效、已过期或与当前检索条件不匹配")
    try:
        corpus = await _historical_breath_corpus(
            as_of=as_of_time,
            domain_values=domain_values,
            include_dormant=include_dormant,
            include_sealed=include_sealed,
        )
    except Exception as exc:
        logger.error("Historical Breath corpus read failed: %s", exc)
        return "历史记忆暂时无法访问。"

    search_trace: dict = {}
    if cursor:
        try:
            frozen_matches, position = _decode_breath_cursor(cursor, cursor_scope)
        except ValueError:
            return "cursor 无效、已过期或与当前检索条件不匹配。"
        by_id = {str(bucket.get("id", "")): bucket for bucket in corpus}
        eligible = []
        for index, record in enumerate(frozen_matches):
            bucket = by_id.get(record["id"])
            if bucket is None:
                continue
            rendered = dict(bucket)
            rendered["_breath_score"] = float(record["score"])
            rendered.pop("score", None)
            if record.get("retrieval_score") is not None:
                rendered["score"] = float(record["retrieval_score"])
            if (
                record.get("retrieval_score") is None and record["channel"] == "精确"
                and not _breath_exact_anchor_match(query, rendered)
            ):
                record["channel"] = "关键词"
            rendered["_breath_channel"] = record["channel"]
            rendered["_breath_weak"] = bool(record["weak"])
            rendered["vector_match"] = bool(record.get("vector_match", False))
            rendered["_breath_index"] = index
            eligible.append(rendered)
        ordered_matches = frozen_matches
        prior_consumed = sum(1 for match in eligible if match["_breath_index"] < position)
        matches = [match for match in eligible if match["_breath_index"] >= position][:max_results]
        total_matches = len(eligible)
        downgraded_count = sum(1 for match in eligible if match.get("_breath_weak", False))
    else:
        try:
            matches = await bucket_mgr.search(
                query,
                limit=1000,
                query_valence=q_valence,
                query_arousal=q_arousal,
                include_dormant=True,
                include_sealed=True,
                candidate_buckets=corpus,
                trace=search_trace,
                include_semantic=False,
            )
        except Exception as exc:
            logger.error("Historical Breath search failed: %s", exc)
            return "历史检索过程出错，请稍后重试。"
        trace_by_id = {
            str(entry.get("id", "")): entry
            for entry in search_trace.get("candidates", [])
        }
        matches = _annotate_breath_query_matches(
            matches,
            query=query,
            trace_by_id=trace_by_id,
            min_score=resolved_min_score,
        )
        ordered_matches = _order_breath_query_matches(matches)
        for index, match in enumerate(ordered_matches):
            match["_breath_index"] = index
        matches = ordered_matches[:max_results]
        position = 0
        prior_consumed = 0
        total_matches = len(ordered_matches)
        downgraded_count = sum(1 for match in ordered_matches if match.get("_breath_weak", False))
    hidden_count = max(0, total_matches - prior_consumed - len(matches))
    return await _compose_historical_breath_matches(
        matches,
        max_tokens=max_tokens,
        hidden_count=hidden_count,
        total_matches=total_matches,
        downgraded_count=downgraded_count,
        next_cursor="",
        requested_as_of=requested_as_of,
        ordered_matches=ordered_matches,
        start_position=position,
        prior_consumed=prior_consumed,
        cursor_scope=cursor_scope,
        cursor_context=cursor_context,
    )


async def _breath_impl(
    query: str = "",
    max_tokens: int = 10000,
    domain: str = "",
    valence: float = -1,
    arousal: float = -1,
    max_results: int = 5,
    importance_min: int = -1,
    mode: str = "summary",
    recent_days: int = -1,
    emotion_trend: bool = False,
    include_dormant: bool = False,
    include_sealed: bool = False,
    date_from: str = "",
    date_to: str = "",
    resonance: str = "",
    tags_filter: list[str] | None = None,
    topic_filter: list[str] | None = None,
    wake_dormant: bool = False,
    touch: bool = True,
    cursor: str = "",
    min_score: float = -1,
    as_of: str = "",
    _request: dict | None = None,
) -> str:
    # MCP schema note: emotion_trend must stay in the tool signature.
    """Resolve one canonical selector, then apply its supported filters."""
    try:
        request = _request if _request is not None else _prepare_breath_request(**{
            name: value for name, value in locals().copy().items() if name != "_request"
        })
    except ValueError as exc:
        return str(exc)
    selector = request["selector"]
    domain = request["domain"]
    mode = request["mode"]
    tags_filter, topic_filter = request["tags_filter"], request["topic_filter"]
    date_from, date_to = request["date_from"], request["date_to"]
    resonance_target = request["resonance_target"]
    cursor_state = None
    if cursor:
        try:
            cursor_state = _validated_breath_cursor_state(cursor, require_context=True)
            context = cursor_state["context"]
            if context["selector"] != selector or context["recent_days"] != recent_days:
                raise ValueError("invalid cursor")
        except ValueError:
            return _breath_parameter_error(selector, "cursor", "cursor 无效、已过期或与当前检索条件不匹配")

    if (as_of or "").strip():
        return await _breath_as_of_impl(
            as_of=as_of,
            query=query,
            max_tokens=max_tokens,
            domain=domain,
            valence=valence,
            arousal=arousal,
            max_results=max_results,
            importance_min=importance_min,
            recent_days=recent_days,
            emotion_trend=emotion_trend,
            include_dormant=include_dormant,
            include_sealed=include_sealed,
            date_from=date_from,
            date_to=date_to,
            resonance=resonance,
            tags_filter=tags_filter,
            topic_filter=topic_filter,
            wake_dormant=wake_dormant,
            cursor=cursor,
            min_score=min_score,
            mode=mode,
        )

    query = _apply_display_aliases(query)
    max_results = max(1, min(max_results, 50))
    max_tokens = min(max_tokens, 20000)
    recent_cutoff = cursor_state["context"]["recent_cutoff"] if cursor_state is not None else _recent_cutoff(recent_days)
    try:
        resolved_min_score = _resolve_breath_min_score(min_score) if selector == "ordinary_query" else 0.0
    except ValueError as exc:
        return str(exc)
    cursor_context = {"selector": selector, "recent_days": recent_days, "recent_cutoff": recent_cutoff}
    cursor_scope = _breath_cursor_scope(
        query=query, domain=domain, valence=valence, arousal=arousal,
        recent_cutoff=recent_cutoff, include_dormant=include_dormant,
        include_sealed=include_sealed, date_from=date_from, date_to=date_to,
        resonance=resonance, min_score=resolved_min_score, touch=touch, mode=mode,
        selector=selector, importance_min=importance_min,
        wake_dormant=wake_dormant, recent_days=recent_days,
    )
    if cursor:
        try:
            _decode_breath_cursor(cursor, cursor_scope)
        except ValueError:
            return _breath_parameter_error(selector, "cursor", "cursor 无效、已过期或与当前检索条件不匹配")
    if touch:
        await decay_engine.ensure_started()

    domain_values = domain.split(",") if domain else []
    def common_filters(buckets, *, core=False):
        return _filter_breath_candidates(
            buckets, domain_values=domain_values, recent_cutoff=None if core else recent_cutoff,
            include_dormant=include_dormant, include_sealed=include_sealed,
            date_from=date_from, date_to=date_to, tags_filter=tags_filter,
            importance_min=importance_min, recent_days=-1 if core else recent_days,
            apply_dormant=not core,
        )

    if (tags_filter or topic_filter) and selector in ("ordinary_query", "tags_only", "session", "feel"):
        return await _breath_filtered_impl(
            query=query,
            max_tokens=max_tokens,
            domain=domain,
            valence=valence,
            arousal=arousal,
            max_results=max_results,
            mode=mode,
            recent_cutoff=recent_cutoff,
            include_dormant=include_dormant, include_sealed=include_sealed,
            wake_dormant=wake_dormant,
            touch=touch,
            date_from=date_from,
            date_to=date_to,
            resonance_target=resonance_target,
            emotion_trend=emotion_trend,
            tags_filter=tags_filter,
            topic_filter=topic_filter,
            min_score=resolved_min_score,
            importance_min=importance_min,
            recent_days=recent_days,
        )

    # --- Session archive retrieval: archived session buckets are searchable by domain ---
    if domain.strip().lower() == "session":
        try:
            all_buckets = await bucket_mgr.list_all(include_archive=True)
            sessions = [
                b for b in all_buckets
                if "session" in b.get("metadata", {}).get("domain", [])
                and _is_recent_bucket(b, recent_cutoff, exact_day=recent_days == 0)
                and _is_in_date_range(b, date_from, date_to)
                and (include_sealed or not _is_sealed(b))
                and _breath_importance_matches(b, importance_min)
            ]
            if query and query.strip():
                q = query.strip().lower()
                sessions = [
                    b for b in sessions
                    if q in str(b.get("metadata", {}).get("name", "")).lower()
                    or q in b.get("content", "").lower()
                ]
            sessions.sort(key=lambda b: _bucket_date(b["metadata"], "updated_at", "created_at", "created"), reverse=True)
            total_sessions = len(sessions)
            sessions = sessions[:max_results]
            if not sessions:
                return _with_emotion_timeline("没有找到对话归档。", emotion_trend)
            results = []
            for b in sessions:
                meta = b.get("metadata", {})
                body = str(b.get("content", ""))
                if query.strip() and mode == "full":
                    available = max_tokens - count_tokens_approx("\n---\n".join(results))
                    body = _prefix_within_token_budget(body, available)
                    display = "原文·已截断" if body != str(b.get("content", "")) else "原文"
                    body = f"[显示={display}] {body}"
                else:
                    preview = body[:1200]
                    body = strip_wikilinks(preview)
                    if query.strip():
                        display = "原文节选·已截断" if len(str(b.get("content", ""))) > 1200 else "原文"
                        if body != preview:
                            display += "·双链标记已省略"
                        body = f"[显示={display}] {body}"
                    elif len(str(b.get("content", ""))) > 1200:
                        body += "\n" + _format_bucket_truncation_notice(
                            str(b["id"]), len(body), len(str(b.get("content", "")))
                        )
                text = (
                    f"[session] [bucket_id:{b['id']}] {meta.get('name', b['id'])}\n"
                    f"{body}"
                )
                entry = await _append_bucket_extras(text, b, emotion_trend)
                if not body or count_tokens_approx(body if query.strip() and mode == "full" else "\n---\n".join(results + [entry])) > max_tokens:
                    break
                results.append(entry)
            text = "\n---\n".join(results)
            text += _breath_listing_accounting(total_sessions, len(sessions), len(results))
            return _with_emotion_timeline(text, emotion_trend)
        except Exception as e:
            logger.error(f"Session archive retrieval failed: {e}")
            return "读取对话归档失败。"

    # --- Feel retrieval: domain="feel" is a special channel ---
    if domain.strip().lower() == "feel":
        try:
            all_buckets = await bucket_mgr.list_all(include_archive=False)
            feels = [
                b for b in all_buckets
                if b["metadata"].get("type") == "feel"
                and _is_recent_bucket(b, recent_cutoff, exact_day=recent_days == 0)
                and _is_in_date_range(b, date_from, date_to)
                and (include_sealed or not _is_sealed(b))
                and _breath_importance_matches(b, importance_min)
            ]
            if query and query.strip():
                q = query.strip().lower()
                feels = [
                    b for b in feels
                    if q in str(b.get("metadata", {}).get("name", "")).lower()
                    or q in b.get("content", "").lower()
                    or any(q in str(tag).lower() for tag in b.get("metadata", {}).get("tags", []))
                ]
            feels.sort(key=lambda b: _bucket_date(b["metadata"], "updated_at", "created_at", "created"), reverse=True)
            if not feels:
                return _with_emotion_timeline("没有留下过 feel。", emotion_trend)
            results = []
            for f in feels[:max_results]:
                meta = f["metadata"]
                created = _bucket_date(meta, "created_at", "created")
                updated = _bucket_date(meta, "updated_at", "last_active", "created")
                entry = (
                    f"[{created}] [bucket_id:{f['id']}] "
                    f"name:{meta.get('name', f['id'])} updated_at:{updated} "
                    f"tags:{','.join(meta.get('tags', []))}\n"
                    f"{strip_wikilinks(f['content'])}"
                )
                entry = await _append_bucket_extras(entry, f, emotion_trend)
                if count_tokens_approx("\n---\n".join(results + [entry])) > max_tokens:
                    break
                results.append(entry)
            return _with_emotion_timeline(
                "=== 你留下的 feel ===\n" + "\n---\n".join(results)
                + _breath_listing_accounting(len(feels), min(len(feels), max_results), len(results)),
                emotion_trend,
            )
        except Exception as e:
            logger.error(f"Feel retrieval failed: {e}")
            return "读取 feel 失败。"

    # --- importance_min mode: bulk fetch by importance threshold ---
    if selector == "importance_only":
        try:
            all_buckets = await bucket_mgr.list_all(include_archive=False)
        except Exception as e:
            logger.error("Breath importance retrieval failed: %s", e); return "记忆系统暂时无法访问。"
        filtered = [b for b in common_filters(all_buckets) if b["metadata"].get("type") != "feel"]
        filtered.sort(key=lambda b: int(b["metadata"].get("importance", 0)), reverse=True)
        total_filtered = len(filtered)
        filtered = filtered[:max_results]
        if not filtered:
            return _with_emotion_timeline(
                f"没有重要度 >= {importance_min} 的记忆。",
                emotion_trend,
            )
        results, emitted, failed = [], [], 0
        for b in filtered:
            try:
                entry = await _append_bucket_extras(await _bucket_summary_line(b, importance_only=True), b, emotion_trend)
                if count_tokens_approx("\n---\n".join(results + [entry])) > max_tokens:
                    break
                results.append(entry)
                emitted.append(b)
            except Exception:
                failed += 1
                logger.warning("Breath importance rendering failed", exc_info=True)
        response = "\n---\n".join(results) if results else "没有可以展示的记忆。"
        hidden_count = max(0, total_filtered - len(filtered))
        if hidden_count:
            response += f"\n\n还有{hidden_count}个相关桶未显示"
        response = _with_emotion_timeline(response + _breath_listing_accounting(total_filtered, len(filtered), len(results), failed), emotion_trend)
        touch_failures = 0
        if touch:
            for bucket in emitted:
                try:
                    await bucket_mgr.touch(bucket["id"], wake_dormant=wake_dormant)
                except Exception:
                    logger.warning("Breath importance direct touch failed", exc_info=True)
                    touch_failures += 1
        return response + _breath_side_effect_warning(touch_failures)

    # --- Resonance mode without query: sort visible memories by emotion distance ---
    if resonance_target and (not query or not query.strip()):
        try:
            all_buckets = await bucket_mgr.list_all(include_archive=False)
        except Exception as e:
            logger.error(f"Failed to list buckets for resonance: {e}")
            return "记忆系统暂时无法访问。"
        candidates = [b for b in common_filters(all_buckets) if b["metadata"].get("type") != "feel"]
        candidates.sort(key=lambda b: _resonance_distance(b, resonance_target))
        total = len(candidates)
        candidates = candidates[:max_results]
        results, emitted, failed = [], [], 0
        for b in candidates:
            try:
                entry = await _append_bucket_extras(await _bucket_summary_line(b, score=_resonance_distance(b, resonance_target)), b, emotion_trend)
                if count_tokens_approx("\n---\n".join(results + [entry])) > max_tokens:
                    break
                results.append(entry)
                emitted.append(b)
            except Exception:
                failed += 1
                logger.warning("Breath resonance rendering failed", exc_info=True)
        if not results:
            if total == 0:
                return _with_emotion_timeline("未找到共鸣记忆。", emotion_trend)
        response = "\n---\n".join(results)
        hidden_count = max(0, total - len(candidates))
        if hidden_count:
            response += f"\n\n还有{hidden_count}个共鸣桶未显示"
        response = _with_emotion_timeline(response + _breath_listing_accounting(total, len(candidates), len(results), failed), emotion_trend)
        touch_failures = 0
        if touch:
            for bucket in emitted:
                try:
                    await bucket_mgr.touch(bucket["id"], wake_dormant=wake_dormant)
                except Exception:
                    logger.warning("Breath resonance direct touch failed", exc_info=True)
                    touch_failures += 1
        return response + _breath_side_effect_warning(touch_failures)

    # --- No args or empty query: surfacing mode (weight pool active push) ---
    if not query or not query.strip():
        try:
            all_buckets = await bucket_mgr.list_all(include_archive=False)
        except Exception as e:
            logger.error(f"Failed to list buckets for surfacing / 浮现列桶失败: {e}")
            return "记忆系统暂时无法访问。"

        pinned_buckets = [
            b for b in common_filters(all_buckets, core=True)
            if b["metadata"].get("pinned") or b["metadata"].get("protected")
            if _is_in_date_range(b, date_from, date_to)
            if include_sealed or not _is_sealed(b)
        ]
        unresolved = [
            b for b in common_filters(all_buckets)
            if not b["metadata"].get("resolved", False)
            and b["metadata"].get("type") not in ("permanent", "feel")
            and not b["metadata"].get("pinned", False)
            and not b["metadata"].get("protected", False)
            and (include_dormant or not b["metadata"].get("dormant", False))
            and (include_sealed or not _is_sealed(b))
            and _is_recent_bucket(b, recent_cutoff)
            and _is_in_date_range(b, date_from, date_to)
        ]

        logger.info(f"Breath surfacing: {len(all_buckets)} total, {len(pinned_buckets)} pinned, {len(unresolved)} unresolved")
        scored = sorted(unresolved, key=lambda b: decay_engine.calculate_score(b["metadata"]), reverse=True)
        cold_start = [
            b for b in unresolved
            if int(b["metadata"].get("activation_count", 0)) == 0
            and int(b["metadata"].get("importance", 0)) >= 8
        ][:2]
        cold_start_ids = {b["id"] for b in cold_start}
        scored_deduped = [b for b in scored if b["id"] not in cold_start_ids]
        scored_with_cold = cold_start + scored_deduped

        candidates = list(scored_with_cold)
        if len(candidates) > 1:
            n_cold = len(cold_start)
            non_cold = candidates[n_cold:]
            if len(non_cold) > 1:
                top1 = [non_cold[0]]
                pool = non_cold[1:min(20, len(non_cold))]
                random.shuffle(pool)
                non_cold = top1 + pool + non_cold[min(20, len(non_cold)):]
            candidates = cold_start + non_cold
        candidates = candidates[:max_results]
        summary_mode = mode == "summary"
        pinned_results = []
        dynamic_results = []
        emitted = []
        failed = 0
        token_budget = max_tokens

        if summary_mode:
            for b in pinned_buckets:
                entry = await _append_bucket_extras(
                    await _bucket_summary_line(
                        b,
                        pinned=bool(b["metadata"].get("pinned", False)),
                    ),
                    b,
                    emotion_trend,
                )
                required = count_tokens_approx(entry)
                if required > token_budget:
                    break
                pinned_results.append(entry)
                token_budget -= required
            for b in candidates:
                try:
                    entry = await _append_bucket_extras(await _bucket_summary_line(b, score=decay_engine.calculate_score(b["metadata"])), b, emotion_trend)
                    required = count_tokens_approx(entry)
                    if required > token_budget:
                        break
                    dynamic_results.append(entry)
                    emitted.append(b)
                    token_budget -= required
                except Exception:
                    failed += 1
                    logger.warning("Breath emergence rendering failed", exc_info=True)
        else:
            for b in pinned_buckets:
                try:
                    clean_meta = {k: v for k, v in b["metadata"].items() if k != "tags"}
                    content = strip_wikilinks(b["content"])
                    if touch:
                        summary = await dehydrator.dehydrate(content, clean_meta)
                    else:
                        summary = await dehydrator.dehydrate(
                            content, clean_meta, cache_read=True, cache_write=False
                        )
                    marker = "📌 " if b["metadata"].get("pinned", False) else ""
                    line = f"{marker}[核心准则] [bucket_id:{b['id']}] {summary}"
                    t = count_tokens_approx(line)
                    if token_budget - t < 0:
                        break
                    pinned_results.append(await _append_bucket_extras(line, b, emotion_trend))
                    token_budget -= t
                except Exception as e:
                    logger.warning(f"Failed to dehydrate pinned bucket / 钉选桶脱水失败: {e}")
                    failed += 1
            for b in candidates:
                if token_budget <= 0:
                    break
                try:
                    clean_meta = {k: v for k, v in b["metadata"].items() if k != "tags"}
                    content = strip_wikilinks(b["content"])
                    if touch:
                        summary = await dehydrator.dehydrate(content, clean_meta)
                    else:
                        summary = await dehydrator.dehydrate(
                            content, clean_meta, cache_read=True, cache_write=False
                        )
                    summary_tokens = count_tokens_approx(summary)
                    if summary_tokens > token_budget:
                        break
                    score = decay_engine.calculate_score(b["metadata"])
                    line = f"[权重:{score:.2f}] [bucket_id:{b['id']}] {summary}"
                    dynamic_results.append(await _append_bucket_extras(line, b, emotion_trend))
                    emitted.append(b)
                    token_budget -= summary_tokens
                except Exception as e:
                    logger.warning(f"Failed to dehydrate surfaced bucket / 浮现脱水失败: {e}")
                    failed += 1
                    continue

        if not pinned_buckets and not unresolved:
            return _with_emotion_timeline(
                "权重池平静，没有需要处理的记忆。",
                emotion_trend,
            )

        parts = []
        if pinned_results:
            parts.append("=== 核心准则 ===\n" + "\n---\n".join(pinned_results))
        if dynamic_results:
            parts.append("=== 浮现记忆 ===\n" + "\n---\n".join(dynamic_results))
        response = _with_emotion_timeline(
            "\n\n".join(parts) + _breath_listing_accounting(
                len(pinned_buckets) + len(unresolved), len(pinned_buckets) + len(candidates),
                len(pinned_results) + len(dynamic_results), failed,
            ), emotion_trend,
        )
        touch_failures = 0
        if touch:
            for bucket in emitted:
                try:
                    await bucket_mgr.touch(bucket["id"], wake_dormant=wake_dormant)
                except Exception:
                    logger.warning("Breath emergence direct touch failed", exc_info=True)
                    touch_failures += 1
        return response + _breath_side_effect_warning(touch_failures)

    # --- Feel retrieval: domain="feel" is a special channel ---
    # --- Feel 检索：domain="feel" 是独立入口 ---
    if domain.strip().lower() == "feel":
        try:
            all_buckets = await bucket_mgr.list_all(include_archive=False)
            feels = [
                b for b in all_buckets
                if b["metadata"].get("type") == "feel"
                and _is_recent_bucket(b, recent_cutoff)
                and _is_in_date_range(b, date_from, date_to)
                and (include_sealed or not _is_sealed(b))
            ]
            feels.sort(key=lambda b: _bucket_date(b["metadata"], "updated_at", "created_at", "created"), reverse=True)
            if not feels:
                return _with_emotion_timeline("没有留下过 feel。", emotion_trend)
            results = []
            for f in feels:
                meta = f["metadata"]
                created = _bucket_date(meta, "created_at", "created")
                updated = _bucket_date(meta, "updated_at", "last_active", "created")
                entry = (
                    f"[{created}] [bucket_id:{f['id']}] "
                    f"name:{meta.get('name', f['id'])} updated_at:{updated} "
                    f"tags:{','.join(meta.get('tags', []))}\n"
                    f"{strip_wikilinks(f['content'])}"
                )
                entry = await _append_bucket_extras(entry, f, emotion_trend)
                results.append(entry)
                if count_tokens_approx("\n---\n".join(results)) > max_tokens:
                    break
            return _with_emotion_timeline(
                "=== 你留下的 feel ===\n" + "\n---\n".join(results),
                emotion_trend,
            )
        except Exception as e:
            logger.error(f"Feel retrieval failed: {e}")
            return "读取 feel 失败。"

    # --- With args: search mode (keyword + vector dual channel) ---
    # --- 有参数：检索模式（关键词 + 向量双通道）---
    domain_filter = [d.strip() for d in domain.split(",") if d.strip()] or None
    q_valence = valence if 0 <= valence <= 1 else None
    q_arousal = arousal if 0 <= arousal <= 1 else None

    search_trace = {}
    if cursor:
        try:
            frozen_matches, position = _decode_breath_cursor(cursor, cursor_scope)
        except ValueError:
            return "cursor 无效、已过期或与当前检索条件不匹配。"
        # Revalidate the whole frozen order so sealed/deleted/dormant entries
        # cannot leak into the public total or leave holes in a resumed page.
        eligible = []
        for index, record in enumerate(frozen_matches):
            bucket = await bucket_mgr.get(record["id"])
            if bucket is None:
                continue
            rendered = dict(bucket)
            rendered["_breath_score"] = float(record["score"])
            rendered.pop("score", None)
            if record.get("retrieval_score") is not None:
                rendered["score"] = float(record["retrieval_score"])
            if (
                record.get("retrieval_score") is None and record["channel"] == "精确"
                and not _breath_exact_anchor_match(query, rendered)
            ):
                record["channel"] = "关键词"
            rendered["_breath_channel"] = record["channel"]
            rendered["_breath_weak"] = bool(record["weak"])
            rendered["vector_match"] = bool(record.get("vector_match", False))
            rendered["_breath_index"] = index
            if not _filter_breath_query_matches(
                [rendered],
                recent_cutoff=recent_cutoff,
                date_from=date_from,
                date_to=date_to,
                include_sealed=include_sealed,
            ):
                continue
            if not include_dormant and rendered.get("metadata", {}).get("dormant", False):
                continue
            if not common_filters([rendered]):
                continue
            eligible.append(rendered)
        prior_consumed = sum(1 for bucket in eligible if bucket["_breath_index"] < position)
        matches = [bucket for bucket in eligible if bucket["_breath_index"] >= position][:max_results]
        ordered_matches = frozen_matches
        total_matches = len(eligible)
        downgraded_count = sum(1 for bucket in eligible if bucket["_breath_weak"])
    else:
        try:
            scoped_candidates = None
            if domain_values or importance_min != -1 or recent_days != -1 or date_from or date_to:
                scoped_candidates = common_filters(await bucket_mgr.list_all(include_archive=False))
            matches = await bucket_mgr.search(
                query,
                limit=1000,
                domain_filter=domain_filter if scoped_candidates is None else None,
                query_valence=q_valence,
                query_arousal=q_arousal,
                include_dormant=include_dormant,
                include_sealed=include_sealed,
                trace=search_trace,
                **({"candidate_buckets": scoped_candidates} if scoped_candidates is not None else {}),
            ) if scoped_candidates is None or scoped_candidates else []
        except Exception as e:
            logger.error(f"Search failed / 检索失败: {e}")
            return "检索过程出错，请稍后重试。"

        matches = _filter_breath_query_matches(
            matches,
            recent_cutoff=recent_cutoff,
            date_from=date_from,
            date_to=date_to,
            include_sealed=include_sealed,
        )
        matches = common_filters(matches)
        if resonance_target:
            matches.sort(key=lambda bucket: _resonance_distance(bucket, resonance_target))
        trace_by_id = {
            str(entry.get("id", "")): entry
            for entry in search_trace.get("candidates", [])
        }
        matches = _annotate_breath_query_matches(
            matches,
            query=query,
            trace_by_id=trace_by_id,
            min_score=resolved_min_score,
        )
        ordered_matches = _order_breath_query_matches(matches)
        for index, bucket in enumerate(ordered_matches):
            bucket["_breath_index"] = index
        position = 0
        prior_consumed = 0
        total_matches = len(ordered_matches)
        downgraded_count = sum(1 for bucket in ordered_matches if bucket["_breath_weak"])
        matches = ordered_matches[:max_results]
    hidden_count = max(0, total_matches - prior_consumed - len(matches))

    final_text, composition = await _compose_breath_query_matches(
        matches,
        max_tokens=max_tokens,
        q_valence=q_valence,
        emotion_trend=emotion_trend,
        hidden_count=hidden_count,
        total_matches=total_matches,
        trace_by_id={
            str(entry.get("id", "")): entry
            for entry in search_trace.get("candidates", [])
        },
        touch=touch,
        wake_dormant=wake_dormant,
        downgraded_count=downgraded_count,
        mode=mode,
        ordered_matches=ordered_matches,
        start_position=position,
        prior_consumed=prior_consumed,
        cursor_scope=cursor_scope,
        cursor_context=cursor_context,
    )
    if not final_text:
        if touch:
            await _fire_webhook("breath", {"mode": "empty", "matches": 0})
        return _with_emotion_timeline("未找到相关记忆。", emotion_trend)

    if touch:
        await _fire_webhook(
            "breath",
            {
                "mode": "ok",
                "matches": len(matches),
                "chars": len(final_text),
            },
        )
    return final_text


ASSET_PROBE_MAX_BASE64_CHARS = 4 * 1024 * 1024
ASSET_PROBE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "probe.png")
ASSET_INGEST_TTL_SECONDS = 10 * 60
ASSET_INGEST_MAX_UPLOADS = 100
ASSET_INGEST_MAX_BYTES = 2 * 1024 * 1024
ASSET_INGEST_RECOMMENDED_CHUNK_BASE64_CHARS = 8192
ASSET_INGEST_MAX_CHUNK_BASE64_CHARS = 16384
_asset_ingest_uploads = {}
_asset_ingest_lock = threading.Lock()
ASSET_BROWSER_UPLOAD_TTL_SECONDS = 10 * 60
ASSET_BROWSER_UPLOAD_MAX_UPLOADS = 100
ASSET_BROWSER_UPLOAD_MAX_BYTES = 2 * 1024 * 1024
ASSET_BROWSER_UPLOAD_MAX_WIRE_OVERHEAD = 64 * 1024
RM_ASSET_MAX_UPLOAD_BYTES = 10 * 1024 * 1024


def _selected_asset_backend():
    # Keep the module-level seam intact: production receives the lazy proxy,
    # while tests and explicit runtime overrides may replace the registry.
    return asset_backend_registry.selected_backend()
_asset_browser_uploads = {}
_asset_browser_upload_tokens = {}
_asset_browser_upload_lock = threading.Lock()
RM_ASSET_UPLOAD_TTL_SECONDS = 10 * 60
RM_ASSET_UPLOAD_MAX_UPLOADS = 100
RM_ASSET_DOWNLOAD_TTL_SECONDS = 5 * 60
RM_ASSET_DOWNLOAD_MAX_TOKENS = 100
RM_ASSET_DOWNLOAD_MAX_GETS = 3
_rm_asset_uploads = {}
_rm_asset_upload_tokens = {}
_rm_asset_upload_sources = {}
_rm_asset_upload_lock = threading.Lock()
_rm_asset_download_tokens = {}
_rm_asset_download_sources = {}
_rm_asset_download_lock = threading.Lock()
ASSET_VISION_WIDTH = 256
ASSET_VISION_HEIGHT = 256
ASSET_VISION_TTL_SECONDS = 10 * 60
ASSET_VISION_MAX_TRIALS = 100
ASSET_VISION_DOWNLOAD_TTL_SECONDS = 5 * 60
ASSET_VISION_MAX_DOWNLOAD_TOKENS = 100
ASSET_VISION_DOWNLOAD_MAX_GETS = 3
ASSET_VISION_COLORS = {
    "red": (220, 38, 38),
    "green": (34, 197, 94),
    "blue": (37, 99, 235),
    "orange": (249, 115, 22),
    "purple": (147, 51, 234),
    "yellow": (250, 204, 21),
}
ASSET_VISION_SYMBOLS = ("circle", "triangle", "square")
ASSET_VISION_POSITIONS = ("top_left", "top_right", "bottom_left", "bottom_right")
_ASSET_VISION_RNG = secrets.SystemRandom()
_asset_vision_trials = {}
_asset_vision_download_tokens = {}
_asset_vision_lock = threading.Lock()


def _asset_ingest_response(ok: bool, upload_id: str = "", error: str = "", **fields) -> str:
    payload = {"ok": ok}
    if upload_id:
        payload["upload_id"] = upload_id
    if error:
        payload["error"] = error
    payload.update(fields)
    return _json_lib.dumps(payload, ensure_ascii=False, sort_keys=True)


_ATTACHMENT_CONTAINER_KEYS = {
    "attachment",
    "attachments",
    "file",
    "files",
    "resource",
    "resources",
}
_ATTACHMENT_REFERENCE_KEYS = {
    "attachment_id",
    "attachment_reference",
    "attachment_url",
    "file_id",
    "resource_uri",
    "uri",
    "url",
}
_ATTACHMENT_BYTES_KEYS = {"blob", "bytes", "content", "data"}
_ATTACHMENT_MIME_KEYS = {"content_type", "media_type", "mime_type", "mimetype"}


def _attachment_probe_value_available(value) -> bool:
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bool(value)
    return value is not None


def _attachment_probe_scan(
    value,
    *,
    in_attachment_scope: bool = False,
    depth: int = 0,
    seen: set[int] | None = None,
) -> tuple[bool, bool, bool]:
    if value is None or depth > 4:
        return False, False, False
    seen = seen if seen is not None else set()
    identity = id(value)
    if identity in seen:
        return False, False, False
    seen.add(identity)

    if hasattr(value, "model_extra"):
        value = getattr(value, "model_extra", None) or {}
    if isinstance(value, dict):
        reference = False
        raw_bytes = False
        mime_type = False
        for raw_key, child in value.items():
            key = str(raw_key).strip().lower().replace("-", "_")
            child_scope = in_attachment_scope or key in _ATTACHMENT_CONTAINER_KEYS
            if child_scope and key in _ATTACHMENT_REFERENCE_KEYS:
                reference = reference or _attachment_probe_value_available(child)
            if child_scope and key in _ATTACHMENT_BYTES_KEYS:
                raw_bytes = raw_bytes or _attachment_probe_value_available(child)
            if child_scope and key in _ATTACHMENT_MIME_KEYS:
                mime_type = mime_type or _attachment_probe_value_available(child)
            if child_scope and isinstance(child, (dict, list, tuple)):
                nested = _attachment_probe_scan(
                    child,
                    in_attachment_scope=True,
                    depth=depth + 1,
                    seen=seen,
                )
                reference = reference or nested[0]
                raw_bytes = raw_bytes or nested[1]
                mime_type = mime_type or nested[2]
        return reference, raw_bytes, mime_type
    if isinstance(value, (list, tuple)) and in_attachment_scope:
        reference = raw_bytes = mime_type = False
        for child in value:
            nested = _attachment_probe_scan(
                child,
                in_attachment_scope=True,
                depth=depth + 1,
                seen=seen,
            )
            reference = reference or nested[0]
            raw_bytes = raw_bytes or nested[1]
            mime_type = mime_type or nested[2]
        return reference, raw_bytes, mime_type
    return False, False, False


def _attachment_probe_context_signals(ctx: Context | None) -> tuple[bool, bool, bool]:
    if ctx is None:
        return False, False, False
    try:
        request_context = ctx.request_context
    except (AttributeError, LookupError, RuntimeError):
        return False, False, False
    meta_signals = _attachment_probe_scan(getattr(request_context, "meta", None))
    experimental_signals = _attachment_probe_scan(
        getattr(request_context, "experimental", None)
    )
    return tuple(
        meta_signals[index] or experimental_signals[index]
        for index in range(3)
    )

def _asset_cleanup_expired_ingest_uploads(now: float) -> None:
    expired = [upload_id for upload_id, item in _asset_ingest_uploads.items() if item["expires_at"] <= now]
    for upload_id in expired:
        _asset_ingest_uploads.pop(upload_id, None)


def _asset_sanitize_ingest_filename(filename: str) -> str:
    cleaned = re.sub(r"[\x00-\x1f\x7f/\\:]+", "_", (filename or "").strip())
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    return cleaned[:255]


def _asset_begin_ingest_upload(
    expected_bytes: int,
    expected_sha256: str,
    mime_type: str = "application/octet-stream",
    filename: str = "",
    now: float | None = None,
) -> str:
    if isinstance(expected_bytes, bool) or not isinstance(expected_bytes, int) or expected_bytes < 0:
        return _asset_ingest_response(False, error="invalid_expected_bytes")
    if expected_bytes > ASSET_INGEST_MAX_BYTES:
        return _asset_ingest_response(False, error="file_too_large", max_bytes=ASSET_INGEST_MAX_BYTES)
    expected = (expected_sha256 or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", expected):
        return _asset_ingest_response(False, error="invalid_expected_sha256")

    current = time.time() if now is None else now
    with _asset_ingest_lock:
        _asset_cleanup_expired_ingest_uploads(current)
        if len(_asset_ingest_uploads) >= ASSET_INGEST_MAX_UPLOADS:
            return _asset_ingest_response(False, error="upload_store_full")
        while True:
            upload_id = secrets.token_hex(16)
            if upload_id not in _asset_ingest_uploads:
                break
        _asset_ingest_uploads[upload_id] = {
            "expected_bytes": expected_bytes,
            "expected_sha256": expected,
            "mime_type": (mime_type or "application/octet-stream").strip() or "application/octet-stream",
            "filename": _asset_sanitize_ingest_filename(filename),
            "chunks": [],
            "decoded_bytes": 0,
            "expires_at": current + ASSET_INGEST_TTL_SECONDS,
        }
    return _asset_ingest_response(
        True,
        upload_id=upload_id,
        recommended_chunk_base64_chars=ASSET_INGEST_RECOMMENDED_CHUNK_BASE64_CHARS,
        max_chunk_base64_chars=ASSET_INGEST_MAX_CHUNK_BASE64_CHARS,
        expires_in_seconds=ASSET_INGEST_TTL_SECONDS,
    )


def _asset_ingest_chunk_data(
    upload_id: str,
    chunk_index: int,
    data_base64: str,
    now: float | None = None,
) -> str:
    upload_id = (upload_id or "").strip()
    if not re.fullmatch(r"[0-9a-f]{32}", upload_id):
        return _asset_ingest_response(False, error="invalid_upload_id")
    if isinstance(chunk_index, bool) or not isinstance(chunk_index, int) or chunk_index < 0:
        return _asset_ingest_response(False, upload_id=upload_id, error="invalid_chunk_index")
    base64_chars = len(data_base64 or "")
    if base64_chars > ASSET_INGEST_MAX_CHUNK_BASE64_CHARS:
        return _asset_ingest_response(
            False,
            upload_id=upload_id,
            error="chunk_too_large",
            base64_chars=base64_chars,
            max_chunk_base64_chars=ASSET_INGEST_MAX_CHUNK_BASE64_CHARS,
        )
    try:
        raw = base64.b64decode((data_base64 or "").encode("ascii"), validate=True)
    except (binascii.Error, UnicodeEncodeError, ValueError):
        return _asset_ingest_response(False, upload_id=upload_id, error="invalid_base64")
    if not raw:
        return _asset_ingest_response(False, upload_id=upload_id, error="empty_chunk")

    current = time.time() if now is None else now
    with _asset_ingest_lock:
        _asset_cleanup_expired_ingest_uploads(current)
        upload = _asset_ingest_uploads.get(upload_id)
        if not upload:
            return _asset_ingest_response(False, upload_id=upload_id, error="upload_unavailable")
        chunks = upload["chunks"]
        if chunk_index < len(chunks):
            if not hmac.compare_digest(chunks[chunk_index], raw):
                return _asset_ingest_response(False, upload_id=upload_id, error="chunk_conflict")
            return _asset_ingest_response(
                True,
                upload_id=upload_id,
                decoded_bytes=upload["decoded_bytes"],
                received_chunks=len(chunks),
                idempotent=True,
            )
        if chunk_index > len(chunks):
            return _asset_ingest_response(
                False,
                upload_id=upload_id,
                error="chunk_out_of_order",
                expected_chunk_index=len(chunks),
            )
        if upload["decoded_bytes"] + len(raw) > ASSET_INGEST_MAX_BYTES:
            return _asset_ingest_response(False, upload_id=upload_id, error="file_too_large", max_bytes=ASSET_INGEST_MAX_BYTES)
        chunks.append(raw)
        upload["decoded_bytes"] += len(raw)
        return _asset_ingest_response(
            True,
            upload_id=upload_id,
            decoded_bytes=upload["decoded_bytes"],
            received_chunks=len(chunks),
            idempotent=False,
        )


def _asset_finish_ingest_upload(upload_id: str, now: float | None = None) -> str:
    upload_id = (upload_id or "").strip()
    if not re.fullmatch(r"[0-9a-f]{32}", upload_id):
        return _asset_ingest_response(False, error="invalid_upload_id")
    current = time.time() if now is None else now
    with _asset_ingest_lock:
        _asset_cleanup_expired_ingest_uploads(current)
        upload = _asset_ingest_uploads.pop(upload_id, None)
    if not upload:
        return _asset_ingest_response(False, upload_id=upload_id, error="upload_unavailable")

    raw = b"".join(upload["chunks"])
    sha256 = hashlib.sha256(raw).hexdigest()
    expected_sha256 = upload["expected_sha256"]
    return _asset_ingest_response(
        True,
        upload_id=upload_id,
        decoded_bytes=len(raw),
        sha256=sha256,
        expected_sha256=expected_sha256,
        size_match=len(raw) == upload["expected_bytes"],
        hash_match=hmac.compare_digest(sha256, expected_sha256),
        received_chunks=len(upload["chunks"]),
    )


def _asset_abort_ingest_upload(upload_id: str, now: float | None = None) -> str:
    upload_id = (upload_id or "").strip()
    if not re.fullmatch(r"[0-9a-f]{32}", upload_id):
        return _asset_ingest_response(False, error="invalid_upload_id")
    current = time.time() if now is None else now
    with _asset_ingest_lock:
        _asset_cleanup_expired_ingest_uploads(current)
        aborted = _asset_ingest_uploads.pop(upload_id, None) is not None
    return _asset_ingest_response(True, upload_id=upload_id, aborted=aborted)

class _AssetBrowserUploadError(Exception):
    pass


class _AssetBrowserUploadTooLarge(_AssetBrowserUploadError):
    pass


def _asset_cleanup_browser_uploads(now: float) -> None:
    for upload_id, item in list(_asset_browser_uploads.items()):
        if item["state"] in ("pending", "uploading") and item["expires_at"] <= now:
            token = item.get("token", "")
            if token:
                _asset_browser_upload_tokens.pop(token, None)
            item["token"] = ""
            item["state"] = "expired"
        if item["retire_at"] <= now:
            token = item.get("token", "")
            if token:
                _asset_browser_upload_tokens.pop(token, None)
            _asset_browser_uploads.pop(upload_id, None)


def _asset_sanitize_mime_type(mime_type: str) -> str:
    cleaned = re.sub(r"[\x00-\x1f\x7f]+", "", mime_type or "").strip()
    return (cleaned or "application/octet-stream")[:255]


def _asset_create_browser_upload_link(
    expected_bytes: int,
    expected_sha256: str = "",
    filename: str = "",
    mime_type: str = "application/octet-stream",
    now: float | None = None,
) -> str:
    if isinstance(expected_bytes, bool) or not isinstance(expected_bytes, int) or not 0 <= expected_bytes <= ASSET_BROWSER_UPLOAD_MAX_BYTES:
        return _asset_ingest_response(False, error="invalid_expected_bytes", max_bytes=ASSET_BROWSER_UPLOAD_MAX_BYTES)
    expected = (expected_sha256 or "").strip().lower()
    if expected and not re.fullmatch(r"[0-9a-f]{64}", expected):
        return _asset_ingest_response(False, error="invalid_expected_sha256")

    current = time.time() if now is None else now
    with _asset_browser_upload_lock:
        _asset_cleanup_browser_uploads(current)
        active = sum(1 for item in _asset_browser_uploads.values() if item["state"] in ("pending", "uploading"))
        if active >= ASSET_BROWSER_UPLOAD_MAX_UPLOADS:
            return _asset_ingest_response(False, error="upload_store_full")
        while True:
            upload_id = secrets.token_hex(16)
            if upload_id not in _asset_browser_uploads:
                break
        while True:
            token = secrets.token_urlsafe(32)
            if token not in _asset_browser_upload_tokens:
                break
        expires_at = current + ASSET_BROWSER_UPLOAD_TTL_SECONDS
        _asset_browser_uploads[upload_id] = {
            "state": "pending",
            "token": token,
            "expected_bytes": expected_bytes,
            "expected_sha256": expected,
            "filename": _asset_sanitize_ingest_filename(filename),
            "mime_type": _asset_sanitize_mime_type(mime_type),
            "expires_at": expires_at,
            "retire_at": expires_at + ASSET_BROWSER_UPLOAD_TTL_SECONDS,
            "result": None,
        }
        _asset_browser_upload_tokens[token] = upload_id

    upload_path = f"/rm/upload/{token}"
    base_url = _asset_public_base_url()
    return _json_lib.dumps({
        "ok": True,
        "upload_id": upload_id,
        "upload_path": upload_path,
        "upload_url": f"{base_url}{upload_path}" if base_url else "",
        "status_path": f"/rm/upload-status/{upload_id}",
        "expires_in_seconds": ASSET_BROWSER_UPLOAD_TTL_SECONDS,
        "max_bytes": ASSET_BROWSER_UPLOAD_MAX_BYTES,
    }, ensure_ascii=False, sort_keys=True)


def _asset_browser_upload_status_payload(upload_id: str, now: float | None = None) -> str:
    upload_id = (upload_id or "").strip()
    if not re.fullmatch(r"[0-9a-f]{32}", upload_id):
        return _asset_ingest_response(False, error="invalid_upload_id")
    current = time.time() if now is None else now
    with _asset_browser_upload_lock:
        _asset_cleanup_browser_uploads(current)
        item = _asset_browser_uploads.get(upload_id)
        if not item:
            return _asset_ingest_response(False, upload_id=upload_id, error="upload_unavailable")
        state = "pending" if item["state"] == "uploading" else item["state"]
        result = dict(item["result"] or {})
        payload = {
            "ok": True,
            "state": state,
            "decoded_bytes": result.get("decoded_bytes", 0),
            "sha256": result.get("sha256", ""),
            "expected_bytes": item["expected_bytes"],
            "expected_sha256": item["expected_sha256"],
            "size_match": result.get("size_match", False),
            "hash_match": result.get("hash_match", False),
            "filename": item["filename"],
            "mime_type": item["mime_type"],
        }
    return _json_lib.dumps(payload, ensure_ascii=False, sort_keys=True)


def _asset_get_browser_upload(token: str, now: float | None = None) -> dict | None:
    if not re.fullmatch(r"[A-Za-z0-9_-]{40,128}", token or ""):
        return None
    current = time.time() if now is None else now
    with _asset_browser_upload_lock:
        _asset_cleanup_browser_uploads(current)
        upload_id = _asset_browser_upload_tokens.get(token)
        item = _asset_browser_uploads.get(upload_id or "")
        if not item or item["state"] != "pending":
            return None
        return {
            "upload_id": upload_id,
            "expected_bytes": item["expected_bytes"],
            "filename": item["filename"],
            "expires_at": item["expires_at"],
        }


def _asset_claim_browser_upload(token: str, now: float | None = None) -> dict | None:
    if not re.fullmatch(r"[A-Za-z0-9_-]{40,128}", token or ""):
        return None
    current = time.time() if now is None else now
    with _asset_browser_upload_lock:
        _asset_cleanup_browser_uploads(current)
        upload_id = _asset_browser_upload_tokens.pop(token, None)
        item = _asset_browser_uploads.get(upload_id or "")
        if not item or item["state"] != "pending":
            return None
        item["state"] = "uploading"
        return {"upload_id": upload_id, "token": token}


def _asset_release_browser_upload(upload_id: str, now: float | None = None) -> None:
    current = time.time() if now is None else now
    with _asset_browser_upload_lock:
        _asset_cleanup_browser_uploads(current)
        item = _asset_browser_uploads.get(upload_id)
        if not item or item["state"] != "uploading":
            return
        if item["expires_at"] <= current:
            item["state"] = "expired"
            item["token"] = ""
            return
        item["state"] = "pending"
        _asset_browser_upload_tokens[item["token"]] = upload_id


def _asset_complete_browser_upload(upload_id: str, decoded_bytes: int, sha256: str, now: float | None = None) -> dict | None:
    current = time.time() if now is None else now
    with _asset_browser_upload_lock:
        _asset_cleanup_browser_uploads(current)
        item = _asset_browser_uploads.get(upload_id)
        if not item or item["state"] != "uploading" or item["expires_at"] <= current:
            return None
        expected_sha256 = item["expected_sha256"]
        result = {
            "decoded_bytes": decoded_bytes,
            "sha256": sha256,
            "size_match": decoded_bytes == item["expected_bytes"],
            "hash_match": bool(expected_sha256) and hmac.compare_digest(sha256, expected_sha256),
        }
        item["state"] = "completed"
        item["token"] = ""
        item["result"] = result
        item["retire_at"] = current + ASSET_BROWSER_UPLOAD_TTL_SECONDS
        return dict(result)


async def _asset_stream_browser_upload(
    request,
    sink=None,
    *,
    max_bytes: int = ASSET_BROWSER_UPLOAD_MAX_BYTES,
    wire_overhead: int = ASSET_BROWSER_UPLOAD_MAX_WIRE_OVERHEAD,
) -> dict:
    from python_multipart import MultipartParser
    from python_multipart.multipart import parse_options_header

    content_type = request.headers.get("content-type", "")
    kind, options = parse_options_header(content_type.encode("latin-1", errors="ignore"))
    boundary = options.get(b"boundary")
    if kind != b"multipart/form-data" or not boundary:
        raise _AssetBrowserUploadError("invalid_multipart")

    state = {
        "headers": {},
        "header_name": bytearray(),
        "header_value": bytearray(),
        "in_file": False,
        "file_count": 0,
        "seen_file": False,
        "ended": False,
        "decoded_bytes": 0,
        "hasher": hashlib.sha256(),
    }

    def on_part_begin():
        state["headers"] = {}
        state["header_name"].clear()
        state["header_value"].clear()
        state["in_file"] = False

    def on_header_field(data, start, end):
        state["header_name"].extend(data[start:end])

    def on_header_value(data, start, end):
        state["header_value"].extend(data[start:end])

    def on_header_end():
        name = bytes(state["header_name"]).lower()
        state["headers"][name] = bytes(state["header_value"])
        state["header_name"].clear()
        state["header_value"].clear()

    def on_headers_finished():
        disposition, params = parse_options_header(state["headers"].get(b"content-disposition", b""))
        if disposition != b"form-data" or params.get(b"name") != b"file" or b"filename" not in params:
            raise _AssetBrowserUploadError("single_file_required")
        if state["file_count"]:
            raise _AssetBrowserUploadError("single_file_required")
        state["file_count"] = 1
        state["in_file"] = True

    def on_part_data(data, start, end):
        if not state["in_file"]:
            raise _AssetBrowserUploadError("single_file_required")
        block = data[start:end]
        state["decoded_bytes"] += len(block)
        if state["decoded_bytes"] > max_bytes:
            raise _AssetBrowserUploadTooLarge("file_too_large")
        state["hasher"].update(block)
        if sink is not None:
            sink(block)

    def on_part_end():
        if not state["in_file"]:
            raise _AssetBrowserUploadError("single_file_required")
        state["seen_file"] = True
        state["in_file"] = False

    def on_end():
        state["ended"] = True

    parser = MultipartParser(boundary, {
        "on_part_begin": on_part_begin,
        "on_part_data": on_part_data,
        "on_part_end": on_part_end,
        "on_header_field": on_header_field,
        "on_header_value": on_header_value,
        "on_header_end": on_header_end,
        "on_headers_finished": on_headers_finished,
        "on_end": on_end,
    })
    wire_bytes = 0
    async for block in request.stream():
        wire_bytes += len(block)
        if wire_bytes > max_bytes + wire_overhead:
            raise _AssetBrowserUploadTooLarge("request_too_large")
        parser.write(block)
    parser.finalize()
    if not state["ended"] or not state["seen_file"] or state["file_count"] != 1:
        raise _AssetBrowserUploadError("invalid_multipart")
    return {
        "decoded_bytes": state["decoded_bytes"],
        "sha256": state["hasher"].hexdigest(),
    }


def _asset_browser_security_headers() -> dict:
    return {
        "Cache-Control": "no-store",
        "Pragma": "no-cache",
        "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'",
        "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "DENY",
        "Referrer-Policy": "no-referrer",
    }


def _asset_browser_upload_page(token: str, item: dict, now: float | None = None) -> str:
    current = time.time() if now is None else now
    filename = html.escape(item["filename"] or "Any filename")
    action = html.escape(f"/rm/upload/{token}", quote=True)
    expires_in = max(0, int(item["expires_at"] - current))
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Remember-Me upload probe</title><style>body{{font:16px system-ui;max-width:42rem;margin:3rem auto;padding:0 1rem}}label,input,button{{display:block;margin:.8rem 0}}code{{word-break:break-all}}</style></head>
<body><h1>Remember-Me upload probe</h1><p>Expected file: <code>{filename}</code></p><p>Allowed size: {item["expected_bytes"]} bytes; hard limit: {ASSET_BROWSER_UPLOAD_MAX_BYTES} bytes.</p><p>Link expires in {expires_in} seconds.</p>
<form method="post" enctype="multipart/form-data" action="{action}"><label for="file">Choose file</label><input id="file" name="file" type="file" required><button type="submit">Upload and verify</button></form></body></html>"""


def _asset_browser_result_page(result: dict) -> str:
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Upload result</title></head>
<body><h1>Upload result</h1><p>decoded_bytes: {result["decoded_bytes"]}</p><p>sha256: <code>{html.escape(result["sha256"])}</code></p><p>size_match: {str(result["size_match"]).lower()}</p><p>hash_match: {str(result["hash_match"]).lower()}</p></body></html>"""

def _rm_asset_public_metadata(asset: dict, deduplicated: bool | None = None) -> dict:
    payload = {
        "asset_id": asset["asset_id"],
        "source_sha256": asset["source_sha256"],
        "stored_sha256": asset["stored_sha256"],
        "decoded_bytes": asset["decoded_bytes"],
        "stored_bytes": asset["stored_bytes"],
        "mime_type": asset["mime_type"],
        "filename": asset["original_filename"],
        "kind": asset["kind"],
        "width": asset["width"],
        "height": asset["height"],
        "created_at": asset["created_at"],
        "title": asset.get("title", ""),
        "description": asset.get("description", ""),
        "tags": asset.get("tags", []),
        "updated_at": asset.get("updated_at", asset["created_at"]),
    }
    if deduplicated is not None:
        payload["deduplicated"] = deduplicated
    return payload


def _rm_retire_asset_upload_locked(upload_id: str) -> None:
    item = _rm_asset_uploads.get(upload_id)
    token = item.get("token", "") if item else ""
    if token:
        _rm_asset_upload_tokens.pop(token, None)
    _rm_asset_uploads.pop(upload_id, None)
    _rm_asset_upload_sources.pop(upload_id, None)


def _rm_asset_upload_source_locked(upload_id: str) -> str:
    source = _rm_asset_upload_sources.get(upload_id, "legacy")
    if source in {"legacy", "remember_me"}:
        return source
    _rm_retire_asset_upload_locked(upload_id)
    return ""


def _rm_store_asset_upload_locked(upload_id: str, token: str, item: dict, source: str) -> bool:
    if source not in {"legacy", "remember_me"}:
        return False
    try:
        _rm_asset_uploads[upload_id] = item
        _rm_asset_upload_tokens[token] = upload_id
        _rm_asset_upload_sources[upload_id] = source
        return True
    except Exception:
        _rm_retire_asset_upload_locked(upload_id)
        _rm_asset_upload_tokens.pop(token, None)
        return False


def _rm_cleanup_asset_uploads(now: float) -> None:
    for upload_id, item in list(_rm_asset_uploads.items()):
        source = _rm_asset_upload_sources.get(upload_id, "legacy")
        if source not in {"legacy", "remember_me"}:
            _rm_retire_asset_upload_locked(upload_id)
            continue
        if item["state"] == "pending" and item["expires_at"] <= now:
            token = item.get("token", "")
            if token:
                _rm_asset_upload_tokens.pop(token, None)
            item["token"] = ""
            item["state"] = "expired"
        if item["retire_at"] <= now:
            _rm_retire_asset_upload_locked(upload_id)


def _rm_host_sanitize_upload_filename(filename: str) -> str:
    cleaned = re.sub(r"[\x00-\x1f\x7f/\\:]+", "_", (filename or "").strip())
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    return cleaned[:255] or "asset.bin"


def _rm_create_upload_temp_path() -> Path:
    fd, name = tempfile.mkstemp(prefix="ombre-rm-upload-", suffix=".tmp")
    os.close(fd)
    return Path(name)


def _rm_delete_upload_temp_path(path: Path | None) -> None:
    if path is None:
        return
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def _rm_create_asset_upload_link(
    expected_bytes: int,
    filename: str = "",
    mime_type: str = "application/octet-stream",
    now: float | None = None,
    *,
    source: str = "legacy",
) -> str:
    if source not in {"legacy", "remember_me"}:
        return _asset_ingest_response(False, error="upload_unavailable")
    if isinstance(expected_bytes, bool) or not isinstance(expected_bytes, int) or not 0 <= expected_bytes <= RM_ASSET_MAX_UPLOAD_BYTES:
        return _asset_ingest_response(False, error="invalid_expected_bytes", max_bytes=RM_ASSET_MAX_UPLOAD_BYTES)
    mime = (mime_type or "application/octet-stream").strip().lower()
    if mime not in {"application/octet-stream", "image/jpeg", "image/png"}:
        return _asset_ingest_response(False, error="unsupported_mime_type")

    current = time.time() if now is None else now
    safe_filename = asset_store.sanitize_filename(filename) if source == "legacy" else _rm_host_sanitize_upload_filename(filename)
    with _rm_asset_upload_lock:
        _rm_cleanup_asset_uploads(current)
        active = sum(1 for item in _rm_asset_uploads.values() if item["state"] in ("pending", "uploading"))
        if active >= RM_ASSET_UPLOAD_MAX_UPLOADS:
            return _asset_ingest_response(False, error="upload_store_full")
        while True:
            upload_id = secrets.token_hex(16)
            if upload_id not in _rm_asset_uploads:
                break
        while True:
            token = secrets.token_urlsafe(32)
            if token not in _rm_asset_upload_tokens:
                break
        expires_at = current + RM_ASSET_UPLOAD_TTL_SECONDS
        item = {
            "state": "pending",
            "token": token,
            "expected_bytes": expected_bytes,
            "filename": safe_filename,
            "mime_type": mime,
            "expires_at": expires_at,
            "retire_at": expires_at + RM_ASSET_UPLOAD_TTL_SECONDS,
            "result": None,
        }
        if not _rm_store_asset_upload_locked(upload_id, token, item, source):
            return _asset_ingest_response(False, error="upload_unavailable")

    upload_path = f"/rm/asset-upload/{token}"
    base_url = _asset_public_base_url()
    return _json_lib.dumps({
        "ok": True,
        "upload_id": upload_id,
        "upload_path": upload_path,
        "upload_url": f"{base_url}{upload_path}" if base_url else "",
        "status_path": f"/rm/asset-upload-status/{upload_id}",
        "expires_in_seconds": RM_ASSET_UPLOAD_TTL_SECONDS,
        "max_bytes": RM_ASSET_MAX_UPLOAD_BYTES,
    }, ensure_ascii=False, sort_keys=True)


def _rm_asset_upload_status_payload(upload_id: str, now: float | None = None, *, expected_source: str = "legacy") -> str:
    upload_id = (upload_id or "").strip()
    if not re.fullmatch(r"[0-9a-f]{32}", upload_id):
        return _asset_ingest_response(False, error="invalid_upload_id")
    if expected_source not in {"legacy", "remember_me"}:
        return _asset_ingest_response(False, upload_id=upload_id, error="upload_unavailable")
    current = time.time() if now is None else now
    with _rm_asset_upload_lock:
        _rm_cleanup_asset_uploads(current)
        item = _rm_asset_uploads.get(upload_id)
        if not item:
            return _asset_ingest_response(False, upload_id=upload_id, error="upload_unavailable")
        source = _rm_asset_upload_source_locked(upload_id)
        if not source or source != expected_source:
            return _asset_ingest_response(False, upload_id=upload_id, error="upload_unavailable")
        state = "pending" if item["state"] == "uploading" else item["state"]
        result = dict(item["result"] or {})
        payload = {
            "ok": True,
            "state": state,
            "asset_id": result.get("asset_id", ""),
            "source_sha256": result.get("source_sha256", ""),
            "stored_sha256": result.get("stored_sha256", ""),
            "decoded_bytes": result.get("decoded_bytes", 0),
            "stored_bytes": result.get("stored_bytes", 0),
            "mime_type": result.get("mime_type", item["mime_type"]),
            "filename": result.get("filename", item["filename"]),
            "kind": result.get("kind", ""),
            "width": result.get("width", 0),
            "height": result.get("height", 0),
            "deduplicated": result.get("deduplicated", False),
        }
    return _json_lib.dumps(payload, ensure_ascii=False, sort_keys=True)


def _rm_get_asset_upload(token: str, now: float | None = None) -> dict | None:
    if not re.fullmatch(r"[A-Za-z0-9_-]{40,128}", token or ""):
        return None
    current = time.time() if now is None else now
    with _rm_asset_upload_lock:
        _rm_cleanup_asset_uploads(current)
        upload_id = _rm_asset_upload_tokens.get(token)
        item = _rm_asset_uploads.get(upload_id or "")
        if not item or item["state"] != "pending":
            return None
        source = _rm_asset_upload_source_locked(upload_id)
        if not source:
            return None
        return {
            "upload_id": upload_id,
            "expected_bytes": item["expected_bytes"],
            "filename": item["filename"],
            "expires_at": item["expires_at"],
            "source": source,
        }


def _rm_claim_asset_upload(token: str, now: float | None = None) -> dict | None:
    if not re.fullmatch(r"[A-Za-z0-9_-]{40,128}", token or ""):
        return None
    current = time.time() if now is None else now
    with _rm_asset_upload_lock:
        _rm_cleanup_asset_uploads(current)
        upload_id = _rm_asset_upload_tokens.pop(token, None)
        item = _rm_asset_uploads.get(upload_id or "")
        if not item or item["state"] != "pending":
            return None
        source = _rm_asset_upload_source_locked(upload_id)
        if not source:
            return None
        item["state"] = "uploading"
        return {
            "upload_id": upload_id,
            "expected_bytes": item["expected_bytes"],
            "filename": item["filename"],
            "mime_type": item["mime_type"],
            "source": source,
        }


def _rm_release_asset_upload(upload_id: str, now: float | None = None) -> None:
    current = time.time() if now is None else now
    with _rm_asset_upload_lock:
        _rm_cleanup_asset_uploads(current)
        item = _rm_asset_uploads.get(upload_id)
        if not item or item["state"] != "uploading":
            return
        source = _rm_asset_upload_source_locked(upload_id)
        if not source:
            return
        if item["expires_at"] <= current:
            item["state"] = "expired"
            item["token"] = ""
            return
        item["state"] = "pending"
        _rm_asset_upload_tokens[item["token"]] = upload_id


def _rm_normalize_remember_me_upload_result(result, expected_bytes: int, source_sha256: str) -> dict:
    from collections.abc import Mapping

    def require_str(value) -> str:
        if not isinstance(value, str):
            raise ValueError("invalid_upload_result")
        return value

    def require_hex(value, length: int) -> str:
        text = require_str(value)
        if not re.fullmatch(rf"[0-9a-f]{{{length}}}", text):
            raise ValueError("invalid_upload_result")
        return text

    def require_int(value, *, positive: bool) -> int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("invalid_upload_result")
        if positive and value <= 0:
            raise ValueError("invalid_upload_result")
        if not positive and value < 0:
            raise ValueError("invalid_upload_result")
        return value

    if not isinstance(result, Mapping):
        raise ValueError("invalid_upload_result")
    asset_id = require_hex(result["asset_id"], 32)
    result_source_sha256 = require_hex(result["source_sha256"], 64)
    stored_sha256 = require_hex(result["stored_sha256"], 64)
    if not hmac.compare_digest(result_source_sha256, source_sha256):
        raise ValueError("invalid_upload_result")
    decoded_bytes = require_int(result["decoded_bytes"], positive=False)
    if decoded_bytes != expected_bytes:
        raise ValueError("invalid_upload_result")
    stored_bytes = require_int(result["stored_bytes"], positive=False)
    filename = require_str(result["filename"])
    mime_type = require_str(result["mime_type"])
    if mime_type not in {"image/png", "image/jpeg"}:
        raise ValueError("invalid_upload_result")
    kind = require_str(result["kind"])
    if kind != "image":
        raise ValueError("invalid_upload_result")
    width = require_int(result["width"], positive=True)
    height = require_int(result["height"], positive=True)
    require_str(result["created_at"])
    require_str(result["updated_at"])
    require_str(result["title"])
    require_str(result["description"])
    tags = result["tags"]
    if not isinstance(tags, (list, tuple)) or any(not isinstance(tag, str) for tag in tags):
        raise ValueError("invalid_upload_result")
    deduplicated = result["deduplicated"]
    if not isinstance(deduplicated, bool):
        raise ValueError("invalid_upload_result")
    return {
        "asset_id": asset_id,
        "source_sha256": result_source_sha256,
        "stored_sha256": stored_sha256,
        "decoded_bytes": decoded_bytes,
        "stored_bytes": stored_bytes,
        "mime_type": mime_type,
        "filename": filename,
        "kind": kind,
        "width": width,
        "height": height,
        "deduplicated": deduplicated,
    }


def _rm_complete_asset_upload(upload_id: str, asset: dict, source_sha256: str, *, expected_source: str = "legacy") -> dict | None:
    if expected_source not in {"legacy", "remember_me"}:
        return None
    with _rm_asset_upload_lock:
        item = _rm_asset_uploads.get(upload_id)
        if not item or item["state"] != "uploading":
            return None
        source = _rm_asset_upload_source_locked(upload_id)
        if source != expected_source:
            return None
        result = dict(asset)
        if not hmac.compare_digest(str(result.get("source_sha256", "")), source_sha256):
            return None
        item["state"] = "completed"
        item["token"] = ""
        item["result"] = result
        item["retire_at"] = time.time() + RM_ASSET_UPLOAD_TTL_SECONDS
        return dict(result)


def _rm_asset_upload_page(token: str, item: dict, now: float | None = None) -> str:
    current = time.time() if now is None else now
    filename = html.escape(item["filename"])
    action = html.escape(f"/rm/asset-upload/{token}", quote=True)
    expires_in = max(0, int(item["expires_at"] - current))
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Remember-Me asset upload</title><style>body{{font:16px system-ui;max-width:42rem;margin:3rem auto;padding:0 1rem}}label,input,button{{display:block;margin:.8rem 0}}code{{word-break:break-all}}</style></head>
<body><h1>Remember-Me asset upload</h1><p>Expected file: <code>{filename}</code></p><p>Expected size: {item["expected_bytes"]} bytes; hard limit: {RM_ASSET_MAX_UPLOAD_BYTES} bytes.</p><p>Link expires in {expires_in} seconds.</p>
<form method="post" enctype="multipart/form-data" action="{action}"><label for="file">Choose file</label><input id="file" name="file" type="file" required><button type="submit">Upload and store</button></form></body></html>"""


def _rm_asset_result_page(result: dict) -> str:
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Asset stored</title></head>
<body><h1>Asset stored</h1><p>asset_id: <code>{html.escape(result["asset_id"])}</code></p><p>stored_sha256: <code>{html.escape(result["stored_sha256"])}</code></p><p>stored_bytes: {result["stored_bytes"]}</p><p>deduplicated: {str(result["deduplicated"]).lower()}</p></body></html>"""


def _rm_retire_asset_download_locked(token: str) -> None:
    _rm_asset_download_tokens.pop(token, None)
    _rm_asset_download_sources.pop(token, None)


def _rm_cleanup_asset_downloads(now: float) -> None:
    expired = [
        token
        for token, item in _rm_asset_download_tokens.items()
        if item["expires_at"] <= now
    ]
    for token in expired:
        _rm_retire_asset_download_locked(token)


def _rm_safe_download_filename(asset: dict) -> str:
    name = re.sub(r"[^A-Za-z0-9._ -]+", "_", asset.get("original_filename", "")).strip(" .")
    extension = Path(asset["stored_relpath"]).suffix
    if not name:
        name = f"remember-me-{asset['asset_id']}{extension}"
    elif extension and not name.lower().endswith(extension.lower()):
        name += extension
    return name[:180]


def _rm_store_asset_download_ticket_locked(
    token: str,
    asset_id: str,
    expires_at: float,
    source: str,
) -> bool:
    try:
        _rm_asset_download_tokens[token] = {
            "asset_id": asset_id,
            "expires_at": expires_at,
            "get_count": 0,
        }
        _rm_asset_download_sources[token] = source
        return True
    except Exception:
        _rm_retire_asset_download_locked(token)
        return False


def _rm_create_asset_download_link(asset_id: str, now: float | None = None) -> str:
    resolved = asset_store.resolve_file((asset_id or "").strip())
    if not resolved:
        return _asset_ingest_response(False, error="asset_unavailable")
    asset, _ = resolved
    current = time.time() if now is None else now
    with _rm_asset_download_lock:
        _rm_cleanup_asset_downloads(current)
        if len(_rm_asset_download_tokens) >= RM_ASSET_DOWNLOAD_MAX_TOKENS:
            return _asset_ingest_response(False, error="download_store_full")
        while True:
            token = secrets.token_urlsafe(32)
            if token not in _rm_asset_download_tokens:
                break
        if not _rm_store_asset_download_ticket_locked(
            token,
            asset["asset_id"],
            current + RM_ASSET_DOWNLOAD_TTL_SECONDS,
            "legacy",
        ):
            return _asset_ingest_response(False, error="download_unavailable")
    download_path = f"/rm/asset-download/{token}"
    base_url = _asset_public_base_url()
    return _json_lib.dumps({
        "ok": True,
        "asset_id": asset["asset_id"],
        "filename": _rm_safe_download_filename(asset),
        "mime_type": asset["mime_type"],
        "stored_bytes": asset["stored_bytes"],
        "stored_sha256": asset["stored_sha256"],
        "download_path": download_path,
        "download_url": f"{base_url}{download_path}" if base_url else "",
        "expires_in_seconds": RM_ASSET_DOWNLOAD_TTL_SECONDS,
    }, ensure_ascii=False, sort_keys=True)


def _rm_asset_download_headers(asset: dict, filename: str) -> dict:
    return {
        "Content-Type": asset["mime_type"],
        "Content-Disposition": f'attachment; filename="{filename}"',
        "Cache-Control": "no-store",
        "Pragma": "no-cache",
        "X-Content-Type-Options": "nosniff",
        "Content-Length": str(asset["stored_bytes"]),
    }


def _rm_resolve_asset_download_body(asset_id: str, source: str) -> tuple[dict, Path | bytes, str] | None:
    if source == "legacy":
        resolved = asset_store.resolve_file(asset_id)
        if not resolved:
            return None
        asset, path = resolved
        return asset, path, _rm_safe_download_filename(asset)
    if source == "remember_me":
        bundle = _get_remember_me_host_bundle()
        if bundle is None:
            return None
        metadata, content = bundle.core_adapter.resolve_ob_download(asset_id)
        from remember_me_download_links import safe_download_filename

        return metadata, content, safe_download_filename(metadata)
    return None


def _rm_read_asset_download(token: str, method: str, now: float | None = None) -> tuple[dict, Path | bytes, dict, str] | None:
    if not re.fullmatch(r"[A-Za-z0-9_-]{40,128}", token or ""):
        return None
    current = time.time() if now is None else now
    with _rm_asset_download_lock:
        _rm_cleanup_asset_downloads(current)
        item = _rm_asset_download_tokens.get(token)
        if not item:
            return None
        source = _rm_asset_download_sources.get(token, "legacy")
        if source not in {"legacy", "remember_me"}:
            _rm_retire_asset_download_locked(token)
            return None
        asset_id = item["asset_id"]

    try:
        resolved = _rm_resolve_asset_download_body(asset_id, source)
    except Exception:
        resolved = None
    if resolved is None:
        with _rm_asset_download_lock:
            item = _rm_asset_download_tokens.get(token)
            if item and item.get("asset_id") == asset_id:
                _rm_retire_asset_download_locked(token)
        return None

    asset, body, filename = resolved
    if isinstance(body, Path):
        body_source = "legacy"
    elif isinstance(body, bytes):
        body_source = "remember_me"
    else:
        with _rm_asset_download_lock:
            _rm_retire_asset_download_locked(token)
        return None
    if body_source != source:
        with _rm_asset_download_lock:
            _rm_retire_asset_download_locked(token)
        return None
    headers = _rm_asset_download_headers(asset, filename)

    final = time.time() if now is None else now
    with _rm_asset_download_lock:
        _rm_cleanup_asset_downloads(final)
        item = _rm_asset_download_tokens.get(token)
        if not item or item.get("asset_id") != asset_id:
            return None
        if _rm_asset_download_sources.get(token, "legacy") != source:
            return None
        if method.upper() == "GET":
            if item["get_count"] >= RM_ASSET_DOWNLOAD_MAX_GETS:
                return None
            item["get_count"] += 1
        return asset, body, headers, source

def _rm_asset_view_error(error: str) -> CallToolResult:
    messages = {
        "asset_unavailable": "The requested Remember-Me asset is unavailable.",
        "asset_not_image": "The requested Remember-Me asset is not an image.",
        "invalid_image_mime": "The requested Remember-Me image type is not supported.",
        "image_too_large": "The requested Remember-Me image exceeds the viewer limit.",
        "image_unavailable": "The requested Remember-Me image could not be verified.",
        "download_unavailable": "A temporary fallback download link could not be created.",
    }
    return CallToolResult(
        content=[
            TextContent(
                type="text",
                text=messages.get(error, "The Remember-Me image could not be displayed."),
            )
        ],
        structuredContent={"ok": False, "error": error},
        isError=True,
    )


def _rm_asset_inspect_error(error: str) -> CallToolResult:
    messages = {
        "asset_unavailable": "The requested Remember-Me asset is unavailable.",
        "asset_not_image": "The requested Remember-Me asset is not an image.",
        "invalid_image_mime": "The requested Remember-Me image type is not supported for inspection.",
        "image_too_large": "The requested Remember-Me image exceeds the inspection limit.",
        "image_unavailable": "The requested Remember-Me image could not be verified.",
    }
    return CallToolResult(
        content=[
            TextContent(
                type="text",
                text=messages.get(error, "The Remember-Me image could not be inspected."),
            )
        ],
        structuredContent={"ok": False, "error": error},
        isError=True,
    )

def _rm_verified_view_image(asset_id: str) -> tuple[dict, bytes] | str:
    asset_id = (asset_id or "").strip()
    if not re.fullmatch(r"[0-9a-f]{32}", asset_id):
        return "asset_unavailable"
    try:
        resolved = asset_store.resolve_file(asset_id)
    except (AssetStoreError, OSError):
        return "image_unavailable"
    if not resolved:
        return "asset_unavailable"
    asset, path = resolved
    if asset.get("kind") != "image":
        return "asset_not_image"
    if asset.get("mime_type") not in {"image/jpeg", "image/png"}:
        return "invalid_image_mime"
    try:
        actual_bytes = path.stat().st_size
    except OSError:
        return "image_unavailable"
    if actual_bytes <= 0 or actual_bytes != asset.get("stored_bytes"):
        return "image_unavailable"
    if actual_bytes > RM_ASSET_MAX_UPLOAD_BYTES:
        return "image_too_large"
    try:
        data = path.read_bytes()
        with Image.open(io.BytesIO(data)) as image:
            image_format = image.format
            image_size = image.size
            image.verify()
    except (OSError, ValueError, UnidentifiedImageError):
        return "image_unavailable"
    expected_format = "JPEG" if asset["mime_type"] == "image/jpeg" else "PNG"
    if image_format != expected_format or image_size != (asset["width"], asset["height"]):
        return "image_unavailable"
    return asset, data


def _asset_png_chunk(chunk_type: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + chunk_type + data + struct.pack(">I", zlib.crc32(chunk_type + data) & 0xFFFFFFFF)


def _asset_encode_rgb_png(width: int, height: int, rgb: bytes) -> bytes:
    if len(rgb) != width * height * 3:
        raise ValueError("rgb_size_mismatch")
    rows = bytearray()
    stride = width * 3
    for y in range(height):
        rows.append(0)
        start = y * stride
        rows.extend(rgb[start:start + stride])
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + _asset_png_chunk(b"IHDR", ihdr) + _asset_png_chunk(b"IDAT", zlib.compress(bytes(rows))) + _asset_png_chunk(b"IEND", b"")


def _asset_symbol_center(position: str) -> tuple[int, int]:
    centers = {
        "top_left": (64, 64),
        "top_right": (192, 64),
        "bottom_left": (64, 192),
        "bottom_right": (192, 192),
    }
    return centers[position]


def _asset_draw_symbol(rgb: bytearray, symbol: str, position: str) -> None:
    width = ASSET_VISION_WIDTH
    cx, cy = _asset_symbol_center(position)
    black = b"\x00\x00\x00"
    for y in range(cy - 34, cy + 35):
        if y < 0 or y >= ASSET_VISION_HEIGHT:
            continue
        for x in range(cx - 34, cx + 35):
            if x < 0 or x >= width:
                continue
            dx = x - cx
            dy = y - cy
            if symbol == "circle":
                inside = dx * dx + dy * dy <= 28 * 28
            elif symbol == "square":
                inside = abs(dx) <= 26 and abs(dy) <= 26
            elif symbol == "triangle":
                top = cy - 30
                bottom = cy + 28
                inside = top <= y <= bottom and abs(dx) <= int((y - top) * 30 / max(1, bottom - top))
            else:
                inside = False
            if inside:
                offset = (y * width + x) * 3
                rgb[offset:offset + 3] = black


def _asset_generate_vision_png(answer: dict) -> bytes:
    width = ASSET_VISION_WIDTH
    height = ASSET_VISION_HEIGHT
    rgb = bytearray(width * height * 3)
    for y in range(height):
        vertical = "top" if y < height // 2 else "bottom"
        for x in range(width):
            horizontal = "left" if x < width // 2 else "right"
            position = f"{vertical}_{horizontal}"
            color = ASSET_VISION_COLORS[answer[position]]
            offset = (y * width + x) * 3
            rgb[offset:offset + 3] = bytes(color)
    _asset_draw_symbol(rgb, answer["symbol"], answer["symbol_position"])
    return _asset_encode_rgb_png(width, height, bytes(rgb))


def _asset_new_vision_trial(now: float | None = None) -> dict:
    colors = _ASSET_VISION_RNG.sample(tuple(ASSET_VISION_COLORS), 4)
    answer = dict(zip(ASSET_VISION_POSITIONS, colors))
    answer["symbol"] = _ASSET_VISION_RNG.choice(ASSET_VISION_SYMBOLS)
    answer["symbol_position"] = _ASSET_VISION_RNG.choice(ASSET_VISION_POSITIONS)
    trial_id = secrets.token_hex(16)
    png = _asset_generate_vision_png(answer)
    created_at = time.time() if now is None else now
    return {
        "trial_id": trial_id,
        "answer": answer,
        "png": png,
        "sha256": hashlib.sha256(png).hexdigest(),
        "expires_at": created_at + ASSET_VISION_TTL_SECONDS,
    }


def _asset_cleanup_expired_trials(now: float) -> None:
    expired = [trial_id for trial_id, trial in _asset_vision_trials.items() if trial["expires_at"] <= now]
    for trial_id in expired:
        trial = _asset_vision_trials.pop(trial_id, None)
        token = trial.get("download_token") if trial else ""
        if token:
            _asset_vision_download_tokens.pop(token, None)


def _asset_cleanup_expired_vision_downloads(now: float) -> None:
    expired = [token for token, item in _asset_vision_download_tokens.items() if item["expires_at"] <= now]
    for token in expired:
        item = _asset_vision_download_tokens.pop(token, None)
        trial = _asset_vision_trials.get(item.get("trial_id", "")) if item else None
        if trial and trial.get("download_token") == token:
            trial["download_token"] = ""


def _asset_store_vision_trial(trial: dict, now: float | None = None) -> tuple[bool, str]:
    current = time.time() if now is None else now
    with _asset_vision_lock:
        _asset_cleanup_expired_trials(current)
        if len(_asset_vision_trials) >= ASSET_VISION_MAX_TRIALS:
            return False, "trial_store_full"
        png = bytes(trial["png"])
        _asset_vision_trials[trial["trial_id"]] = {
            "answer": dict(trial["answer"]),
            "expires_at": trial["expires_at"],
            "png": png,
            "sha256": hashlib.sha256(png).hexdigest(),
            "exported": False,
            "download_token": "",
        }
    return True, ""


def _asset_vision_prompt(trial_id: str, decoded_bytes: int, sha256: str) -> str:
    return _json_lib.dumps({
        "trial_id": trial_id,
        "decoded_bytes": decoded_bytes,
        "sha256": sha256,
        "answer_format": {
            "top_left": "<color>",
            "top_right": "<color>",
            "bottom_left": "<color>",
            "bottom_right": "<color>",
            "symbol": "<symbol>",
            "symbol_position": "<position>",
        },
        "allowed_colors": list(ASSET_VISION_COLORS),
        "allowed_symbols": list(ASSET_VISION_SYMBOLS),
        "allowed_symbol_positions": list(ASSET_VISION_POSITIONS),
        "submit_to": "asset_vision_verify",
    }, ensure_ascii=False, sort_keys=True)


def _asset_vision_upload_payload(trial_id: str, decoded_bytes: int, sha256: str) -> str:
    return _json_lib.dumps({
        "ok": True,
        "trial_id": trial_id,
        "decoded_bytes": decoded_bytes,
        "sha256": sha256,
        "answer_format": {
            "top_left": "<color>",
            "top_right": "<color>",
            "bottom_left": "<color>",
            "bottom_right": "<color>",
            "symbol": "<symbol>",
            "symbol_position": "<position>",
        },
        "allowed_colors": list(ASSET_VISION_COLORS),
        "allowed_symbols": list(ASSET_VISION_SYMBOLS),
        "allowed_symbol_positions": list(ASSET_VISION_POSITIONS),
    }, ensure_ascii=False, sort_keys=True)


def _asset_reject_vision_answer(error: str, trial_id: str = "") -> str:
    return _json_lib.dumps({
        "ok": False,
        "trial_id": trial_id,
        "error": error,
    }, ensure_ascii=False, sort_keys=True)


def _asset_export_vision_trial(trial_id: str, now: float | None = None) -> str:
    trial_id = (trial_id or "").strip()
    if not re.fullmatch(r"[0-9a-f]{32}", trial_id):
        return _asset_reject_vision_answer("invalid_trial_id")
    current = time.time() if now is None else now
    with _asset_vision_lock:
        _asset_cleanup_expired_trials(current)
        trial = _asset_vision_trials.get(trial_id)
        if not trial:
            return _asset_reject_vision_answer("trial_unavailable", trial_id)
        if trial.get("exported"):
            return _asset_reject_vision_answer("already_exported", trial_id)
        png = bytes(trial["png"])
        sha256 = str(trial["sha256"])
        trial["exported"] = True
    return _json_lib.dumps({
        "ok": True,
        "trial_id": trial_id,
        "filename": f"remember-me-vision-{trial_id}.png",
        "mime_type": "image/png",
        "decoded_bytes": len(png),
        "sha256": sha256,
        "data_base64": base64.b64encode(png).decode("ascii"),
    }, ensure_ascii=False, sort_keys=True)


def _asset_vision_filename(trial_id: str) -> str:
    if not re.fullmatch(r"[0-9a-f]{32}", trial_id or ""):
        raise ValueError("invalid_trial_id")
    return f"remember-me-vision-{trial_id}.png"


def _asset_public_base_url() -> str:
    raw = os.environ.get("OMBRE_PUBLIC_BASE_URL", "").strip()
    if not raw:
        return ""
    parsed = urlparse(raw)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return ""
    return raw.rstrip("/")


def _bootstrap_remember_me_host(store=None, embedding=None):
    if not _env_flag_enabled(os.environ.get("OMBRE_RM_RUNTIME_ENABLED", "")):
        logger.info("remember-me runtime disabled")
        return None

    try:
        raw_data_root = os.environ.get("OMBRE_RM_DATA_ROOT")
        if (
            raw_data_root is None
            or not raw_data_root.strip()
            or "\x00" in raw_data_root
        ):
            raise RuntimeError("remember_me_host_bootstrap_failed")
        data_root = Path(raw_data_root.strip())
        if not data_root.is_absolute():
            raise RuntimeError("remember_me_host_bootstrap_failed")
        data_root = data_root.expanduser().resolve()
        legacy_root = (
            store.data_root
            if store is not None
            else Path(config["buckets_dir"]).expanduser().resolve()
        )
        if data_root == legacy_root:
            raise RuntimeError("remember_me_host_bootstrap_failed")

        from remember_me_host_runtime import create_remember_me_host_bundle
        from remember_me_vector_provider import RememberMeVectorProviderAdapter

        vector_provider = RememberMeVectorProviderAdapter(
            embedding if embedding is not None else EmbeddingEngine(config)
        )
        bundle = create_remember_me_host_bundle(
            data_root=data_root,
            token_store=_rm_asset_download_tokens,
            ticket_source_store=_rm_asset_download_sources,
            download_lock=_rm_asset_download_lock,
            public_base_url=_asset_public_base_url,
            ttl_seconds=RM_ASSET_DOWNLOAD_TTL_SECONDS,
            max_tokens=RM_ASSET_DOWNLOAD_MAX_TOKENS,
            vector_provider=vector_provider,
        )
    except Exception:
        logger.error("remember-me runtime bootstrap failed")
        raise RuntimeError("remember_me_host_bootstrap_failed") from None

    logger.info("remember-me runtime enabled")
    return bundle


def _rm_runtime_evidence_headers() -> dict[str, str]:
    return {
        "Cache-Control": "no-store",
        "Pragma": "no-cache",
        "X-Content-Type-Options": "nosniff",
    }


def _rm_process_platform_identity() -> dict[str, str | None]:
    """Expose actual Render identity inputs; expected values stay probe-only.

    The external acceptance process must obtain its expected values from
    independent Render control-plane/log evidence.  This server-side helper
    never reads ``RM_PROBE_TRUSTED_*`` and never turns a local fallback into a
    production identity.
    """

    names = {
        "instance_id": "RENDER_INSTANCE_ID",
        "git_commit": "RENDER_GIT_COMMIT",
        "service_id": "RENDER_SERVICE_ID",
    }
    identity: dict[str, str | None] = {}
    for field, name in names.items():
        value = os.environ.get(name, "").strip()
        identity[field] = (
            value if value and re.fullmatch(r"[A-Za-z0-9_.:/-]{1,160}", value) else None
        )
    return identity


def _require_rm_runtime_evidence_auth(request):
    from starlette.responses import JSONResponse

    headers = _rm_runtime_evidence_headers()
    expected = _mcp_auth_token()
    if not expected:
        return JSONResponse(
            {"status": "unavailable", "error": "runtime_evidence_unavailable"},
            status_code=503,
            headers=headers,
        )
    authorization = request.headers.get("authorization", "")
    scheme, separator, candidate = authorization.partition(" ")
    if scheme.casefold() != "bearer" or not separator or not candidate.strip():
        return JSONResponse({"status": "unauthorized"}, status_code=401, headers=headers)
    if not _constant_time_token_match(candidate.strip(), expected):
        return JSONResponse({"status": "unauthorized"}, status_code=401, headers=headers)
    return None


async def _rm_runtime_evidence(request):
    from starlette.responses import JSONResponse

    headers = _rm_runtime_evidence_headers()
    auth_error = _require_rm_runtime_evidence_auth(request)
    if auth_error is not None:
        return auth_error
    try:
        registry = asset_backend_registry
        validation = registry._validate_boot()
        selected = registry.selected_backend()
        snapshot = registry.snapshot
        if snapshot is None:
            raise AssetBackendError("asset_authority_unavailable")
        return JSONResponse(
            {
                "status": "ok",
                "authority": validation.authority.value,
                "durable_authority": snapshot.authority.value,
                "selected_backend": selected.name,
                "cutover_state": snapshot.state.value,
                "freeze_status": snapshot.freeze_status,
                "boot_mode": validation.boot_mode,
                "writes_allowed": validation.writes_allowed,
                "frozen": validation.frozen,
                "recovery_required": validation.recovery_required,
                "legacy_fallback_allowed": validation.legacy_fallback_allowed,
                "rm_available": snapshot.rm_available,
                "process_boot_id": _RM_PROCESS_BOOT_ID,
                "process_started_at": _RM_PROCESS_STARTED_AT,
                "platform_identity": _rm_process_platform_identity(),
                # This means only that the current registry's _validate_boot()
                # completed successfully.  It is not restart provenance.
                "runtime_boot_validation_passed": True,
            },
            headers=headers,
        )
    except Exception:
        return JSONResponse(
            {"status": "unavailable", "error": "runtime_evidence_unavailable"},
            status_code=503,
            headers=headers,
        )


# Register this operator-only read surface without adding a public MCP tool or
# changing the decorated route inventory used by the compatibility contract.
mcp.custom_route("/__operator/rm-runtime-evidence", methods=["GET"])(
    _rm_runtime_evidence
)


def _rm_probe_upload_ticket() -> dict[str, object]:
    """Exercise the host upload ticket lifecycle without invoking HTTP/Core."""

    upload_id = ""
    token = ""
    result: dict[str, object] = {"status": "FAIL", "error": "upload_ticket_probe_failed"}
    try:
        raw = _rm_create_asset_upload_link(
            0,
            "__rm_acceptance_probe__.bin",
            "application/octet-stream",
            source="remember_me",
        )
        payload = _json_lib.loads(raw)
        if not isinstance(payload, dict) or payload.get("ok") is not True:
            result = {"status": "INCOMPLETE", "error": "upload_ticket_unavailable"}
        else:
            upload_id = payload.get("upload_id", "")
            upload_path = payload.get("upload_path", "")
            if not isinstance(upload_id, str) or not re.fullmatch(r"[0-9a-f]{32}", upload_id):
                result = {"status": "FAIL", "error": "upload_ticket_invalid"}
            elif not isinstance(upload_path, str):
                result = {"status": "FAIL", "error": "upload_ticket_invalid"}
            else:
                token = upload_path.rsplit("/", 1)[-1]
                if not re.fullmatch(r"[A-Za-z0-9_-]{40,128}", token):
                    result = {"status": "FAIL", "error": "upload_ticket_invalid"}
                else:
                    pending = _rm_get_asset_upload(token)
                    if not pending or pending.get("upload_id") != upload_id or pending.get("source") != "remember_me":
                        result = {"status": "FAIL", "error": "upload_ticket_pending_missing"}
                    else:
                        claimed = _rm_claim_asset_upload(token)
                        if not claimed or claimed.get("upload_id") != upload_id or claimed.get("source") != "remember_me":
                            result = {"status": "FAIL", "error": "upload_ticket_claim_failed"}
                        else:
                            _rm_release_asset_upload(upload_id)
                            restored = _rm_get_asset_upload(token)
                            result = (
                                {"status": "PASS", "error": None}
                                if restored and restored.get("upload_id") == upload_id
                                else {"status": "FAIL", "error": "upload_ticket_release_failed"}
                            )
    except Exception:
        result = {"status": "FAIL", "error": "upload_ticket_probe_failed"}
    finally:
        if upload_id:
            try:
                with _rm_asset_upload_lock:
                    _rm_retire_asset_upload_locked(upload_id)
            except Exception:
                result = {"status": "FAIL", "error": "upload_ticket_cleanup_failed"}
    return result


def _rm_probe_download_ticket() -> dict[str, object]:
    """Exercise only the in-memory download ticket store with a sentinel."""

    token = ""
    sentinel = "__rm_acceptance_probe_sentinel__"
    try:
        current = time.time()
        with _rm_asset_download_lock:
            _rm_cleanup_asset_downloads(current)
            if len(_rm_asset_download_tokens) >= RM_ASSET_DOWNLOAD_MAX_TOKENS:
                return {"status": "INCOMPLETE", "error": "download_ticket_store_full"}
            token = secrets.token_urlsafe(32)
            if not _rm_store_asset_download_ticket_locked(
                token,
                sentinel,
                current + RM_ASSET_DOWNLOAD_TTL_SECONDS,
                "remember_me",
            ):
                return {"status": "FAIL", "error": "download_ticket_store_failed"}
            present = (
                token in _rm_asset_download_tokens
                and _rm_asset_download_sources.get(token) == "remember_me"
            )
            _rm_retire_asset_download_locked(token)
            removed = (
                token not in _rm_asset_download_tokens
                and token not in _rm_asset_download_sources
            )
        return {"status": "PASS" if present and removed else "FAIL", "error": None if present and removed else "download_ticket_cleanup_failed"}
    except Exception:
        return {"status": "FAIL", "error": "download_ticket_probe_failed"}
    finally:
        if token:
            try:
                with _rm_asset_download_lock:
                    _rm_retire_asset_download_locked(token)
            except Exception:
                pass


def _rm_probe_verification_session() -> dict[str, object]:
    """Run the real RM zero-item verification lifecycle without exposing IDs."""

    def remove_exact_closed_session(expected_service, expected_id, expected_session) -> bool:
        sessions = getattr(expected_service, "_verification_sessions", None)
        sessions_lock = getattr(expected_service, "_verification_sessions_lock", None)
        if not isinstance(sessions, dict) or sessions_lock is None:
            return False
        with sessions_lock:
            current = sessions.get(expected_id)
            if current is not expected_session:
                return False
            session_lock = getattr(current, "lock", None)
            if session_lock is None:
                return False
            with session_lock:
                if getattr(current, "closed", False) is not True:
                    return False
            del sessions[expected_id]
            return sessions.get(expected_id) is None

    snapshot_id = ""
    service = None
    session = None
    completed = False
    removed = False
    result: dict[str, object] = {"status": "FAIL", "error": "verification_probe_failed"}
    try:
        bundle = _get_remember_me_host_bundle()
        adapter = getattr(bundle, "core_adapter", None)
        runtime = getattr(adapter, "_runtime", None)
        service = getattr(runtime, "service", None)
        if service is None:
            return {"status": "INCOMPLETE", "error": "verification_service_unavailable"}
        from remember_me.core import (
            BeginAssetVerificationRequest,
            CompleteAssetVerificationRequest,
            ListAssetVerificationPageRequest,
        )

        snapshot = service.begin_asset_verification(
            BeginAssetVerificationRequest(kind="image")
        )
        snapshot_id = getattr(snapshot, "snapshot_id", "")
        total_count = getattr(snapshot, "total_count", None)
        if not isinstance(snapshot_id, str) or not snapshot_id or total_count != 0:
            return {"status": "FAIL", "error": "verification_snapshot_invalid"}
        sessions = getattr(service, "_verification_sessions", None)
        sessions_lock = getattr(service, "_verification_sessions_lock", None)
        if not isinstance(sessions, dict) or sessions_lock is None:
            return {"status": "FAIL", "error": "verification_cleanup_failed"}
        with sessions_lock:
            session = sessions.get(snapshot_id)
        if session is None:
            return {"status": "FAIL", "error": "verification_cleanup_failed"}
        page = service.list_asset_verification_page(
            ListAssetVerificationPageRequest(
                snapshot_id=snapshot_id,
                cursor="",
                limit=500,
            )
        )
        page_ok = (
            getattr(page, "snapshot_id", None) == snapshot_id
            and getattr(page, "records", None) == ()
            and getattr(page, "total_count", None) == 0
            and getattr(page, "has_more", None) is False
            and getattr(page, "next_cursor", None) == ""
        )
        if not page_ok:
            return {"status": "FAIL", "error": "verification_page_invalid"}
        completion = service.complete_asset_verification(
            CompleteAssetVerificationRequest(snapshot_id=snapshot_id)
        )
        completed = True
        complete_ok = (
            getattr(completion, "complete", None) is True
            and getattr(completion, "unchanged", None) is True
            and getattr(completion, "total_count", None) == 0
            and getattr(completion, "scanned_count", None) == 0
            and getattr(completion, "blob_verified_count", None) == 0
        )
        cleanup_ok = (
            session is not None
            and getattr(session, "closed", False) is True
            and remove_exact_closed_session(service, snapshot_id, session)
        )
        removed = cleanup_ok
        result = {
            "status": "PASS" if complete_ok and cleanup_ok else "FAIL",
            "error": None if complete_ok and cleanup_ok else "verification_cleanup_failed",
        }
    except Exception:
        result = {"status": "FAIL", "error": "verification_probe_failed"}
    finally:
        # A failure after begin must not leave an active process-local session.
        # The completion API is the service-owned cleanup seam; IDs never leave
        # this function and only stable status is returned to the caller.
        if snapshot_id and service is not None and not removed:
            if not completed:
                try:
                    from remember_me.core import CompleteAssetVerificationRequest

                    service.complete_asset_verification(
                        CompleteAssetVerificationRequest(snapshot_id=snapshot_id)
                    )
                    completed = True
                except Exception:
                    pass
            if session is None:
                result = {"status": "FAIL", "error": "verification_cleanup_failed"}
            else:
                session_lock = getattr(session, "lock", None)
                if session_lock is None:
                    result = {"status": "FAIL", "error": "verification_cleanup_failed"}
                else:
                    with session_lock:
                        session.closed = True
                    if getattr(session, "closed", False) is not True:
                        result = {"status": "FAIL", "error": "verification_cleanup_failed"}
            if session is not None and not removed:
                removed = remove_exact_closed_session(service, snapshot_id, session)
            if not removed:
                result = {"status": "FAIL", "error": "verification_cleanup_failed"}
    return result


def _rm_ephemeral_runtime_probe() -> dict[str, object]:
    """Run all cutover-relevant process-local lifecycle probes."""

    try:
        validation = asset_backend_registry._validate_boot()
        snapshot = asset_backend_registry.snapshot
        in_frozen_rm = bool(
            snapshot is not None
            and validation.authority.value == "rm"
            and snapshot.authority.value == "rm"
            and snapshot.state.value == "frozen_rm_acceptance"
            and snapshot.freeze_status == "active"
            and validation.frozen is True
            and validation.writes_allowed is False
            and validation.legacy_fallback_allowed is False
            and snapshot.rm_available is True
        )
    except Exception:
        in_frozen_rm = False
    if not in_frozen_rm:
        return {
            "status": "INCOMPLETE",
            "upload_ticket_recreated": False,
            "download_ticket_recreated": False,
            "verification_session_recreated": False,
            "ephemeral_cleanup_complete": True,
            "capability_not_exposed": True,
            "durable_mutation_performed": False,
        }

    upload = _rm_probe_upload_ticket()
    download = _rm_probe_download_ticket()
    verification = _rm_probe_verification_session()
    statuses = (upload["status"], download["status"], verification["status"])
    overall = "FAIL" if "FAIL" in statuses else "INCOMPLETE" if "INCOMPLETE" in statuses else "PASS"
    return {
        "status": overall,
        "upload_ticket_recreated": upload["status"] == "PASS",
        "download_ticket_recreated": download["status"] == "PASS",
        "verification_session_recreated": verification["status"] == "PASS",
        "ephemeral_cleanup_complete": overall == "PASS",
        "capability_not_exposed": True,
        "durable_mutation_performed": False,
    }


async def _rm_ephemeral_runtime_evidence(request):
    from starlette.responses import JSONResponse

    headers = _rm_runtime_evidence_headers()
    auth_error = _require_rm_runtime_evidence_auth(request)
    if auth_error is not None:
        return auth_error
    try:
        result = _rm_ephemeral_runtime_probe()
        status_code = 200 if result["status"] == "PASS" else 409 if result["status"] == "FAIL" else 503
        return JSONResponse(result, status_code=status_code, headers=headers)
    except Exception:
        return JSONResponse(
            {"status": "INCOMPLETE", "error": "ephemeral_probe_unavailable"},
            status_code=503,
            headers=headers,
        )


mcp.custom_route("/__operator/rm-runtime-evidence/ephemeral-probe", methods=["POST"])(
    _rm_ephemeral_runtime_evidence
)

def _asset_vision_download_payload(trial_id: str, png: bytes, sha256: str, token: str, expires_at: float, now: float) -> str:
    download_path = f"/rm/vision-download/{token}"
    base_url = _asset_public_base_url()
    return _json_lib.dumps({
        "ok": True,
        "trial_id": trial_id,
        "filename": _asset_vision_filename(trial_id),
        "mime_type": "image/png",
        "decoded_bytes": len(png),
        "sha256": sha256,
        "download_path": download_path,
        "download_url": f"{base_url}{download_path}" if base_url else "",
        "expires_in_seconds": max(0, int(expires_at - now)),
    }, ensure_ascii=False, sort_keys=True)


def _asset_create_vision_download_link(trial_id: str, now: float | None = None) -> str:
    trial_id = (trial_id or "").strip()
    if not re.fullmatch(r"[0-9a-f]{32}", trial_id):
        return _asset_reject_vision_answer("invalid_trial_id")
    current = time.time() if now is None else now
    with _asset_vision_lock:
        _asset_cleanup_expired_trials(current)
        _asset_cleanup_expired_vision_downloads(current)
        trial = _asset_vision_trials.get(trial_id)
        if not trial:
            return _asset_reject_vision_answer("trial_unavailable", trial_id)

        token = trial.get("download_token") or ""
        token_item = _asset_vision_download_tokens.get(token) if token else None
        if token_item and token_item["expires_at"] > current:
            expires_at = min(token_item["expires_at"], trial["expires_at"])
            return _asset_vision_download_payload(trial_id, bytes(trial["png"]), str(trial["sha256"]), token, expires_at, current)

        if token:
            _asset_vision_download_tokens.pop(token, None)
            trial["download_token"] = ""
        if len(_asset_vision_download_tokens) >= ASSET_VISION_MAX_DOWNLOAD_TOKENS:
            return _asset_reject_vision_answer("download_store_full", trial_id)

        while True:
            token = secrets.token_urlsafe(32)
            if token not in _asset_vision_download_tokens:
                break
        expires_at = current + ASSET_VISION_DOWNLOAD_TTL_SECONDS
        trial["download_token"] = token
        _asset_vision_download_tokens[token] = {
            "trial_id": trial_id,
            "expires_at": expires_at,
            "get_count": 0,
        }
        return _asset_vision_download_payload(trial_id, bytes(trial["png"]), str(trial["sha256"]), token, min(expires_at, trial["expires_at"]), current)


def _asset_read_vision_download(token: str, method: str, now: float | None = None) -> tuple[bytes, dict] | None:
    if not re.fullmatch(r"[A-Za-z0-9_-]{32,256}", token or ""):
        return None
    current = time.time() if now is None else now
    with _asset_vision_lock:
        _asset_cleanup_expired_trials(current)
        _asset_cleanup_expired_vision_downloads(current)
        item = _asset_vision_download_tokens.get(token)
        if not item:
            return None
        trial = _asset_vision_trials.get(item["trial_id"])
        if not trial or trial["expires_at"] <= current:
            _asset_vision_download_tokens.pop(token, None)
            return None
        if method.upper() == "GET":
            if item["get_count"] >= ASSET_VISION_DOWNLOAD_MAX_GETS:
                return None
            item["get_count"] += 1
        png = bytes(trial["png"])
        filename = _asset_vision_filename(item["trial_id"])
    return png, {
        "Content-Type": "image/png",
        "Content-Disposition": f'attachment; filename="{filename}"',
        "Cache-Control": "no-store",
        "Pragma": "no-cache",
        "X-Content-Type-Options": "nosniff",
        "Content-Length": str(len(png)),
    }


def _asset_pop_vision_trial(trial_id: str, now: float | None = None) -> tuple[dict | None, str]:
    current = time.time() if now is None else now
    with _asset_vision_lock:
        _asset_cleanup_expired_trials(current)
        trial = _asset_vision_trials.pop((trial_id or "").strip(), None)
        token = trial.get("download_token") if trial else ""
        if token:
            _asset_vision_download_tokens.pop(token, None)
    if not trial:
        return None, "trial_unavailable"
    if trial["expires_at"] <= current:
        return None, "trial_unavailable"
    return trial, ""


def _asset_score_vision_answer(trial_id: str, answer_json: str, now: float | None = None) -> str:
    trial_id = (trial_id or "").strip()
    trial, error = _asset_pop_vision_trial(trial_id, now=now)
    if error:
        return _asset_reject_vision_answer(error, trial_id)

    try:
        submitted = _json_lib.loads(answer_json)
    except Exception:
        return _asset_reject_vision_answer("invalid_json", trial_id)
    if not isinstance(submitted, dict):
        return _asset_reject_vision_answer("answer_must_be_object", trial_id)

    expected_keys = set(ASSET_VISION_POSITIONS) | {"symbol", "symbol_position"}
    if set(submitted) != expected_keys:
        return _asset_reject_vision_answer("invalid_fields", trial_id)
    if not all(isinstance(submitted[key], str) for key in expected_keys):
        return _asset_reject_vision_answer("invalid_field_type", trial_id)

    allowed_colors = set(ASSET_VISION_COLORS)
    if any(submitted[position] not in allowed_colors for position in ASSET_VISION_POSITIONS):
        return _asset_reject_vision_answer("invalid_enum", trial_id)
    if submitted["symbol"] not in ASSET_VISION_SYMBOLS or submitted["symbol_position"] not in ASSET_VISION_POSITIONS:
        return _asset_reject_vision_answer("invalid_enum", trial_id)

    answer = trial["answer"]
    field_results = {key: submitted[key] == answer[key] for key in ASSET_VISION_POSITIONS}
    field_results["symbol"] = submitted["symbol"] == answer["symbol"]
    field_results["symbol_position"] = submitted["symbol_position"] == answer["symbol_position"]
    score = sum(1 for ok in field_results.values() if ok)
    return _json_lib.dumps({
        "ok": True,
        "trial_id": trial_id,
        "score": score,
        "max_score": 6,
        "all_correct": score == 6,
        "field_results": field_results,
    }, ensure_ascii=False, sort_keys=True)


@diagnostic_tool()
async def asset_attachment_context_probe(
    ctx: Context,
    attachment_reference: str = "",
    attachment_mime_type: str = "",
) -> str:
    """Safely test whether the MCP host exposes the current chat attachment.

    This Stage-4 diagnostic persists and logs nothing. Do not transcribe, OCR,
    redraw, download, or base64-encode an image for this tool. Only provide
    attachment_reference when the client exposes a stable machine-readable
    reference directly. Parameter presence does not prove that it identifies
    the original attachment.
    """
    received_parameter_names = []
    explicit_reference = isinstance(attachment_reference, str) and bool(
        attachment_reference.strip()
    )
    explicit_mime = isinstance(attachment_mime_type, str) and bool(
        attachment_mime_type.strip()
    )
    if explicit_reference:
        received_parameter_names.append("attachment_reference")
    if explicit_mime:
        received_parameter_names.append("attachment_mime_type")

    context_reference, context_bytes, context_mime = (
        _attachment_probe_context_signals(ctx)
    )
    reference_available = context_reference or explicit_reference
    mime_available = context_mime or explicit_mime
    if context_bytes:
        source_kind = "request_context_bytes"
    elif context_reference:
        source_kind = "request_context_reference"
    elif explicit_reference:
        source_kind = "explicit_reference_parameter"
    elif mime_available:
        source_kind = "metadata_only"
    else:
        source_kind = "none"

    return _json_lib.dumps(
        {
            "ok": True,
            "attachment_reference_available": reference_available,
            "attachment_bytes_available": context_bytes,
            "mime_type_available": mime_available,
            "source_kind": source_kind,
            "received_parameter_names": received_parameter_names,
            "original_attachment_identity_verified": False,
        },
        ensure_ascii=False,
        sort_keys=True,
    )

@diagnostic_tool()
async def asset_ingest_probe(
    data_base64: str,
    expected_sha256: str = "",
    mime_type: str = "application/octet-stream",
) -> str:
    """Phase-0 transport probe: decode base64, hash it, and persist nothing."""
    base64_chars = len(data_base64 or "")
    if base64_chars > ASSET_PROBE_MAX_BASE64_CHARS:
        return _json_lib.dumps({
            "ok": False,
            "error": "base64_too_large",
            "base64_chars": base64_chars,
            "max_base64_chars": ASSET_PROBE_MAX_BASE64_CHARS,
            "mime_type": mime_type,
        }, ensure_ascii=False, sort_keys=True)

    try:
        raw = base64.b64decode((data_base64 or "").encode("ascii"), validate=True)
    except (binascii.Error, UnicodeEncodeError, ValueError):
        return _json_lib.dumps({
            "ok": False,
            "error": "invalid_base64",
            "base64_chars": base64_chars,
            "mime_type": mime_type,
        }, ensure_ascii=False, sort_keys=True)

    sha256 = hashlib.sha256(raw).hexdigest()
    expected = (expected_sha256 or "").strip().lower()
    hash_match = bool(expected) and hmac.compare_digest(sha256, expected)
    return _json_lib.dumps({
        "ok": True,
        "base64_chars": base64_chars,
        "decoded_bytes": len(raw),
        "sha256": sha256,
        "expected_sha256": expected,
        "hash_match": hash_match,
        "mime_type": mime_type,
    }, ensure_ascii=False, sort_keys=True)


@diagnostic_tool()
async def asset_ingest_begin(
    expected_bytes: int,
    expected_sha256: str,
    mime_type: str = "application/octet-stream",
    filename: str = "",
) -> str:
    """Begin a temporary Phase-0 chunked upload that persists nothing."""
    return _asset_begin_ingest_upload(expected_bytes, expected_sha256, mime_type, filename)


@diagnostic_tool()
async def asset_ingest_chunk(upload_id: str, chunk_index: int, data_base64: str) -> str:
    """Strictly decode and append one bounded base64 chunk without logging it."""
    return _asset_ingest_chunk_data(upload_id, chunk_index, data_base64)


@diagnostic_tool()
async def asset_ingest_finish(upload_id: str) -> str:
    """Hash a completed temporary upload, report matches, and discard its bytes."""
    return _asset_finish_ingest_upload(upload_id)


@diagnostic_tool()
async def asset_ingest_abort(upload_id: str) -> str:
    """Discard a temporary Phase-0 chunked upload; repeated aborts are safe."""
    return _asset_abort_ingest_upload(upload_id)


@diagnostic_tool()
async def asset_browser_upload_link(
    expected_bytes: int,
    expected_sha256: str = "",
    filename: str = "",
    mime_type: str = "application/octet-stream",
) -> str:
    """Create a short-lived browser upload URL; raw file bytes never enter the model context."""
    return _asset_create_browser_upload_link(expected_bytes, expected_sha256, filename, mime_type)


@diagnostic_tool()
async def asset_browser_upload_status(upload_id: str) -> str:
    """Return metadata-only status for a Phase-0 browser upload."""
    return _asset_browser_upload_status_payload(upload_id)


@mcp.custom_route("/rm/upload/{token}", methods=["GET", "POST"])
@guarded_http_mutation("legacy_asset_browser_upload", methods=("POST",))
async def asset_browser_upload_route(request):
    from starlette.responses import HTMLResponse, Response

    token = request.path_params.get("token", "")
    headers = _asset_browser_security_headers()
    if request.method.upper() == "GET":
        item = _asset_get_browser_upload(token)
        if item is None:
            return Response(status_code=404, headers=headers)
        return HTMLResponse(_asset_browser_upload_page(token, item), headers=headers)

    claim = _asset_claim_browser_upload(token)
    if claim is None:
        return Response(status_code=404, headers=headers)
    try:
        streamed = await _asset_stream_browser_upload(request)
    except _AssetBrowserUploadTooLarge:
        _asset_release_browser_upload(claim["upload_id"])
        return Response(status_code=413, headers=headers)
    except Exception:
        _asset_release_browser_upload(claim["upload_id"])
        return Response(status_code=400, headers=headers)

    result = _asset_complete_browser_upload(
        claim["upload_id"], streamed["decoded_bytes"], streamed["sha256"]
    )
    if result is None:
        return Response(status_code=404, headers=headers)
    return HTMLResponse(_asset_browser_result_page(result), headers=headers)


@mcp.tool()
async def rm_asset_upload_link(
    expected_bytes: int,
    filename: str = "",
    mime_type: str = "application/octet-stream",
) -> str:
    """Create a short-lived browser upload URL; the server computes the file hash."""
    try:
        backend = _selected_asset_backend()
        backend.assert_public_mutation_allowed()
        source = backend.name
    except AssetBackendError as exc:
        return _asset_ingest_response(
            False,
            error=_safe_asset_ingest_error(exc, "upload_unavailable"),
        )
    source = "remember_me" if source == "rm" else "legacy"
    return _rm_create_asset_upload_link(expected_bytes, filename, mime_type, source=source)


@mcp.tool()
async def rm_asset_upload_status(upload_id: str) -> str:
    """Return metadata-only status for a persistent Remember-Me asset upload."""
    try:
        source = _selected_asset_backend().name
    except AssetBackendError:
        return _asset_ingest_response(
            False,
            upload_id=upload_id,
            error="upload_unavailable",
        )
    source = "remember_me" if source == "rm" else "legacy"
    return _rm_asset_upload_status_payload(upload_id, expected_source=source)


@mcp.tool()
async def rm_asset_get(asset_id: str) -> str:
    """Return persistent asset metadata without file bytes or disk paths."""
    try:
        backend = _selected_asset_backend()
        if backend.name == "rm":
            return backend.mcp_get(asset_id)
        asset = backend.get((asset_id or "").strip())
    except AssetBackendError:
        return _asset_ingest_response(False, error="asset_unavailable")
    except Exception:
        return _asset_ingest_response(False, error="asset_unavailable")
    if not asset:
        return _asset_ingest_response(False, error="asset_unavailable")
    return _json_lib.dumps({"ok": True, **_rm_asset_public_metadata(asset)}, ensure_ascii=False, sort_keys=True)


@mcp.tool()
async def rm_asset_update_metadata(
    asset_id: str,
    title: str | None = None,
    description: str | None = None,
    tags: list[str] | None = None,
) -> str:
    """Update persistent asset title, description, and tags without changing file bytes."""
    try:
        backend = _selected_asset_backend()
        if backend.name == "rm":
            return backend.mcp_update_metadata(
                asset_id,
                title=title,
                description=description,
                tags=tags,
            )
        asset = backend.update_metadata(
            asset_id,
            title=title,
            description=description,
            tags=tags,
        )
    except (AssetStoreError, AssetBackendError) as exc: return _asset_ingest_response(False, error=_safe_asset_ingest_error(exc))
    except Exception as exc: logger.warning("Asset metadata update failed error=%s", type(exc).__name__); return _asset_ingest_response(False, error="asset_unavailable")
    try:
        await asset_embedding_index.index_asset(asset)
    except Exception as exc:
        logger.warning(
            "Asset embedding refresh failed asset_id=%s error=%s",
            asset["asset_id"],
            type(exc).__name__,
        )
    return _json_lib.dumps(
        {"ok": True, **_rm_asset_public_metadata(asset)},
        ensure_ascii=False,
        sort_keys=True,
    )

@mcp.tool()
async def rm_asset_search(
    query: str = "",
    tags: list[str] | None = None,
    kind: str = "",
    mime_type: str = "",
    created_from: str = "",
    created_to: str = "",
    limit: int = 20,
    offset: int = 0,
) -> str:
    """Search persistent assets through keyword and optional semantic channels."""
    try:
        backend = _selected_asset_backend()
        if backend.name == "rm":
            return await backend.mcp_search(
                query=query,
                tags=tags,
                kind=kind,
                mime_type=mime_type,
                created_from=created_from,
                created_to=created_to,
                limit=limit,
                offset=offset,
            )
        result = backend.search(
            query=query,
            tags=tags,
            kind=kind,
            mime_type=mime_type,
            created_from=created_from,
            created_to=created_to,
            limit=limit,
            offset=offset,
        )
    except AssetStoreError as exc: return _asset_ingest_response(False, error=_safe_asset_ingest_error(exc, "search_unavailable"))
    except Exception as exc: logger.warning("Asset search failed error=%s", type(exc).__name__); return _asset_ingest_response(False, error="search_unavailable")
    if query.strip() and embedding_engine.enabled:
        try:
            semantic_scores = await asset_embedding_index.search(query)
            if semantic_scores:
                result = backend.search(
                    query=query,
                    tags=tags,
                    kind=kind,
                    mime_type=mime_type,
                    created_from=created_from,
                    created_to=created_to,
                    limit=limit,
                    offset=offset,
                    semantic_scores=semantic_scores,
                )
        except Exception as exc:
            logger.warning(
                "Asset semantic search fallback error=%s",
                type(exc).__name__,
            )
    return _json_lib.dumps(
        {"ok": True, **result},
        ensure_ascii=False,
        sort_keys=True,
    )


@mcp.tool()
async def rm_asset_reindex_embeddings(
    asset_id: str = "",
    limit: int = 100,
) -> str:
    """Backfill missing or stale Remember-Me asset embeddings without changing assets."""
    try:
        backend = _selected_asset_backend()
        if backend.name == "rm":
            return await backend.mcp_reindex(
                asset_id=asset_id,
                limit=limit,
            )
        result = await backend.reindex(
            asset_id=(asset_id or "").strip(),
            limit=limit,
        )
    except (AssetStoreError, AssetBackendError, ValueError) as exc: return _asset_ingest_response(False, error=_safe_asset_ingest_error(exc))
    except Exception as exc: logger.warning("Asset embedding reindex failed error=%s", type(exc).__name__); return _asset_ingest_response(False, error="asset_unavailable")
    return _json_lib.dumps(
        {"ok": True, **result},
        ensure_ascii=False,
        sort_keys=True,
    )

@mcp.tool()
async def rm_asset_download_link(asset_id: str) -> str:
    """Create a five-minute signed download URL for one persistent asset."""
    try:
        backend = _selected_asset_backend()
        if backend.name == "legacy":
            return _rm_create_asset_download_link(asset_id)
        return backend.mcp_download_link(asset_id)
    except AssetBackendError:
        return _asset_ingest_response(False, error="download_unavailable")
    except Exception:
        return _asset_ingest_response(False, error="download_unavailable")


@mcp.resource(
    ASSET_VIEWER_URI,
    name="remember-me-asset-viewer",
    title="Remember-Me asset viewer",
    description="Inline viewer for one privacy-cleaned Remember-Me image.",
    mime_type=ASSET_VIEWER_MIME_TYPE,
    meta=ASSET_VIEWER_RESOURCE_META,
)
async def rm_asset_viewer_resource() -> str:
    return ASSET_VIEWER_HTML


@mcp.tool(meta=ASSET_VIEWER_TOOL_META)
async def rm_asset_view(asset_id: str) -> CallToolResult:
    """Display one cleaned Remember-Me image inline with a signed-link fallback."""
    try:
        backend = _selected_asset_backend()
    except AssetBackendError:
        return _rm_asset_view_error("image_unavailable")
    if backend.name == "legacy":
        verified = _rm_verified_view_image(asset_id)
        if isinstance(verified, str):
            return _rm_asset_view_error(verified)
        asset, data = verified
        try:
            download = _json_lib.loads(_rm_create_asset_download_link(asset["asset_id"]))
        except (TypeError, ValueError, _json_lib.JSONDecodeError):
            return _rm_asset_view_error("download_unavailable")
        if not download.get("ok"):
            return _rm_asset_view_error("download_unavailable")
        fallback_url = download.get("download_url") or download.get("download_path")
        title = asset.get("title") or asset["original_filename"]
        structured = {
            "asset_id": asset["asset_id"],
            "title": asset.get("title", ""),
            "filename": asset["original_filename"],
            "mime_type": asset["mime_type"],
            "width": asset["width"],
            "height": asset["height"],
            "tags": asset.get("tags", []),
            "stored_bytes": asset["stored_bytes"],
        }
        return CallToolResult(
            content=[
                TextContent(
                    type="text",
                    text=(
                        f"Remember-Me image: {title}\n"
                        "If this client does not display the inline viewer, use this "
                        f"short-lived download link: {fallback_url}"
                    ),
                )
            ],
            structuredContent=structured,
            _meta={
                "rememberMe": {
                    "schemaVersion": 1,
                    "imageBase64": base64.b64encode(data).decode("ascii"),
                    "mimeType": asset["mime_type"],
                }
            },
        )
    try:
        return backend.mcp_view(asset_id)
    except Exception:
        return _rm_asset_view_error("image_unavailable")

@mcp.tool()
async def rm_asset_inspect(asset_id: str) -> CallToolResult:
    """Return the cleaned stored image for actual visual understanding.

    Call rm_asset_inspect when the model needs to read the image or text inside it.
    Call rm_asset_view when the goal is only to show the image to the user.
    Never guess image content from metadata. This tool does not update metadata
    or embeddings.
    """
    try:
        backend = _selected_asset_backend()
    except AssetBackendError:
        return _rm_asset_inspect_error("image_unavailable")
    if backend.name == "legacy":
        verified = _rm_verified_view_image(asset_id)
        if isinstance(verified, str):
            return _rm_asset_inspect_error(verified)
        asset, data = verified
        width = asset["width"]
        height = asset["height"]
        if (
            width <= 0
            or height <= 0
            or width * height > RM_ASSET_MAX_IMAGE_PIXELS
        ):
            return _rm_asset_inspect_error("image_too_large")
        structured = {
            "asset_id": asset["asset_id"],
            "title": asset.get("title", ""),
            "filename": asset["original_filename"],
            "mime_type": asset["mime_type"],
            "width": width,
            "height": height,
            "tags": asset.get("tags", []),
            "stored_bytes": asset["stored_bytes"],
        }
        encoded = base64.b64encode(data).decode("ascii")
        return CallToolResult(
            content=[
                TextContent(
                    type="text",
                    text=(
                        f"Remember-Me image asset {asset['asset_id']}; "
                        f"filename: {asset['original_filename']}; "
                        f"MIME type: {asset['mime_type']}; "
                        f"dimensions: {width} x {height}."
                    ),
                ),
                ImageContent(
                    type="image",
                    data=encoded,
                    mimeType=asset["mime_type"],
                ),
            ],
            structuredContent=structured,
        )
    try:
        return backend.mcp_inspect(asset_id)
    except Exception:
        return _rm_asset_inspect_error("image_unavailable")


_RM_UPLOAD_CORE_ERROR_STATUS = {
    "upload_too_large": 413,
    "upload_size_mismatch": 422,
    "pixel_limit": 422,
    "invalid_image": 422,
    "invalid_metadata": 422,
    "invalid_asset_id": 422,
    "repository_failure": 500,
    "core_failure": 500,
    "runtime_unavailable": 503,
}


def _rm_core_upload_error_status(exc: Exception) -> int:
    return _RM_UPLOAD_CORE_ERROR_STATUS.get(str(getattr(exc, "code", "")), 500)


async def _rm_persist_remember_me_upload(request, claim: dict) -> tuple[int, dict | None]:
    try:
        backend = _selected_asset_backend()
    except AssetBackendError:
        return 503, None
    if backend.name != "rm":
        return 503, None
    try:
        # Keep the host-owned transient file helper for this browser route;
        # authority and the persistent freeze gate are still enforced by the
        # selected backend before Core receives the bytes.
        backend.assert_public_mutation_allowed()
        temp_path = _rm_create_upload_temp_path()
    except AssetBackendError as exc:
        return (409 if exc.code == "asset_write_frozen" else 503), None
    except Exception:
        return 500, None
    try:
        try:
            with temp_path.open("wb") as handle:
                streamed = await _asset_stream_browser_upload(
                    request,
                    handle.write,
                    max_bytes=RM_ASSET_MAX_UPLOAD_BYTES,
                )
        except _AssetBrowserUploadTooLarge:
            return 413, None
        except Exception:
            return 400, None
        if streamed["decoded_bytes"] != claim["expected_bytes"]:
            return 422, None
        try:
            content = await asyncio.to_thread(temp_path.read_bytes)
        except Exception:
            return 500, None
        if len(content) != streamed["decoded_bytes"]:
            return 500, None
        if not hmac.compare_digest(hashlib.sha256(content).hexdigest(), streamed["sha256"]):
            return 500, None
        try:
            raw_result = await asyncio.to_thread(
                backend.ingest_public_metadata,
                content,
                claim["expected_bytes"],
                claim["filename"],
                claim["mime_type"],
                title="",
                description="",
                tags=(),
            )
            result = _rm_normalize_remember_me_upload_result(
                raw_result,
                claim["expected_bytes"],
                streamed["sha256"],
            )
        except Exception as exc:
            return _rm_core_upload_error_status(exc), None
        return 200, result
    finally:
        _rm_delete_upload_temp_path(temp_path)

@mcp.custom_route("/rm/asset-upload/{token}", methods=["GET", "POST"])
@guarded_http_mutation("remember_me_asset_upload", methods=("POST",))
async def rm_asset_upload_route(request):
    from starlette.responses import HTMLResponse, Response

    token = request.path_params.get("token", "")
    headers = _asset_browser_security_headers()
    if request.method.upper() == "GET":
        item = _rm_get_asset_upload(token)
        if item is None:
            return Response(status_code=404, headers=headers)
        return HTMLResponse(_rm_asset_upload_page(token, item), headers=headers)

    claim = _rm_claim_asset_upload(token)
    if claim is None:
        return Response(status_code=404, headers=headers)

    source = claim.get("source")
    try:
        backend = _selected_asset_backend()
    except AssetBackendError:
        _rm_release_asset_upload(claim["upload_id"])
        return Response(status_code=503, headers=headers)
    expected_source = "remember_me" if backend.name == "rm" else "legacy"
    if source != expected_source:
        _rm_release_asset_upload(claim["upload_id"])
        return Response(status_code=409, headers=headers)
    if source == "legacy":
        try:
            temp_path = backend.create_temp_path()
        except AssetBackendError:
            _rm_release_asset_upload(claim["upload_id"])
            return Response(status_code=409, headers=headers)
        try:
            with temp_path.open("wb") as handle:
                streamed = await _asset_stream_browser_upload(
                    request,
                    handle.write,
                    max_bytes=RM_ASSET_MAX_UPLOAD_BYTES,
                )
            size_match = streamed["decoded_bytes"] == claim["expected_bytes"]
            if not size_match:
                _rm_release_asset_upload(claim["upload_id"])
                return Response(status_code=422, headers=headers)
            asset = await asyncio.to_thread(
                backend.persist_upload,
                temp_path,
                streamed["sha256"],
                streamed["decoded_bytes"],
                claim["filename"],
                claim["mime_type"],
                require_image=True,
            )
            result_asset = _rm_asset_public_metadata(asset, bool(asset.get("deduplicated")))
            result_asset["source_sha256"] = streamed["sha256"]
        except _AssetBrowserUploadTooLarge:
            _rm_release_asset_upload(claim["upload_id"])
            return Response(status_code=413, headers=headers)
        except InvalidAssetImage:
            _rm_release_asset_upload(claim["upload_id"])
            return Response(status_code=422, headers=headers)
        except AssetStoreError:
            _rm_release_asset_upload(claim["upload_id"])
            return Response(status_code=500, headers=headers)
        except Exception:
            _rm_release_asset_upload(claim["upload_id"])
            return Response(status_code=400, headers=headers)
        finally:
            temp_path.unlink(missing_ok=True)
    elif source == "remember_me":
        status_code, result_asset = await _rm_persist_remember_me_upload(request, claim)
        if status_code != 200 or result_asset is None:
            _rm_release_asset_upload(claim["upload_id"])
            return Response(status_code=status_code, headers=headers)
        streamed = {"sha256": result_asset["source_sha256"]}
    else:
        _rm_release_asset_upload(claim["upload_id"])
        return Response(status_code=404, headers=headers)

    result = _rm_complete_asset_upload(
        claim["upload_id"],
        result_asset,
        streamed["sha256"],
        expected_source=source,
    )
    if result is None:
        _rm_release_asset_upload(claim["upload_id"])
        return Response(status_code=500, headers=headers)
    return HTMLResponse(_rm_asset_result_page(result), headers=headers)


@mcp.custom_route("/rm/asset-download/{token}", methods=["GET", "HEAD"])
async def rm_asset_download_route(request):
    from starlette.responses import FileResponse, Response

    result = _rm_read_asset_download(
        request.path_params.get("token", ""), request.method
    )
    if result is None:
        return Response(status_code=404, headers=_asset_browser_security_headers())
    _, body, headers, source = result
    if request.method.upper() == "HEAD":
        return Response(content=b"", headers=headers)
    if source == "legacy":
        return FileResponse(body, media_type=headers["Content-Type"], headers=headers)
    return Response(content=body, media_type=headers["Content-Type"], headers=headers)

@diagnostic_tool()
async def asset_render_probe() -> CallToolResult:
    """Phase-0 transport probe: return the built-in PNG as an MCP image block."""
    with open(ASSET_PROBE_PATH, "rb") as handle:
        encoded = base64.b64encode(handle.read()).decode("ascii")
    return CallToolResult(
        content=[
            TextContent(
                type="text",
                text="asset_render_probe: phase-0 image content block",
            ),
            ImageContent(
                type="image",
                data=encoded,
                mimeType="image/png",
            ),
        ]
    )


@diagnostic_tool()
async def asset_export_probe() -> str:
    """Phase-0 export probe. Caller should decode data_base64 to a file, verify decoded_bytes and sha256, then present it as a user-visible attachment."""
    with open(ASSET_PROBE_PATH, "rb") as handle:
        data = handle.read()
    return _json_lib.dumps({
        "ok": True,
        "filename": "remember-me-probe.png",
        "mime_type": "image/png",
        "decoded_bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "data_base64": base64.b64encode(data).decode("ascii"),
    }, ensure_ascii=False)


@diagnostic_tool()
async def asset_vision_challenge() -> CallToolResult:
    """Phase-0 blind vision probe: return a machine-scored ImageContent challenge without revealing the answer."""
    trial = _asset_new_vision_trial()
    ok, error = _asset_store_vision_trial(trial)
    if not ok:
        return CallToolResult(content=[TextContent(type="text", text=_asset_reject_vision_answer(error))])
    encoded = base64.b64encode(trial["png"]).decode("ascii")
    return CallToolResult(
        content=[
            TextContent(type="text", text=_asset_vision_prompt(trial["trial_id"], len(trial["png"]), trial["sha256"])),
            ImageContent(type="image", data=encoded, mimeType="image/png"),
        ]
    )


@diagnostic_tool()
async def asset_vision_verify(trial_id: str, answer_json: str) -> str:
    """Phase-0 blind vision verifier: score one submitted answer without returning the correct answer."""
    return _asset_score_vision_answer(trial_id, answer_json)


@diagnostic_tool()
async def asset_vision_export(trial_id: str) -> str:
    """Phase-0 file-view vision probe: export a live challenge PNG as JSON/base64 without revealing the answer."""
    return _asset_export_vision_trial(trial_id)


@diagnostic_tool()
async def asset_vision_download_link(trial_id: str) -> str:
    """Phase-0 signed download path for a live vision trial PNG; returns no base64 or ImageContent."""
    return _asset_create_vision_download_link(trial_id)


@mcp.custom_route("/rm/vision-download/{token}", methods=["GET", "HEAD"])
async def asset_vision_download_route(request):
    from starlette.responses import Response

    result = _asset_read_vision_download(request.path_params.get("token", ""), request.method)
    if result is None:
        return Response(status_code=404)
    png, headers = result
    content = b"" if request.method.upper() == "HEAD" else png
    return Response(content=content, headers=headers)


@diagnostic_tool()
async def asset_vision_upload_challenge() -> str:
    """Phase-0 user-upload vision control: create a blind trial without returning ImageContent or base64."""
    trial = _asset_new_vision_trial()
    ok, error = _asset_store_vision_trial(trial)
    if not ok:
        return _asset_reject_vision_answer(error)
    return _asset_vision_upload_payload(trial["trial_id"], len(trial["png"]), trial["sha256"])


@mcp.tool()
async def digest(
    dry_run: bool = True,
    max_groups: int = 10,
    confirm_token: str = "",
    mode: str = "maintenance",
    include_archive: bool = False,
    limit: int = 30,
) -> str:
    """Preview separate consolidation/rebalance plans; max_groups limits consolidation only, while limit controls rebalance preview rows only."""
    normalized_mode = (mode or "maintenance").strip().lower()
    if normalized_mode == "dedupe":
        try:
            return await _run_dedupe_scan(limit=limit, include_archive=include_archive)
        except Exception as exc:
            logger.error("Dedupe scan failed: %s", exc)
            return "embedding 查重失败。"
    if normalized_mode != "maintenance":
        return "mode 必须是 maintenance 或 dedupe。"
    await decay_engine.ensure_started()
    try:
        return await _run_digest(dry_run=dry_run, max_groups=max_groups, confirm_token=confirm_token, limit=limit)
    except Exception as exc:
        logger.error("Digest failed: %s", exc)
        return "自动消化失败。"


@mcp.tool()
async def related_backfill(
    dry_run: bool = True,
    limit: int = 100,
    threshold: Annotated[float, Field(description="-1 uses the configured default threshold; a non-negative value sets the semantic-link threshold.")] = -1,
) -> str:
    """Controlled related-link maintenance: dry-run by default; execution writes links and skips sealed buckets."""
    try:
        actual_threshold = None if threshold < 0 else threshold
        return await _run_related_backfill(dry_run=dry_run, limit=limit, threshold=actual_threshold)
    except Exception as exc:
        logger.error("Related backfill failed: %s", exc)
        return "自动 related 回填失败。"


@mcp.tool()
async def breath(
    query: Annotated[str, Field(description="Directed keyword/semantic retrieval in ordinary mode; session/feel use substring matching and recency order. Empty query uses the selected mailbox/session/feel, resonance, tags or importance listing, otherwise default emergence. as_of requires query.")] = "",
    max_tokens: Annotated[int, Field(description="Approximate output token budget, capped at 20000; metadata and attachments share this budget. Budget limits can emit fewer than max_results. Mailbox rejects a non-default value.")] = 10000,
    domain: Annotated[str, Field(description="Comma-separated normal domains are exact metadata filters (OR), never a fallback to all buckets. Pure session/feel selects that mode; reserved and normal domains cannot be mixed.")] = "",
    valence: Annotated[float, Field(description="-1 disables this coordinate. With arousal, 0.0-1.0 participates in existing query emotion ranking. Valence alone is summary presentation only for ordinary query or tags-only; unsupported in full/session/feel. Historical query requires both coordinates.")] = -1,
    arousal: Annotated[float, Field(description="-1 disables this coordinate. A 0.0-1.0 value requires valence and participates only in ordinary/historical query emotion ranking; it is not a metadata filter.")] = -1,
    max_results: Annotated[int, Field(description="Result limit clamped to 1-50. In default emergence this limits dynamic candidates; pinned/protected items are additional and share the token budget. Query and fixed listings count pinned/protected within the limit. Remaining counts do not guarantee pagination; only supported query routes return a cursor.")] = 5,
    importance_min: Annotated[int, Field(description="-1 disables the stored bucket importance filter; 1-10 intersects with the selected bucket candidates. Only without another selector, retain importance-descending listing. Unsupported with as_of/mailbox.")] = -1,
    mode: Annotated[str, Field(description="summary/full control ordinary query and default emergence (full retains legacy dehydration). Session full requires query; feel full requires query plus tags_filter. Fixed listings/mailbox reject full. as_of always renders historical body for either mode, and cursors bind the requested mode.")] = "summary",
    recent_days: Annotated[int, Field(description="-1 disables recency; 0 means the service-local current calendar day; positive N retains the inclusive date cutoff today minus N. Values below -1 are invalid. Query cursors freeze the first-page window; pinned/protected emergence retains its exception. Unsupported with as_of/mailbox.")] = -1,
    emotion_trend: bool = False,
    include_dormant: Annotated[bool, Field(description="Include dormant ordinary/historical candidates. Session/feel retain their existing eligibility and reject True; pinned/protected emergence retains its eligibility exception.")] = False,
    include_sealed: bool = False,
    date_from: Annotated[str, Field(description="Inclusive YYYY-MM-DD lower bound on the bucket updated_at date, falling back to last_active/created. Empty disables it; must not exceed date_to. Unsupported with as_of/mailbox.")] = "",
    date_to: Annotated[str, Field(description="Inclusive YYYY-MM-DD upper bound using the same bucket date as date_from. Empty disables it. Unsupported with as_of/mailbox.")] = "",
    resonance: Annotated[str, Field(description="Optional valence,arousal pair, each 0-1, for emotional-distance ordering. With ordinary query it reorders matches; without query it selects a distance-ordered listing. Compatible metadata filters still intersect. Unsupported for session/feel/as_of/mailbox; it is not a related-bucket selector.")] = "",
    mailbox: Annotated[bool, Field(description="Independent letter selector. Only mailbox_limit and include_sealed may vary; non-default bucket retrieval arguments are rejected.")] = False,
    mailbox_limit: Annotated[int, Field(description="Mailbox letter limit, clamped to 1-50. Outside mailbox only the default 1 is accepted.")] = 1,
    feels: Annotated[bool, Field(description="Explicit feel selector; compatible only with empty or pure feel domain. Conflicts with topic_filter, mailbox, as_of and resonance.")] = False,
    tags_filter: Annotated[
        list[str] | None,
        Field(description="Optional exact bucket-tag filters (OR), intersecting with domain/importance and session topics. Tags do not override query/resonance; tagged query does not support cursor."),
    ] = None,
    topic_filter: Annotated[
        list[str] | None,
        Field(
            description=(
                "Optional exact archived-session topic filters (OR), selecting session mode. "
                "Tags and importance intersect; only empty or pure session domain is compatible."
            )
        ),
    ] = None,
    wake_dormant: Annotated[
        bool,
        Field(
            description=(
                "Defaults to False. With touch=True requires include_dormant=True; touch=False overrides all waking. "
                "wake only emitted, directly touched dormant buckets. Unsupported for session/feel/as_of/mailbox; query cursors bind this choice."
            )
        ),
    ] = False,
    touch: Annotated[
        bool,
        Field(
            description=(
                "Defaults to True. Set False for maintenance or acceptance "
                "retrieval that must not update activation, last_active, or "
                "dormant state; as_of is always read-only."
            )
        ),
    ] = True,
    min_score: Annotated[float, Field(description="Strong/weak display threshold, not a hard filter or the displayed ranking score. Only ordinary/historical query supports it. -1 reads OMBRE_BREATH_MIN_SCORE, defaulting to 0; explicit values must be 0-1. Weak matches remain in total accounting.")] = -1,
    as_of: Annotated[
        str,
        Field(
            description=(
                "Optional ISO8601 date or timestamp for read-only historical-body "
                "keyword/fuzzy retrieval. Date-only means the end of the local day; "
                "historical semantic embeddings and deleted buckets are unavailable."
            )
        ),
    ] = "",
    cursor: Annotated[
        str,
        Field(
            description=(
                "Process-local opaque cursor only for ordinary query without tags_filter or historical query. Other selectors do not support pagination, even when remaining is nonzero. "
                "Reuse the same selector, query, filters, mode, touch and wake_dormant; recency stays frozen. max_results/max_tokens may change."
            )
        ),
    ] = "",
) -> str:
    """Retrieve directed memories with query, or use the selected listing/default emergence.

    Related buckets are result annotations, not a search parameter. Remaining
    reports undisplayed results; pagination exists only when a query cursor is
    returned. touch=False skips activation updates, waking, decay startup and
    dehydration cache writes; lazy runtime initialization can still write storage.
    """
    arguments = locals().copy()
    try:
        request = _prepare_breath_request(**arguments)
    except ValueError as exc:
        return _with_response_seal(str(exc))
    if mailbox:
        return _with_response_seal(
            _format_mailbox(mailbox_limit, include_sealed=include_sealed)
        )
    domain = request["domain"]
    result = await _breath_impl(
        query=query,
        max_tokens=max_tokens,
        domain=domain,
        valence=valence,
        arousal=arousal,
        max_results=max_results,
        importance_min=importance_min,
        mode=mode,
        recent_days=recent_days,
        emotion_trend=emotion_trend if (as_of or "").strip() else False,
        include_dormant=include_dormant,
        wake_dormant=wake_dormant,
        touch=touch,
        include_sealed=include_sealed,
        date_from=date_from,
        date_to=date_to,
        resonance=resonance,
        tags_filter=tags_filter,
        topic_filter=topic_filter,
        cursor=cursor,
        min_score=min_score,
        as_of=as_of,
        _request=request,
    )
    if emotion_trend and not (as_of or "").strip() and not result.startswith("breath mode="):
        result = _with_emotion_timeline(result, True, min(max_tokens, 20000))
    return _with_response_seal(result)


# =============================================================
# Tool 2: hold — Hold on to this
# 工具 2：hold — 握住，留下来
# =============================================================

async def _format_hold_created(bucket_id: str) -> str:
    """Report the persisted values for a newly created hold bucket."""
    bucket = await bucket_mgr.get(bucket_id)
    if not bucket:
        return f"新建 {bucket_id}"
    metadata = bucket.get("metadata", {})
    tags = metadata.get("tags", [])
    domains = metadata.get("domain", [])
    if isinstance(tags, str):
        tags = _parse_csv_ids(tags)
    if not isinstance(tags, list):
        tags = []
    if isinstance(domains, str):
        domains = _parse_csv_ids(domains)
    if not isinstance(domains, list):
        domains = []
    name = str(metadata.get("name") or bucket_id)
    importance = int(metadata.get("importance", 0) or 0)
    return (
        f"新建 {bucket_id} | {name} | importance={importance} | "
        f"tags=[{', '.join(str(tag) for tag in tags)}] | "
        f"domain=[{', '.join(str(domain) for domain in domains)}]"
    )


def _format_hold_feel_source_receipt(bucket_id: str, source_id: str, error: str) -> str:
    """Append six stable fields; JSON IDs cannot inject receipt lines."""
    return (
        "[hold_feel_source_receipt]\n"
        f"feel_created={str(bool(bucket_id)).lower()}\n"
        "feel_reused=false\n"
        f"bucket_id={_json_lib.dumps(bucket_id, ensure_ascii=True)}\n"
        f"source_bucket_id={_json_lib.dumps(source_id, ensure_ascii=True)}\n"
        f"source_marked={str(error == 'none').lower()}\n"
        f"source_mark_error={error}"
    )

_LEGACY_POST_EFFECT_TASKS = set()


async def _await_legacy_post_effects(function, *args):
    """One invocation, no retry identity or durable record. Cancel only the waiter."""
    task = asyncio.create_task(function(*args))
    _LEGACY_POST_EFFECT_TASKS.add(task)
    def finished(done):
        _LEGACY_POST_EFFECT_TASKS.discard(done)
        if done.cancelled():
            logger.error('legacy post-effects executor cancelled; recovery unavailable')
        else:
            error = done.exception()
            if error is not None:
                logger.error('legacy post-effects failed: %s',
                             getattr(error, 'code', type(error).__name__))
    task.add_done_callback(finished)
    return await asyncio.shield(task)


@guarded_async_mutation('legacy_archive_post_effects')
async def _run_legacy_archive(store, payload, frozen_config):
    plan = await short_step(store.publish_legacy, payload, frozen_config, _capture_source=True)
    identity, guard = plan['bucket_id'], plan['source_guard']
    if not bucket_mgr._record_boot_delta_event(identity, 'created', _expected_source=guard):
        raise BucketIdempotencyError('confirmed_delete_source_changed')
    if not payload['sealed']:
        await bucket_mgr._refresh_ordinary_embedding_best_effort(identity, plan['embedding_input'])
    with bucket_write_scope(frozen_config['buckets_dir']):
        bucket_mgr.admit_delayed_effect(identity, guard, 'archive_post_effects')
        if payload['letter']:
            bucket_mgr.record_letter(payload['letter'], identity, sealed=payload['sealed'], _expected_source=guard)
        if plan['snapshot'] is not None:
            entry = plan['snapshot']
            _record_emotion_snapshot(entry['valence'], entry['arousal'], 'archive', identity,
                                     _expected_entry=entry, _expected_source=guard, _strict=True)
    return plan['result_text']


@guarded_async_mutation('legacy_pinned_post_effects')
async def _run_legacy_pinned(values, emotion, trigger_date):
    publication = {}
    identity = await bucket_mgr.create(**values, _publication_out=publication)
    guard = publication['source_guard']
    with bucket_write_scope(config['buckets_dir']):
        bucket_mgr.admit_delayed_effect(identity, guard, 'pinned_post_effects')
        if emotion is not None:
            _record_emotion_snapshot(*emotion, 'hold', identity, _expected_source=guard, _strict=True)
        if trigger_date:
            # This metadata-only update has no suspended provider work. The
            # outer root mutex binds admission, field preimage and publication.
            if not await bucket_mgr.update(identity, trigger_date=trigger_date, trigger_last_seen='',
                                           _expected_source=guard):
                raise BucketIdempotencyError('legacy_trigger_write_failed')
            guard = bucket_mgr.delete_admission.capture(identity)
    await _auto_link_related(identity, _expected_source=guard)
    return identity


_S4_HOLD_GROW_RUNNERS = {}


async def _hold_grow_keyed(operation_id, kind, values):
    """The HTTP caller is a waiter; cancellation never cancels its owned runner.

    S-4B safe surface excludes keyed supersedes_id, before get/update/startup.
    None is dispatched by the public tools directly to their original bodies.
    """
    try:
        BucketManager.validate_trace_operation_id(operation_id)
        if kind == 'hold' and values['supersedes_id'].strip():
            return 'unsupported_combination: operation_id does not support supersedes_id.'
        content = values['content']
        if not content or not content.strip():
            return '内容为空，无法存储。' if kind == 'hold' else '内容为空，无法整理。'
        if kind == 'hold':
            if not 1 <= values['importance'] <= 10:
                return 'importance must be within 1-10.'
            for field in ('valence', 'arousal'):
                if values[field] != -1 and not 0 <= values[field] <= 1:
                    return f'{field} must be -1 or within 0.0-1.0.'
        manager = bucket_mgr
        existing = manager.inspect_trace_request(operation_id)
        normalization = existing['normalization_context'] if existing else {
            'version': 1, 'aliases': list(DISPLAY_ALIASES.items())}
        payload = {'kind': kind, 'content': BucketManager._trace_alias_value(content, normalization['aliases'])}
        if kind == 'hold':
            payload.update({field: values[field] for field in ('importance', 'pinned', 'feel')})
            payload.update(tags=_parse_csv_ids(values['tags']), source_bucket=values['source_bucket'].strip(),
                supersedes_id=values['supersedes_id'].strip(), valence=float(values['valence']),
                arousal=float(values['arousal']), trigger_date=_parse_optional_date(values['trigger_date'], 'trigger_date') or '',
                provenance_kind=_parse_explicit_provenance_kind(values['provenance_kind']))
        _, digest = manager._canonical_import_payload(payload)
        if existing and (existing['kind'] != kind or existing['payload_digest'] != digest):
            return 'operation_id_conflict'
        key = (os.getpid(), asyncio.get_running_loop(), str(Path(manager.base_dir).resolve()), operation_id)
        active = _S4_HOLD_GROW_RUNNERS.get(key)
        if active is None:
            task = asyncio.create_task(_run_hold_grow_request(manager, operation_id, payload, normalization))
            active = (digest, task)
            _S4_HOLD_GROW_RUNNERS[key] = active
            def finished(done):
                if _S4_HOLD_GROW_RUNNERS.get(key) == active:
                    _S4_HOLD_GROW_RUNNERS.pop(key, None)
                # Retrieve failures even when the last HTTP waiter disconnected.
                if not done.cancelled():
                    done.exception()
            task.add_done_callback(finished)
        if active[0] != digest:
            return 'operation_id_conflict'
        return await asyncio.shield(active[1])
    except (BucketIdempotencyError, RelatedError, ValueError) as exc:
        return str(exc)


@guarded_async_mutation('hold_grow_request_execute')
async def _run_hold_grow_request(manager, operation_id, payload, normalization):
    owner = secrets.token_hex(16)
    while (context := manager._claim_s4_request(operation_id, payload, normalization, owner)) is None:
        await asyncio.sleep(.05)
    if 'replay' in context:
        return context['replay']
    heartbeat = asyncio.create_task(manager._trace_heartbeat(context))
    try:
        await decay_engine.ensure_started()
        request = manager._trace_fence(context)
        parent = request['plan']
        if 'items' not in parent:
            if payload['kind'] == 'hold':
                for field, provider in (('similarity', _similarity_doorbell), ('conflict', _detect_conflict_warning)):
                    if field not in parent:
                        parent[field] = await provider(payload['content'])
                        manager._trace_checkpoint(context, plan=parent)
                if payload['feel'] and payload['source_bucket'] and 'source_preflight' not in parent:
                    with bucket_write_scope(manager.base_dir):
                        parent['source_preflight'] = manager.preview_feel_source(payload['source_bucket'])['status']
                        if parent['source_preflight'] == 'valid':
                            parent['feel_source_guard'] = manager.delete_admission.capture(payload['source_bucket'])
                        manager._trace_checkpoint(context, plan=parent)
                if parent.get('source_preflight', 'valid') != 'valid':
                    error = parent['source_preflight']
                    result = f'feel 未创建；source 校验失败（{error}）。\n' + _format_hold_feel_source_receipt('', payload['source_bucket'], error)
                    manager._trace_checkpoint(context, 'completed', result=result, resolutions={})
                    return result
                parent['mode'] = 'feel' if payload['feel'] else 'pinned' if payload['pinned'] else 'ordinary'
            else:
                parent['mode'] = 'short' if len(payload['content'].strip()) < 30 else 'digest'
                if parent['mode'] == 'short' and 'conflict' not in parent:
                    parent['conflict'] = await _detect_conflict_warning(payload['content'])
                    manager._trace_checkpoint(context, plan=parent)
            if parent['mode'] == 'digest':
                # A process death before this checkpoint may repeat the provider.
                # HTTP cancellation cannot: it only cancels the shielded waiter.
                try:
                    items = await dehydrator.digest(payload['content'])
                except Exception as exc:
                    result = f'日记整理失败。 reason={_provider_failure_category(exc)}'
                    manager._trace_checkpoint(context, 'completed', plan=parent, result=result, resolutions={})
                    return result
                if not items:
                    result = '日记整理失败。 reason=parse_error'
                    manager._trace_checkpoint(context, 'completed', plan=parent, result=result, resolutions={})
                    return result
            else:
                if 'analysis' not in parent:
                    failure = ''
                    try:
                        analysis = await dehydrator.analyze(payload['content'])
                    except Exception as exc:
                        failure = _provider_failure_category(exc)
                        analysis = {'tags': []} if parent['mode'] == 'feel' else {
                            'domain': ['未分类'], 'valence': .5, 'arousal': .3,
                            'tags': [], 'suggested_name': '', 'todos': []}
                    parent.update(analysis=analysis, analysis_failure=failure)
                    manager._trace_checkpoint(context, plan=parent)
                items = [parent['analysis']]
            parent['items'] = [{'ordinal': i, 'identity': manager._trace_child_key(operation_id, f'item:{i}', kind=payload['kind']),
                                'item': item, 'plan': None} for i, item in enumerate(items)]
            manager._trace_checkpoint(context, 'items_frozen', plan=parent)
        # Refresh after every checkpoint; no completed item is planned/executed again.
        for ordinal in range(len(parent['items'])):
            request = manager._trace_fence(context)
            parent, resolutions = request['plan'], request['resolutions']
            current = resolutions.setdefault('items', {}).setdefault(str(ordinal), {})
            if 'result' in current:
                continue
            entry = parent['items'][ordinal]
            if entry['plan'] is None:
                if parent['mode'] == 'digest' and 'conflict' not in entry:
                    entry['conflict'] = await _detect_conflict_warning(entry['item']['content'])
                    manager._trace_checkpoint(context, plan=parent)
                values = _hold_grow_item_values(payload, parent, entry)
                candidate = None
                if parent['mode'] not in ('feel', 'pinned'):
                    try:
                        found = await manager.search(values['content'], limit=1,
                            domain_filter=values['domain'] or None, include_sealed=False)
                    except Exception:
                        found = []
                    if found:
                        first = found[0]
                        meta = first.get('metadata', {})
                        if (not _is_sealed(first) and meta.get('type') != 'feel'
                                and not (meta.get('pinned') or meta.get('protected'))
                                and BucketManager._normalize_search_text(first.get('content')) == BucketManager._normalize_search_text(values['content'])):
                            candidate = first
                manager.plan_hold_grow_item(context, ordinal, values, candidate)
            await _execute_hold_grow_item(manager, context, ordinal)
            request = manager._trace_fence(context)
            resolutions = request['resolutions']
            resolutions['items'][str(ordinal)]['result'] = _hold_grow_item_response(payload, request['plan'], ordinal, resolutions)
            manager._trace_checkpoint(context, resolutions=resolutions)
        request = manager._trace_fence(context)
        parent, resolutions = request['plan'], request['resolutions']
        texts = [resolutions['items'][str(i)]['result'] for i in range(len(parent['items']))]
        result = texts[0]
        if parent['mode'] == 'digest':
            reused = sum(entry['plan']['reused'] for entry in parent['items'])
            result = f"{len(texts)}条|新建{len(texts)-reused}/复用{reused}\n" + '\n'.join(texts)
            conflicts = [f"{entry['item'].get('name', entry['plan']['result_name'])}: {entry['conflict']}"
                         for entry in parent['items'] if entry.get('conflict')]
            if conflicts:
                result += '\nconflict: ' + '；'.join(conflicts)
        manager._trace_checkpoint(context, 'completed', resolutions=resolutions, result=result)
        return result
    finally:
        heartbeat.cancel()
        try:
            await heartbeat
        except asyncio.CancelledError:
            pass
        finally:
            manager._release_trace_request(context)


def _hold_grow_item_values(payload, parent, entry):
    analysis, mode = entry['item'], parent['mode']
    content = payload['content'] if mode != 'digest' else analysis['content']
    if mode == 'short':
        content = content.strip()
    values = dict(content=content, tags=analysis.get('tags', []), importance=analysis.get('importance', 5),
        domain=analysis.get('domain', ['未分类']), valence=analysis.get('valence', .5),
        arousal=analysis.get('arousal', .3), name=analysis.get('name') or analysis.get('suggested_name') or _canonical_body_name(content),
        todos=_canonical_todos(analysis.get('todos')), provenance_kind='summary' if mode == 'digest' else None)
    if payload['kind'] == 'hold':
        values.update(importance=payload['importance'], provenance_kind=payload['provenance_kind'], trigger_date=payload['trigger_date'])
        values['tags'] = list(dict.fromkeys(analysis.get('tags', []) + payload['tags']))
        for field in ('valence', 'arousal'):
            if payload[field] != -1:
                values[field] = payload[field]
        if mode == 'feel':
            values.update(tags=analysis.get('tags', []), importance=5, domain=[], todos=[],
                valence=payload['valence'] if payload['valence'] != -1 else .5,
                arousal=payload['arousal'] if payload['arousal'] != -1 else .3,
                name=_canonical_body_name(content.strip().replace('\n', ' ')) or None,
                bucket_type='feel', provenance_kind=payload['provenance_kind'] or 'inference')
        elif mode == 'pinned':
            values.update(importance=10, bucket_type='permanent', pinned=True)
    elif mode == 'short' and not isinstance(values['importance'], int):
        values['importance'] = 5
    return values


@guarded_mutation('hold_grow_related_commit')
def _hold_grow_related(manager, context, ordinal):
    with bucket_write_scope(manager.base_dir):
        request = manager._trace_fence(context)
        plan, resolutions = request['plan']['items'][ordinal]['plan'], request['resolutions']
        current = resolutions['items'][str(ordinal)]
        if 'selection' not in current:
            engine, selected = manager.embedding_engine, []
            if engine and engine.enabled:
                inventory = scan_relation_store(manager.base_dir)
                vectors = read_vectors(engine.db_path, plan['embedding_model'])
                target = plan['target']
                if automatic_eligible(inventory, target) and target in vectors:
                    for other in inventory.order:
                        if other != target and automatic_eligible(inventory, other) and other in vectors:
                            score = engine._cosine_similarity(vectors[target], vectors[other])
                            if score >= plan['related_threshold']:
                                selected.append((other, score))
                    selected.sort(key=lambda pair: pair[1], reverse=True)
                    selected = selected[:plan['related_top_k']]
            current['selection'] = selected
            manager._trace_checkpoint(context, resolutions=resolutions)
        selected = current['selection']
        if not selected:
            return {'status': 'not_requested'}
        manager._trace_fence(context)
        relation = {'source': plan['target'], 'add': [pair[0] for pair in selected], 'remove': [], 'origin': 'inferred'}
        source = request['payload'].get('source_bucket') if request['plan']['mode'] == 'feel' else None
        guard = request['plan'].get('feel_source_guard')
        existing = manager.relation_store.lookup(plan['keys']['relation'])
        if source in relation['add']:
            try:
                manager.admit_delayed_effect(source,guard,'hold_grow_related_source')
            except BucketIdempotencyError as exc:
                if exc.code != 'confirmed_delete_source_changed' or not existing:
                    raise
                current_source = _hold_grow_relation_receipt(manager,plan['keys']['relation'],relation,
                    source,guard)
                if not guard or current_source['non_relation_hash'] != guard.get('non_relation_hash'):
                    raise BucketIdempotencyError('confirmed_delete_source_changed')
        receipt = manager.relation_store.commit(lambda inv: plan_mutation(inv, relation['source'],
            add=relation['add'], origin='inferred'), operation_key=plan['keys']['relation'],
            request_digest=related_digest(mutation_request(relation['source'], add=relation['add'],
                remove=relation['remove'], origin=relation['origin'])))
        if source in relation['add']:
            current_source = _hold_grow_relation_receipt(manager,plan['keys']['relation'],relation,
                source,guard)
            manager.admit_delayed_effect(source,current_source,'hold_grow_related_receipt')
            parent = manager._trace_fence(context)['plan']
            parent['feel_source_guard'] = current_source
            manager._trace_checkpoint(context,plan=parent)
        return receipt


async def _hold_grow_embedding(manager, context, ordinal):
    request = manager._trace_fence(context)
    plan, resolutions = request['plan']['items'][ordinal]['plan'], request['resolutions']
    current = resolutions['items'][str(ordinal)]
    engine = manager.embedding_engine
    if plan['reused']:
        return {'outcome': 'not_requested'}
    receipt = engine.trace_embedding_receipt(plan['keys']['embedding']) if engine else None
    if receipt is not None:
        return receipt
    if not engine or not engine.enabled:
        return {'outcome': 'disabled'}
    if 'embedding_candidate' not in current:
        try:
            current['embedding_candidate'] = await engine._generate_embedding(plan['embedding_input'], model=plan['embedding_model'])
        except Exception:
            current['embedding_candidate'] = []
        manager._trace_checkpoint(context, resolutions=resolutions)
    with bucket_write_scope(manager.base_dir):
        manager._trace_fence(context)
        candidate = current['embedding_candidate']
        if not candidate:
            return {'outcome': 'failed'}
        path = manager._find_bucket_file(plan['target'])
        post = frontmatter.load(path) if path else None
        if (post is None or _is_sealed({'metadata': post.metadata}) or post.content != plan['embedding_input']
                or post.get('last_active') != plan['logical_time']):
            return {'outcome': 'superseded_before_refresh'}
        manager.admit_delayed_effect(plan['target'],current.get('source_guard'),'hold_grow_embedding')
        return engine.store_trace_embedding(plan['keys']['embedding'], plan['target'], candidate,
            hashlib.sha256(plan['embedding_input'].encode()).hexdigest(), plan['embedding_model'], plan['logical_time'],
            expected_source=current.get('source_guard'))


def _hold_grow_relation_receipt(manager, key, relation, bucket_id, expected_source):
    """Resolve only the exact journaled relation effect before refreshing a guard."""
    try:
        current = manager.delete_admission.published_lineage(bucket_id,expected_source)
    except DeleteAdmissionError as exc:
        raise BucketIdempotencyError(exc.code) from exc
    journal = manager.relation_store.lookup(key)
    expected_request = mutation_request(relation['source'], add=relation['add'],
        remove=relation['remove'], origin=relation['origin'])
    if (journal is None or journal.get('key') != key
            or journal['plan']['root'] != str(Path(manager.base_dir).resolve())
            or journal['plan']['kind'] != 'relation'
            or journal['plan']['request'] != expected_request
            or bucket_id not in (expected_request['source'], *expected_request['add'], *expected_request['remove'])
            or journal['request_digest'] not in (related_digest(expected_request), related_digest(relation))):
        raise BucketIdempotencyError('operation_relation_receipt_conflict')
    endpoint = scan_relation_store(manager.base_dir).endpoint(bucket_id)
    if not any(
            step['id']==bucket_id and step.get('path')==endpoint.path
            and step.get('after')==endpoint.related.fingerprint
            and (journal['status']=='complete' or index < journal['progress'])
            for index,step in enumerate(journal['plan']['steps'])):
        raise BucketIdempotencyError('operation_relation_receipt_conflict')
    try:
        return manager.delete_admission.published_lineage(bucket_id,expected_source)
    except DeleteAdmissionError as exc:
        raise BucketIdempotencyError(exc.code) from exc


def _hold_grow_admit_effect(manager, context, ordinal, kind):
    """Reconcile this request's publish-before-guard-checkpoint effects only."""
    request = manager._trace_fence(context)
    plan = request['plan']['items'][ordinal]['plan']
    current = request['resolutions']['items'][str(ordinal)]
    guard = current.get('source_guard')
    try:
        return manager.admit_delayed_effect(plan['target'],guard,kind)
    except BucketIdempotencyError as exc:
        if exc.code != 'confirmed_delete_source_changed':
            raise
        own_effect = False
        journal = manager.relation_store.lookup(plan['keys']['relation'])
        relation = {'source':plan['target'],'add':[pair[0] for pair in current.get('selection',[])],
                    'remove':[],'origin':'inferred'}
        if journal:
            fresh = _hold_grow_relation_receipt(manager,plan['keys']['relation'],relation,plan['target'],guard)
            own_effect = fresh['non_relation_hash'] == (guard or {}).get('non_relation_hash')
        date = plan['values'].get('trigger_date','')
        if not own_effect and not plan['reused'] and date:
            try:
                fresh = manager.delete_admission.published_lineage(plan['target'],guard)
            except DeleteAdmissionError as lineage_exc:
                raise BucketIdempotencyError(lineage_exc.code) from lineage_exc
            post = frontmatter.load(manager._find_bucket_file(plan['target']))
            marker = manager._operation_marker(post,plan['keys']['trigger'])
            _, expected_digest = manager._canonical_import_payload({'kwargs':{'trigger_date':date,'trigger_last_seen':''}})
            omitted = {'related_buckets','last_active','updated_at','trigger_date','trigger_last_seen','_ob_import_operations'}
            own_effect = (marker is not None and marker.get('operation_kind')=='update'
                and marker['payload_digest']==expected_digest
                and post.get('trigger_date')==date and post.get('trigger_last_seen')==''
                and post.content==plan['embedding_input']
                and {k:v for k,v in post.metadata.items() if k not in omitted}
                    == {k:v for k,v in plan['metadata'].items() if k not in omitted})
        if not own_effect:
            raise exc
        manager.admit_delayed_effect(plan['target'],fresh,'hold_grow_published_effect')
        request['resolutions']['items'][str(ordinal)]['source_guard'] = fresh
        manager._trace_checkpoint(context,resolutions=request['resolutions'])
        return fresh


@guarded_async_mutation('hold_grow_item_execute')
async def _execute_hold_grow_item(manager, context, ordinal):
    request = manager._trace_fence(context)
    mode, payload = request['plan']['mode'], request['payload']
    plan = request['plan']['items'][ordinal]['plan']
    tail = ['trigger', 'related'] if mode in ('feel', 'pinned') else ['related', 'trigger']
    steps = ['memory', 'delta', 'embedding']
    if mode in ('feel', 'pinned'):
        steps += ['emotion', *tail]
    else:
        steps += tail + ['emotion']
    steps += ['source']
    for step in steps:
        request = manager._trace_fence(context)
        resolutions = request['resolutions']
        current = resolutions.setdefault('items', {}).setdefault(str(ordinal), {})
        if step in current:
            continue
        if step == 'memory':
            receipt = manager.commit_hold_grow_memory(context, ordinal)
        elif step == 'delta':
            receipt = manager.commit_hold_grow_delta(context, ordinal)
        elif step == 'embedding':
            with bucket_write_scope(manager.base_dir):
                source_guard = current.get('source_guard') or manager.delete_admission.capture(plan['target'])
                manager.admit_delayed_effect(plan['target'],source_guard,'hold_grow_effect')
                resolutions.setdefault('items',{}).setdefault(str(ordinal),{}).setdefault('source_guard',source_guard)
                manager._trace_checkpoint(context,resolutions=resolutions)
            receipt = await _hold_grow_embedding(manager, context, ordinal)
        elif step == 'trigger':
            with bucket_write_scope(manager.base_dir):
                _hold_grow_admit_effect(manager,context,ordinal,'hold_grow_trigger')
                receipt = manager.commit_hold_grow_trigger(context, ordinal)
                latest = manager._trace_fence(context)['resolutions']
                latest['items'][str(ordinal)]['source_guard'] = manager.delete_admission.capture(plan['target'])
                manager._trace_checkpoint(context,resolutions=latest)
        elif step == 'related':
            with bucket_write_scope(manager.base_dir):
                _hold_grow_admit_effect(manager,context,ordinal,'hold_grow_related')
                receipt = {'status': 'not_requested'} if plan['reused'] else _hold_grow_related(manager, context, ordinal)
                latest = manager._trace_fence(context)['resolutions']
                latest['items'][str(ordinal)]['source_guard'] = manager.delete_admission.capture(plan['target'])
                manager._trace_checkpoint(context,resolutions=latest)
        elif step == 'emotion':
            receipt = {'outcome': 'not_requested'}
            if payload['kind'] == 'hold' and payload['valence'] != -1 and payload['arousal'] != -1:
                entry = dict(timestamp=plan['logical_time'], valence=round(payload['valence'], 3),
                             arousal=round(payload['arousal'], 3), source='hold', bucket_id=plan['target'])
                _record_emotion_snapshot(payload['valence'], payload['arousal'], 'hold', plan['target'],
                    _expected_entry=entry, _strict=True, _effect_key=plan['keys']['emotion'], _s4_context=context,
                    _expected_source=current.get('source_guard'))
                receipt = {'outcome': 'applied'}
        else:
            receipt = {'status': 'not_requested'}
            if mode == 'feel' and payload['source_bucket']:
                receipt = manager.mark_feel_source(payload['source_bucket'],
                    model_valence=plan['values']['valence'] if payload['valence'] != -1 else None,
                    _expected_source=request['plan'].get('feel_source_guard'),
                    _s4_effect={'context': context, 'key': plan['keys']['source'], 'logical_time': plan['logical_time']})
                if receipt['status'] != 'marked':
                    raise BucketIdempotencyError('operation_source_' + receipt['status'])
        # Selection/candidate helpers may have checkpointed extra resolutions.
        resolutions = manager._trace_fence(context)['resolutions']
        resolutions.setdefault('items', {}).setdefault(str(ordinal), {})[step] = receipt
        manager._trace_checkpoint(context, resolutions=resolutions)


def _hold_grow_item_response(payload, parent, ordinal, resolutions):
    entry = parent['items'][ordinal]
    plan, mode = entry['plan'], parent['mode']
    target, reused, name = plan['target'], plan['reused'], plan['result_name']
    values, meta = plan['values'], plan['metadata']
    conflict = entry.get('conflict', parent.get('conflict', ''))
    if mode == 'digest':
        result = (f'📎复用了已匹配到的相同内容桶，未新建：{name} | bucket_id={target} reused=true' if reused else
                  f"📝新建：{entry['item'].get('name') or _canonical_body_name(entry['item']['content'])} | bucket_id={target} reused=false")
        if entry['item'].get('_metadata_failure'):
            result += '\n自动打标失败；原因=parse_error；已使用默认 metadata'
        return result
    if mode == 'short':
        action = '复用了已匹配到的相同内容桶，未新建' if reused else '新建'
        analysis = parent['analysis']
        result = (f"{action} → {name} | bucket_id={target} reused={str(reused).lower()} | "
                  f"{','.join(analysis.get('domain', []))} V{analysis.get('valence', .5):.1f}/A{analysis.get('arousal', .3):.1f}")
    elif reused:
        ignored = [*plan['ignored_fields'], *(['source_bucket'] if payload['source_bucket'] else [])]
        result = (f"复用了已匹配到的相同内容桶，未新建：{name} {','.join(values['domain'])}\n"
                  f"bucket_id={target} reused=true written_fields={plan['written_fields']} ignored_fields={ignored}")
    else:
        result = (f"新建 {target} | {meta['name']} | importance={meta['importance']} | "
                  f"tags=[{', '.join(meta['tags'])}] | domain=[{', '.join(meta['domain'])}]")
        if mode == 'feel':
            result = '🫧feel→' + result
        elif mode == 'pinned':
            result = '📌' + result
        else:
            result += f"\nbucket_id={target} reused=false written_fields={plan['written_fields']} ignored_fields={plan['ignored_fields']}"
    if mode not in ('short',) and not reused and parent.get('similarity'):
        result += '\nsimilarity: ' + parent['similarity']
    if conflict:
        result += '\nconflict: ' + conflict
    if parent.get('analysis_failure'):
        result += f"\n自动打标失败；原因={parent['analysis_failure']}；已使用默认 metadata"
    if mode == 'feel' and payload['source_bucket']:
        result += '\n' + _format_hold_feel_source_receipt(target, payload['source_bucket'], 'none')
    return result


@mcp.tool()
async def hold(
    content: str,
    tags: str = "",
    importance: int = 5,
    pinned: bool = False,
    feel: bool = False,
    source_bucket: str = "",
    valence: Annotated[float, Field(description="-1 means unspecified and uses analysis fallback; 0.0-1.0 sets the stored valence.")] = -1,
    arousal: Annotated[float, Field(description="-1 means unspecified and uses analysis fallback; 0.0-1.0 sets the stored arousal.")] = -1,
    trigger_date: str = "",
    supersedes_id: str = "",
    provenance_kind: Annotated[str, Field(description="Optional body provenance classification: unknown, summary, inference, or system. Blank leaves normal writer defaults in effect.")] = "",
    operation_id: Annotated[str, Field(strict=True, min_length=1, max_length=128,
        description="Opaque retry identity shared with trace/grow. Preserved verbatim; keyed supersedes_id is unsupported. None preserves legacy behavior.")] | None = None,
) -> str:
    """存储单条记忆并自动打标；匹配到规范化正文相同的可复用桶时复用，未新建，不做语义合并。tags逗号分隔,importance 1-10。pinned=True创建永久钉选桶。feel=True存储你的第一人称感受(不参与普通浮现)。source_bucket=被消化的记忆桶ID(feel模式下,标记源记忆为已消化)。supersedes_id 是同一桶原地演化，不新建桶。"""
    if operation_id is not None:
        return await _hold_grow_keyed(operation_id, 'hold', {
            key: value for key, value in locals().items() if key != 'operation_id'})
    await decay_engine.ensure_started()

    # --- Input validation / 输入校验 ---
    if not content or not content.strip():
        return "内容为空，无法存储。"
    if not 1 <= importance <= 10:
        return "importance must be within 1-10."
    for field_name, value in (("valence", valence), ("arousal", arousal)):
        if value != -1 and not 0 <= value <= 1:
            return f"{field_name} must be -1 or within 0.0-1.0."
    try:
        explicit_provenance_kind = _parse_explicit_provenance_kind(provenance_kind)
    except ValueError as exc:
        return str(exc)
    content = _apply_display_aliases(content)
    target_id = supersedes_id.strip()
    if target_id:
        target = await bucket_mgr.get(target_id)
        metadata = target.get("metadata", {}) if isinstance(target, dict) else {}
        if not isinstance(metadata, dict):
            return f"supersedes target not found or invalid: {target_id}"
        try:
            sealed = int(metadata.get("sealed", 0) or 0) == 1
        except (TypeError, ValueError):
            sealed = True
        if metadata.get("type") != "dynamic" or sealed:
            return f"supersedes target not found or invalid: {target_id}"
        if metadata.get("pinned") or metadata.get("protected"):
            return f"supersedes target not found or invalid: {target_id}"
        try:
            update_kwargs = {"content": content}
            if explicit_provenance_kind is not None:
                update_kwargs["provenance_kind"] = explicit_provenance_kind
            updated = await bucket_mgr.update(target_id, **update_kwargs)
        except Exception as exc:
            logger.warning(f"Explicit supersession failed for {target_id}: {exc}")
            return f"supersedes update failed: {target_id}"
        if not updated:
            return f"supersedes update failed: {target_id}"
        return f"fact evolved in place: {target_id}"
    similarity_notice = await _similarity_doorbell(content)
    conflict_warning = await _detect_conflict_warning(content)
    try:
        trigger_date = _parse_optional_date(trigger_date, "trigger_date") or ""
    except ValueError as exc:
        return str(exc)
    should_record_emotion = 0 <= valence <= 1 and 0 <= arousal <= 1

    extra_tags = [t.strip() for t in tags.split(",") if t.strip()]

    # --- Feel mode: store as feel type, minimal metadata ---
    # --- Feel 模式：存为 feel 类型，最少元数据 ---
    if feel:
        source_id = source_bucket.strip() if source_bucket else ""
        if source_id:
            preview = bucket_mgr.preview_feel_source(source_id)
            if preview["status"] != "valid":
                error = preview["status"]
                return (
                    f"feel 未创建；source 校验失败（{error}）。\n"
                    + _format_hold_feel_source_receipt("", source_id, error)
                )
        # Feel valence/arousal = model's own perspective
        feel_valence = valence if 0 <= valence <= 1 else 0.5
        feel_arousal = arousal if 0 <= arousal <= 1 else 0.3
        feel_failure = ""
        try:
            feel_analysis = await dehydrator.analyze(content)
        except Exception as e:
            logger.warning(f"Feel auto-tagging failed, using defaults: {e}")
            feel_failure = _provider_failure_category(e)
            feel_analysis = {"tags": []}
        feel_name = _canonical_body_name(content.strip().replace("\n", " ")) or None
        bucket_id = await bucket_mgr.create(
            content=content,
            tags=feel_analysis.get("tags", []),
            importance=5,
            domain=[],
            valence=feel_valence,
            arousal=feel_arousal,
            name=feel_name,
            bucket_type="feel",
            provenance_kind=(explicit_provenance_kind or "inference"),
        )
        if should_record_emotion:
            _record_emotion_snapshot(valence, arousal, "hold", bucket_id)
        if trigger_date:
            await bucket_mgr.update(bucket_id, trigger_date=trigger_date, trigger_last_seen="")
        await _auto_link_related(bucket_id)
        # --- Mark source memory as digested + store model's valence perspective ---
        # --- 标记源记忆为已消化 + 存储模型视角的 valence ---
        source_error = "none"
        if source_id:
            try:
                outcome = bucket_mgr.mark_feel_source(
                    source_id, model_valence=feel_valence if 0 <= valence <= 1 else None,
                )
                source_error = "none" if outcome["status"] == "marked" else outcome["status"]
            except MaintenanceWriteError:
                source_error = "write_failed"
            except Exception:
                source_error = "write_outcome_unknown"
            try:
                created_text = await _format_hold_created(bucket_id)
            except Exception:
                created_text = f"新建 {bucket_id}"
            response = f"🫧feel→{created_text}"
        else:
            response = f"🫧feel→{await _format_hold_created(bucket_id)}"
        if similarity_notice:
            response += f"\nsimilarity: {similarity_notice}"
        if conflict_warning:
            response += f"\nconflict: {conflict_warning}"
        if feel_failure:
            response += f"\n自动打标失败；原因={feel_failure}；已使用默认 metadata"
        if source_id:
            if source_error == "write_outcome_unknown":
                response += "\nfeel 已创建；source 标记结果无法确认，feel 保留。"
            elif source_error != "none":
                response += f"\nfeel 已创建；source 标记失败（{source_error}），feel 保留。"
            response += "\n" + _format_hold_feel_source_receipt(bucket_id, source_id, source_error)
        return response

    # --- Step 1: auto-tagging / 自动打标 ---
    analysis_failure = ""
    try:
        analysis = await dehydrator.analyze(content)
    except Exception as e:
        logger.warning(f"Auto-tagging failed, using defaults / 自动打标失败: {e}")
        analysis_failure = _provider_failure_category(e)
        analysis = {
            "domain": ["未分类"], "valence": 0.5, "arousal": 0.3,
            "tags": [], "suggested_name": "", "todos": [],
        }

    domain = analysis["domain"]
    auto_valence = analysis["valence"]
    auto_arousal = analysis["arousal"]
    auto_tags = analysis["tags"]
    suggested_name = analysis.get("suggested_name", "")
    analysis_todos = _canonical_todos(analysis.get("todos"))

    # --- User-supplied valence/arousal takes priority over analyze() result ---
    # --- 用户显式传入的 valence/arousal 优先，analyze() 结果作为 fallback ---
    final_valence = valence if 0 <= valence <= 1 else auto_valence
    final_arousal = arousal if 0 <= arousal <= 1 else auto_arousal

    all_tags = list(dict.fromkeys(auto_tags + extra_tags))

    # --- Pinned buckets bypass merge and are created directly in permanent dir ---
    # --- 钉选桶跳过合并，直接新建到 permanent 目录 ---
    if pinned:
        values = dict(
            content=content,
            tags=all_tags,
            importance=10,
            domain=domain,
            valence=final_valence,
            arousal=final_arousal,
            name=suggested_name or _canonical_body_name(content),
            bucket_type="permanent",
            pinned=True,
            todos=analysis_todos,
            todo_provenance=automatic_todo_provenance(analysis_todos),
            provenance_kind=explicit_provenance_kind,
        )
        bucket_id = await _await_legacy_post_effects(
            _run_legacy_pinned, copy.deepcopy(values),
            (valence, arousal) if should_record_emotion else None, trigger_date)
        response = f"📌{await _format_hold_created(bucket_id)}"
        if similarity_notice:
            response += f"\nsimilarity: {similarity_notice}"
        if conflict_warning:
            response += f"\nconflict: {conflict_warning}"
        if analysis_failure:
            response += f"\n自动打标失败；原因={analysis_failure}；已使用默认 metadata"
        return response

    # --- Step 2: merge or create / 合并或新建 ---
    source_ids: list[str] = []
    outcome: dict = {}
    try:
        result_name, is_merged = await _merge_or_create(
            content=content,
            tags=all_tags,
            importance=importance,
            domain=domain,
            valence=final_valence,
            arousal=final_arousal,
            name=suggested_name,
            trigger_date=trigger_date,
            todos=analysis_todos,
            provenance_kind=explicit_provenance_kind,
            source_id_out=source_ids,
            outcome_out=outcome,
        )
    except (ValueError, RuntimeError) as exc:
        return str(exc)
    if should_record_emotion:
        _record_emotion_snapshot(valence, arousal, "hold", source_ids[0])

    if is_merged:
        ignored = [*outcome["ignored_fields"], *(["source_bucket"] if source_bucket else [])]
        response = (
            f"复用了已匹配到的相同内容桶，未新建：{result_name} {','.join(domain)}\n"
            f"bucket_id={outcome['bucket_id']} reused=true "
            f"written_fields={outcome['written_fields']} ignored_fields={ignored}"
        )
    else:
        response = await _format_hold_created(result_name)
        response += f"\nbucket_id={outcome['bucket_id']} reused=false written_fields={outcome['written_fields']} ignored_fields={outcome['ignored_fields']}"
        if similarity_notice:
            response += f"\nsimilarity: {similarity_notice}"
    if conflict_warning:
        response += f"\nconflict: {conflict_warning}"
    if analysis_failure:
        response += f"\n自动打标失败；原因={analysis_failure}；已使用默认 metadata"
    return response


# =============================================================
# Tool 3: grow — Grow, fragments become memories
# 工具 3：grow — 生长，一天的碎片长成记忆
# =============================================================
@mcp.tool()
async def grow(content: str, operation_id: Annotated[str, Field(strict=True, min_length=1, max_length=128,
    description="Opaque retry identity shared with trace/hold. Frozen ordered digest items resume without another digest. None preserves legacy behavior.")] | None = None) -> str:
    """日记归档,自动拆分为多桶。短内容(<30字)走快速路径。"""
    if operation_id is not None:
        return await _hold_grow_keyed(operation_id, 'grow', {'content': content})
    await decay_engine.ensure_started()

    if not content or not content.strip():
        return "内容为空，无法整理。"
    content = _apply_display_aliases(content)

    # --- Short content fast path: skip digest, use hold logic directly ---
    # --- 短内容快速路径：跳过 digest 拆分，直接走 hold 逻辑省一次 API ---
    # For very short inputs (like "1"), calling digest is wasteful:
    # it sends the full DIGEST_PROMPT (~800 tokens) to DeepSeek for nothing.
    # Instead, run analyze + create directly.
    if len(content.strip()) < 30:
        logger.info(f"grow short-content fast path: {len(content.strip())} chars")
        conflict_warning = await _detect_conflict_warning(content)
        metadata_failure = ""
        try:
            analysis = await dehydrator.analyze(content)
        except Exception as e:
            logger.warning(f"Fast-path analyze failed / 快速路径打标失败: {e}")
            metadata_failure = _provider_failure_category(e)
            analysis = {
                "domain": ["未分类"], "valence": 0.5, "arousal": 0.3,
                "tags": [], "suggested_name": "",
            }
        outcome: dict = {}
        try:
            result_name, is_merged = await _merge_or_create(
                content=content.strip(),
                tags=analysis.get("tags", []),
                importance=analysis.get("importance", 5) if isinstance(analysis.get("importance"), int) else 5,
                domain=analysis.get("domain", ["未分类"]),
                valence=analysis.get("valence", 0.5),
                arousal=analysis.get("arousal", 0.3),
                name=analysis.get("suggested_name", ""),
                todos=_canonical_todos(analysis.get("todos")),
                outcome_out=outcome,
            )
        except Exception as exc:
            logger.exception("Fast-path grow persistence failed")
            return "记忆写入失败。 reason=persistence_error"
        action = "复用了已匹配到的相同内容桶，未新建" if is_merged else "新建"
        reused = "true" if is_merged else "false"
        response = (
            f"{action} → {result_name} | bucket_id={outcome['bucket_id']} "
            f"reused={reused} | {','.join(analysis.get('domain', []))} "
            f"V{analysis.get('valence', 0.5):.1f}/A{analysis.get('arousal', 0.3):.1f}"
        )
        if conflict_warning:
            response += f"\nconflict: {conflict_warning}"
        if metadata_failure:
            response += f"\n自动打标失败；原因={metadata_failure}；已使用默认 metadata"
        return response

    # --- Step 1: let API split and organize / 让 API 拆分整理 ---
    try:
        items = await dehydrator.digest(content)
    except Exception as e:
        logger.error("Diary digest failed (%s)", type(e).__name__, exc_info=True)
        return f"日记整理失败。 reason={_provider_failure_category(e)}"

    if not items:
        return "日记整理失败。 reason=parse_error"

    results = []
    conflicts = []
    created = 0
    reused = 0

    # --- Step 2: merge or create each item (with per-item error handling) ---
    # --- 逐条合并或新建（单条失败不影响其他）---
    for item in items:
        try:
            conflict_warning = await _detect_conflict_warning(item["content"])
            outcome: dict = {}
            result_name, is_merged = await _merge_or_create(
                content=item["content"],
                tags=item.get("tags", []),
                importance=item.get("importance", 5),
                domain=item.get("domain", ["未分类"]),
                valence=item.get("valence", 0.5),
                arousal=item.get("arousal", 0.3),
                name=item.get("name") or _canonical_body_name(item["content"]),
                todos=_canonical_todos(item.get("todos")),
                provenance_kind="summary",
                outcome_out=outcome,
            )

            if is_merged:
                results.append(
                    f"📎复用了已匹配到的相同内容桶，未新建：{result_name} | "
                    f"bucket_id={outcome['bucket_id']} reused=true"
                )
                reused += 1
            else:
                results.append(
                    f"📝新建：{item.get('name') or _canonical_body_name(item['content'])} | "
                    f"bucket_id={outcome['bucket_id']} reused=false"
                )
                created += 1
            if item.get("_metadata_failure"):
                results.append("自动打标失败；原因=parse_error；已使用默认 metadata")
            if conflict_warning:
                conflicts.append(f"{item.get('name', result_name)}: {conflict_warning}")
        except Exception as e:
            logger.warning(
                f"Failed to process diary item / 日记条目处理失败: "
                f"{item.get('name', '?')}: {e}"
            )
            results.append(f"⚠️{item.get('name', '?')} reason=persistence_error")

    response = f"{len(items)}条|新建{created}/复用{reused}\n" + "\n".join(results)
    if conflicts:
        response += "\nconflict: " + "；".join(conflicts)
    return response


@mcp.tool()
async def get_letter(letter_id: int, include_sealed: bool = False) -> str:
    """Read one handoff letter by exact id; sealed letters require explicit opt-in."""
    try:
        letter_id = int(letter_id)
    except (TypeError, ValueError):
        return "Please provide a valid letter_id."
    if letter_id < 1:
        return "Please provide a valid letter_id."
    letter = bucket_mgr.get_letter(letter_id, include_sealed=include_sealed)
    if not letter:
        return _with_response_seal(f"letter_id not found: {letter_id}")
    body = (
        "=== 信箱 ===\n"
        f"[letter_id:{letter.get('id')}] "
        f"created_at:{letter.get('created_at')} "
        f"session_id:{letter.get('session_id')}\n"
        f"{letter.get('content', '')}"
    )
    return _with_response_seal(body)


@mcp.tool()
async def list_notes(
    limit: Annotated[int, Field(ge=1, le=100)] = 20,
    include_sealed: bool = False,
) -> str:
    """List Ting-note history, newest first; sealed notes require explicit opt-in."""
    try:
        notes = bucket_mgr.list_notes(limit=int(limit), include_sealed=include_sealed)
    except (TypeError, ValueError):
        return "limit must be an integer between 1 and 100."
    if not notes:
        return _with_response_seal("=== 婷留言历史 ===\n（暂无可见留言）")
    parts = ["=== 婷留言历史 ==="]
    for note in notes:
        parts.append(
            f"[note_id:{note['note_id']}] created_at:{note['created_at']} "
            f"author:{note['author']} via:{note['via']} "
            f"delivery:{_note_delivery_state(note)} "
            f"read:{'yes' if note.get('read_at') else 'no'} "
            f"open_at:{note.get('open_at') or '-'}\n"
            f"{_format_note_preview(note.get('text', ''))}"
        )
    return _with_response_seal("\n---\n".join(parts))


@mcp.tool()
async def get_note(note_id: int, include_sealed: bool = False) -> str:
    """Read one Ting note by exact ID; a successful full read records read/delivery state."""
    try:
        note_id = int(note_id)
    except (TypeError, ValueError):
        return "Please provide a valid note_id."
    if note_id < 1:
        return "Please provide a valid note_id."
    note = bucket_mgr.get_note(note_id, include_sealed=include_sealed)
    if not note:
        return _with_response_seal(f"note_id not found: {note_id}")
    bucket_mgr.mark_note_read(note_id)
    body = (
        "=== 婷留言 ===\n"
        f"[note_id:{note['note_id']}] created_at:{note['created_at']} "
        f"author:{note['author']} via:{note['via']} "
        f"open_at:{note.get('open_at') or '-'}\n"
        f"{note['text']}"
    )
    return _with_response_seal(body)


@mcp.tool()
async def leave_note(
    text: str,
    sealed: bool = False,
    open_at: str = "",
) -> str:
    """Record Ting's exact words for a later boot; this creates no memory bucket."""
    if not isinstance(text, str) or not text.strip():
        return "text 不能为空；未创建留言。"
    try:
        normalized_open_at = _parse_note_open_at(open_at)
    except ValueError as exc:
        return f"{exc} 未创建留言。"
    try:
        note_id = bucket_mgr.record_note(
            text,
            author="婷",
            via="mcp",
            sealed=bool(sealed),
            open_at=normalized_open_at,
        )
    except Exception:
        logger.warning("Ting note creation failed")
        return "留言创建失败；未创建留言。"
    if note_id is None:
        return "留言创建失败；未创建留言。"
    return _with_response_seal(
        f"已创建婷留言 note_id:{note_id} preview:{text[:20]}"
    )


@mcp.tool()
async def dismiss_note(
    note_id: int,
    include_sealed: bool = False,
    confirm_token: str = "",
) -> str:
    """Preview, then confirm dismissal of one note without deleting its history."""
    try:
        note_id = int(note_id)
    except (TypeError, ValueError):
        return "Please provide a valid note_id."
    if note_id < 1:
        return "Please provide a valid note_id."
    note = bucket_mgr.get_note(note_id, include_sealed=include_sealed)
    if note is None:
        return _with_response_seal(f"note_id not found: {note_id}")
    if note.get("dismissed_at"):
        return _with_response_seal(f"婷留言 note_id:{note_id} 已 dismiss；历史仍保留。")
    if not (confirm_token or "").strip():
        token = _issue_mutation_confirmation("note.dismiss", note)
        return _with_response_seal(
            f"待确认 dismiss_note：note_id:{note_id} "
            f"preview:{_format_note_preview(note['text'])}\n"
            "确认后停止自动 boot 投递；正文和历史保留，且不标记 delivered/read。\n"
            f"请用相同 note_id、include_sealed 和 confirm_token={token} 再次调用；"
            f"有效期 {_MUTATION_CONFIRM_TTL_SECONDS} 秒。"
        )
    if not _consume_mutation_confirmation("note.dismiss", note, confirm_token):
        return _with_response_seal("dismiss_note 确认无效、已过期或留言状态已变化；请重新预览。")
    if not bucket_mgr.dismiss_note(note_id, expected_note=note):
        return _with_response_seal("dismiss_note 未执行：留言状态已变化；请重新预览。")
    return _with_response_seal(f"已 dismiss 婷留言 note_id:{note_id}；正文和历史保留。")


# =============================================================
# Tool 4: trace — Trace, redraw the outline of a memory
# 工具 4：trace — 描摹，重新勾勒记忆的轮廓
# Also handles deletion (delete=True)
# 同时承接删除功能
# =============================================================
def _todo_terminal_confirmation_payload(plan: dict) -> dict:
    target = plan["target"]
    return {"bucket_id": plan["bucket_id"], "todo_id": plan["todo_id"],
            "target_identity": target.get("id"), "target_text": target["text"],
            "done_at": target.get("done_at"), "dropped_at": target.get("dropped_at"),
            "todo_state_sha256": plan["todo_state_sha256"]}


def _trace_todo_done(bucket_id: str, todo_id: str, confirm_token: str) -> str:
    try:
        if not (confirm_token or "").strip():
            plan = bucket_mgr.preview_todo_completion(bucket_id, todo_id)
            if plan["target"].get("dropped_at"):
                raise ValueError("该 todo 已放弃，不能标记为完成")
            if plan["target"].get("done_at"):
                outcome = {"status": "already_completed", "done_at": plan["target"]["done_at"]}
            else:
                token = _issue_mutation_confirmation("trace.todo_done", _todo_terminal_confirmation_payload(plan))
                return (f"todo completion preview: no changes made; bucket_id={bucket_id} "
                        f"name={plan['bucket_name']}; todo_id={todo_id}; text={plan['target']['text']}; "
                        "current=pending; action=mark completed; bucket resolved will not change.\n"
                        f"confirm_token: {token}")
        else:
            outcome = bucket_mgr.complete_todo(bucket_id, todo_id,
                lambda plan: _consume_mutation_confirmation("trace.todo_done",
                    _todo_terminal_confirmation_payload(plan), confirm_token))
    except (ValueError, BucketWriteLockError, OSError) as exc:
        return f"todo completion rejected: {exc}"
    status = outcome["status"]
    if status == "confirmation_invalid":
        return "todo completion confirmation invalid, expired, used, or stale; preview again."
    if status == "write_failed":
        return "todo completion write failed; no completion committed; token consumed; preview again."
    label = "already completed" if status == "already_completed" else "todo completed"
    return (f"{label}: bucket_id={bucket_id}; todo_id={todo_id}; "
            f"done_at={outcome['done_at']}; bucket resolved unchanged.")


def _trace_todo_drop(bucket_id: str, todo_id: str, confirm_token: str) -> str:
    try:
        if not (confirm_token or "").strip():
            plan = bucket_mgr.preview_todo_drop(bucket_id, todo_id)
            if plan["target"].get("done_at"):
                raise ValueError("该 todo 已完成，不能标记为放弃")
            if plan["target"].get("dropped_at"):
                outcome = {"status": "already_dropped", "dropped_at": plan["target"]["dropped_at"]}
            else:
                token = _issue_mutation_confirmation("trace.todo_drop", _todo_terminal_confirmation_payload(plan))
                return (f"todo drop preview: no changes made; bucket_id={bucket_id} "
                        f"name={plan['bucket_name']}; todo_id={todo_id}; text={plan['target']['text']}; "
                        "current=active; action=放弃该 todo; 不等于已完成；不删除历史；不自动 resolved bucket。\n"
                        f"confirm_token: {token}")
        else:
            outcome = bucket_mgr.drop_todo(bucket_id, todo_id,
                lambda plan: _consume_mutation_confirmation("trace.todo_drop",
                    _todo_terminal_confirmation_payload(plan), confirm_token))
    except (ValueError, BucketWriteLockError, OSError) as exc:
        return f"todo drop rejected: {exc}"
    if outcome["status"] == "confirmation_invalid":
        return "todo drop confirmation invalid, expired, used, or stale; preview again."
    if outcome["status"] == "write_failed":
        return "todo drop write failed; no drop committed; token consumed; preview again."
    label = "already dropped (已放弃)" if outcome["status"] == "already_dropped" else "todo dropped (已放弃)"
    return (f"{label}: bucket_id={bucket_id}; todo_id={todo_id}; "
            f"dropped_at={outcome['dropped_at']}; history preserved; bucket resolved unchanged.")


async def _trace_keyed(operation_id, values):
    """Normalize an ordinary trace and freeze its bucket-dependent choices once."""
    try:
        bucket_mgr.validate_trace_operation_id(operation_id)
        if (values['delete'] or values['merge'] or values['todo_done'] is not None
                or values['todo_drop'] is not None or values['superseded_by'] is not None
                or values['pinned'] != -1 or values['permanent'] != -1 or values['sealed'] != -1
                or values['confirm_token']):
            return 'unsupported_combination: operation_id supports ordinary single-bucket trace only.'
        targets = list(dict.fromkeys(_parse_csv_ids(values['bucket_id'])))
        if len(targets) != 1:
            return 'unsupported_combination: operation_id requires one bucket.'
        if values['importance'] != -1 and not 1 <= values['importance'] <= 10:
            return 'importance must be -1 or within 1-10.'
        for field in ('valence', 'arousal'):
            if values[field] != -1 and not 0 <= values[field] <= 1:
                return f'{field} must be -1 or within 0.0-1.0.'
        for field in ('resolved', 'digested', 'dormant'):
            if values[field] not in (-1, 0, 1):
                return f'{field} must be -1, 0, or 1.'
        if values['todos'] is not None and values['todo_items'] is not None:
            return 'todos 与 todo_items 不能同时使用。'
        existing = bucket_mgr.inspect_trace_request(operation_id)
        normalization = existing['normalization_context'] if existing else {
            'version': 1, 'aliases': list(DISPLAY_ALIASES.items())}

        def aliases(text):
            for source, target in normalization['aliases']:
                text = text.replace(source, target)
            return text

        payload = {key: values[key] for key in (
            'valence', 'arousal', 'importance', 'resolved', 'digested', 'dormant', 'append')}
        for field in ('valence', 'arousal'):
            payload[field] = float(payload[field])
        payload.update(kind='trace', bucket_id=targets[0],
            name=aliases(values['name']), content=aliases(values['content']),
            domain=[aliases(v) for v in _parse_csv_ids(values['domain'])],
            tags=[aliases(v) for v in _parse_csv_ids(values['tags'])],
            related=list(dict.fromkeys(_parse_csv_ids(values['related']))),
            unrelate=list(dict.fromkeys(_parse_csv_ids(values['unrelate']))),
            provenance_kind=_parse_explicit_provenance_kind(values['provenance_kind']))
        if payload['related'] and payload['unrelate']:
            return 'related 与 unrelate 不能同时使用。'
        trigger = values['trigger_date']
        payload['trigger_date'] = ('none' if trigger.strip().lower() == 'none' else
                                   (_parse_optional_date(trigger, 'trigger_date') or ''))
        payload['todos'] = ([aliases(v) for v in _canonical_todos(values['todos'])]
                            if values['todos'] is not None else None)
        payload['todo_items'] = None
        if values['todo_items'] is not None:
            texts, records = _structured_todo_items(values['todo_items'])
            payload['todo_items'] = [{**record, 'text': aliases(text)} for text, record in zip(texts, records)]

        def planner(bucket):
            metadata = bucket['metadata']
            if _is_sealed(bucket):
                raise BucketIdempotencyError('unsupported_combination: sealed target')
            if payload['content'] and metadata.get('protected'):
                raise BucketIdempotencyError('content_protected')
            updates = {}
            for field in ('name', 'domain', 'tags'):
                if payload[field]:
                    updates[field] = payload[field]
            for field in ('valence', 'arousal'):
                if payload[field] != -1:
                    updates[field] = payload[field]
            protected = (payload['importance'] != -1 and (metadata.get('pinned') or metadata.get('protected')))
            if payload['importance'] != -1 and not protected:
                updates['importance'] = payload['importance']
            if payload['todo_items'] is not None:
                records = payload['todo_items']
                updates.update(todos=[r['text'] for r in records], todo_provenance=records,
                               _todo_references_only=True)
            elif payload['todos'] is not None:
                updates['todos'] = payload['todos']
            for field in ('resolved', 'digested', 'dormant'):
                if payload[field] != -1:
                    updates[field] = bool(payload[field])
            if payload['trigger_date']:
                updates.update(trigger_date='' if payload['trigger_date'] == 'none' else payload['trigger_date'],
                               trigger_last_seen='')
            if payload['content']:
                updates['content'] = (f"{bucket['content']}\n\n{payload['content']}"
                    if payload['append'] and bucket['content'] else payload['content'])
                updates['_history_change_type'] = 'append' if payload['append'] else 'replace'
            if payload['provenance_kind'] is not None:
                updates['provenance_kind'] = payload['provenance_kind']
            if 'valence' in updates or 'arousal' in updates:
                updates['emotion_history'] = _append_emotion_history(metadata,
                    updates.get('valence', metadata.get('valence', .5)),
                    updates.get('arousal', metadata.get('arousal', .3)))
            changed = ', '.join(f'{k}={v}' for k, v in updates.items()
                if k not in ('content', '_history_change_type', '_todo_references_only', 'provenance_kind'))
            labels = [changed] if changed else []
            if 'content' in updates:
                labels.append('content=已追加' if payload['append'] else 'content=已替换')
            if payload['provenance_kind'] is not None or 'content' in updates:
                labels.append(f"provenance_kind={normalize_provenance_kind(metadata.get('provenance_kind'))}->"
                              f"{payload['provenance_kind'] or 'unknown'}")
            if 'resolved' in updates:
                labels.append('→ 已沉底，只在关键词触发时重新浮现' if updates['resolved'] else '→ 已重新激活，将参与浮现排序')
            if 'digested' in updates:
                labels.append('→ 已隐藏，保留但不再浮现' if updates['digested'] else '→ 已取消隐藏，重新参与浮现')
            if protected:
                labels.append('importance 未修改：受到 pinned/protected protection，importance 锁定为 10')
            response = (f"已修改记忆桶 {payload['bucket_id']}: {', '.join(labels)}"
                        if labels or payload['related'] or payload['unrelate'] else '没有任何字段需要修改。')
            return updates, response

        return await bucket_mgr.execute_trace_request(operation_id, payload, normalization, planner)
    except (BucketIdempotencyError, RelatedError, ValueError) as exc:
        return str(exc)


@_guard_todo_drop_presence
@mcp.tool()
async def trace(
    bucket_id: str,
    name: Annotated[str, Field(description="An empty string leaves the bucket name unchanged. Batch trace rejects a non-empty name.")] = "",
    domain: str = "",
    valence: Annotated[float, Field(description="-1 means unspecified and leaves valence unchanged; 0.0-1.0 sets valence.")] = -1,
    arousal: Annotated[float, Field(description="-1 means unspecified and leaves arousal unchanged; 0.0-1.0 sets arousal.")] = -1,
    importance: Annotated[int, Field(description="-1 means unchanged; 1-10 sets the stored importance.")] = -1,
    tags: str = "",
    todos: str | list[str] | None = None,
    todo_items: Annotated[
        list[TodoItem] | None,
        Field(
            description=(
                "Optional structured todo entries with text, said_by, said_at, "
                "source_bucket, and optional id referencing this bucket's existing "
                "todo identity (including explicit text rewrites). New IDs are server-generated. "
                "Mutually exclusive with legacy todos."
            )
        ),
    ] = None,
    resolved: Annotated[int, Field(description="-1 means unchanged; 0 means False; 1 means True.")] = -1,
    pinned: Annotated[int, Field(description="-1 means unchanged; 0 means False (unpinning a pinned bucket requires confirm_token); 1 means True.")] = -1,
    permanent: Annotated[int, Field(description="-1 leaves lifecycle type unchanged; 0 changes an unpinned permanent bucket to dynamic with confirmation; 1 changes it to permanent. pinned always implies permanent.")] = -1,
    digested: Annotated[int, Field(description="-1 means unchanged; 0 means False; 1 means True.")] = -1,
    dormant: Annotated[int, Field(description="-1 means unchanged; 0 means False and explicitly wakes; 1 means True and marks dormant.")] = -1,
    sealed: Annotated[int, Field(description="-1 means unchanged; 0 means unsealed; 1 means sealed.")] = -1,
    content: Annotated[str, Field(description="An empty string leaves content unchanged. Batch trace rejects non-empty content.")] = "",
    provenance_kind: Annotated[str, Field(description="Optional body provenance classification: unknown, summary, inference, or system. Batch trace rejects it.")] = "",
    related: str = "",
    unrelate: Annotated[str, Field(description="Comma-separated related bucket IDs to remove bidirectionally. Cannot be combined with related.")] = "",
    superseded_by: str | None = None,
    merge: str = "",
    append: bool = False,
    trigger_date: str = "",
    delete: bool = False,
    confirm_token: Annotated[str, Field(description="Two-stage confirmation for delete (including batch), merge, unpinning an already pinned bucket, permanent-to-dynamic conversion, todo_done and todo_drop. First call without a token to preview; then return the issued short-lived, one-shot token with the same operation and plan. Expired, used or mismatched tokens are rejected; a token does not bypass protection checks.")] = "",
    todo_done: Annotated[str | None, Field(description="Use only when Ting explicitly says the task is completed. Complete one stable todo ID using preview then confirm_token. Call alone with bucket_id; never resolves the bucket; dropped todos cannot be completed.")] = None,
    todo_drop: Annotated[str | None, Field(description="Use only when Ting explicitly cancels, abandons, or says the task will not be done; never infer abandonment from age, importance, or inactivity. Drop one stable todo ID using preview then confirm_token. Only bucket_id, todo_drop, confirm_token may be supplied. Preserves history, is not completion, never resolves the bucket; completed todos cannot be dropped.")] = None,
    operation_id: Annotated[str, Field(strict=True, min_length=1, max_length=128,
        description="Opaque caller retry identity for ordinary single-bucket trace. Preserved verbatim; reuse only with the same payload. None preserves legacy behavior.")] | None = None,
) -> str:
    # MCP schema note: related and superseded_by stay in the signature for relations.
    """Mixed memory operation: metadata/content, relations, merge, seal, delete, and confirmed todo completion/abandonment driven by Ting's explicit intent; no MCP undo command."""

    if operation_id is not None:
        return await _trace_keyed(operation_id, {
            key: value for key, value in locals().items() if key != 'operation_id'
        })

    if not bucket_id or not bucket_id.strip():
        return "请提供有效的 bucket_id。"

    if todo_done is not None or todo_drop is not None:
        action = "todo_drop" if todo_drop is not None else "todo_done"
        if (name or domain or valence != -1 or arousal != -1 or importance != -1 or
                tags or todos is not None or todo_items is not None or resolved != -1 or
                pinned != -1 or permanent != -1 or digested != -1 or dormant != -1 or
                sealed != -1 or content or provenance_kind or related or unrelate or
                superseded_by is not None or merge or append or trigger_date or delete or
                (todo_done is not None and todo_drop is not None)):
            return f"{action} must be called alone with bucket_id, {action}, and optional confirm_token."
        targets = list(dict.fromkeys(_parse_csv_ids(bucket_id)))
        if len(targets) != 1:
            return f"{action} requires exactly one bucket; batch mutation is not supported."
        if todo_drop is not None:
            return _trace_todo_drop(targets[0], todo_drop, confirm_token)
        return _trace_todo_done(targets[0], todo_done, confirm_token)

    if importance != -1 and not 1 <= importance <= 10:
        return "importance must be -1 or within 1-10."
    for field_name, value in (("valence", valence), ("arousal", arousal)):
        if value != -1 and not 0 <= value <= 1:
            return f"{field_name} must be -1 or within 0.0-1.0."
    for field_name, value in (("resolved", resolved), ("pinned", pinned),
                              ("permanent", permanent), ("digested", digested),
                              ("dormant", dormant), ("sealed", sealed)):
        if value not in (-1, 0, 1):
            return f"{field_name} must be -1, 0, or 1."

    if todos is not None and todo_items is not None:
        return "todos 与 todo_items 不能同时使用。"
    try:
        explicit_provenance_kind = _parse_explicit_provenance_kind(provenance_kind)
    except ValueError as exc:
        return str(exc)
    structured_todos = None
    structured_provenance = None
    if todo_items is not None:
        try:
            structured_todos, structured_provenance = _structured_todo_items(todo_items)
        except ValueError as exc:
            return str(exc)

    bucket_ids = list(dict.fromkeys(_parse_csv_ids(bucket_id)))
    if not bucket_ids:
        return "请提供有效的 bucket_id。"
    if len(bucket_ids) > 1:
        if name or content:
            return "批量 trace 不支持修改 content/name，请逐桶操作。"
        if explicit_provenance_kind is not None:
            return "批量 trace 不支持 provenance_kind，请逐桶操作。"
        if merge:
            return "批量 trace 不能与 merge 同时使用。"
        if superseded_by is not None:
            return "批量 trace 不支持 superseded_by。"
        if delete:
            return await _trace_delete_with_confirmation(bucket_ids, confirm_token)
        if related or unrelate:
            try:
                add_ids, remove_ids = _parse_csv_ids(related), _parse_csv_ids(unrelate)
                if add_ids and remove_ids:
                    return "related 与 unrelate 不能同时使用。"
                for current_id in bucket_ids:
                    bucket_mgr.preview_related(current_id, add=add_ids, remove=remove_ids)
            except RelatedError as exc:
                return f"related rejected: {exc.code}; no batch changes made."
        results = []
        for current_id in bucket_ids:
            result = await trace(
                bucket_id=current_id,
                name="",
                domain=domain,
                valence=valence,
                arousal=arousal,
                importance=importance,
                tags=tags,
                todos=todos,
                todo_items=todo_items,
                resolved=resolved,
                pinned=pinned,
                permanent=permanent,
                digested=digested,
                dormant=dormant,
                sealed=sealed,
                content="",
                related=related,
                unrelate=unrelate,
                merge="",
                append=append,
                trigger_date=trigger_date,
                delete=delete,
                confirm_token=confirm_token,
            )
            results.append(f"[{current_id}] {result}")
        return "\n".join(results)
    bucket_id = bucket_ids[0]

    if merge and delete:
        return "merge 不能与 delete 同时使用。"
    if merge:
        if (name or domain or valence != -1 or arousal != -1 or
                importance != -1 or tags or todos is not None or
                todo_items is not None or resolved != -1 or pinned != -1 or
                permanent != -1 or digested != -1 or dormant != -1 or
                sealed != -1 or content or explicit_provenance_kind is not None or
                related or unrelate or superseded_by is not None or append or
                trigger_date):
            return "merge must be called alone with bucket_id, merge, and optional confirm_token."
        return await _merge_bucket_into_target(bucket_id, merge.strip(), confirm_token)
    try:
        trigger_date = ("none" if trigger_date.strip().lower() == "none" else
                        (_parse_optional_date(trigger_date, "trigger_date") or ""))
    except ValueError as exc:
        return str(exc)

    # --- Delete mode / 删除模式 ---
    if delete:
        return await _trace_delete_with_confirmation([bucket_id], confirm_token)

    bucket = await bucket_mgr.get(bucket_id)
    if not bucket:
        return f"未找到记忆桶: {bucket_id}"
    metadata = bucket.get("metadata", {})
    previous_provenance_kind = normalize_provenance_kind(
        metadata.get("provenance_kind")
    )
    if permanent in (0, 1) and metadata.get("type") not in ("dynamic", "permanent"):
        return (f"permanent cannot change bucket type {metadata.get('type')}; "
                "only dynamic and permanent buckets are supported.")
    requested_superseded_by = None
    if superseded_by is not None:
        if not isinstance(superseded_by, str):
            return "superseded_by 必须是 bucket_id、none 或空字符串。"
        requested_superseded_by = superseded_by.strip()
        if requested_superseded_by and requested_superseded_by != "none":
            if requested_superseded_by == bucket_id:
                return "superseded_by 不能指向自身。"
            successor = await bucket_mgr.get(requested_superseded_by)
            if not successor:
                return f"未找到 superseded_by 目标桶: {requested_superseded_by}"
            if _is_sealed(successor):
                return (
                    "superseded_by 目标桶已封存，不能作为取代桶: "
                    f"{requested_superseded_by}"
                )
        try:
            bucket_mgr.preflight_supersession(bucket_id, requested_superseded_by)
        except SupersessionError as exc:
            return f"superseded_by rejected: {exc.code}"
    importance_requested = 1 <= importance <= 10
    importance_protected = importance_requested and (
        metadata.get("pinned") or metadata.get("protected")
    )
    protection_label = "pinned/protected"
    if permanent == 0 and (pinned == 1 or (metadata.get("pinned") and pinned != 0)):
        return "permanent=0 rejected: cancel pinned first with pinned=0; permanent=0 never unpins implicitly."
    if permanent == 0 and metadata.get("protected"):
        return "permanent=0 rejected: protected bucket cannot be downgraded."
    if content and (_is_sealed(bucket) or metadata.get("protected")):
        return f"内容修改失败：记忆桶 {bucket_id} 受到保护。"

    # --- Collect only fields actually passed / 只收集用户实际传入的字段 ---
    updates = {}
    if name:
        updates["name"] = name
    if domain:
        updates["domain"] = [d.strip() for d in domain.split(",") if d.strip()]
    if 0 <= valence <= 1:
        updates["valence"] = valence
    if 0 <= arousal <= 1:
        updates["arousal"] = arousal
    if importance_requested and not importance_protected:
        updates["importance"] = importance
    if tags:
        updates["tags"] = [t.strip() for t in tags.split(",") if t.strip()]
    if todo_items is not None:
        try:
            prepare_todo_provenance(
                structured_todos, structured_provenance,
                previous_todos=metadata.get("todos"),
                previous_provenance=metadata.get("todo_provenance"),
                references_only=True, assign_ids=False,
            )
        except ValueError as exc:
            return f"todo_items 无效：{exc}"
        updates["todos"] = structured_todos
        updates["todo_provenance"] = structured_provenance
        updates["_todo_references_only"] = True
    elif todos is not None:
        updates["todos"] = _canonical_todos(todos)
    if resolved in (0, 1):
        updates["resolved"] = bool(resolved)
    if pinned in (0, 1):
        updates["pinned"] = bool(pinned)
        if pinned == 1:
            updates["importance"] = 10  # pinned → lock importance
    if permanent in (0, 1):
        updates["permanent"] = permanent
    elif pinned == 0 and metadata.get("pinned") and metadata.get("type") != "permanent":
        # Normalize only the bucket being explicitly unpinned; do not bulk-repair data.
        updates["permanent"] = 1
    if digested in (0, 1):
        updates["digested"] = bool(digested)
    if dormant in (0, 1):
        updates["dormant"] = bool(dormant)
    if sealed in (0, 1):
        updates["sealed"] = sealed
    if trigger_date:
        updates["trigger_date"] = "" if trigger_date == "none" else trigger_date
        updates["trigger_last_seen"] = ""
    if content:
        if append:
            current_content = bucket.get("content", "")
            updates["content"] = (
                f"{current_content}\n\n{content}" if current_content else content
            )
            updates["_history_change_type"] = "append"
        else:
            updates["content"] = content
            updates["_history_change_type"] = "replace"
    if explicit_provenance_kind is not None:
        updates["provenance_kind"] = explicit_provenance_kind
    related_ids = _parse_csv_ids(related)
    unrelated_ids = _parse_csv_ids(unrelate)
    if related_ids and unrelated_ids:
        return "related 与 unrelate 不能同时使用。"
    if related_ids or unrelated_ids:
        try:
            bucket_mgr.preview_related(bucket_id, add=related_ids, remove=unrelated_ids)
        except RelatedError as exc:
            return f"related rejected: {exc.code}; no changes made."

    if "valence" in updates or "arousal" in updates:
        meta = bucket.get("metadata", {})
        next_valence = updates.get("valence", meta.get("valence", 0.5))
        next_arousal = updates.get("arousal", meta.get("arousal", 0.3))
        updates["emotion_history"] = _append_emotion_history(meta, next_valence, next_arousal)

    if importance_protected:
        updates.pop("importance", None)
        if not updates and not related_ids and not unrelated_ids:
            return (
                f"importance 未修改：记忆桶 {bucket_id} 受到 {protection_label} protection，"
                "importance 锁定为 10。"
            )

    lowering = ((pinned == 0 and bool(metadata.get("pinned"))) or
                (permanent == 0 and metadata.get("type") == "permanent"))
    if lowering:
        payload = {
            "bucket_id": bucket_id,
            "content_sha256": hashlib.sha256(str(bucket.get("content", "")).encode("utf-8")).hexdigest(),
            "metadata_sha256": hashlib.sha256(
                _json_lib.dumps(metadata, ensure_ascii=False, sort_keys=True,
                                default=str).encode("utf-8")
            ).hexdigest(),
            "updates": updates,
        }
        if not (confirm_token or "").strip():
            token = _issue_mutation_confirmation("trace.lifecycle", payload)
            planned_type = (
                "dynamic" if updates.get("permanent") == 0 else
                "permanent" if updates.get("permanent") == 1 else
                metadata.get("type")
            )
            return (f"confirmation required: bucket_id={bucket_id} "
                    f"pinned={metadata.get('pinned', False)}->{updates.get('pinned', metadata.get('pinned', False))} "
                    f"type={metadata.get('type')}->{planned_type}; "
                    f"confirm_token: {token}")
        if not _consume_mutation_confirmation("trace.lifecycle", payload, confirm_token):
            return "lifecycle confirmation invalid, expired, used, or stale; preview again."

    if not updates and requested_superseded_by is None and not unrelated_ids and not related_ids:
        return "没有任何字段需要修改。"

    if updates:
        success = await bucket_mgr.update(bucket_id, **updates)
        if not success:
            return f"修改失败: {bucket_id}"

    relation_result = None
    if related_ids or unrelated_ids:
        try:
            relation_result = bucket_mgr.mutate_related(bucket_id, add=related_ids, remove=unrelated_ids)
        except Exception as exc:
            detail = str(exc) if isinstance(exc, RelatedError) else 'related_commit_failed'
            return (f"related failure: {detail}; ordinary mutation={'applied' if updates else 'unchanged'}; "
                    "relation not reported as successful; retry relation-only after recovery.")

    supersession_label = None
    if requested_superseded_by is not None:
        success, supersession_label = await _apply_supersession(
            bucket,
            requested_superseded_by,
        )
        if not success:
            return supersession_label

    changed = ", ".join(
        f"{k}={v}"
        for k, v in updates.items()
        if k not in ("content", "_history_change_type", "_todo_references_only", "provenance_kind")
    )
    if "content" in updates:
        content_label = "content=已追加" if append else "content=已替换"
        changed += (f", {content_label}" if changed else content_label)
    if explicit_provenance_kind is not None or "content" in updates:
        next_provenance_kind = (
            explicit_provenance_kind
            if explicit_provenance_kind is not None
            else "unknown"
        )
        provenance_change = (
            f"provenance_kind={previous_provenance_kind}->{next_provenance_kind}"
        )
        changed += f", {provenance_change}" if changed else provenance_change
    # Explicit hint about resolved state change semantics
    # 特别提示 resolved 状态变化的语义
    if "resolved" in updates:
        if updates["resolved"]:
            changed += " → 已沉底，只在关键词触发时重新浮现"
        else:
            changed += " → 已重新激活，将参与浮现排序"
    if "digested" in updates:
        if updates["digested"]:
            changed += " → 已隐藏，保留但不再浮现"
        else:
            changed += " → 已取消隐藏，重新参与浮现"
    if importance_protected:
        changed += (
            "; importance 未修改：受到 pinned/protected protection，"
            "importance 锁定为 10"
        )
    if supersession_label is not None:
        supersession_change = f"superseded_by={supersession_label}"
        changed += f", {supersession_change}" if changed else supersession_change
    if related_ids:
        relation_change = f"related={','.join(related_ids)} ({relation_result['status']})"
        changed += f", {relation_change}" if changed else relation_change
    if unrelated_ids:
        unrelate_change = f"unrelate={','.join(unrelated_ids)} ({relation_result['status']})"
        changed += f", {unrelate_change}" if changed else unrelate_change
    if pinned == 0 or permanent in (0, 1):
        current = await bucket_mgr.get(bucket_id)
        if current:
            state = current["metadata"]
            changed += f"; pinned={bool(state.get('pinned'))}, type={state.get('type')}"
    if todo_items is not None:
        written = await bucket_mgr.get(bucket_id)
        if written:
            records = reconcile_todo_provenance(
                written["metadata"].get("todos"), written["metadata"].get("todo_provenance"),
            )
            changed += "\n" + "\n".join(
                f"- {record['text']} | todo_id:{record['id']}" for record in records
            )
    return f"已修改记忆桶 {bucket_id}: {changed}"

@mcp.tool()
async def seal_letter(letter_id: int, sealed: int = 1) -> str:
    """Sealed-memory maintenance: change handoff-letter visibility by id."""
    if int(letter_id or 0) < 1:
        return "Please provide a valid letter_id."
    if sealed not in (0, 1):
        return "sealed must be 0 or 1."
    success = bucket_mgr.seal_letter(int(letter_id), sealed=bool(sealed))
    if not success:
        return f"letter_id not found: {letter_id}"
    state = "sealed" if sealed else "unsealed"
    return f"letter_id:{letter_id} {state}"

# =============================================================
# Tool 5: archive_session — Archive a conversation summary
# =============================================================
@mcp.tool()
async def archive_session(
    summary: str,
    highlights: str = "",
    mood: str = "",
    valence: Union[
        Literal[-1],
        Annotated[float, Field(ge=0, le=1, description="情绪效价，范围 0-1")],
    ] = -1,
    arousal: Union[
        Literal[-1],
        Annotated[float, Field(ge=0, le=1, description="情绪唤醒度，范围 0-1")],
    ] = -1,
    letter: str = "",
    sealed: bool = False,
    topics: Annotated[
        list[str] | None,
        Field(
            description=(
                "Optional structured topic labels describing the main subjects "
                "covered by the archived session."
            )
        ),
    ] = None,
    operation_id: str | None = None,
) -> str:
    # MCP schema note: this function is intentionally registered as a tool.
    """Archive the current conversation summary into archive/session.

    ``topics`` contains optional structured labels for the main subjects of
    the archived session. When useful, provide roughly 3–8 moderately scoped
    labels, such as ``项目/OB``, ``项目/RM``, ``学习/生化``, ``关系/沟通``, or
    ``日常/作息``. Avoid labels that are too broad, such as ``闲聊``, or
    excessively narrow labels.

    Generate optional operation_id before the first call and reuse it on retry
    or reconnect. IDs are case-sensitive, 1–128 ASCII characters matching
    [A-Za-z0-9][A-Za-z0-9._:-]*, with no trimming.
    Same ID with changed parameters conflicts; different IDs with
    identical content create independent sessions. Omitted IDs remain compatible
    but cannot deduplicate response-loss retries. Provider calls are best-effort
    and are not guaranteed exactly-once.
    """
    store = ArchiveSessionOperations(config["buckets_dir"])
    operation = None
    try:
        validate_operation_id(operation_id)
        payload = canonical_payload(summary, highlights, mood, valence, arousal, letter, sealed, topics)
        if operation_id is not None:
            operation = store.lookup(operation_id, payload)
            if operation is not None and operation["status"] == "completed":
                return operation["result_text"]
            operation = await short_step(store.lookup_or_plan, operation_id, payload, config)
            if operation["status"] == "completed":
                return operation["result_text"]
        await decay_engine.ensure_started()
        if operation_id is not None:
            def write_snapshot(entry, *, verify_only=False):
                _record_emotion_snapshot(
                    entry["valence"], entry["arousal"], "archive", entry["bucket_id"],
                    _expected_entry=entry, _strict=True, _verify_only=verify_only,
                )
            return await execute_archive_operation(
                store, operation, bucket_mgr.embedding_engine, write_snapshot,
            )
        return await _await_legacy_post_effects(_run_legacy_archive, store, copy.deepcopy(payload), copy.deepcopy(config))
    except Exception as exc:
        code = (exc.code if isinstance(exc, ArchiveSessionError)
                or (operation_id is None and isinstance(exc, BucketIdempotencyError))
                else "archive_session_storage_failed")
        if operation is not None and code != "archive_operation_payload_conflict":
            try:
                store.blocked(operation_id, code)
            except Exception:
                logger.warning("archive_session journal error: archive_journal_unavailable")
        if code in {"topics must be a list of strings.", "summary 不能为空。"}:
            return code
        return f"archive_session: {code}"




# =============================================================
# Tool 6: pulse — Heartbeat, system status + memory listing
# 工具 5：pulse — 脉搏，系统状态 + 记忆列表
# =============================================================
@mcp.tool()
async def todos(
    include_provenance: Annotated[
        bool,
        Field(
            description=(
                "Defaults to false. When true, group the dedicated todo output "
                "by explicit provenance without changing ordinary todo text."
            )
        ),
    ] = False,
) -> str:
    """Return unresolved todos excluding exact test-tagged buckets; provenance is opt-in."""
    await decay_engine.ensure_started()
    try:
        all_buckets = await bucket_mgr.list_all(include_archive=True)
    except Exception as exc:
        logger.error("Failed to list buckets for todos: %s", exc)
        return "待办汇总暂时无法读取。"

    groups = []
    provenance_groups = {
        "ting": [],
        "model": [],
        "system": [],
        "unknown": [],
    }
    for bucket in all_buckets:
        meta = bucket.get("metadata", {})
        if _is_sealed(bucket) or _is_test_bucket(bucket):
            continue
        if meta.get("resolved", False):
            continue
        try:
            active_texts, records = active_todo_projection(meta.get("todos"), meta.get("todo_provenance"))
        except ValueError as exc:
            return f"todo provenance conflict: {exc}"
        items = [text for text in _normalize_todos(meta.get("todos")) if text in active_texts or text not in _canonical_todos(meta.get("todos"))]
        if not items:
            continue
        name = meta.get("name", bucket["id"])
        importance = meta.get("importance", "?")
        if include_provenance:
            for item in items:
                matches = [record for record in records if record["text"] == item] or [{}]
                for record in matches:
                    said_by = record.get("said_by", "unknown")
                    said_at = record.get("said_at")
                    suffix = f" | said_at:{said_at}" if said_at else ""
                    suffix += f" | todo_id:{record.get('id') or 'null'}"
                    provenance_groups[said_by].append(
                        (
                            int(meta.get("importance", 0) or 0),
                            f"- {item} [bucket_id:{bucket['id']}] {name} "
                            f"| 重要度:{importance}{suffix}",
                        )
                    )
            continue
        lines = [
            f"[bucket_id:{bucket['id']}] {name} | 重要度:{importance}",
            *(f"- {item}" for item in items),
        ]
        groups.append((int(meta.get("importance", 0) or 0), "\n".join(lines)))

    if include_provenance:
        headings = {
            "ting": "=== 婷明确说的 ===",
            "model": "=== 模型自己列的 ===",
            "system": "=== 系统 ===",
            "unknown": "=== 出处未知 ===",
        }
        sections = []
        for said_by in ("ting", "model", "system", "unknown"):
            entries = provenance_groups[said_by]
            if not entries:
                continue
            entries.sort(key=lambda item: item[0], reverse=True)
            sections.append(headings[said_by] + "\n" + "\n".join(text for _, text in entries))
        return "\n\n".join(sections) if sections else "当前没有未完成待办。"
    if not groups:
        return "当前没有未完成待办。"
    groups.sort(key=lambda item: item[0], reverse=True)
    return "\n---\n".join(text for _, text in groups)


@mcp.tool()
async def boot(
    pinned_chars: int = 5000,
    max_tokens: Annotated[int, Field(ge=1000, le=16000)] = 16000,
    profile: Annotated[Literal["talk", "code", "tg"], Field(description="Boot context profile: talk, code, or tg.")] = "talk",
) -> str:
    """Stateful startup context for the selected talk, code, or TG profile.

    Starts background decay. Advances the selected profile delta checkpoint only
    over the safe consumed range of the fully emitted delta. Fully emitted
    triggers update shared trigger_last_seen. Fully delivered latest eligible
    notes receive boot_delivered_at; older eligible pending notes may be skipped.
    Mailbox/letters are read without marking seen. Repeated boot calls can change
    later delta, note and same-day trigger output. Exact test-tagged buckets and
    their associated letters are excluded from this delivery output.
    """
    if not isinstance(profile, str) or profile not in BOOT_PROFILE_NAMES:
        return "profile 必须是 talk、code 或 tg。"
    await decay_engine.ensure_started()
    profile_config = BOOT_PROFILE_CONFIG[profile]
    pinned_chars = max(
        80,
        min(int(pinned_chars or 5000), int(profile_config["pinned_chars"])),
    )
    max_tokens = min(
        max(1000, min(int(max_tokens or 16000), 16000)),
        int(profile_config["max_tokens"]),
    )

    try:
        active_buckets = await bucket_mgr.list_all(include_archive=False)
        archive_buckets = await bucket_mgr.list_all(include_archive=True)
    except Exception as exc:
        logger.error("Boot failed to list buckets: %s", exc)
        return _with_response_seal("boot 暂时无法读取记忆库。")

    test_bucket_ids = {
        str(bucket["id"]) for bucket in archive_buckets if _is_test_bucket(bucket)
    }
    active_buckets = [bucket for bucket in active_buckets if not _is_test_bucket(bucket)]
    archive_buckets = [bucket for bucket in archive_buckets if not _is_test_bucket(bucket)]

    try:
        todo_display = todo_page(active_display_candidates(archive_buckets), profile, shanghai_date())
    except ZoneInfoNotFoundError:
        return _with_response_seal("boot 无法计算每日待办轮转：缺少 Asia/Shanghai 时区数据，请安装 tzdata。")
    except (ValueError, TypeError, KeyError):
        return _with_response_seal("boot 待办活动总数无法确认：todo provenance conflict 或损坏的展示 identity。")

    checkpoint = bucket_mgr.get_boot_delta_checkpoint(profile=profile)
    high_water = bucket_mgr.get_boot_delta_high_water()
    visible_buckets = list({
        bucket["id"]: bucket for bucket in [*active_buckets, *archive_buckets]
    }.values())
    profile_buckets = [
        bucket for bucket in visible_buckets
        if _profile_allows_bucket(bucket, profile)
    ]

    def _delta_candidate() -> tuple[str, int]:
        return _format_boot_delta(
            checkpoint=checkpoint,
            high_water=high_water,
            visible_buckets=profile_buckets,
            max_tokens=int(profile_config["delta_tokens"]),
            include_omitted_ids=profile == "tg",
            return_progress=True,
        )

    trigger_text, trigger_items = await _format_due_triggers(
        active_buckets,
        max_items=int(profile_config["trigger_items"]),
        show_preview_truncation=profile == "tg",
    )

    pinned = [
        b for b in active_buckets
        if (b.get("metadata", {}).get("pinned") or b.get("metadata", {}).get("protected"))
        and _profile_allows_bucket(b, profile)
    ]
    pinned.sort(
        key=lambda b: _bucket_date(b["metadata"], "updated_at", "last_active", "created"),
        reverse=True,
    )
    tg_metadata = {}
    if profile == "tg":
        pinned_by_id = {}
        for bucket in pinned:
            pinned_by_id.setdefault(str(bucket["id"]), bucket)
        pinned = list(pinned_by_id.values())
        for bucket in pinned:
            metadata = await bucket_mgr.get_tg_summary_metadata(str(bucket["id"]))
            if metadata is not None:
                tg_metadata[str(bucket["id"])] = metadata
        # A bucket removed or sealed since the visibility snapshot contributes
        # neither ordinary text, recovery metadata, nor visible counts.
        pinned = [bucket for bucket in pinned if str(bucket["id"]) in tg_metadata]
    pinned_lines = []
    recovery_items = []
    pinned_end = len("=== boot: 开机索引 ===\n")
    for bucket in pinned:
        meta = bucket.get("metadata", {})
        if profile == "tg":
            summary_state, source_hash, summary = tg_metadata[str(bucket["id"])]
            if summary_state == "fresh":
                preview = _format_tg_summary_preview(bucket, source_hash, summary)
            else:
                preview = _format_boot_preview(
                    bucket,
                    pinned_chars,
                    show_truncation=True,
                )
                preview += "\n" + _format_tg_summary_refresh_notice(
                    str(bucket["id"]), summary_state, source_hash
                )
        else:
            preview = _format_boot_preview(bucket, pinned_chars, show_truncation=True)
        line = f"[bucket_id:{bucket['id']}] {meta.get('name', bucket['id'])}\n{preview}"
        pinned_lines.append(line)
        pinned_end += len(line)
        if profile == "tg":
            recovery_items.append((str(bucket["id"]), summary_state, source_hash, pinned_end))
        pinned_end += len("\n---\n")
    pinned_text = "=== boot: 开机索引 ===\n" + (
        "\n---\n".join(pinned_lines) if pinned_lines else "（暂无可见钉选桶）"
    )

    mailbox_text = _format_mailbox(1, exclude_session_ids=test_bucket_ids).replace("=== 信箱 ===", "=== boot: 最新信箱 ===", 1)
    echo_text = _format_feel_echo(active_buckets)

    sessions = [
        b for b in archive_buckets
        if "session" in b.get("metadata", {}).get("domain", [])
        and _profile_allows_bucket(b, profile)
    ]
    sessions.sort(
        key=lambda b: _bucket_date(b["metadata"], "updated_at", "created_at", "created"),
        reverse=True,
    )
    session_lines = []
    for bucket in sessions[:3]:
        meta = bucket.get("metadata", {})
        full_summary = _extract_session_summary(bucket.get("content", ""), max_chars=None)
        summary = full_summary[:700].strip()
        if len(full_summary) > 700:
            summary += "\n" + _format_bucket_truncation_notice(
                str(bucket["id"]), len(summary), len(full_summary)
            )
        session_lines.append(
            f"[bucket_id:{bucket['id']}] {meta.get('name', bucket['id'])}\n{summary}"
        )
    sessions_text = "=== boot: 最近 3 次归档 ===\n" + (
        "\n---\n".join(session_lines) if session_lines else "（暂无 session 归档）"
    )

    todos_text = fit_todos(todo_display, max_tokens).text

    note_now = datetime.now().isoformat(timespec="seconds")
    ting_note_text, deliver_note_id = _format_ting_note_for_boot(note_now, max_tokens)

    def _compose_boot_body(current_delta_text: str) -> tuple[str, dict[str, str]]:
        sections = [
            ("ting_note", "婷留言", ting_note_text),
            ("delta", "增量摘要", current_delta_text),
            ("triggers", "今日触发", trigger_text),
        ]
        if profile_config["include_mailbox"]:
            sections.append(("mailbox", "最新 letter", mailbox_text))
        sections.append(("todos", "todos", todos_text))
        if profile_config["include_sessions"]:
            sections.append(("sessions", "最近归档", sessions_text))
        sections.append(("pinned", "钉选索引", pinned_text))
        if profile_config["include_echo"]:
            sections.append(("echo", "feel 回声", echo_text))
        omission_item_refs = None
        truncation_notice_tokens = BOOT_TRUNCATION_NOTICE_TOKENS
        if profile == "tg":
            def _stable_refs(text: str) -> list[str]:
                return list(dict.fromkeys(re.findall(
                    r"\[((?:bucket|note|letter)_id:[^\]]+)\]",
                    text,
                )))

            omission_item_refs = {
                "ting_note": _stable_refs(ting_note_text),
                "delta": _stable_refs(current_delta_text),
                "triggers": [f"bucket_id:{bucket_id}" for bucket_id, _ in trigger_items],
                "mailbox": _stable_refs(mailbox_text),
                "pinned": [f"bucket_id:{bucket['id']}" for bucket in pinned],
            }
            truncation_notice_tokens = BOOT_TG_TRUNCATION_NOTICE_TOKENS
        fitter = _fit_tg_boot_sections if profile == "tg" else _fit_sections_to_budget
        return fitter(
            sections,
            max_tokens=max_tokens if profile == "tg" else max_tokens - 40,
            minimum_chars={
                **profile_config["section_minimums"],
                "ting_note": len(ting_note_text),
                "delta": len(current_delta_text),
            },
            atomic_sections={"ting_note", "delta"},
            omission_item_refs=omission_item_refs,
            truncation_notice_tokens=truncation_notice_tokens,
            todo_display=todo_display,
            **({
                "recovery_items": recovery_items,
                "omission_item_ends": {"pinned": [
                    (f"bucket_id:{bucket_id}", end)
                    for bucket_id, _, _, end in recovery_items
                ]},
            } if profile == "tg" else {"return_sections": True}),
        )

    for _ in range(3):
        delta_text, safe_event_id = _delta_candidate()
        body, emitted_sections = _compose_boot_body(delta_text)
        expected_event_id = (
            int(checkpoint["last_event_id"]) if checkpoint is not None else None
        )
        # An omitted delta has no consumed range. A partial delta only crosses
        # the contiguous eligible prefix selected by _format_boot_delta.
        next_event_id = (
            safe_event_id if emitted_sections.get("delta") == delta_text
            else expected_event_id
        )
        if next_event_id == expected_event_id or bucket_mgr.advance_boot_delta_checkpoint(
            expected_event_id,
            next_event_id,
            profile=profile,
        ):
            break
        # A concurrent boot advanced this profile. Recalculate both the body
        # and every consumption decision against the new checkpoint.
        checkpoint = bucket_mgr.get_boot_delta_checkpoint(profile=profile)
        high_water = bucket_mgr.get_boot_delta_high_water()
    else:
        # Do not advance after repeated CAS conflicts. The final body still
        # determines note and trigger consumption.
        delta_text, _ = _delta_candidate()
        body, emitted_sections = _compose_boot_body(delta_text)

    response = _with_response_seal(f"boot profile: {profile}\n\n{body}")
    if deliver_note_id is not None and emitted_sections.get("ting_note") == ting_note_text:
        try:
            bucket_mgr.mark_note_boot_delivered(deliver_note_id, delivered_at=note_now)
        except Exception:
            logger.exception("Boot note delivery state update failed")
    emitted_triggers = emitted_sections.get("triggers", "")
    if trigger_text.startswith(emitted_triggers):
        today = datetime.now().date().isoformat()
        for bucket_id, end_offset in trigger_items:
            if end_offset <= len(emitted_triggers):
                try:
                    await bucket_mgr.update(bucket_id, trigger_last_seen=today)
                except Exception:
                    logger.exception("Boot trigger delivery state update failed")
    return response


@mcp.tool(description=TG_SUMMARY_TOOL_DESCRIPTION)
async def refresh_tg_summary(
    bucket_id: str,
    summary: Annotated[
        str,
        Field(
            description=(
                "Caller-generated TG summary, at most 1200 Unicode characters. "
                "Follow the stable TG summary generation contract in this tool description."
            )
        ),
    ],
    source_hash: Annotated[
        str,
        Field(
            description=(
                "SHA-256 source_hash reported by TG boot for this bucket. "
                "The write is rejected if the stored body changed since that hash."
            )
        ),
    ],
) -> str:
    """Persist one caller-generated TG summary after source-hash validation."""
    normalized_id = (bucket_id or "").strip()
    normalized_summary = (summary or "").strip()
    normalized_hash = (source_hash or "").strip().lower()
    if not normalized_id:
        return "请提供有效的 bucket_id。"
    if not normalized_summary:
        return "TG summary 不能为空。"
    if len(normalized_summary) > TG_SUMMARY_MAX_CHARS:
        return (
            f"TG summary 超过 {TG_SUMMARY_MAX_CHARS} 字符上限；"
            "请压缩后重试。"
        )
    if not re.fullmatch(r"[0-9a-f]{64}", normalized_hash):
        return "source_hash 必须是 TG boot 返回的 64 位 SHA-256。"

    bucket = await bucket_mgr.get(normalized_id)
    if not bucket:
        return f"未找到记忆桶: {normalized_id}"
    if _is_sealed(bucket):
        return f"记忆桶已封存，不能刷新 TG summary: {normalized_id}"
    source_metadata = await bucket_mgr.get_tg_summary_metadata(normalized_id)
    if source_metadata is None:
        # A concurrent removal/seal or read failure must use existing outcomes,
        # rather than introducing a distinct availability/existence signal.
        current_bucket = await bucket_mgr.get(normalized_id)
        if not current_bucket:
            return f"未找到记忆桶: {normalized_id}"
        if _is_sealed(current_bucket):
            return f"记忆桶已封存，不能刷新 TG summary: {normalized_id}"
        return f"TG summary 保存失败: {normalized_id}"
    _, current_hash, _ = source_metadata
    if not hmac.compare_digest(normalized_hash, current_hash):
        return (
            f"TG summary 未保存：原文已变化。bucket {normalized_id} 当前 source_hash:{current_hash}；"
            f"请用 dream(detail_ids=\"{normalized_id}\") 重新读取后生成。"
        )

    outcome, current_hash = await bucket_mgr.refresh_tg_summary(
        normalized_id,
        normalized_summary,
        normalized_hash,
    )
    if outcome == "updated":
        return (
            f"TG summary 已刷新：bucket {normalized_id}；source_hash:{current_hash}；"
            "TG boot 将使用该压缩版，原 bucket 仍是唯一真实来源。"
        )
    if outcome == "source_hash_mismatch":
        return (
            f"TG summary 未保存：原文已变化。bucket {normalized_id} 当前 source_hash:{current_hash}；"
            f"请用 dream(detail_ids=\"{normalized_id}\") 重新读取后生成。"
        )
    if outcome == "sealed":
        return f"记忆桶已封存，不能刷新 TG summary: {normalized_id}"
    if outcome == "missing":
        return f"未找到记忆桶: {normalized_id}"
    return f"TG summary 保存失败: {normalized_id}"


def _health_todo_age_days(metadata: dict) -> float | None:
    """Return bucket-level todo activity age, or None when it is unavailable."""
    for field in ("last_active", "updated_at", "created"):
        value = metadata.get(field)
        if not value:
            continue
        try:
            timestamp = datetime.fromisoformat(str(value))
            if timestamp.tzinfo is not None:
                timestamp = datetime.fromtimestamp(timestamp.timestamp())
            return max(0.0, (datetime.now() - timestamp).total_seconds() / 86400)
        except (ValueError, TypeError):
            continue
    return None


def _health_supersession_report(
    visible_buckets: list[dict],
    all_buckets: list[dict],
) -> dict[str, int]:
    """Return count-only integrity findings without crossing sealed boundaries.

    The total is the number of distinct visible buckets participating in at
    least one finding.  Category counts can overlap for a malformed relation.
    """
    visible_by_id = {
        str(bucket.get("id", "")): bucket
        for bucket in visible_buckets
        if str(bucket.get("id", ""))
    }
    all_by_id = {
        str(bucket.get("id", "")): bucket
        for bucket in all_buckets
        if str(bucket.get("id", ""))
    }
    sealed_ids = {
        bucket_id for bucket_id, bucket in all_by_id.items() if _is_sealed(bucket)
    }
    findings = {
        "missing_successor": set(),
        "missing_reverse": set(),
        "stale_reverse": set(),
        "forward_self": set(),
        "reverse_self": set(),
        "cycle": set(),
        "malformed_successor": set(),
    }

    def add(kind: str, *bucket_ids: str) -> None:
        for bucket_id in bucket_ids:
            if bucket_id in visible_by_id:
                findings[kind].add(bucket_id)

    for source_id, source in visible_by_id.items():
        successor_id = _superseded_by_id(source.get("metadata", {}))
        if not successor_id or successor_id == "none":
            continue
        if successor_id == source_id:
            add("forward_self", source_id)
            continue
        if successor_id.casefold() == "none":
            add("malformed_successor", source_id)
            continue
        # A sealed successor is intentionally indistinguishable from a missing
        # one in ordinary maintenance output, so it contributes no finding.
        if successor_id in sealed_ids:
            continue
        successor = all_by_id.get(successor_id)
        if successor is None:
            add("missing_successor", source_id)
            continue
        if source_id not in _supersedes_ids(successor.get("metadata", {})):
            add("missing_reverse", source_id)

    for holder_id, holder in visible_by_id.items():
        for source_id in _supersedes_ids(holder.get("metadata", {})):
            if source_id == holder_id:
                add("reverse_self", holder_id)
                continue
            if source_id in sealed_ids:
                continue
            source = all_by_id.get(source_id)
            if source is None:
                add("stale_reverse", holder_id)
                continue
            if _superseded_by_id(source.get("metadata", {})) != holder_id:
                add("stale_reverse", holder_id, source_id)

    # Each bucket has at most one forward edge.  Walk only visible, non-sealed
    # edges so a sealed endpoint cannot affect an ordinary health count.
    for start_id in visible_by_id:
        path: list[str] = []
        positions: dict[str, int] = {}
        current_id = start_id
        cycle_start = None
        while current_id in visible_by_id:
            if current_id in positions:
                cycle_start = positions[current_id]
                break
            positions[current_id] = len(path)
            path.append(current_id)
            successor_id = _superseded_by_id(
                visible_by_id[current_id].get("metadata", {})
            )
            if not successor_id or successor_id == "none" or successor_id in sealed_ids:
                break
            current_id = successor_id
        if cycle_start is not None:
            cycle_ids = path[cycle_start:]
            # A one-node loop is already reported as forward_self above.
            if len(cycle_ids) > 1:
                for bucket_id in cycle_ids:
                    add("cycle", bucket_id)

    total = set().union(*findings.values())
    return {
        "total": len(total),
        **{name: len(bucket_ids) for name, bucket_ids in findings.items()},
    }


def _maintenance_health_report(
    visible_buckets: list[dict],
    all_buckets: list[dict],
    *,
    todo_stale_days: int,
    include_archive: bool,
) -> str:
    """Format count-only, read-only maintenance health for ordinary access."""
    unnamed = 0
    untagged = 0
    stale_todos = 0
    todo_age_unavailable = 0
    pinned_low_importance = 0

    for bucket in visible_buckets:
        bucket_id = str(bucket.get("id", ""))
        metadata = bucket.get("metadata", {})
        name = metadata.get("name")
        if name is None or not str(name).strip() or str(name) == bucket_id:
            unnamed += 1
        if not any(str(tag).strip() for tag in _structured_metadata_values(metadata, "tags")):
            untagged += 1
        if metadata.get("pinned"):
            try:
                importance = int(metadata.get("importance", 0) or 0)
            except (TypeError, ValueError):
                importance = None
            if importance is not None and importance < 3:
                pinned_low_importance += 1

        if metadata.get("resolved", False):
            continue
        active_todos, _ = active_todo_projection(
            metadata.get("todos"), metadata.get("todo_provenance")
        )
        if not active_todos:
            continue
        age_days = _health_todo_age_days(metadata)
        if age_days is None:
            todo_age_unavailable += 1
        elif age_days > todo_stale_days:
            stale_todos += 1

    supersession = _health_supersession_report(visible_buckets, all_buckets)
    return "\n".join(
        [
            "=== maintenance health ===",
            (
                f"scope: {len(visible_buckets)} parsed, unsealed buckets; "
                f"archive {'included' if include_archive else 'excluded'}; "
                "independent of list limit/offset"
            ),
            (
                "denominators: bucket counts use this scope; supersession problems count "
                "distinct in-scope buckets and may overlap category findings"
            ),
            f"unnamed buckets: {unnamed}",
            f"untagged buckets: {untagged}",
            f"stale todo buckets (>{todo_stale_days}d): {stale_todos}",
            f"todo age unavailable: {todo_age_unavailable}",
            f"supersession problems: {supersession['total']}",
            f"pinned importance <3: {pinned_low_importance}",
        ]
    )


@mcp.tool()
async def pulse(
    include_archive: bool = False,
    show_all: bool = False,
    include_sealed: bool = False,
    health: Annotated[
        bool,
        Field(description="Enable read-only maintenance health counts. Requires touch=false."),
    ] = False,
    todo_stale_days: Annotated[
        int,
        Field(
            ge=1,
            description=(
                "Bucket-level inactivity threshold used by health reporting; "
                "does not represent per-todo age."
            ),
        ),
    ] = 30,
    limit: Annotated[
        int,
        Field(ge=1, le=50, description="Maximum number of bucket summaries to display."),
    ] = 50,
    offset: Annotated[
        int,
        Field(ge=0, description="Number of ordered bucket summaries to skip."),
    ] = 0,
    touch: Annotated[
        bool,
        Field(
            description=(
                "Defaults to True. Set False for maintenance or acceptance "
                "listing that must not update dormant or decay-related metadata."
            )
        ),
    ] = True,
) -> str:
    """Status/listing readout; touch=False keeps maintenance listing read-only."""
    try:
        todo_stale_days = int(todo_stale_days)
    except (TypeError, ValueError):
        return "todo_stale_days 必须是正整数。"
    if todo_stale_days < 1:
        return "todo_stale_days 必须是正整数。"
    if health and touch:
        return "health=True 需要 touch=False，以保持维护报告只读。"
    if health and include_sealed:
        return "health=True 不支持 include_sealed，以保护封存记忆边界。"
    if touch:
        await decay_engine.ensure_started()
    try:
        limit = int(limit)
        offset = int(offset)
    except (TypeError, ValueError):
        return "limit 和 offset 必须是整数。"
    if limit < 1:
        return "limit 必须大于等于 1。"
    if offset < 0:
        return "offset 必须大于等于 0。"
    limit = min(limit, 50)
    try:
        stats = await bucket_mgr.get_stats()
    except Exception as e:
        logger.error("Pulse stats failed: %s", e); return "获取系统状态失败。"

    status = (
        f"=== Ombre Brain 记忆系统 ===\n"
        f"目录原始 .md 文件计数（可含 sealed/dormant/superseded/不可读文件，不等于可见桶数）\n"
        f"固化记忆桶: {stats['permanent_count']} 个\n"
        f"动态记忆桶: {stats['dynamic_count']} 个\n"
        f"归档记忆桶: {stats['archive_count']} 个\n"
        f"总存储大小: {stats['total_size_kb']:.1f} KB\n"
        f"衰减引擎: {'运行中' if decay_engine.is_running else '已停止'}\n"
    )

    # --- List all bucket summaries / 列出所有桶摘要 ---
    try:
        buckets = await bucket_mgr.list_all(include_archive=include_archive)
    except Exception as e:
        logger.error("Pulse bucket listing failed: %s", e); return status + "\n列出记忆桶失败。"

    if touch:
        await _mark_dormant_buckets(buckets)
    listable_buckets = [
        b for b in buckets
        if include_sealed or not _is_sealed(b)
    ]
    health_report = ""
    if health:
        try:
            all_health_buckets = await bucket_mgr.list_all(include_archive=True)
        except Exception as exc:
            logger.error("Pulse health listing failed: %s", exc)
            health_report = "=== maintenance health ===\nhealth unavailable"
        else:
            health_report = _maintenance_health_report(
                listable_buckets,
                all_health_buckets,
                todo_stale_days=todo_stale_days,
                include_archive=include_archive,
            )

    if not buckets:
        empty = "记忆库为空。\n总数:0个可见桶，当前显示:0个，还有更多:否"
        return status + "\n" + empty + ("\n" + health_report if health else "")

    total_buckets = len(listable_buckets)
    pinned_buckets = [
        b for b in listable_buckets
        if b["metadata"].get("pinned", False)
        or b["metadata"].get("protected", False)
    ]
    pinned_ids = {b["id"] for b in pinned_buckets}

    def pulse_score(bucket: dict) -> float:
        try:
            return decay_engine.calculate_score(bucket.get("metadata", {}))
        except Exception:
            return 0.0

    if show_all:
        ordered_buckets = sorted(
            listable_buckets,
            key=lambda b: (
                bool(b["metadata"].get("pinned") or b["metadata"].get("protected")),
                _bucket_date(b["metadata"], "updated_at", "last_active", "created"),
            ),
            reverse=True,
        )
        dynamic_count = len(listable_buckets) - len(pinned_buckets)
        visible_buckets = ordered_buckets[offset:offset + limit]
    else:
        dynamic_buckets = [
            b for b in listable_buckets
            if b["id"] not in pinned_ids
            and b["metadata"].get("type", "dynamic") == "dynamic"
            and not b["metadata"].get("dormant", False)
        ]
        def default_order_key(bucket: dict) -> tuple[float, str]:
            meta = bucket["metadata"]
            return pulse_score(bucket), _bucket_date(
                meta, "updated_at", "last_active", "created"
            )

        pinned_buckets.sort(key=default_order_key, reverse=True)
        dynamic_buckets.sort(key=default_order_key, reverse=True)
        dynamic_buckets = dynamic_buckets[:15]
        dynamic_count = len(dynamic_buckets)
        ordered_buckets = pinned_buckets + dynamic_buckets
        visible_buckets = ordered_buckets[offset:offset + limit]

    lines = []
    for b in visible_buckets:
        meta = b.get("metadata", {})
        icon = _bucket_display_icon(meta, protected_as_pinned=True)
        try:
            score = decay_engine.calculate_score(meta)
        except Exception:
            score = 0.0
        domains = ",".join(meta.get("domain", []))
        val = meta.get("valence", 0.5)
        aro = meta.get("arousal", 0.3)
        resolved_tag = " [已解决]" if meta.get("resolved", False) else ""
        sealed_tag = " [封存]" if int(meta.get("sealed", 0) or 0) == 1 else ""
        dormant_tag = " [休眠]" if meta.get("dormant", False) else ""
        superseded_prefix = "⊘" if _superseded_by_id(meta) else ""
        created_at = _bucket_date(meta, "created_at", "created")
        updated_at = _bucket_date(meta, "updated_at", "last_active", "created")
        lines.append(
            f"{superseded_prefix}{icon} [{meta.get('name', b['id'])}]{resolved_tag}{sealed_tag}{dormant_tag} "
            f"bucket_id:{b['id']} "
            f"主题:{domains} "
            f"情感:V{val:.1f}/A{aro:.1f} "
            f"重要:{meta.get('importance', '?')} "
            f"权重:{score:.2f} "
            f"created_at:{created_at} "
            f"updated_at:{updated_at} "
            f"标签:{','.join(meta.get('tags', []))}"
        )

    has_more = (
        offset + len(visible_buckets) < total_buckets
        if show_all
        else len(visible_buckets) < total_buckets
    )
    if show_all:
        breakdown = f"有界全部列表，limit={limit}, offset={offset}"
        display_stats = (
            f"\n总数:{total_buckets}个可见桶，当前显示:{len(visible_buckets)}个"
            f"（{breakdown}），还有更多:{'是' if has_more else '否'}\n"
        )
    else:
        displayed_ids = {b["id"] for b in visible_buckets}
        unlisted_permanent = sum(
            b["metadata"].get("type") == "permanent" and b["id"] not in displayed_ids
            for b in listable_buckets
        )
        unlisted_feel = sum(
            b["metadata"].get("type") == "feel" and b["id"] not in displayed_ids
            for b in listable_buckets
        )
        display_stats = (
            f"\n总数:{total_buckets}个可见桶，当前显示:{len(visible_buckets)}个"
            f"（钉选{len(pinned_buckets)}个 + 动态Top15，limit={limit}, offset={offset}），"
            f"固化 {unlisted_permanent} / feel {unlisted_feel} 个未列入当前输出，"
            f"还有更多:{'是' if has_more else '否'}"
            f"{'（用 show_all=True 查看当前可见范围内未显示的桶）' if has_more else ''}\n"
        )
    return (
        status
        + "\n=== 记忆列表 ===\n"
        + "\n".join(lines)
        + display_stats
        + ("\n" + health_report if health else "")
    )


# =============================================================
# Tool 6: dream — Dreaming, digest recent memories
# 工具 6：dream — 做梦，消化最近的记忆
#
# Reads recent surface-level buckets (≤10), returns them for
# Claude to introspect under prompt guidance.
# 读取最近新增的表层桶（≤10个），返回给 Claude 在提示词引导下自主思考。
# Claude then decides: resolve some, write feels, or do nothing.
# =============================================================
@mcp.tool()
async def dream(detail_ids: str = "", wake_dormant: bool = False) -> str:
    """Optional reflection readout: recent memory summaries, or full details for selected buckets."""
    await decay_engine.ensure_started()

    requested_ids = list(dict.fromkeys(_parse_csv_ids(detail_ids)))
    if requested_ids:
        details = []
        for bucket_id in requested_ids:
            bucket = await bucket_mgr.get(bucket_id)
            if not bucket:
                details.append(f"未找到记忆桶: {bucket_id}")
                continue
            if _is_sealed(bucket):
                details.append("指定记忆桶已封存，默认不显示。")
                continue
            meta = bucket.get("metadata", {})
            resolved_tag = " [已解决]" if meta.get("resolved", False) else " [未解决]"
            domains = ",".join(meta.get("domain", []))
            val = meta.get("valence", 0.5)
            aro = meta.get("arousal", 0.3)
            updated = _bucket_date(meta, "updated_at", "last_active", "created")
            superseded_notice = await _dream_superseded_notice(bucket)
            detail_prefix = f"{superseded_notice}\n" if superseded_notice else ""
            details.append(
                f"[{meta.get('name', bucket_id)}]{resolved_tag}{await _superseded_marker(bucket)} "
                f"主题:{domains} V{val:.1f}/A{aro:.1f} 更新:{updated}\n"
                f"ID: {bucket_id}\n"
                f"{detail_prefix}{strip_wikilinks(bucket.get('content', ''))}"
            )
            await bucket_mgr.touch(
                bucket_id,
                wake_dormant=wake_dormant,
            )
        return "=== Dream Details ===\n" + "\n---\n".join(details)

    try:
        all_buckets = await bucket_mgr.list_all(include_archive=False)
    except Exception as e:
        logger.error(f"Dream failed to list buckets: {e}")
        return "记忆系统暂时无法访问。"

    # --- Filter: recent surface-level dynamic buckets (not permanent/pinned/feel) ---
    candidates = [
        b for b in all_buckets
        if b["metadata"].get("type") not in ("permanent", "feel")
        and not b["metadata"].get("pinned", False)
        and not b["metadata"].get("protected", False)
        and not b["metadata"].get("dormant", False)
        and not _is_sealed(b)
    ]

    # --- Sort by latest update time desc, take top 5 ---
    candidates.sort(
        key=lambda b: _bucket_date(b["metadata"], "updated_at", "last_active", "created"),
        reverse=True,
    )
    recent = candidates[:5]
    for bucket in recent:
        await bucket_mgr.touch(
            bucket["id"],
            wake_dormant=wake_dormant,
        )

    if not recent:
        return "没有需要消化的新记忆。"

    parts = []
    for b in recent:
        meta = b["metadata"]
        parts.append(await _dream_summary_line(b))

    header = (
        "=== Dreaming ===\n"
        "以下是你最近的记忆。用第一人称想：\n"
        "- 这些东西里有什么在你这里留下了重量？\n"
        "- 有什么还没想清楚？\n"
        "- 有什么可以放下了？\n"
        "想完之后：值得放下的用 trace(bucket_id, resolved=1)；\n"
        "有沉淀的用 hold(content=\"...\", feel=True, source_bucket=\"bucket_id\", valence=你的感受) 写下来。\n"
        "valence 是你对这段记忆的感受，不是事件本身的情绪。\n"
        "没有沉淀就不写，不强迫产出。\n"
    )

    # --- Connection hint: find most similar pair via embeddings ---
    connection_hint = ""
    if embedding_engine and embedding_engine.enabled and len(recent) >= 2:
        try:
            best_pair = None
            best_sim = 0.0
            ids = [b["id"] for b in recent]
            names = {b["id"]: b["metadata"].get("name", b["id"]) for b in recent}
            embeddings = {}
            for bid in ids:
                emb = await embedding_engine.get_embedding(bid)
                if emb is not None:
                    embeddings[bid] = emb
            for i, id_a in enumerate(ids):
                for id_b in ids[i+1:]:
                    if id_a in embeddings and id_b in embeddings:
                        sim = embedding_engine._cosine_similarity(embeddings[id_a], embeddings[id_b])
                        if sim > best_sim:
                            best_sim = sim
                            best_pair = (id_a, id_b)
            if best_pair and best_sim > 0.5:
                connection_hint = (
                    f"\n💭 [{names[best_pair[0]]}] 和 [{names[best_pair[1]]}] "
                    f"似乎有关联 (相似度:{best_sim:.2f})——不替你下结论，你自己想。\n"
                )
        except Exception as e:
            logger.warning(f"Dream connection hint failed: {e}")

    # --- Feel crystallization hint: detect repeated feel themes ---
    crystal_hint = ""
    if embedding_engine and embedding_engine.enabled:
        try:
            feels = [b for b in all_buckets if b["metadata"].get("type") == "feel"]
            if len(feels) >= 3:
                feel_embeddings = {}
                for f in feels:
                    emb = await embedding_engine.get_embedding(f["id"])
                    if emb is not None:
                        feel_embeddings[f["id"]] = emb
                # Find clusters: feels with similarity > 0.7 to at least 2 others
                for fid, femb in feel_embeddings.items():
                    similar_feels = []
                    for oid, oemb in feel_embeddings.items():
                        if oid != fid:
                            sim = embedding_engine._cosine_similarity(femb, oemb)
                            if sim > 0.7:
                                similar_feels.append(oid)
                    if len(similar_feels) >= 2:
                        feel_bucket = next((f for f in feels if f["id"] == fid), None)
                        if feel_bucket and not feel_bucket["metadata"].get("pinned"):
                            content_preview = strip_wikilinks(feel_bucket["content"][:80])
                            crystal_hint = (
                                f"\n🔮 你已经写过 {len(similar_feels)+1} 条相似的 feel "
                                f"（围绕「{content_preview}…」）。"
                                f"如果这已经是确信而不只是感受了，"
                                f"你可以用 hold(content=\"...\", pinned=True) 升级它。"
                                f"不急，你自己决定。\n"
                            )
                            break
        except Exception as e:
            logger.warning(f"Dream crystallization hint failed: {e}")

    final_text = header + "\n---\n".join(parts) + connection_hint + crystal_hint
    await _fire_webhook("dream", {"recent": len(recent), "chars": len(final_text)})
    return final_text


# =============================================================
# Dashboard API endpoints (for lightweight Web UI)
# 仪表板 API（轻量 Web UI 用）
# =============================================================
def _dashboard_bucket_summary(bucket: dict) -> dict:
    meta = bucket.get("metadata", {})
    return {
        "id": bucket["id"],
        "name": meta.get("name", bucket["id"]),
        "type": meta.get("type", "dynamic"),
        "domain": meta.get("domain", []),
        "tags": meta.get("tags", []),
        "valence": meta.get("valence", 0.5),
        "arousal": meta.get("arousal", 0.3),
        "model_valence": meta.get("model_valence"),
        "importance": meta.get("importance", 5),
        "resolved": meta.get("resolved", False),
        "pinned": meta.get("pinned", False),
        "digested": meta.get("digested", False),
        "created": meta.get("created", ""),
        "last_active": meta.get("last_active", ""),
        "activation_count": meta.get("activation_count", 1),
        "score": decay_engine.calculate_score(meta),
        "content_preview": strip_wikilinks(bucket.get("content", ""))[:200],
    }


_DASHBOARD_BUCKET_ID_MARKER_RE = re.compile(
    r"(?:^|\[)\s*bucket_id\s*:\s*([0-9a-fA-F]+)(?![0-9A-Za-z])",
    re.IGNORECASE,
)
_DASHBOARD_LEADING_BUCKET_ID_RE = re.compile(
    r"^\s*([0-9a-fA-F]+)(?=$|\s+name\s*=)",
    re.IGNORECASE,
)
_DASHBOARD_ID_PREFIX_RE = re.compile(r"^id\s*:\s*(.*)$", re.IGNORECASE)
_DASHBOARD_NAME_PREFIX_RE = re.compile(r"^name\s*:\s*(.*)$", re.IGNORECASE)
_DASHBOARD_BODY_BUCKET_ID_RE = re.compile(r"\b[0-9a-f]{12}\b", re.IGNORECASE)


def _dashboard_valid_bucket_id_prefix(value: str) -> str:
    """Return a canonical 6-12 character hexadecimal Dashboard bucket prefix."""
    candidate = str(value or "").strip()
    if 6 <= len(candidate) <= 12 and re.fullmatch(r"[0-9a-fA-F]+", candidate):
        return candidate.casefold()
    return ""


def _dashboard_extract_bucket_id_prefix(value: str) -> str:
    """Extract a bucket prefix from Dashboard-friendly OB output formats."""
    text = str(value or "").strip()
    marker = _DASHBOARD_BUCKET_ID_MARKER_RE.search(text)
    if marker:
        return _dashboard_valid_bucket_id_prefix(marker.group(1))
    leading = _DASHBOARD_LEADING_BUCKET_ID_RE.match(text)
    if leading:
        return _dashboard_valid_bucket_id_prefix(leading.group(1))
    return ""


def _dashboard_search_query(raw_query: str) -> dict:
    """Normalize Dashboard-only ID/name query syntax without changing MCP search."""
    query = str(raw_query or "").strip()
    name_match = _DASHBOARD_NAME_PREFIX_RE.match(query)
    if name_match:
        return {
            "mode": "name",
            "normalized_query": name_match.group(1).strip(),
            "id_prefix": "",
        }

    id_match = _DASHBOARD_ID_PREFIX_RE.match(query)
    if id_match:
        candidate = id_match.group(1).strip()
        return {
            "mode": "id",
            "normalized_query": (
                _dashboard_extract_bucket_id_prefix(candidate)
                or _dashboard_valid_bucket_id_prefix(candidate)
                or candidate.casefold()
            ),
            "id_prefix": (
                _dashboard_extract_bucket_id_prefix(candidate)
                or _dashboard_valid_bucket_id_prefix(candidate)
            ),
        }

    id_prefix = _dashboard_extract_bucket_id_prefix(query)
    if id_prefix:
        return {
            "mode": "id",
            "normalized_query": id_prefix,
            "id_prefix": id_prefix,
        }
    return {"mode": "text", "normalized_query": query, "id_prefix": ""}


def _dashboard_search_result(
    bucket: dict,
    *,
    score: float | None = None,
    match_reason: str = "",
    reference_kinds: list[str] | None = None,
) -> dict:
    """Return a stable Dashboard-only search result, including sealed state."""
    result = _dashboard_bucket_summary(bucket)
    if score is not None:
        result["score"] = score
    result["sealed"] = bool(int(bucket.get("metadata", {}).get("sealed", 0) or 0))
    if match_reason:
        result["match_reason"] = match_reason
    if reference_kinds:
        result["reference_kinds"] = reference_kinds
    return result


def _dashboard_name_matches(all_buckets: list[dict], query: str) -> list[dict]:
    """Provide a Dashboard name-first ordering without changing generic search."""
    needle = query.casefold()
    if not needle:
        return []
    return [
        bucket
        for bucket in all_buckets
        if needle in str(bucket.get("metadata", {}).get("name", "")).casefold()
    ]


def _dashboard_bucket_links(content: str, all_buckets: list[dict]) -> dict[str, dict]:
    """Describe every full bucket ID mentioned in Dashboard display content."""
    mentioned_ids = {
        match.group(0).casefold()
        for match in _DASHBOARD_BODY_BUCKET_ID_RE.finditer(str(content or ""))
    }
    buckets_by_id = {
        str(bucket.get("id", "")).casefold(): bucket
        for bucket in all_buckets
    }
    links = {}
    for bucket_id in sorted(mentioned_ids):
        target = buckets_by_id.get(bucket_id)
        if not target:
            links[bucket_id] = {"id": bucket_id, "exists": False}
            continue
        meta = target.get("metadata", {})
        links[bucket_id] = {
            "id": target.get("id", bucket_id),
            "exists": True,
            "name": meta.get("name", target.get("id", bucket_id)),
            "sealed": bool(int(meta.get("sealed", 0) or 0)),
            "dormant": bool(meta.get("dormant", False)),
            "type": meta.get("type", "dynamic"),
        }
    return links


def _dashboard_bucket_references(
    all_buckets: list[dict], target_ids: set[str], *, include_dormant: bool = False
) -> list[dict]:
    """Find Dashboard-visible content and related_buckets references to target IDs."""
    if not target_ids:
        return []
    id_patterns = {
        target_id: re.compile(
            rf"(?<![0-9a-fA-F]){re.escape(target_id)}(?![0-9a-fA-F])",
            re.IGNORECASE,
        )
        for target_id in target_ids
    }
    results = []
    for bucket in all_buckets:
        kinds = []
        content = str(bucket.get("content", ""))
        if any(pattern.search(content) for pattern in id_patterns.values()):
            kinds.append("content")
        related_ids = {
            relation_id.casefold()
            for relation_id in _related_ids(bucket.get("metadata", {}))
        }
        if related_ids & target_ids:
            kinds.append("related_buckets")
        if kinds:
            result = _dashboard_search_result(
                bucket,
                match_reason="reference",
                reference_kinds=kinds,
            )
            if include_dormant:
                result["dormant"] = bool(
                    bucket.get("metadata", {}).get("dormant", False)
                )
            results.append(result)
    return results


def _is_session_archive(bucket: dict) -> bool:
    meta = bucket.get("metadata", {})
    domains = {
        str(domain).strip().casefold()
        for domain in meta.get("domain", [])
    }
    return meta.get("type") == "archived" and "session" in domains


def _dashboard_pagination(request, *, default_limit: int = 20) -> tuple[int, int]:
    return asset_dashboard.parse_pagination(
        request.query_params.get("limit", str(default_limit)),
        request.query_params.get("offset", "0"),
    )

@mcp.custom_route("/api/buckets", methods=["GET"])
async def api_buckets(request):
    """List active memory buckets with metadata."""
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err:
        return err
    try:
        all_buckets = await bucket_mgr.list_all(include_archive=False)
        result = [_dashboard_bucket_summary(bucket) for bucket in all_buckets]
        result.sort(key=lambda item: item["score"], reverse=True)
        return JSONResponse(result)
    except Exception:
        logger.exception("Dashboard bucket listing failed")
        return JSONResponse({"error": "bucket_list_failed"}, status_code=500)


@mcp.custom_route("/api/archives", methods=["GET"])
async def api_archives(request):
    """List archived conversations separately from ordinary memory buckets."""
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err:
        return err
    try:
        limit, offset = _dashboard_pagination(request)
        query = request.query_params.get("q", "").strip().casefold()
        archives = [
            _dashboard_bucket_summary(bucket)
            for bucket in await bucket_mgr.list_all(include_archive=True)
            if _is_session_archive(bucket)
        ]
        if query:
            archives = [
                item for item in archives
                if query in item["id"].casefold()
                or query in item["name"].casefold()
                or query in item["content_preview"].casefold()
                or any(query in str(tag).casefold() for tag in item["tags"])
            ]
        archives.sort(
            key=lambda item: (item["last_active"] or item["created"], item["id"]),
            reverse=True,
        )
        return JSONResponse({
            "total": len(archives),
            "offset": offset,
            "limit": limit,
            "results": archives[offset:offset + limit],
        })
    except AssetDashboardError as exc:
        return JSONResponse({"error": exc.code}, status_code=exc.status_code)
    except Exception:
        logger.exception("Dashboard archive listing failed")
        return JSONResponse({"error": "archive_list_failed"}, status_code=500)

@mcp.custom_route("/api/bucket/{bucket_id}", methods=["GET", "PATCH", "DELETE"])
@guarded_http_mutation(
    "dashboard_bucket_mutation",
    methods=("PATCH", "DELETE"),
)
async def api_bucket_detail(request):
    """Get, update, or delete bucket content by ID."""
    from starlette.responses import JSONResponse

    method = request.method.upper()
    route = "/api/bucket/{bucket_id}"
    err = (
        _require_dashboard_write(request, route)
        if method in {"PATCH", "DELETE"}
        else _require_auth(request)
    )
    if err:
        return err
    bucket_id = request.path_params["bucket_id"]
    if method == "DELETE":
        try:
            raw_body = await request.body()
            body = _json_lib.loads(raw_body) if raw_body else {}
        except (UnicodeDecodeError, ValueError):
            return _dashboard_write_error(route,400,"invalid_json")
        if not isinstance(body,dict) or set(body)-{"confirm_token"} or not isinstance(body.get("confirm_token",""),str):
            return _dashboard_write_error(route,400,"invalid_delete_request")
        token = body.get("confirm_token","")
        exists = await bucket_mgr.get(bucket_id)
        rows = bucket_mgr.confirmed_delete_rows(token_hash=bucket_mgr.confirmed_token_hash(token.strip())) if token else []
        if not exists and not rows:
            return JSONResponse({"error":"not found"},status_code=404)
        try:
            outcome = await _delete_with_confirmation([bucket_id],token)
        except Exception:
            return _dashboard_write_error(route,500,"bucket_delete_failed")
        return _dashboard_delete_response(bucket_id,outcome)
    bucket = await bucket_mgr.get(bucket_id)
    if not bucket:
        return JSONResponse({"error": "not found"}, status_code=404)
    meta = bucket.get("metadata", {})

    if method == "PATCH":
        try:
            body = await request.json()
        except Exception:
            return _dashboard_write_error(route, 400, "invalid_json")
        if not isinstance(body, dict) or set(body) != {"content"}:
            return _dashboard_write_error(route, 400, "content_only")
        if not isinstance(body["content"], str):
            return _dashboard_write_error(route, 400, "invalid_content")
        try:
            updated = await bucket_mgr.update(
                bucket_id,
                content=body["content"],
                _history_change_type="dashboard_replace",
            )
        except Exception:
            logger.error(
                "Dashboard bucket content update failed route=%s "
                "code=content_update_failed",
                route,
            )
            return _dashboard_write_error(route, 500, "content_update_failed")
        if not updated:
            return _dashboard_write_error(route, 500, "content_update_failed")
        bucket = await bucket_mgr.get(bucket_id) or bucket
        meta = bucket.get("metadata", {})


    response = {
        "id": bucket["id"],
        "metadata": meta,
        "content": strip_wikilinks(bucket.get("content", "")),
        "raw_content": bucket.get("content", ""),
        "score": decay_engine.calculate_score(meta),
    }
    if method == "GET":
        display_content = response["content"]
        try:
            all_buckets = await bucket_mgr.list_all(include_archive=True)
        except Exception:
            logger.exception("Dashboard bucket detail enrichment failed")
            return JSONResponse({"error": "bucket_detail_enrichment_failed"}, status_code=500)
        response["bucket_links"] = _dashboard_bucket_links(
            display_content, all_buckets
        )
        response["referenced_by"] = _dashboard_bucket_references(
            all_buckets,
            {str(bucket.get("id", "")).casefold()},
            include_dormant=True,
        )
    return JSONResponse(response)


@mcp.custom_route("/api/search", methods=["GET"])
async def api_search(request):
    """Search Dashboard buckets with optional ID/name-specific result groups."""
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err:
        return err
    query = str(request.query_params.get("q", "")).strip()
    if not query:
        return JSONResponse({"error": "missing q parameter"}, status_code=400)
    try:
        parsed = _dashboard_search_query(query)
        normalized_query = parsed["normalized_query"]
        all_buckets = await bucket_mgr.list_all(include_archive=True)
        generic_matches = await bucket_mgr.search(
            normalized_query,
            limit=10,
            include_sealed=True,
        ) if normalized_query else []

        id_matches = []
        references = []
        if parsed["mode"] == "id":
            prefix = parsed["id_prefix"]
            matched_buckets = [
                bucket for bucket in all_buckets
                if prefix and str(bucket.get("id", "")).casefold().startswith(prefix)
            ]
            matched_buckets.sort(key=lambda bucket: (
                str(bucket.get("id", "")).casefold() != prefix,
                str(bucket.get("id", "")).casefold(),
            ))
            id_matches = [
                _dashboard_search_result(
                    bucket,
                    match_reason=(
                        "id_exact"
                        if str(bucket.get("id", "")).casefold() == prefix and len(prefix) == 12
                        else "id_prefix"
                    ),
                )
                for bucket in matched_buckets
            ]
            references = _dashboard_bucket_references(
                all_buckets,
                {str(bucket.get("id", "")).casefold() for bucket in matched_buckets},
            )

        related_by_id = {}
        if parsed["mode"] == "name":
            for bucket in _dashboard_name_matches(all_buckets, normalized_query):
                related_by_id[str(bucket.get("id", ""))] = _dashboard_search_result(
                    bucket,
                    match_reason="name",
                )
        for bucket in generic_matches:
            bucket_id = str(bucket.get("id", ""))
            related_by_id.setdefault(
                bucket_id,
                _dashboard_search_result(
                    bucket,
                    score=bucket.get("score", 0),
                    match_reason="related",
                ),
            )

        return JSONResponse({
            "query": query,
            "mode": parsed["mode"],
            "normalized_query": normalized_query,
            "groups": {
                "id_matches": id_matches,
                "references": references,
                "related": list(related_by_id.values()),
            },
        })
    except Exception:
        logger.exception("Dashboard search failed")
        return JSONResponse({"error": "search_failed"}, status_code=500)


@mcp.custom_route("/api/network", methods=["GET"])
async def api_network(request):
    """Get embedding similarity network for visualization."""
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err: return err
    try:
        all_buckets = await bucket_mgr.list_all(include_archive=False)
        nodes = []
        edges = []
        embeddings = {}

        for b in all_buckets:
            meta = b.get("metadata", {})
            bid = b["id"]
            nodes.append({
                "id": bid,
                "name": meta.get("name", bid),
                "type": meta.get("type", "dynamic"),
                "domain": meta.get("domain", []),
                "valence": meta.get("valence", 0.5),
                "arousal": meta.get("arousal", 0.3),
                "score": decay_engine.calculate_score(meta),
                "resolved": meta.get("resolved", False),
                "pinned": meta.get("pinned", False),
                "digested": meta.get("digested", False),
            })
            if embedding_engine and embedding_engine.enabled:
                emb = await embedding_engine.get_embedding(bid)
                if emb is not None:
                    embeddings[bid] = emb

        # Build edges from embeddings (similarity > 0.5)
        ids = list(embeddings.keys())
        for i, id_a in enumerate(ids):
            for id_b in ids[i+1:]:
                sim = embedding_engine._cosine_similarity(embeddings[id_a], embeddings[id_b])
                if sim > 0.5:
                    edges.append({"source": id_a, "target": id_b, "similarity": round(sim, 3)})

        return JSONResponse({"nodes": nodes, "edges": edges})
    except Exception: logger.exception("Dashboard network failed"); return JSONResponse({"error": "network_failed"}, status_code=500)


@mcp.custom_route("/api/breath-debug", methods=["GET"])
async def api_breath_debug(request):
    """Explain the real query Breath path without mutating memory state."""
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err:
        return err
    query = request.query_params.get("q", "").strip()

    def parse_float(name: str) -> float | None:
        value = request.query_params.get(name)
        if value in (None, ""):
            return None
        try:
            return float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{name} must be numeric.") from exc

    def parse_int(name: str, default: int) -> int:
        value = request.query_params.get(name)
        if value in (None, ""):
            return default
        try:
            return int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{name} must be an integer.") from exc

    def parse_bool(name: str, default: bool = False) -> bool:
        value = request.query_params.get(name)
        if value in (None, ""):
            return default
        return str(value).strip().lower() in {"1", "true", "yes", "on"}

    try:
        q_valence = parse_float("valence")
        q_arousal = parse_float("arousal")
        recent_days = parse_int("recent_days", -1)
        max_results = max(1, min(parse_int("max_results", 5), 50))
        max_tokens = max(1, min(parse_int("max_tokens", 10000), 20000))
        date_from = _parse_date_filter(
            request.query_params.get("date_from", ""), "date_from"
        )
        date_to = _parse_date_filter(
            request.query_params.get("date_to", ""), "date_to"
        )
        if date_from and date_to and date_from > date_to:
            raise ValueError("date_from cannot be later than date_to.")
        tags_filter = _normalize_breath_filter(
            [item for item in request.query_params.get("tags", "").split(",") if item.strip()],
            "tags",
            apply_aliases=True,
        )
        topic_filter = _normalize_breath_filter(
            [item for item in request.query_params.get("topics", "").split(",") if item.strip()],
            "topics",
        )
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)

    domain_values = [
        item.strip()
        for item in request.query_params.get("domain", "").split(",")
        if item.strip()
    ]
    domain_set = {item.casefold() for item in domain_values}
    include_dormant = parse_bool("include_dormant")
    include_sealed = parse_bool("include_sealed")
    unsupported_paths = []
    if not query:
        unsupported_paths.append("no_query_surfacing")
    if topic_filter:
        unsupported_paths.append("session_archive_topic_route")
    if domain_set & {"session", "feel"}:
        unsupported_paths.append("session_or_feel_route")
    if request.query_params.get("importance_min") not in (None, "", "-1"):
        unsupported_paths.append("importance_route")
    resonance = request.query_params.get("resonance", "").strip()
    if resonance and not query:
        unsupported_paths.append("no_query_resonance_route")
    if unsupported_paths:
        if not query:
            try:
                await bucket_mgr.list_all(include_archive=False)
            except Exception:
                logger.exception("Dashboard breath debug failed")
                return JSONResponse(
                    {"error": "breath_debug_failed"},
                    status_code=500,
                )
        return JSONResponse({
            "status": "unsupported_route",
            "equivalence": "untraced",
            "query": query,
            "filters": {
                "domain": domain_values,
                "date_from": date_from,
                "date_to": date_to,
                "recent_days": recent_days,
                "tags": tags_filter,
                "topics": topic_filter,
                "include_dormant": include_dormant,
                "include_sealed": include_sealed,
            },
            "unsupported_paths": sorted(set(unsupported_paths)),
            "results": [],
        })

    try:
        w = {
            "topic": bucket_mgr.w_topic,
            "emotion": bucket_mgr.w_emotion,
            "time": bucket_mgr.w_time,
            "importance": bucket_mgr.w_importance,
        }
        recent_cutoff = _recent_cutoff(recent_days)
        all_buckets = await bucket_mgr.list_all(include_archive=False)
        visible_buckets = [
            bucket
            for bucket in all_buckets
            if include_sealed or not _is_sealed(bucket)
        ]
        structured_filters = bool(tags_filter)
        candidate_buckets = _filter_breath_candidates(
            visible_buckets, domain_values=domain_values,
            recent_cutoff=recent_cutoff, recent_days=recent_days,
            include_dormant=include_dormant, include_sealed=True,
            date_from=date_from, date_to=date_to,
            tags_filter=tags_filter, topic_filter=[],
        )
        search_domain_filter = None
        search_include_dormant = include_dormant
        candidate_source = "privacy_filtered_active_buckets"
        if structured_filters:
            candidate_buckets = _filter_breath_candidates(
                visible_buckets,
                domain_values=domain_values,
                recent_cutoff=recent_cutoff,
                recent_days=recent_days,
                include_dormant=include_dormant,
                include_sealed=True,
                date_from=date_from,
                date_to=date_to,
                tags_filter=tags_filter,
                topic_filter=[],
            )
            search_domain_filter = None
            search_include_dormant = True
            candidate_source = "structured_filtered_active_buckets"

        search_trace = {}
        matches = await bucket_mgr.search(
            query,
            limit=1000,
            domain_filter=search_domain_filter,
            query_valence=q_valence,
            query_arousal=q_arousal,
            include_dormant=search_include_dormant,
            include_sealed=include_sealed,
            candidate_buckets=candidate_buckets,
            trace=search_trace,
        ) if candidate_buckets else []
        search_trace["candidate_source"] = candidate_source
        if not structured_filters:
            matches = _filter_breath_query_matches(
                matches,
                recent_cutoff=recent_cutoff,
                date_from=date_from,
                date_to=date_to,
                include_sealed=True,
            )
        if resonance:
            resonance_target = _parse_resonance(resonance)
            matches.sort(key=lambda bucket: _resonance_distance(bucket, resonance_target))
            search_trace["ranking"] = [
                str(bucket.get("id", "")) for bucket in matches
            ]
            for route_rank, bucket in enumerate(matches, start=1):
                entry = next(
                    (
                        item
                        for item in search_trace.get("candidates", [])
                        if item.get("id") == str(bucket.get("id", ""))
                    ),
                    None,
                )
                if entry is not None:
                    entry["route_rank"] = route_rank
        hidden_count = max(0, len(matches) - max_results)
        selected_matches = matches[:max_results]
        trace_by_id = {
            str(entry.get("id", "")): entry
            for entry in search_trace.get("candidates", [])
        }
        selected_ids = {str(bucket.get("id", "")) for bucket in selected_matches}
        matched_ids = {str(bucket.get("id", "")) for bucket in matches}
        for entry in trace_by_id.values():
            if entry.get("admitted") and entry["id"] not in matched_ids:
                entry["final_decision"] = "excluded_post_search_filter"
                entry.setdefault("exclusion_reasons", []).append("date_or_recent_filter")
            elif entry.get("admitted") and entry["id"] not in selected_ids:
                entry["final_decision"] = "omitted_max_results"

        final_text, composition = await _compose_breath_query_matches(
            selected_matches,
            max_tokens=max_tokens,
            q_valence=q_valence,
            emotion_trend=False,
            hidden_count=hidden_count,
            total_matches=len(matches),
            trace_by_id=trace_by_id,
            touch=False,
            cache=False,
        )
        bucket_by_id = {
            str(bucket.get("id", "")): bucket for bucket in visible_buckets
        }
        results = []
        for entry in trace_by_id.values():
            if not entry.get("eligible", True):
                continue
            bucket = bucket_by_id.get(entry["id"])
            if bucket is None:
                continue
            meta = bucket.get("metadata", {})
            scores = entry.get("scores", {})
            results.append({
                **entry,
                "name": meta.get("name", entry["id"]),
                "domain": meta.get("domain", []),
                "type": meta.get("type", "dynamic"),
                "resolved": bool(meta.get("resolved", False)),
                "pinned": bool(meta.get("pinned", False)),
                "tags": _structured_metadata_values(meta, "tags"),
                "weights": w,
                "raw_total": entry.get("pre_penalty_score", 0),
                "normalized": entry.get("final_ranking_score", entry.get("pre_penalty_score", 0)),
                "semantic_score": scores.get("semantic", 0),
                "passed_threshold": bool(entry.get("admitted", False)),
            })
        rank_order = {
            bid: rank for rank, bid in enumerate(search_trace.get("ranking", []), start=1)
        }
        results.sort(
            key=lambda item: (
                0 if item.get("final_decision") == "surfaced" else 1,
                rank_order.get(item["id"], 100000),
                -float(item.get("final_ranking_score", item.get("pre_penalty_score", 0))),
            )
        )
        return JSONResponse({
            "status": "ok",
            "equivalence": "runtime_query_trace",
            "query": query,
            "valence": q_valence,
            "arousal": q_arousal,
            "filters": {
                "domain": domain_values,
                "date_from": date_from,
                "date_to": date_to,
                "recent_days": recent_days,
                "tags": tags_filter,
                "include_dormant": include_dormant,
                "include_sealed": include_sealed,
                "resonance": resonance,
            },
            "candidate_source": candidate_source,
            "weights": w,
            "threshold": bucket_mgr.fuzzy_threshold,
            "semantic": search_trace.get("semantic", {}),
            "total_candidates": len(results),
            "passed_count": sum(1 for item in results if item["passed_threshold"]),
            "final_composition": composition,
            "trace": {
                "candidate_count": search_trace.get("candidate_count", len(results)),
                "eligible_count": search_trace.get("eligible_count", 0),
                "admitted_count": search_trace.get("admitted_count", 0),
                "ranking": search_trace.get("ranking", []),
            },
            "results": results[:50],
        })
    except Exception:
        logger.exception("Dashboard breath debug failed")
        return JSONResponse({"error": "breath_debug_failed"}, status_code=500)


@mcp.custom_route("/api/assets", methods=["GET", "POST"])
@guarded_http_mutation("dashboard_asset_create", methods=("POST",))
async def api_assets(request):
    """List or create cleaned Remember-Me image assets for the Dashboard."""
    from starlette.responses import JSONResponse

    if request.method.upper() == "POST":
        route = "/api/assets"
        err = _require_dashboard_write(request, route)
        if err:
            return err
        try:
            upload = await asset_dashboard.parse_upload(request)
            asset = await asyncio.to_thread(asset_dashboard.create_asset, upload)
            backend = _selected_asset_backend()
            if backend.name == "legacy":
                stored = backend.get(asset["asset_id"])
                if stored:
                    try:
                        await asset_embedding_index.index_asset(stored)
                    except Exception:
                        logger.warning("Dashboard asset embedding refresh failed after upload")
            return JSONResponse(asset, status_code=200 if asset["deduplicated"] else 201)
        except AssetDashboardError as exc:
            return _dashboard_write_error(route, exc.status_code, exc.code)
        except Exception:
            logger.error(
                "Dashboard write failed route=%s status=500 code=asset_upload_failed",
                route,
            )
            return JSONResponse({"error": "asset_upload_failed"}, status_code=500)

    err = _require_auth(request)
    if err:
        return err
    try:
        limit, offset = _dashboard_pagination(request)
        result = await asyncio.to_thread(
            asset_dashboard.list_assets,
            query=request.query_params.get("q", ""),
            tag=request.query_params.get("tag", ""),
            limit=limit,
            offset=offset,
        )
        return JSONResponse(result)
    except AssetDashboardError as exc:
        return JSONResponse({"error": exc.code}, status_code=exc.status_code)
    except Exception:
        logger.error("Dashboard asset listing failed")
        return JSONResponse({"error": "asset_list_failed"}, status_code=500)

@mcp.custom_route("/api/assets/{asset_id}", methods=["GET", "PATCH", "DELETE"])
@guarded_http_mutation(
    "dashboard_asset_mutation",
    methods=("PATCH", "DELETE"),
)
async def api_asset_detail(request):
    """Read, edit, or permanently delete one cleaned image asset."""
    from starlette.responses import JSONResponse

    asset_id = request.path_params["asset_id"]
    method = request.method.upper()
    route = "/api/assets/{asset_id}"
    if method in {"PATCH", "DELETE"}:
        err = _require_dashboard_write(request, route)
    else:
        err = _require_auth(request)
    if err:
        return err
    try:
        if method == "GET":
            return JSONResponse(asset_dashboard.get_asset(asset_id))
        if method == "PATCH":
            try:
                payload = await request.json()
            except Exception:
                return _dashboard_write_error(route, 400, "invalid_json")
            asset = await asyncio.to_thread(
                asset_dashboard.update_asset,
                asset_id,
                payload,
            )
            backend = _selected_asset_backend()
            if backend.name == "legacy":
                stored = backend.get(asset_id)
                if stored:
                    try:
                        await asset_embedding_index.index_asset(stored)
                    except Exception:
                        logger.warning("Dashboard asset embedding refresh failed after metadata update")
            return JSONResponse(asset)
        result = await asyncio.to_thread(asset_dashboard.delete_asset, asset_id)
        return JSONResponse(result)
    except AssetDashboardError as exc:
        if method in {"PATCH", "DELETE"}:
            return _dashboard_write_error(route, exc.status_code, exc.code)
        return JSONResponse({"error": exc.code}, status_code=exc.status_code)
    except Exception:
        if method in {"PATCH", "DELETE"}:
            logger.error(
                "Dashboard write failed route=%s status=500 code=asset_operation_failed",
                route,
            )
        else:
            logger.error("Dashboard asset detail failed")
        return JSONResponse({"error": "asset_operation_failed"}, status_code=500)

@mcp.custom_route("/api/assets/{asset_id}/thumbnail", methods=["GET"])
async def api_asset_thumbnail(request):
    """Return a bounded thumbnail generated from the cleaned stored image."""
    from starlette.responses import JSONResponse, Response
    err = _require_auth(request)
    if err:
        return err
    try:
        image = asset_dashboard.resolve_image(
            request.path_params["asset_id"],
            thumbnail=True,
        )
        return Response(
            image.thumbnail_bytes,
            media_type=image.mime_type,
            headers={
                "Cache-Control": "private, no-store",
                "X-Content-Type-Options": "nosniff",
            },
        )
    except AssetDashboardError as exc:
        return JSONResponse({"error": exc.code}, status_code=exc.status_code)
    except Exception:
        logger.error("Dashboard asset thumbnail failed")
        return JSONResponse({"error": "asset_image_failed"}, status_code=500)


@mcp.custom_route("/api/assets/{asset_id}/image", methods=["GET", "HEAD"])
async def api_asset_image(request):
    """Stream a cleaned stored image inside the Dashboard auth boundary."""
    from starlette.responses import FileResponse, JSONResponse, Response
    err = _require_auth(request)
    if err:
        return err
    try:
        image = asset_dashboard.resolve_image(request.path_params["asset_id"])
        headers = {
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
            "Content-Disposition": "inline",
        }
        if image.path is not None:
            return FileResponse(
                image.path,
                media_type=image.mime_type,
                headers=headers,
            )
        return Response(
            content=image.content,
            media_type=image.mime_type,
            headers=headers,
        )
    except AssetDashboardError as exc:
        return JSONResponse({"error": exc.code}, status_code=exc.status_code)
    except Exception:
        logger.error("Dashboard asset image failed")
        return JSONResponse({"error": "asset_image_failed"}, status_code=500)


@mcp.custom_route("/dashboard-assets.css", methods=["GET"])
async def dashboard_assets_styles(request):
    """Serve styles for the reusable read-only asset browser component."""
    from starlette.responses import PlainTextResponse
    style_path = os.path.join(os.path.dirname(__file__), "dashboard_assets.css")
    try:
        with open(style_path, "r", encoding="utf-8") as handle:
            return PlainTextResponse(
                handle.read(),
                media_type="text/css",
                headers={"Cache-Control": "no-cache"},
            )
    except FileNotFoundError:
        return PlainTextResponse("", status_code=404)

@mcp.custom_route("/dashboard-assets.js", methods=["GET"])
async def dashboard_assets_script(request):
    """Serve the reusable read-only asset browser component."""
    from starlette.responses import PlainTextResponse
    script_path = os.path.join(os.path.dirname(__file__), "dashboard_assets.js")
    try:
        with open(script_path, "r", encoding="utf-8") as handle:
            return PlainTextResponse(
                handle.read(),
                media_type="application/javascript",
                headers={"Cache-Control": "no-cache"},
            )
    except FileNotFoundError:
        return PlainTextResponse("", status_code=404)

@mcp.custom_route("/dashboard", methods=["GET"])
async def dashboard(request):
    """Serve the dashboard HTML page."""
    from starlette.responses import HTMLResponse
    import os
    dashboard_path = os.path.join(os.path.dirname(__file__), "dashboard.html")
    try:
        with open(dashboard_path, "r", encoding="utf-8") as f:
            return HTMLResponse(f.read())
    except FileNotFoundError:
        return HTMLResponse("<h1>dashboard.html not found</h1>", status_code=404)


@mcp.custom_route("/api/config", methods=["GET"])
async def api_config_get(request):
    """Get current runtime config (safe fields only, API key masked)."""
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err: return err
    dehy = config.get("dehydration", {})
    emb = config.get("embedding", {})
    api_key = dehy.get("api_key", "")
    masked_key = f"{api_key[:4]}...{api_key[-4:]}" if len(api_key) > 8 else ("***" if api_key else "")
    return JSONResponse({
        "dehydration": {
            "model": dehy.get("model", ""),
            "base_url": dehy.get("base_url", ""),
            "api_key_masked": masked_key,
            "max_tokens": dehy.get("max_tokens", 1024),
            "temperature": dehy.get("temperature", 0.1),
        },
        "embedding": {
            "enabled": emb.get("enabled", False),
            "model": emb.get("model", ""),
        },
        "merge_threshold": config.get("merge_threshold", 75),
        "transport": config.get("transport", "stdio"),
        "buckets_dir": config.get("buckets_dir", ""),
    })


@mcp.custom_route("/api/config", methods=["POST"])
@guarded_http_mutation("dashboard_config_write", methods=("POST",))
async def api_config_update(request):
    """Hot-update runtime config. Optionally persist to config.yaml."""
    from starlette.responses import JSONResponse
    import yaml
    err = _require_dashboard_write(request, "/api/config")
    if err: return err
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON"}, status_code=400)

    updated = []

    # --- Dehydration config ---
    if "dehydration" in body:
        d = body["dehydration"]
        dehy = config.setdefault("dehydration", {})
        for key in ("model", "base_url", "max_tokens", "temperature"):
            if key in d:
                dehy[key] = d[key]
                updated.append(f"dehydration.{key}")
        if "api_key" in d and d["api_key"]:
            dehy["api_key"] = d["api_key"]
            updated.append("dehydration.api_key")
        # Hot-reload dehydrator
        dehydrator.model = dehy.get("model", "deepseek-chat")
        dehydrator.base_url = dehy.get("base_url", "")
        dehydrator.api_key = dehy.get("api_key", "")
        if hasattr(dehydrator, "client") and dehydrator.api_key:
            from openai import AsyncOpenAI
            dehydrator.client = AsyncOpenAI(
                api_key=dehydrator.api_key,
                base_url=dehydrator.base_url,
                timeout=60.0,
                max_retries=2,
            )

    # --- Embedding config ---
    if "embedding" in body:
        e = body["embedding"]
        emb = config.setdefault("embedding", {})
        if "enabled" in e:
            emb["enabled"] = bool(e["enabled"])
            embedding_engine.enabled = emb["enabled"]
            updated.append("embedding.enabled")
        if "model" in e:
            emb["model"] = e["model"]
            embedding_engine.model = emb["model"]
            updated.append("embedding.model")

    # --- Merge threshold ---
    if "merge_threshold" in body:
        config["merge_threshold"] = int(body["merge_threshold"])
        updated.append("merge_threshold")

    # --- Persist to config.yaml if requested ---
    if body.get("persist", False):
        config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.yaml")
        try:
            save_config = {}
            if os.path.exists(config_path):
                with open(config_path, "r", encoding="utf-8") as f:
                    save_config = yaml.safe_load(f) or {}

            if "dehydration" in body:
                sc_dehy = save_config.setdefault("dehydration", {})
                for key in ("model", "base_url", "max_tokens", "temperature"):
                    if key in body["dehydration"]:
                        sc_dehy[key] = body["dehydration"][key]
                # Never persist api_key to yaml (use env var)

            if "embedding" in body:
                sc_emb = save_config.setdefault("embedding", {})
                for key in ("enabled", "model"):
                    if key in body["embedding"]:
                        sc_emb[key] = body["embedding"][key]

            if "merge_threshold" in body:
                save_config["merge_threshold"] = int(body["merge_threshold"])

            with open(config_path, "w", encoding="utf-8") as f:
                yaml.dump(save_config, f, default_flow_style=False, allow_unicode=True)
            updated.append("persisted_to_yaml")
        except Exception: logger.exception("Dashboard config persistence failed"); return JSONResponse({"error": "persist_failed", "updated": updated}, status_code=500)

    return JSONResponse({"updated": updated, "ok": True})


# =============================================================
# /api/host-vault — read/write the host-side OMBRE_HOST_VAULT_DIR
# 用于在 Dashboard 设置 docker-compose 挂载的宿主机记忆桶目录。
# 写入项目根目录的 .env 文件，需 docker compose down/up 才能生效。
# =============================================================

def _project_env_path() -> str:
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")


def _read_env_var(name: str) -> str:
    """Return current value of `name` from process env first, then .env file (best-effort)."""
    val = os.environ.get(name, "").strip()
    if val:
        return val
    env_path = _project_env_path()
    if not os.path.exists(env_path):
        return ""
    try:
        with open(env_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                if k.strip() == name:
                    return v.strip().strip('"').strip("'")
    except Exception:
        pass
    return ""


@guarded_mutation("dashboard_env_write")
def _write_env_var(name: str, value: str) -> None:
    """
    Idempotent upsert of `NAME=value` in project .env. Creates the file if missing.
    Preserves other entries verbatim. Quotes values containing spaces.
    """
    env_path = _project_env_path()
    quoted = f'"{value}"' if value and (" " in value or "#" in value) else value
    new_line = f"{name}={quoted}\n"

    lines: list[str] = []
    if os.path.exists(env_path):
        with open(env_path, "r", encoding="utf-8") as f:
            lines = f.readlines()

    replaced = False
    for i, raw in enumerate(lines):
        stripped = raw.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        k, _, _v = stripped.partition("=")
        if k.strip() == name:
            lines[i] = new_line
            replaced = True
            break
    if not replaced:
        if lines and not lines[-1].endswith("\n"):
            lines[-1] += "\n"
        lines.append(new_line)

    with open(env_path, "w", encoding="utf-8") as f:
        f.writelines(lines)


@mcp.custom_route("/api/host-vault", methods=["GET"])
async def api_host_vault_get(request):
    """Read the current OMBRE_HOST_VAULT_DIR (process env > project .env)."""
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err: return err
    value = _read_env_var("OMBRE_HOST_VAULT_DIR")
    return JSONResponse({
        "value": value,
        "source": "env" if os.environ.get("OMBRE_HOST_VAULT_DIR", "").strip() else ("file" if value else ""),
        "env_file": _project_env_path(),
    })


@mcp.custom_route("/api/host-vault", methods=["POST"])
@guarded_http_mutation("dashboard_vault_write", methods=("POST",))
async def api_host_vault_set(request):
    """
    Persist OMBRE_HOST_VAULT_DIR to the project .env file.
    Body: {"value": "/path/to/vault"}  (empty string clears the entry)
    Note: container restart is required for docker-compose to pick up the new mount.
    """
    from starlette.responses import JSONResponse
    err = _require_dashboard_write(request, "/api/host-vault")
    if err: return err
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON"}, status_code=400)

    raw = body.get("value", "")
    if not isinstance(raw, str):
        return JSONResponse({"error": "value must be a string"}, status_code=400)
    value = raw.strip()

    # Reject characters that would break .env / shell parsing
    if "\n" in value or "\r" in value or '"' in value or "'" in value:
        return JSONResponse({"error": "value must not contain quotes or newlines"}, status_code=400)

    try:
        _write_env_var("OMBRE_HOST_VAULT_DIR", value)
    except Exception: logger.exception("Dashboard host vault write failed"); return JSONResponse({"error": "env_write_failed"}, status_code=500)

    return JSONResponse({
        "ok": True,
        "value": value,
        "env_file": _project_env_path(),
        "note": "已写入 .env；需在宿主机执行 `docker compose down && docker compose up -d` 让新挂载生效。",
    })


# =============================================================
# Import API — conversation history import
# 导入 API — 对话历史导入
# =============================================================

_IMPORT_BACKGROUND_TASKS: set[asyncio.Task] = set()

@mcp.custom_route("/api/import/upload", methods=["POST"])
@guarded_http_mutation("dashboard_import_start", methods=("POST",))
async def api_import_upload(request):
    """Upload a conversation file and start import."""
    from starlette.responses import JSONResponse
    err = _require_dashboard_write(request, "/api/import/upload")
    if err: return err

    content_type = request.headers.get("content-type", "")
    filename = ""
    raw_bytes = b""
    raw_content = ""
    media_type = content_type.split(";", 1)[0].strip() or "application/octet-stream"
    try:
        from raw_evidence_import import parse_capture_option

        raw_evidence_capture = parse_capture_option(
            request.query_params.get("raw_evidence_capture")
        )
    except Exception:
        return JSONResponse({"error": "invalid_raw_evidence_capture"}, status_code=400)

    resume = request.query_params.get("resume", "").lower() in ("1", "true")
    legacy_saved = None
    if not raw_evidence_capture and resume:
        try:
            legacy_saved = ImportState(config['buckets_dir']).resume_preflight()
        except BucketIdempotencyError as exc:
            return JSONResponse({'error': str(exc)}, status_code=409)
        except OSError:
            return JSONResponse({'error': 'import_failed'}, status_code=500)
    if raw_evidence_capture:
        # Both upload modes share this file; do not overwrite an accepted legacy run.
        active, _ = ImportState(config['buckets_dir']).read_legacy()
        if (ImportState.is_v2(active) and active['status'] != 'completed') or import_engine.is_running:
            return JSONResponse({"error": "Import already running"}, status_code=409)

    try:
        if "multipart/form-data" in content_type:
            form = await request.form()
            file_field = form.get("file")
            if not file_field:
                return JSONResponse({"error": "No file field"}, status_code=400)
            raw_bytes = await file_field.read()
            filename = getattr(file_field, "filename", "upload")
            media_type = (
                getattr(file_field, "content_type", None)
                or media_type
                or "application/octet-stream"
            )
        else:
            raw_bytes = await request.body()
            # Try to get filename from query params
            filename = request.query_params.get("filename", "upload")

        if raw_evidence_capture:
            if not raw_bytes.strip():
                return JSONResponse({"error": "Empty file"}, status_code=400)
        else:
            raw_content = raw_bytes.decode("utf-8", errors="replace")
            if not raw_content.strip():
                return JSONResponse({"error": "Empty file"}, status_code=400)

        preserve_raw = request.query_params.get("preserve_raw", "").lower() in ("1", "true")
        resume = request.query_params.get("resume", "").lower() in ("1", "true")

        if not raw_evidence_capture and not raw_content.strip():
            return JSONResponse({"error": "Empty file"}, status_code=400)

    except Exception: logger.exception("Dashboard import upload read failed"); return JSONResponse({"error": "upload_read_failed"}, status_code=400)

    # Legacy acceptance is durable before creating a task or acknowledging started.
    accepted = None
    if not raw_evidence_capture:
        try:
            if ImportState.v1_completed(legacy_saved):
                if legacy_saved.get('source_hash') != hashlib.sha256(raw_content.encode()).hexdigest()[:16]:
                    return JSONResponse({'error': 'legacy_source_conflict'}, status_code=409)
                accepted = {'replay': ImportState.public_status(legacy_saved)}
            elif ImportState.is_v2(legacy_saved) and legacy_saved['status'] == 'completed':
                if (legacy_saved['root_binding'] != str(Path(config['buckets_dir']).resolve())
                        or legacy_saved['source_digest'] != hashlib.sha256(raw_content.encode()).hexdigest()
                        or legacy_saved['source_file'] != filename
                        or legacy_saved['preserve_raw'] != preserve_raw):
                    return JSONResponse({'error': 'legacy_source_conflict'}, status_code=409)
                accepted = {'replay': ImportState.public_status(legacy_saved)}
            else:
                accepted = import_engine.accept_legacy(raw_content, filename, preserve_raw, resume)
        except BucketIdempotencyError as exc:
            return JSONResponse({'error': str(exc)}, status_code=409)
        except MaintenanceWriteError:
            raise
        except Exception:
            logger.exception('Legacy import acceptance failed')
            return JSONResponse({'error': 'import_failed'}, status_code=500)

    # Keep a strong reference independent of the HTTP caller's lifetime.
    async def _run_import():
        try:
            if raw_evidence_capture:
                await import_engine.start_raw_evidence(
                    raw_bytes,
                    filename,
                    preserve_raw,
                    resume,
                    media_type,
                )
            else:
                await import_engine.run_legacy(accepted)
        except Exception as e:
            logger.error(f"Import failed: {e}")

    if raw_evidence_capture or 'replay' not in accepted:
        task = asyncio.create_task(_run_import())
        _IMPORT_BACKGROUND_TASKS.add(task)
        def finished(done):
            _IMPORT_BACKGROUND_TASKS.discard(done)
            if not done.cancelled():
                done.exception()
        task.add_done_callback(finished)

    return JSONResponse({
        "status": "started",
        "filename": filename,
        "size_bytes": len(raw_bytes) if raw_evidence_capture else len(raw_content.encode()),
    })


@mcp.custom_route("/api/import/status", methods=["GET"])
async def api_import_status(request):
    """Get current import progress."""
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err: return err
    state = ImportState(config['buckets_dir'])
    saved, _ = state.read_legacy()
    return JSONResponse(state.public_status(saved))


@mcp.custom_route("/api/import/pause", methods=["POST"])
@guarded_http_mutation("dashboard_import_pause", methods=("POST",))
async def api_import_pause(request):
    """Pause the running import."""
    from starlette.responses import JSONResponse
    err = _require_dashboard_write(request, "/api/import/pause")
    if err: return err
    state = ImportState(config['buckets_dir'])
    saved, _ = state.read_legacy()
    if state.is_v2(saved):
        if saved['status'] != 'running':
            return JSONResponse({"error": "No import running"}, status_code=400)
        state.request_pause()
        return JSONResponse({"status": "pause_requested"})
    if not import_engine.is_running:
        return JSONResponse({"error": "No import running"}, status_code=400)
    import_engine.pause()
    return JSONResponse({"status": "pause_requested"})


@mcp.custom_route("/api/import/patterns", methods=["GET"])
async def api_import_patterns(request):
    """Detect high-frequency patterns after import."""
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err: return err
    try:
        patterns = await import_engine.detect_patterns()
        return JSONResponse({"patterns": patterns})
    except Exception: logger.exception("Dashboard import pattern detection failed"); return JSONResponse({"error": "pattern_detection_failed"}, status_code=500)


@mcp.custom_route("/api/import/results", methods=["GET"])
async def api_import_results(request):
    """List recently imported/created buckets for review."""
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err: return err
    try:
        limit = int(request.query_params.get("limit", "50"))
        all_buckets = await bucket_mgr.list_all(include_archive=False)
        # Sort by created time, newest first
        all_buckets.sort(key=lambda b: b["metadata"].get("created", ""), reverse=True)
        results = []
        for b in all_buckets[:limit]:
            results.append({
                "id": b["id"],
                "name": b["metadata"].get("name", ""),
                "content": b["content"][:300],
                "type": b["metadata"].get("type", ""),
                "domain": b["metadata"].get("domain", []),
                "tags": b["metadata"].get("tags", []),
                "importance": b["metadata"].get("importance", 5),
                "created": b["metadata"].get("created", ""),
            })
        return JSONResponse({"buckets": results, "total": len(all_buckets)})
    except Exception: logger.exception("Dashboard import result listing failed"); return JSONResponse({"error": "import_results_failed"}, status_code=500)


@mcp.custom_route("/api/import/review", methods=["POST"])
@guarded_http_mutation("dashboard_import_review", methods=("POST",))
async def api_import_review(request):
    """Apply review decisions: mark buckets as important/noise/pinned."""
    from starlette.responses import JSONResponse
    err = _require_dashboard_write(request, "/api/import/review")
    if err: return err
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    if not isinstance(body, dict):
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)
    decisions = body.get("decisions", [])
    if not decisions:
        return JSONResponse({"error": "No decisions provided"}, status_code=400)
    if not isinstance(decisions, list):
        return JSONResponse({"error": "invalid_decisions"}, status_code=400)
    if any(isinstance(d, dict) and d.get("action") == "delete" for d in decisions):
        if len(decisions) != 1:
            return JSONResponse({"error": "delete_requires_single_decision"}, status_code=400)
        decision = decisions[0]
        bid = decision.get("bucket_id", "")
        token = decision.get("confirm_token", "")
        if not isinstance(bid, str) or not bid or not isinstance(token, str):
            return JSONResponse({"error": "invalid_delete_request"}, status_code=400)
        try:
            outcome = await _delete_with_confirmation([bid], token)
        except Exception:
            logger.error("Dashboard import-review delete failed code=bucket_delete_failed")
            return JSONResponse({"error": "bucket_delete_failed"}, status_code=500)
        return _dashboard_delete_response(bid, outcome, review=True)

    applied = 0
    errors = 0
    for d in decisions:
        if not isinstance(d, dict):
            errors += 1
            continue
        bid = d.get("bucket_id", "")
        action = d.get("action", "")
        if not bid or not action:
            errors += 1
            continue
        try:
            if action == "important":
                if not await bucket_mgr.update(bid, importance=9):
                    errors += 1
                    continue
            elif action == "pin":
                if not await bucket_mgr.update(bid, pinned=True):
                    errors += 1
                    continue
            elif action == "noise":
                if not await bucket_mgr.update(bid, resolved=True, importance=1):
                    errors += 1
                    continue
            else:
                errors += 1
                continue
            applied += 1
        except Exception as e:
            logger.warning(f"Review action failed for {bid}: {e}")
            errors += 1

    return JSONResponse(
        {"applied": applied, "errors": errors},
        status_code=409 if errors else 200,
    )


# =============================================================
# /api/status — system status for Dashboard settings tab
# /api/status — Dashboard 设置页用系统状态
# =============================================================
@mcp.custom_route("/api/status", methods=["GET"])
async def api_system_status(request):
    """Return detailed system status for the settings panel."""
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err: return err
    try:
        stats = await bucket_mgr.get_stats()
        return JSONResponse({
            "decay_engine": "running" if decay_engine.is_running else "stopped",
            "embedding_enabled": embedding_engine.enabled,
            "buckets": {
                "permanent": stats.get("permanent_count", 0),
                "dynamic": stats.get("dynamic_count", 0),
                "archive": stats.get("archive_count", 0),
                "total": stats.get("permanent_count", 0) + stats.get("dynamic_count", 0),
            },
            "using_env_password": bool(os.environ.get("OMBRE_DASHBOARD_PASSWORD", "")),
            "version": "1.4.0",
        })
    except Exception: logger.exception("Dashboard system status failed"); return JSONResponse({"error": "status_unavailable"}, status_code=500)

# --- Entry point / 启动入口 ---
_ASSET_INGEST_ERROR_CODES = frozenset({
    "asset_unavailable",
    "asset_file_unavailable",
    "asset_write_frozen",
    "asset_write_gate_unavailable",
    "invalid_asset_id",
    "invalid_date_range",
    "invalid_description",
    "invalid_kind",
    "invalid_limit",
    "invalid_mime_type",
    "invalid_offset",
    "invalid_query",
    "invalid_source_sha256",
    "invalid_stored_path",
    "invalid_tags",
    "invalid_title",
    "invalid_created_from",
    "invalid_created_to",
    "source_hash_mismatch",
    "source_size_mismatch",
    "stored_file_conflict",
    "too_many_tags",
    "title_too_long",
    "description_too_long",
})


def _safe_asset_ingest_error(exc: Exception, fallback: str = "asset_unavailable") -> str:
    code = str(exc)
    return code if code in _ASSET_INGEST_ERROR_CODES else fallback


from mcp_prompts import register_prompts

register_prompts(mcp)


def _is_mcp_http_path(path: str) -> bool:
    return (
        path in {"/mcp", "/sse", "/messages"}
        or path.startswith("/mcp/")
        or path.startswith("/messages/")
    )


def _http_allowed_origins() -> list[str]:
    raw = os.environ.get("OMBRE_HTTP_ALLOWED_ORIGINS", "")
    return [
        origin
        for origin in (item.strip() for item in raw.split(","))
        if origin and origin != "*"
    ]


def add_http_cors_middleware(app):
    from starlette.middleware.cors import CORSMiddleware

    app.add_middleware(
        CORSMiddleware,
        allow_origins=_http_allowed_origins(),
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["*"],
    )
    return app


if __name__ == "__main__":
    transport = config.get("transport", "stdio")
    _register_backup_v2(transport)
    logger.info(f"Ombre Brain starting | transport: {transport}")

    if transport in ("sse", "streamable-http"):
        import threading
        import uvicorn

        # --- Application-level keepalive: ping /health every 60s ---
        # --- 应用层保活：每 60 秒 ping 一次 /health，防止 Cloudflare Tunnel 空闲断连 ---
        async def _keepalive_loop():
            await asyncio.sleep(10)  # Wait for server to fully start
            async with httpx.AsyncClient() as client:
                while True:
                    try:
                        await client.get(f"http://localhost:{OMBRE_PORT}/health", timeout=5)
                        logger.debug("Keepalive ping OK / 保活 ping 成功")
                    except Exception as e:
                        logger.warning(f"Keepalive ping failed / 保活 ping 失败: {e}")
                    await asyncio.sleep(60)

        def _start_keepalive():
            loop = asyncio.new_event_loop()
            loop.run_until_complete(_keepalive_loop())

        t = threading.Thread(target=_start_keepalive, daemon=True)
        t.start()

        def _start_digest_scheduler():
            loop = asyncio.new_event_loop()
            loop.run_until_complete(_digest_scheduler_loop())

        digest_thread = threading.Thread(target=_start_digest_scheduler, daemon=True)
        digest_thread.start()

        # --- Add CORS middleware so remote clients (Cloudflare Tunnel / ngrok) can connect ---
        # --- 添加 CORS 中间件，让远程客户端（Cloudflare Tunnel / ngrok）能正常连接 ---
        if transport == "streamable-http":
            _app = build_streamable_http_app()
        else:
            _app = mcp.sse_app()
        add_mcp_auth_middleware(_app)
        add_http_cors_middleware(_app)
        add_mcp_diagnostic_middleware(_app)
        install_uvicorn_access_log_redaction()
        logger.info("CORS middleware enabled for remote transport / 已启用 CORS 中间件")
        uvicorn.run(_app, host="0.0.0.0", port=OMBRE_PORT)
    else:
        mcp.run(transport=transport)

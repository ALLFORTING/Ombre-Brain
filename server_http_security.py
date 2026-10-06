# ============================================================
# Module: HTTP security and middleware (server_http_security.py)
# 模块：HTTP 安全与中间件
#
# Response seal, access-log redaction, MCP request diagnostics, hook and
# MCP bearer authentication, and the shared CORS policy.
# 响应 seal、访问日志脱敏、MCP 请求诊断、hook 与 MCP 鉴权、共享 CORS 策略。
#
# Stateless: every environment variable is read at call time. This module
# must never import server; server.py re-exports every name defined here.
# 无状态：环境变量均在调用时读取。本模块禁止 import server；
# server.py 会重新导出这里定义的全部名字。
#
# Depended on by: server.py, backup_entry.py (via server)
# 被谁依赖：server.py、backup_entry.py（经由 server）
# ============================================================

import hashlib
import hmac
import logging
import os
import re
import secrets
from urllib.parse import unquote_plus

logger = logging.getLogger("ombre_brain")


def _env_flag_enabled(value: str) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


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

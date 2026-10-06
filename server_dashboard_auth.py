# ============================================================
# Fragment: Dashboard login and session auth (server_dashboard_auth.py)
# 片段：Dashboard 登录与会话鉴权
#
# NOT an importable module. server.py executes this file in its own
# namespace, at the position where this code used to live, through
# _exec_server_fragment("server_dashboard_auth.py"). Never import it.
# 这不是可以单独 import 的模块。server.py 在这段代码原来所在的位置，
# 通过 _exec_server_fragment("server_dashboard_auth.py") 在 server 自己的命名空间里执行本文件。禁止 import。
#
# Why: tests unload and re-import server and keep using older server module
# objects, so each server module needs its own copies of these functions and
# of the state below; executing here gives exactly that.
# 原因：测试会卸载并重新加载 server，且继续使用旧的 server 模块对象，
# 每份 server 必须有自己的一套函数和下面的状态；在 server 命名空间里执行正好做到这一点。
#
# Contents: password store, setup wizard, sessions, same-origin checks, /auth/* routes.
# 内容：密码存储、首次设置、会话、同源检查、/auth/* 路由。
# State: _sessions, _setup_token, _setup_lock (fresh for every server module).
# 状态：_sessions、_setup_token、_setup_lock（每份 server 模块各自全新的一套）。
#
# Every name used here comes from server.py's namespace. Tracebacks show this
# file and its own line numbers. A missing file stops server startup.
# 这里用到的名字都来自 server.py 的命名空间。报错堆栈显示本文件和它自己的行号。缺少本文件时服务无法启动。
# ============================================================
# --- end of fragment header ---

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

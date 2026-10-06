# ============================================================
# Fragment: image assets and Remember-Me (server_assets.py)
# 片段：图片资产与 Remember-Me
#
# NOT an importable module. server.py executes this file in its own
# namespace, at the position where this code used to live, through
# _exec_server_fragment("server_assets.py"), i.e. compile(source, path, "exec").
# Never `import server_assets`.
# 这不是可以单独 import 的模块。server.py 在这段代码原来所在的位置，通过
# _exec_server_fragment("server_assets.py")（即 compile(源码, 路径, "exec")）
# 在 server 自己的命名空间里执行。禁止 import server_assets。
#
# Why: tests unload and re-import server and keep using older server module
# objects, so each server module needs its own copies of these functions,
# ticket tables and locks; executing here gives exactly that.
# 原因：测试会卸载并重新加载 server，且继续使用旧的 server 模块对象，
# 每份 server 必须有自己的一套函数、票据表和锁；在 server 命名空间里执行正好做到这一点。
#
# Contents: probe/ingest/browser-upload/vision diagnostics, Remember-Me upload
# and download tickets, view/inspect helpers, Remember-Me host bootstrap and
# runtime evidence, the nine rm_asset_* MCP tools, the fifteen diagnostic
# tools, and the /rm/* HTTP routes.
# 内容：探针 / 分块上传 / 浏览器上传 / 视觉诊断，Remember-Me 上传与下载票据，
# 查看与识图辅助，Remember-Me 启动与运行证据，9 个 rm_asset_* MCP 工具、
# 15 个诊断工具，以及 /rm/* HTTP 路由。
#
# Every name used here comes from server.py's namespace (imports, runtime
# components, helpers). Tracebacks show this file and its own line numbers.
# 这里用到的名字都来自 server.py 的命名空间。报错堆栈显示本文件和它自己的行号。
# ============================================================
# --- end of fragment header ---

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

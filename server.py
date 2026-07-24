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
#   - Expose 6 MCP tools:
#     暴露 6 个 MCP 工具：
#       breath — Surface unresolved memories or search by keyword
#                浮现未解决记忆 或 按关键词检索
#       hold   — Store a single memory (or write a `feel` reflection)
#                存储单条记忆（或写 feel 反思）
#       grow   — Diary digest, auto-split into multiple buckets
#                日记归档，自动拆分多桶
#       trace  — Modify metadata / resolved / delete
#                修改元数据 / resolved 标记 / 删除
#       pulse  — System status + bucket listing
#                系统状态 + 所有桶列表
#       dream  — Surface recent dynamic buckets for self-digestion
#                返回最近桶 供模型自省/写 feel
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
import io
import random
import logging
import asyncio
import hashlib
import hmac
import html
import secrets
import time
import threading
import struct
import zlib
import re
import json as _json_lib
import httpx
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse
from typing import Union
from typing_extensions import Annotated, Literal
from pydantic import Field
from PIL import Image, UnidentifiedImageError


# --- Ensure same-directory modules can be imported ---
# --- 确保同目录下的模块能被正确导入 ---
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from mcp.server.fastmcp import FastMCP
from mcp.types import CallToolResult, ImageContent, TextContent

from bucket_manager import BucketManager
from asset_store import (
    MAX_IMAGE_PIXELS as RM_ASSET_MAX_IMAGE_PIXELS,
    AssetStore,
    AssetStoreError,
    InvalidAssetImage,
)
from asset_embedding_index import AssetEmbeddingIndex
from asset_viewer import (
    ASSET_VIEWER_HTML,
    ASSET_VIEWER_MIME_TYPE,
    ASSET_VIEWER_RESOURCE_META,
    ASSET_VIEWER_TOOL_META,
    ASSET_VIEWER_URI,
)
from dehydrator import Dehydrator
from decay_engine import DecayEngine
from embedding_engine import EmbeddingEngine
from import_memory import ImportEngine
from utils import (
    DISPLAY_ALIASES,
    apply_display_aliases,
    load_config,
    setup_logging,
    strip_wikilinks,
    count_tokens_approx,
)

# --- Load config & init logging / 加载配置 & 初始化日志 ---
config = load_config()
setup_logging(config.get("log_level", "INFO"))
logger = logging.getLogger("ombre_brain")
asset_store = AssetStore(config["buckets_dir"])

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


def _constant_time_token_match(candidate: str, expected: str) -> bool:
    if not candidate or not expected:
        return False
    return secrets.compare_digest(
        candidate.encode("utf-8"),
        expected.encode("utf-8"),
    )


def add_mcp_auth_middleware(app):
    """
    Protect only the streamable-http MCP endpoint.

    When OMBRE_AUTH_TOKEN is unset, this intentionally keeps the old behavior so
    existing deployments do not disconnect after upgrading.
    """
    if not _mcp_auth_token():
        logger.warning("OMBRE_AUTH_TOKEN not set, /mcp is unauthenticated")
        return app

    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.responses import PlainTextResponse

    class MCPAuthMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            path = request.url.path or ""
            if path != "/mcp" and not path.startswith("/mcp/"):
                return await call_next(request)

            expected = _mcp_auth_token()
            authorization = request.headers.get("authorization", "")
            bearer = ""
            if authorization.lower().startswith("bearer "):
                bearer = authorization[7:].strip()
            query_token = request.query_params.get("token", "").strip()

            if (
                _constant_time_token_match(bearer, expected)
                or _constant_time_token_match(query_token, expected)
            ):
                return await call_next(request)

            return PlainTextResponse("Unauthorized", status_code=401)

    app.add_middleware(MCPAuthMiddleware)
    logger.info("OMBRE_AUTH_TOKEN set, /mcp authentication enabled")
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

# --- Initialize core components / 初始化核心组件 ---
embedding_engine = EmbeddingEngine(config)            # Embedding engine first (BucketManager depends on it)
asset_embedding_index = AssetEmbeddingIndex(asset_store, embedding_engine)
bucket_mgr = BucketManager(config, embedding_engine=embedding_engine)  # Bucket manager / 记忆桶管理器
dehydrator = Dehydrator(config)                      # Dehydrator / 脱水器
decay_engine = DecayEngine(config, bucket_mgr)       # Decay engine / 衰减引擎
import_engine = ImportEngine(config, bucket_mgr, dehydrator, embedding_engine)  # Import engine / 导入引擎

# --- Create MCP server instance / 创建 MCP 服务器实例 ---
# host="0.0.0.0" so Docker container's SSE is externally reachable
# stdio mode ignores host (no network)
mcp = FastMCP(
    "Ombre Brain",
    host="0.0.0.0",
    port=OMBRE_PORT,
)


# =============================================================
# Dashboard Auth — simple cookie-based session auth
# Dashboard 认证 —— 基于 Cookie 的会话认证
#
# Env var OMBRE_DASHBOARD_PASSWORD overrides file-stored password.
# First visit with no password set → forced setup wizard.
# Sessions stored in memory (lost on restart, 7-day expiry).
# =============================================================
_sessions: dict[str, float] = {}  # {token: expiry_timestamp}


def _get_auth_file() -> str:
    return os.path.join(config["buckets_dir"], ".dashboard_auth.json")


def _load_password_hash() -> str | None:
    try:
        auth_file = _get_auth_file()
        if os.path.exists(auth_file):
            with open(auth_file, "r", encoding="utf-8") as f:
                return _json_lib.load(f).get("password_hash")
    except Exception:
        pass
    return None


def _save_password_hash(password: str) -> None:
    salt = secrets.token_hex(16)
    h = hashlib.sha256(f"{salt}:{password}".encode()).hexdigest()
    auth_file = _get_auth_file()
    os.makedirs(os.path.dirname(auth_file), exist_ok=True)
    with open(auth_file, "w", encoding="utf-8") as f:
        _json_lib.dump({"password_hash": f"{salt}:{h}"}, f)


def _verify_password_hash(password: str, stored: str) -> bool:
    if ":" not in stored:
        return False
    salt, h = stored.split(":", 1)
    return hmac.compare_digest(
        h, hashlib.sha256(f"{salt}:{password}".encode()).hexdigest()
    )


def _is_setup_needed() -> bool:
    """True if no password is configured (env var or file)."""
    if os.environ.get("OMBRE_DASHBOARD_PASSWORD", ""):
        return False
    return _load_password_hash() is None


def _verify_any_password(password: str) -> bool:
    """Check password against env var (first) or stored hash."""
    env_pwd = os.environ.get("OMBRE_DASHBOARD_PASSWORD", "")
    if env_pwd:
        return hmac.compare_digest(password, env_pwd)
    stored = _load_password_hash()
    if not stored:
        return False
    return _verify_password_hash(password, stored)


def _create_session() -> str:
    token = secrets.token_urlsafe(32)
    _sessions[token] = time.time() + 86400 * 7  # 7-day expiry
    return token


def _is_authenticated(request) -> bool:
    token = request.cookies.get("ombre_session")
    if not token:
        return False
    expiry = _sessions.get(token)
    if expiry is None or time.time() > expiry:
        _sessions.pop(token, None)
        return False
    return True


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
    """Return auth state (authenticated, setup_needed)."""
    from starlette.responses import JSONResponse
    return JSONResponse({
        "authenticated": _is_authenticated(request),
        "setup_needed": _is_setup_needed(),
    })


@mcp.custom_route("/auth/setup", methods=["POST"])
async def auth_setup_endpoint(request):
    """Initial password setup (only when no password is configured)."""
    from starlette.responses import JSONResponse
    if not _is_setup_needed():
        return JSONResponse({"error": "Already configured"}, status_code=400)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)
    password = body.get("password", "").strip()
    if len(password) < 6:
        return JSONResponse({"error": "密码不能少于6位"}, status_code=400)
    _save_password_hash(password)
    token = _create_session()
    resp = JSONResponse({"ok": True})
    resp.set_cookie("ombre_session", token, httponly=True, samesite="lax", max_age=86400 * 7)
    return resp


@mcp.custom_route("/auth/login", methods=["POST"])
async def auth_login(request):
    """Login with password."""
    from starlette.responses import JSONResponse
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)
    password = body.get("password", "")
    if _verify_any_password(password):
        token = _create_session()
        resp = JSONResponse({"ok": True})
        resp.set_cookie("ombre_session", token, httponly=True, samesite="lax", max_age=86400 * 7)
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
async def auth_change_password(request):
    """Change dashboard password (requires current password)."""
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err:
        return err
    if os.environ.get("OMBRE_DASHBOARD_PASSWORD", ""):
        return JSONResponse({"error": "当前使用环境变量密码，请直接修改 OMBRE_DASHBOARD_PASSWORD"}, status_code=400)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)
    current = body.get("current", "")
    new_pwd = body.get("new", "").strip()
    if not _verify_any_password(current):
        return JSONResponse({"error": "当前密码错误"}, status_code=401)
    if len(new_pwd) < 6:
        return JSONResponse({"error": "新密码不能少于6位"}, status_code=400)
    _save_password_hash(new_pwd)
    _sessions.clear()
    token = _create_session()
    resp = JSONResponse({"ok": True})
    resp.set_cookie("ombre_session", token, httponly=True, samesite="lax", max_age=86400 * 7)
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
    except Exception as e:
        return JSONResponse({"status": "error", "detail": str(e)}, status_code=500)


# =============================================================
# /breath-hook endpoint: Dedicated hook for SessionStart
# 会话启动专用挂载点
# =============================================================
@mcp.custom_route("/breath-hook", methods=["GET"])
async def breath_hook(request):
    from starlette.responses import PlainTextResponse
    try:
        all_buckets = await bucket_mgr.list_all(include_archive=False)
        # pinned
        pinned = [b for b in all_buckets if b["metadata"].get("pinned") or b["metadata"].get("protected")]
        # top 2 unresolved by score
        unresolved = [b for b in all_buckets
                      if not b["metadata"].get("resolved", False)
                      and b["metadata"].get("type") not in ("permanent", "feel")
                      and not b["metadata"].get("dormant", False)
                      and not b["metadata"].get("pinned")
                      and not b["metadata"].get("protected")]
        scored = sorted(unresolved, key=lambda b: decay_engine.calculate_score(b["metadata"]), reverse=True)

        parts = []
        token_budget = 10000
        for b in pinned:
            summary = await dehydrator.dehydrate(strip_wikilinks(b["content"]), {k: v for k, v in b["metadata"].items() if k != "tags"})
            parts.append(f"📌 [核心准则] {summary}")
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
            await bucket_mgr.touch(b["id"])
            token_budget -= summary_tokens

        if not parts:
            await _fire_webhook("breath_hook", {"surfaced": 0})
            return PlainTextResponse("")
        body_text = "[Ombre Brain - 记忆浮现]\n" + "\n---\n".join(parts)
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
    try:
        all_buckets = await bucket_mgr.list_all(include_archive=False)
        candidates = [
            b for b in all_buckets
            if b["metadata"].get("type") not in ("permanent", "feel")
            and not b["metadata"].get("dormant", False)
            and not b["metadata"].get("pinned", False)
            and not b["metadata"].get("protected", False)
        ]
        candidates.sort(key=lambda b: b["metadata"].get("created", ""), reverse=True)
        recent = candidates[:10]

        if not recent:
            return PlainTextResponse("")

        parts = []
        for b in recent:
            meta = b["metadata"]
            await bucket_mgr.touch(b["id"])
            resolved_tag = "[已解决]" if meta.get("resolved", False) else "[未解决]"
            parts.append(
                f"{meta.get('name', b['id'])} {resolved_tag} "
                f"V{meta.get('valence', 0.5):.1f}/A{meta.get('arousal', 0.3):.1f}\n"
                f"{strip_wikilinks(b['content'][:200])}"
            )

        body_text = "[Ombre Brain - Dreaming]\n" + "\n---\n".join(parts)
        await _fire_webhook("dream_hook", {"surfaced": len(parts), "chars": len(body_text)})
        return PlainTextResponse(body_text)
    except Exception as e:
        logger.warning(f"Dream hook failed: {e}")
        return PlainTextResponse("")


# =============================================================
# Internal helper: merge-or-create
# 内部辅助：检查是否可合并，可以则合并，否则新建
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
) -> tuple[str, bool]:
    """
    Check if a similar bucket exists for merging; merge if so, create if not.
    Returns (bucket_id_or_name, is_merged).
    检查是否有相似桶可合并，有则合并，无则新建。
    返回 (桶ID或名称, 是否合并)。
    """
    try:
        existing = await bucket_mgr.search(content, limit=1, domain_filter=domain or None)
    except Exception as e:
        logger.warning(f"Search for merge failed, creating new / 合并搜索失败，新建: {e}")
        existing = []

    if existing and existing[0].get("score", 0) > config.get("merge_threshold", 75):
        bucket = existing[0]
        # --- Never merge into pinned/protected buckets ---
        # --- 不合并到钉选/保护桶 ---
        if not (bucket["metadata"].get("pinned") or bucket["metadata"].get("protected")):
            try:
                merged = await dehydrator.merge(bucket["content"], content)
                old_v = bucket["metadata"].get("valence", 0.5)
                old_a = bucket["metadata"].get("arousal", 0.3)
                merged_valence = round((old_v + valence) / 2, 2)
                merged_arousal = round((old_a + arousal) / 2, 2)
                await bucket_mgr.update(
                    bucket["id"],
                    content=merged,
                    tags=list(set(bucket["metadata"].get("tags", []) + tags)),
                    importance=max(bucket["metadata"].get("importance", 5), importance),
                    domain=list(set(bucket["metadata"].get("domain", []) + domain)),
                    valence=merged_valence,
                    arousal=merged_arousal,
                    **({"trigger_date": trigger_date, "trigger_last_seen": ""} if trigger_date else {}),
                )
                return bucket["metadata"].get("name", bucket["id"]), True
            except Exception as e:
                logger.warning(f"Merge failed, creating new / 合并失败，新建: {e}")

    bucket_id = await bucket_mgr.create(
        content=content,
        tags=tags,
        importance=importance,
        domain=domain,
        valence=valence,
        arousal=arousal,
        name=name or None,
    )
    await _auto_link_related(bucket_id)
    if trigger_date:
        await bucket_mgr.update(bucket_id, trigger_date=trigger_date, trigger_last_seen="")
    return bucket_id, False


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


def _bucket_summary_line(bucket: dict, score: float | None = None, pinned: bool = False) -> str:
    meta = bucket.get("metadata", {})
    label = meta.get("name", bucket["id"])
    topic = _bucket_topic(meta)
    emotion = _bucket_emotion(meta)
    updated = _bucket_date(meta, "updated_at", "last_active", "created")
    if pinned:
        importance = meta.get("importance", "?")
        return f"📌 [bucket_id:{bucket['id']}] {label} | 主题:{topic} | {emotion} | 重要:{importance} | 更新:{updated}"
    weight = f"{score:.2f}" if score is not None else "0.00"
    return f"💭 [bucket_id:{bucket['id']}] {label} | 主题:{topic} | {emotion} | 权重:{weight} | 更新:{updated}"


def _dream_summary_line(bucket: dict) -> str:
    meta = bucket.get("metadata", {})
    label = meta.get("name", bucket["id"])
    topic = _bucket_topic(meta)
    emotion = _bucket_emotion(meta)
    updated = _bucket_date(meta, "updated_at", "last_active", "created")
    content = strip_wikilinks(bucket.get("content", "")).replace("\n", " ").strip()
    one_line = content[:80] + ("…" if len(content) > 80 else "")
    return f"[{label}] 主题:{topic} | {one_line} | {emotion} | 更新:{updated} | bucket_id:{bucket['id']}"


def _recent_cutoff(recent_days: int) -> str | None:
    if recent_days <= 0:
        return None
    return (datetime.now().date() - timedelta(days=recent_days)).isoformat()


def _is_recent_bucket(bucket: dict, cutoff: str | None) -> bool:
    if not cutoff:
        return True
    updated = _bucket_date(bucket.get("metadata", {}), "updated_at", "last_active", "created")
    return bool(updated and updated >= cutoff)


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
    valence = float(meta.get("valence", 0.5) or 0.5)
    arousal = float(meta.get("arousal", 0.3) or 0.3)
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


def _record_emotion_snapshot(valence: float, arousal: float, source: str) -> None:
    if not (0 <= valence <= 1 and 0 <= arousal <= 1):
        return
    timeline = _load_emotion_timeline()
    timeline.append({
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "valence": round(float(valence), 3),
        "arousal": round(float(arousal), 3),
        "source": source,
    })
    path = _emotion_timeline_path()
    temp_path = f"{path}.tmp"
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(temp_path, "w", encoding="utf-8") as handle:
            _json_lib.dump(timeline, handle, ensure_ascii=False, separators=(",", ":"))
        os.replace(temp_path, path)
    except OSError as e:
        logger.warning(f"Failed to persist emotion timeline: {e}")


def _with_emotion_timeline(text: str, enabled: bool) -> str:
    if not enabled:
        return text
    timeline = sorted(
        _load_emotion_timeline(),
        key=lambda item: str(item.get("timestamp", "")),
    )
    payload = _json_lib.dumps(timeline, ensure_ascii=False, separators=(",", ":"))
    return f"{text}\n\nemotion_history: {payload}"


def _related_ids(meta: dict) -> list[str]:
    raw = meta.get("related_buckets", "")
    if isinstance(raw, list):
        return [str(item).strip() for item in raw if str(item).strip()]
    return _parse_csv_ids(str(raw))


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
    lines = [text]
    related_line = await _format_related_line(bucket)
    if related_line:
        lines.append(related_line)
    return "\n".join(lines)


async def _merge_bucket_into_target(target_id: str, source_id: str) -> str:
    if not source_id or source_id == target_id:
        return "merge 必须指定另一个有效的 bucket_id。"
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

    updated = await bucket_mgr.update(
        target_id,
        content=merged_content,
        tags=merged_tags,
        importance=merged_importance,
        valence=merged_valence,
        arousal=merged_arousal,
        dormant=False,
    )
    if not updated:
        return f"合并失败，无法更新目标桶: {target_id}"

    deleted = await bucket_mgr.delete(source_id)
    if not deleted:
        return f"目标桶已更新，但源桶删除失败: {source_id}"
    try:
        embedding_engine.delete_embedding(source_id)
    except Exception:
        pass
    return (
        f"已合并 {source_id} → {target_id}: "
        f"importance={merged_importance}, "
        f"valence={merged_valence:.3f}, arousal={merged_arousal:.3f}, "
        f"tags={','.join(str(tag) for tag in merged_tags)}"
    )


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


def _extract_session_summary(content: str, max_chars: int = 700) -> str:
    """Extract the Summary section from an archived session bucket."""
    text = strip_wikilinks(content or "").strip()
    marker = "## Summary"
    if marker in text:
        text = text.split(marker, 1)[1].strip()
        if "\n## " in text:
            text = text.split("\n## ", 1)[0].strip()
    return text[:max_chars].strip()


def _format_mailbox(limit: int = 1, include_sealed: bool = False) -> str:
    letters = bucket_mgr.get_letters(limit, include_sealed=include_sealed)
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


async def _format_due_triggers(active_buckets: list[dict], max_items: int = 10) -> tuple[str, list[str]]:
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
        preview = strip_wikilinks(bucket.get("content", "")).strip()[:300]
        lines.append(
            f"[bucket_id:{bucket['id']}] {meta.get('name', bucket['id'])} "
            f"trigger_date:{meta.get('trigger_date')}\n{preview}"
        )
    text = "=== boot: 今日浮现 ===\n" + ("\n---\n".join(lines) if lines else "（今日无到期提醒）")
    return text, [bucket["id"] for bucket in shown]


def _format_feel_echo(active_buckets: list[dict]) -> str:
    feels = [
        bucket for bucket in active_buckets
        if bucket.get("metadata", {}).get("type") == "feel"
        and not _is_sealed(bucket)
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


def _fit_sections_to_budget(sections: list[tuple[str, str]], max_tokens: int) -> str:
    """Append sections in priority order, truncating lower-priority content first."""
    output = []
    used = 0
    for _, text in sections:
        section_tokens = count_tokens_approx(text)
        if used + section_tokens <= max_tokens:
            output.append(text)
            used += section_tokens
            continue
        remaining = max_tokens - used
        if remaining <= 40:
            break
        chars = max(200, remaining * 3)
        output.append(text[:chars].rstrip() + "\n...（已按 boot 预算截断）")
        break
    return "\n\n".join(output)


def _digest_api_config() -> tuple[str, str, str]:
    api_key = os.environ.get("OMBRE_DIGEST_API_KEY", "").strip()
    base_url = os.environ.get("OMBRE_DIGEST_BASE_URL", "https://api.deepseek.com/v1").strip()
    model = os.environ.get("OMBRE_DIGEST_MODEL", "deepseek-chat").strip()
    return api_key, base_url.rstrip("/"), model


def _days_since(value: str) -> int:
    try:
        dt = datetime.fromisoformat(str(value))
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
    candidates.sort(key=lambda b: int(b.get("metadata", {}).get("importance", 0) or 0))
    return candidates


async def _importance_rebalance_candidates() -> list[dict]:
    buckets = await bucket_mgr.list_all(include_archive=False)
    candidates = []
    for bucket in buckets:
        meta = bucket.get("metadata", {})
        if meta.get("pinned") or meta.get("protected") or _is_sealed(bucket):
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


def _importance_rebalance_token(candidates: list[dict]) -> str:
    payload = []
    for bucket in candidates:
        meta = bucket.get("metadata", {})
        payload.append(
            "|".join([
                bucket["id"],
                str(meta.get("importance", "")),
                str(meta.get("created") or meta.get("created_at") or ""),
            ])
        )
    return hashlib.sha256("\n".join(payload).encode("utf-8")).hexdigest()[:12]


def _group_digest_candidates(candidates: list[dict]) -> dict[str, list[dict]]:
    groups: dict[str, list[dict]] = {}
    for bucket in candidates:
        domains = bucket.get("metadata", {}).get("domain", []) or ["未分类"]
        domain = str(domains[0] if isinstance(domains, list) and domains else domains)
        groups.setdefault(domain, []).append(bucket)
    return groups


async def _call_digest_api(domain: str, buckets: list[dict]) -> str:
    api_key, base_url, model = _digest_api_config()
    if not api_key:
        raise RuntimeError("OMBRE_DIGEST_API_KEY is not configured")
    excerpts = []
    for bucket in buckets[:20]:
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


async def _run_digest(dry_run: bool = True, max_groups: int = 10, confirm_token: str = "") -> str:
    candidates = await _digest_candidates()
    rebalance_candidates = await _importance_rebalance_candidates()
    groups = _group_digest_candidates(candidates)
    selected = list(groups.items())[:max(1, max_groups)]
    lines = [
        "=== 自动消化 dry-run ===" if dry_run else "=== 自动消化执行 ===",
        f"候选桶数: {len(candidates)}",
        f"主题组数: {len(groups)}",
    ]
    if not selected and not rebalance_candidates:
        return "\n".join(lines + ["No digest or importance rebalance candidates."])
    for domain, buckets in selected:
        ids = ", ".join(bucket["id"] for bucket in buckets)
        lines.append(f"- {domain}: {len(buckets)} 个桶 -> {ids}")
    if rebalance_candidates:
        rebalance_token = _importance_rebalance_token(rebalance_candidates)
        lines.append(
            "=== importance rebalance dry-run ==="
            if dry_run else "=== importance rebalance execute ==="
        )
        lines.append(f"confirm_token: {rebalance_token}")
        for bucket in rebalance_candidates:
            meta = bucket.get("metadata", {})
            importance = int(meta.get("importance", 0) or 0)
            created = meta.get("created") or meta.get("created_at") or ""
            lines.append(
                f"- bucket_id:{bucket['id']} importance:{importance}->{importance - 1} created:{created}"
            )
    if dry_run:
        return "\n".join(lines)
    if rebalance_candidates:
        rebalance_token = _importance_rebalance_token(rebalance_candidates)
        if not hmac.compare_digest((confirm_token or "").strip(), rebalance_token):
            return "\n".join(lines + [
                "confirmation required: rerun dry_run and pass confirm_token to apply importance rebalance."
            ])

    rebalanced_total = 0
    if not selected:
        for bucket in rebalance_candidates:
            meta = bucket.get("metadata", {})
            importance = int(meta.get("importance", 0) or 0)
            await bucket_mgr.update(bucket["id"], importance=importance - 1)
            rebalanced_total += 1
        lines.append(f"importance rebalanced: {rebalanced_total}")
        return "\n".join(lines)

    digested_total = 0
    log_entries = []
    for domain, buckets in selected:
        digest_content = await _call_digest_api(domain, buckets)
        source_ids = [bucket["id"] for bucket in buckets]
        digest_id = await bucket_mgr.create(
            content=digest_content,
            tags=["digest", "auto-digested"],
            importance=6,
            domain=[domain, "digest"],
            valence=0.5,
            arousal=0.3,
            bucket_type="dynamic",
            name=f"digest_{domain}_{datetime.now().date().isoformat()}",
        )
        await bucket_mgr.update(digest_id, source_bucket=",".join(source_ids))
        for bucket_id in source_ids:
            await bucket_mgr.update(bucket_id, digested=True, source_bucket=digest_id)
            digested_total += 1
        log_entries.append(f"[{digest_id}] {domain}: {', '.join(source_ids)}")
    log_content = (
        "# 自动消化日志\n\n"
        f"- 时间: {datetime.now().isoformat(timespec='seconds')}\n"
        f"- 消化桶数: {digested_total}\n\n"
        + "\n".join(log_entries)
    )
    log_id = await bucket_mgr.create(
        content=log_content,
        tags=["digest-log"],
        importance=5,
        domain=["system", "digest"],
        valence=0.5,
        arousal=0.3,
        bucket_type="dynamic",
        name=f"digest_log_{datetime.now().date().isoformat()}",
    )
    lines.append(f"已消化: {digested_total} 个桶")
    lines.append(f"digest log bucket: {log_id}")
    for bucket in rebalance_candidates:
        meta = bucket.get("metadata", {})
        importance = int(meta.get("importance", 0) or 0)
        await bucket_mgr.update(bucket["id"], importance=importance - 1)
        rebalanced_total += 1
    lines.append(f"importance rebalanced: {rebalanced_total}")
    return "\n".join(lines)


async def _auto_link_related(bucket_id: str, threshold: float | None = None, top_k: int = 3) -> list[tuple[str, float]]:
    """Bidirectionally link a new bucket to its closest non-sealed semantic neighbors."""
    if threshold is None:
        threshold = float(os.environ.get("OMBRE_RELATED_THRESHOLD", "0.75") or "0.75")
    if not embedding_engine or not embedding_engine.enabled:
        return []
    bucket = await bucket_mgr.get(bucket_id)
    if not bucket or _is_sealed(bucket):
        return []
    target_embedding = await embedding_engine.get_embedding(bucket_id)
    if target_embedding is None:
        await embedding_engine.generate_and_store(bucket_id, bucket.get("content", ""))
        target_embedding = await embedding_engine.get_embedding(bucket_id)
    if target_embedding is None:
        return []
    all_buckets = await bucket_mgr.list_all(include_archive=False)
    scored = []
    for other in all_buckets:
        other_id = other["id"]
        if other_id == bucket_id or _is_sealed(other):
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
        return []

    target_related = _related_ids(bucket.get("metadata", {}))
    selected_ids = [bucket_id for bucket_id, _ in selected]
    await bucket_mgr.update(
        bucket_id,
        related_buckets=",".join(dict.fromkeys(target_related + selected_ids)),
    )
    for related_id, _ in selected:
        related_bucket = await bucket_mgr.get(related_id)
        if not related_bucket or _is_sealed(related_bucket):
            continue
        current_related = _related_ids(related_bucket.get("metadata", {}))
        if bucket_id not in current_related:
            await bucket_mgr.update(
                related_id,
                related_buckets=",".join(dict.fromkeys(current_related + [bucket_id])),
            )
    return selected


async def _run_related_backfill(dry_run: bool = True, limit: int = 100, threshold: float | None = None) -> str:
    if threshold is None:
        threshold = float(os.environ.get("OMBRE_RELATED_THRESHOLD", "0.75") or "0.75")
    if not embedding_engine or not embedding_engine.enabled:
        return "自动 related 回填不可用：embedding 未启用。"
    buckets = [
        bucket for bucket in await bucket_mgr.list_all(include_archive=False)
        if not _is_sealed(bucket)
    ][:max(1, limit)]
    planned = []
    for bucket in buckets:
        target_embedding = await embedding_engine.get_embedding(bucket["id"])
        if target_embedding is None:
            continue
        scored = []
        for other in buckets:
            if other["id"] == bucket["id"] or _is_sealed(other):
                continue
            other_embedding = await embedding_engine.get_embedding(other["id"])
            if other_embedding is None:
                continue
            score = embedding_engine._cosine_similarity(target_embedding, other_embedding)
            if score >= threshold:
                scored.append((other["id"], score))
        scored.sort(key=lambda item: item[1], reverse=True)
        top = scored[:3]
        if top:
            planned.append((bucket["id"], top))
    lines = [
        "=== 自动 related dry-run ===" if dry_run else "=== 自动 related 回填 ===",
        f"扫描桶数: {len(buckets)}",
        f"计划关联: {len(planned)} 个桶",
    ]
    for bucket_id, links in planned[:50]:
        lines.append(
            f"- {bucket_id}: "
            + ", ".join(f"{related_id}({score:.3f})" for related_id, score in links)
        )
    if dry_run:
        return "\n".join(lines)
    for bucket_id, links in planned:
        bucket = await bucket_mgr.get(bucket_id)
        if not bucket or _is_sealed(bucket):
            continue
        current = _related_ids(bucket.get("metadata", {}))
        next_ids = [related_id for related_id, _ in links]
        await bucket_mgr.update(bucket_id, related_buckets=",".join(dict.fromkeys(current + next_ids)))
    return "\n".join(lines)


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
        "有则用一句中文指出矛盾，并包含相关 bucket_id；无则只回答“无”。"
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
            },
        )
    response.raise_for_status()
    data = response.json()
    return data["choices"][0]["message"]["content"].strip()


def _conflict_tokens(text: str) -> set[str]:
    normalized = strip_wikilinks(_apply_display_aliases(text or "")).lower()
    return {
        token
        for token in re.findall(r"[a-z0-9_]{3,}|[\u4e00-\u9fff]{2,}", normalized)
        if len(token.strip()) >= 2
    }


async def _conflict_candidate_buckets(content: str, limit: int = 3) -> list[dict]:
    candidates = []
    seen = set()

    def add_bucket(bucket: dict) -> None:
        bucket_id = bucket.get("id")
        if not bucket_id or bucket_id in seen or _is_sealed(bucket):
            return
        seen.add(bucket_id)
        candidates.append(bucket)

    try:
        for bucket in await bucket_mgr.search(content, limit=8):
            add_bucket(bucket)
    except Exception as exc:
        logger.warning("Conflict search candidates failed: %s", exc)

    if len(candidates) >= limit:
        return candidates[:limit]

    query_tokens = _conflict_tokens(content)
    if not query_tokens:
        return candidates[:limit]

    try:
        all_buckets = await bucket_mgr.list_all(include_archive=False)
    except Exception as exc:
        logger.warning("Conflict lexical candidates failed: %s", exc)
        return candidates[:limit]

    lexical = []
    for bucket in all_buckets:
        if bucket.get("id") in seen or _is_sealed(bucket):
            continue
        meta = bucket.get("metadata", {})
        haystack = " ".join([
            str(meta.get("name", "")),
            str(meta.get("summary", "")),
            " ".join(map(str, meta.get("tags", []) or [])),
            strip_wikilinks(bucket.get("content", "")),
        ])
        overlap = query_tokens & _conflict_tokens(haystack)
        strong_overlap = [
            token for token in overlap
            if len(token) >= 4 or any(ch.isdigit() for ch in token)
        ]
        if strong_overlap or len(overlap) >= 2:
            lexical.append((len(strong_overlap) * 3 + len(overlap), bucket))

    lexical.sort(key=lambda item: item[0], reverse=True)
    for _, bucket in lexical:
        add_bucket(bucket)
        if len(candidates) >= limit:
            break

    return candidates[:limit]


async def _detect_conflict_warning(content: str) -> str:
    try:
        old_buckets = await _conflict_candidate_buckets(content, limit=3)
        response = await _call_conflict_api(content, old_buckets)
    except Exception as exc:
        logger.warning("Conflict detection failed: %s", exc)
        return ""
    normalized = response.strip()
    if not normalized or normalized in ("无", "沒有", "没有", "無"):
        return ""
    if normalized.startswith("无") and len(normalized) <= 4:
        return ""
    return normalized[:500]


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
) -> str:
    # MCP schema note: emotion_trend must stay in the tool signature.
    """检索/浮现记忆。默认 summary 模式返回摘要；query 检索始终返回 full 内容。"""
    await decay_engine.ensure_started()
    query = _apply_display_aliases(query)
    max_results = max(1, min(max_results, 50))
    max_tokens = min(max_tokens, 20000)
    mode = (mode or "summary").strip().lower()
    if mode not in ("summary", "full"):
        mode = "summary"
    recent_cutoff = _recent_cutoff(recent_days)
    try:
        date_from = _parse_date_filter(date_from, "date_from")
        date_to = _parse_date_filter(date_to, "date_to")
        resonance_target = _parse_resonance(resonance)
    except ValueError as exc:
        return str(exc)
    if date_from and date_to and date_from > date_to:
        return "date_from cannot be later than date_to."

    # --- Session archive retrieval: archived session buckets are searchable by domain ---
    if domain.strip().lower() == "session":
        try:
            all_buckets = await bucket_mgr.list_all(include_archive=True)
            sessions = [
                b for b in all_buckets
                if "session" in b.get("metadata", {}).get("domain", [])
                and _is_recent_bucket(b, recent_cutoff)
                and _is_in_date_range(b, date_from, date_to)
                and (include_sealed or not _is_sealed(b))
            ]
            if query and query.strip():
                q = query.strip().lower()
                sessions = [
                    b for b in sessions
                    if q in str(b.get("metadata", {}).get("name", "")).lower()
                    or q in b.get("content", "").lower()
                ]
            sessions.sort(key=lambda b: _bucket_date(b["metadata"], "updated_at", "created_at", "created"), reverse=True)
            sessions = sessions[:max_results]
            if not sessions:
                return _with_emotion_timeline("没有找到对话归档。", emotion_trend)
            results = []
            for b in sessions:
                meta = b.get("metadata", {})
                text = (
                    f"[session] [bucket_id:{b['id']}] {meta.get('name', b['id'])}\n"
                    f"{strip_wikilinks(b.get('content', '')[:1200])}"
                )
                results.append(await _append_bucket_extras(text, b, emotion_trend))
            return _with_emotion_timeline("\n---\n".join(results), emotion_trend)
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
                and _is_recent_bucket(b, recent_cutoff)
                and _is_in_date_range(b, date_from, date_to)
                and (include_sealed or not _is_sealed(b))
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

    # --- importance_min mode: bulk fetch by importance threshold ---
    if importance_min >= 1:
        try:
            all_buckets = await bucket_mgr.list_all(include_archive=False)
        except Exception as e:
            return f"记忆系统暂时无法访问: {e}"
        filtered = [
            b for b in all_buckets
            if int(b["metadata"].get("importance", 0)) >= importance_min
            and b["metadata"].get("type") not in ("feel",)
            and (include_dormant or not b["metadata"].get("dormant", False))
            and (include_sealed or not _is_sealed(b))
            and _is_recent_bucket(b, recent_cutoff)
            and _is_in_date_range(b, date_from, date_to)
        ]
        filtered.sort(key=lambda b: int(b["metadata"].get("importance", 0)), reverse=True)
        total_filtered = len(filtered)
        filtered = filtered[:max_results]
        if not filtered:
            return _with_emotion_timeline(
                f"没有重要度 >= {importance_min} 的记忆。",
                emotion_trend,
            )
        for bucket in filtered:
            await bucket_mgr.touch(bucket["id"])
        results = [
            await _append_bucket_extras(
                _bucket_summary_line(b, pinned=bool(b["metadata"].get("pinned") or b["metadata"].get("protected"))),
                b,
                emotion_trend,
            )
            for b in filtered
        ]
        response = "\n---\n".join(results) if results else "没有可以展示的记忆。"
        hidden_count = max(0, total_filtered - len(filtered))
        if hidden_count:
            response += f"\n\n还有{hidden_count}个相关桶未显示"
        return _with_emotion_timeline(response, emotion_trend)

    # --- Resonance mode without query: sort visible memories by emotion distance ---
    if resonance_target and (not query or not query.strip()):
        try:
            all_buckets = await bucket_mgr.list_all(include_archive=False)
        except Exception as e:
            logger.error(f"Failed to list buckets for resonance: {e}")
            return "记忆系统暂时无法访问。"
        candidates = [
            b for b in all_buckets
            if b["metadata"].get("type") not in ("feel",)
            and (include_dormant or not b["metadata"].get("dormant", False))
            and (include_sealed or not _is_sealed(b))
            and _is_recent_bucket(b, recent_cutoff)
            and _is_in_date_range(b, date_from, date_to)
        ]
        candidates.sort(key=lambda b: _resonance_distance(b, resonance_target))
        total = len(candidates)
        candidates = candidates[:max_results]
        for bucket in candidates:
            await bucket_mgr.touch(bucket["id"])
        results = [
            await _append_bucket_extras(
                _bucket_summary_line(b, score=_resonance_distance(b, resonance_target)),
                b,
                emotion_trend,
            )
            for b in candidates
        ]
        if not results:
            return _with_emotion_timeline("未找到共鸣记忆。", emotion_trend)
        response = "\n---\n".join(results)
        hidden_count = max(0, total - len(candidates))
        if hidden_count:
            response += f"\n\n还有{hidden_count}个共鸣桶未显示"
        return _with_emotion_timeline(response, emotion_trend)

    # --- No args or empty query: surfacing mode (weight pool active push) ---
    if not query or not query.strip():
        try:
            all_buckets = await bucket_mgr.list_all(include_archive=False)
        except Exception as e:
            logger.error(f"Failed to list buckets for surfacing / 浮现列桶失败: {e}")
            return "记忆系统暂时无法访问。"

        pinned_buckets = [
            b for b in all_buckets
            if b["metadata"].get("pinned") or b["metadata"].get("protected")
            if _is_in_date_range(b, date_from, date_to)
            if include_sealed or not _is_sealed(b)
        ]
        unresolved = [
            b for b in all_buckets
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
        for bucket in candidates:
            await bucket_mgr.touch(bucket["id"])

        summary_mode = mode == "summary"
        pinned_results = []
        dynamic_results = []
        token_budget = max_tokens

        if summary_mode:
            for b in pinned_buckets:
                pinned_results.append(await _append_bucket_extras(_bucket_summary_line(b, pinned=True), b, emotion_trend))
            for b in candidates:
                dynamic_results.append(await _append_bucket_extras(_bucket_summary_line(b, score=decay_engine.calculate_score(b["metadata"])), b, emotion_trend))
        else:
            for b in pinned_buckets:
                try:
                    clean_meta = {k: v for k, v in b["metadata"].items() if k != "tags"}
                    summary = await dehydrator.dehydrate(strip_wikilinks(b["content"]), clean_meta)
                    line = f"📌 [核心准则] [bucket_id:{b['id']}] {summary}"
                    t = count_tokens_approx(line)
                    if token_budget - t < 0:
                        break
                    pinned_results.append(await _append_bucket_extras(line, b, emotion_trend))
                    token_budget -= t
                except Exception as e:
                    logger.warning(f"Failed to dehydrate pinned bucket / 钉选桶脱水失败: {e}")
            for b in candidates:
                if token_budget <= 0:
                    break
                try:
                    clean_meta = {k: v for k, v in b["metadata"].items() if k != "tags"}
                    summary = await dehydrator.dehydrate(strip_wikilinks(b["content"]), clean_meta)
                    summary_tokens = count_tokens_approx(summary)
                    if summary_tokens > token_budget:
                        break
                    score = decay_engine.calculate_score(b["metadata"])
                    line = f"[权重:{score:.2f}] [bucket_id:{b['id']}] {summary}"
                    dynamic_results.append(await _append_bucket_extras(line, b, emotion_trend))
                    token_budget -= summary_tokens
                except Exception as e:
                    logger.warning(f"Failed to dehydrate surfaced bucket / 浮现脱水失败: {e}")
                    continue

        if not pinned_results and not dynamic_results:
            return _with_emotion_timeline(
                "权重池平静，没有需要处理的记忆。",
                emotion_trend,
            )

        parts = []
        if pinned_results:
            parts.append("=== 核心准则 ===\n" + "\n---\n".join(pinned_results))
        if dynamic_results:
            parts.append("=== 浮现记忆 ===\n" + "\n---\n".join(dynamic_results))
        return _with_emotion_timeline("\n\n".join(parts), emotion_trend)

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

    try:
        matches = await bucket_mgr.search(
            query,
            limit=1000,
            domain_filter=domain_filter,
            query_valence=q_valence,
            query_arousal=q_arousal,
            include_dormant=include_dormant,
        )
    except Exception as e:
        logger.error(f"Search failed / 检索失败: {e}")
        return "检索过程出错，请稍后重试。"

    matches = [
        bucket
        for bucket in matches
        if _is_recent_bucket(bucket, recent_cutoff)
        and _is_in_date_range(bucket, date_from, date_to)
        and (include_sealed or not _is_sealed(bucket))
    ]
    if resonance_target:
        matches.sort(key=lambda bucket: _resonance_distance(bucket, resonance_target))
    hidden_count = max(0, len(matches) - max_results)
    matches = matches[:max_results]

    results = []
    token_used = 0
    for bucket in matches:
        if token_used >= max_tokens:
            break
        try:
            clean_meta = {k: v for k, v in bucket["metadata"].items() if k != "tags"}
            # --- Memory reconstruction: shift displayed valence by current mood ---
            # --- 记忆重构：根据当前情绪微调展示层 valence（±0.1）---
            if q_valence is not None and "valence" in clean_meta:
                original_v = float(clean_meta.get("valence", 0.5))
                shift = (q_valence - 0.5) * 0.2  # ±0.1 max shift
                clean_meta["valence"] = max(0.0, min(1.0, original_v + shift))
            summary = await dehydrator.dehydrate(strip_wikilinks(bucket["content"]), clean_meta)
            summary_tokens = count_tokens_approx(summary)
            if token_used + summary_tokens > max_tokens:
                break
            await bucket_mgr.touch(bucket["id"])
            if bucket.get("vector_match"):
                summary = f"[语义关联] [bucket_id:{bucket['id']}] {summary}"
            else:
                summary = f"[bucket_id:{bucket['id']}] {summary}"
            summary = await _append_bucket_extras(summary, bucket, emotion_trend)
            results.append(summary)
            token_used += summary_tokens
        except Exception as e:
            logger.warning(f"Failed to dehydrate search result / 检索结果脱水失败: {e}")
            continue

    if not results:
        await _fire_webhook("breath", {"mode": "empty", "matches": 0})
        return _with_emotion_timeline("未找到相关记忆。", emotion_trend)

    final_text = "\n---\n".join(results)
    if hidden_count:
        final_text += f"\n\n还有{hidden_count}个相关桶未显示"
    await _fire_webhook("breath", {"mode": "ok", "matches": len(matches), "chars": len(final_text)})
    return _with_emotion_timeline(final_text, emotion_trend)


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
_rm_asset_upload_lock = threading.Lock()
_rm_asset_download_tokens = {}
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


async def _asset_stream_browser_upload(request, sink=None) -> dict:
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
        if state["decoded_bytes"] > ASSET_BROWSER_UPLOAD_MAX_BYTES:
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
        if wire_bytes > ASSET_BROWSER_UPLOAD_MAX_BYTES + ASSET_BROWSER_UPLOAD_MAX_WIRE_OVERHEAD:
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


def _rm_cleanup_asset_uploads(now: float) -> None:
    for upload_id, item in list(_rm_asset_uploads.items()):
        if item["state"] == "pending" and item["expires_at"] <= now:
            token = item.get("token", "")
            if token:
                _rm_asset_upload_tokens.pop(token, None)
            item["token"] = ""
            item["state"] = "expired"
        if item["retire_at"] <= now:
            token = item.get("token", "")
            if token:
                _rm_asset_upload_tokens.pop(token, None)
            _rm_asset_uploads.pop(upload_id, None)


def _rm_create_asset_upload_link(
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
    mime = (mime_type or "application/octet-stream").strip().lower()
    if mime not in {"application/octet-stream", "image/jpeg", "image/png"}:
        return _asset_ingest_response(False, error="unsupported_mime_type")

    current = time.time() if now is None else now
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
        _rm_asset_uploads[upload_id] = {
            "state": "pending",
            "token": token,
            "expected_bytes": expected_bytes,
            "expected_sha256": expected,
            "filename": asset_store.sanitize_filename(filename),
            "mime_type": mime,
            "expires_at": expires_at,
            "retire_at": expires_at + RM_ASSET_UPLOAD_TTL_SECONDS,
            "result": None,
        }
        _rm_asset_upload_tokens[token] = upload_id

    upload_path = f"/rm/asset-upload/{token}"
    base_url = _asset_public_base_url()
    return _json_lib.dumps({
        "ok": True,
        "upload_id": upload_id,
        "upload_path": upload_path,
        "upload_url": f"{base_url}{upload_path}" if base_url else "",
        "status_path": f"/rm/asset-upload-status/{upload_id}",
        "expires_in_seconds": RM_ASSET_UPLOAD_TTL_SECONDS,
        "max_bytes": ASSET_BROWSER_UPLOAD_MAX_BYTES,
    }, ensure_ascii=False, sort_keys=True)


def _rm_asset_upload_status_payload(upload_id: str, now: float | None = None) -> str:
    upload_id = (upload_id or "").strip()
    if not re.fullmatch(r"[0-9a-f]{32}", upload_id):
        return _asset_ingest_response(False, error="invalid_upload_id")
    current = time.time() if now is None else now
    with _rm_asset_upload_lock:
        _rm_cleanup_asset_uploads(current)
        item = _rm_asset_uploads.get(upload_id)
        if not item:
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
        return {
            "upload_id": upload_id,
            "expected_bytes": item["expected_bytes"],
            "filename": item["filename"],
            "expires_at": item["expires_at"],
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
        item["state"] = "uploading"
        return {
            "upload_id": upload_id,
            "expected_bytes": item["expected_bytes"],
            "expected_sha256": item["expected_sha256"],
            "filename": item["filename"],
            "mime_type": item["mime_type"],
        }


def _rm_release_asset_upload(upload_id: str, now: float | None = None) -> None:
    current = time.time() if now is None else now
    with _rm_asset_upload_lock:
        _rm_cleanup_asset_uploads(current)
        item = _rm_asset_uploads.get(upload_id)
        if not item or item["state"] != "uploading":
            return
        if item["expires_at"] <= current:
            item["state"] = "expired"
            item["token"] = ""
            return
        item["state"] = "pending"
        _rm_asset_upload_tokens[item["token"]] = upload_id


def _rm_complete_asset_upload(upload_id: str, asset: dict, source_sha256: str) -> dict | None:
    with _rm_asset_upload_lock:
        item = _rm_asset_uploads.get(upload_id)
        if not item or item["state"] != "uploading":
            return None
        result = _rm_asset_public_metadata(asset, bool(asset.get("deduplicated")))
        result["source_sha256"] = source_sha256
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
<body><h1>Remember-Me asset upload</h1><p>Expected file: <code>{filename}</code></p><p>Expected size: {item["expected_bytes"]} bytes; hard limit: {ASSET_BROWSER_UPLOAD_MAX_BYTES} bytes.</p><p>Link expires in {expires_in} seconds.</p>
<form method="post" enctype="multipart/form-data" action="{action}"><label for="file">Choose file</label><input id="file" name="file" type="file" required><button type="submit">Upload and store</button></form></body></html>"""


def _rm_asset_result_page(result: dict) -> str:
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Asset stored</title></head>
<body><h1>Asset stored</h1><p>asset_id: <code>{html.escape(result["asset_id"])}</code></p><p>stored_sha256: <code>{html.escape(result["stored_sha256"])}</code></p><p>stored_bytes: {result["stored_bytes"]}</p><p>deduplicated: {str(result["deduplicated"]).lower()}</p></body></html>"""


def _rm_cleanup_asset_downloads(now: float) -> None:
    expired = [token for token, item in _rm_asset_download_tokens.items() if item["expires_at"] <= now]
    for token in expired:
        _rm_asset_download_tokens.pop(token, None)


def _rm_safe_download_filename(asset: dict) -> str:
    name = re.sub(r"[^A-Za-z0-9._ -]+", "_", asset.get("original_filename", "")).strip(" .")
    extension = Path(asset["stored_relpath"]).suffix
    if not name:
        name = f"remember-me-{asset['asset_id']}{extension}"
    elif extension and not name.lower().endswith(extension.lower()):
        name += extension
    return name[:180]


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
        _rm_asset_download_tokens[token] = {
            "asset_id": asset["asset_id"],
            "expires_at": current + RM_ASSET_DOWNLOAD_TTL_SECONDS,
            "get_count": 0,
        }
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


def _rm_read_asset_download(token: str, method: str, now: float | None = None) -> tuple[dict, Path, dict] | None:
    if not re.fullmatch(r"[A-Za-z0-9_-]{40,128}", token or ""):
        return None
    current = time.time() if now is None else now
    with _rm_asset_download_lock:
        _rm_cleanup_asset_downloads(current)
        item = _rm_asset_download_tokens.get(token)
        if not item:
            return None
        resolved = asset_store.resolve_file(item["asset_id"])
        if not resolved:
            _rm_asset_download_tokens.pop(token, None)
            return None
        asset, path = resolved
        if method.upper() == "GET":
            if item["get_count"] >= RM_ASSET_DOWNLOAD_MAX_GETS:
                return None
            item["get_count"] += 1
        headers = {
            "Content-Type": asset["mime_type"],
            "Content-Disposition": f'attachment; filename="{_rm_safe_download_filename(asset)}"',
            "Cache-Control": "no-store",
            "Pragma": "no-cache",
            "X-Content-Type-Options": "nosniff",
            "Content-Length": str(asset["stored_bytes"]),
        }
        return asset, path, headers


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
    if actual_bytes > ASSET_BROWSER_UPLOAD_MAX_BYTES:
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


@mcp.tool()
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
    except (binascii.Error, UnicodeEncodeError, ValueError) as exc:
        return _json_lib.dumps({
            "ok": False,
            "error": "invalid_base64",
            "detail": str(exc),
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


@mcp.tool()
async def asset_ingest_begin(
    expected_bytes: int,
    expected_sha256: str,
    mime_type: str = "application/octet-stream",
    filename: str = "",
) -> str:
    """Begin a temporary Phase-0 chunked upload that persists nothing."""
    return _asset_begin_ingest_upload(expected_bytes, expected_sha256, mime_type, filename)


@mcp.tool()
async def asset_ingest_chunk(upload_id: str, chunk_index: int, data_base64: str) -> str:
    """Strictly decode and append one bounded base64 chunk without logging it."""
    return _asset_ingest_chunk_data(upload_id, chunk_index, data_base64)


@mcp.tool()
async def asset_ingest_finish(upload_id: str) -> str:
    """Hash a completed temporary upload, report matches, and discard its bytes."""
    return _asset_finish_ingest_upload(upload_id)


@mcp.tool()
async def asset_ingest_abort(upload_id: str) -> str:
    """Discard a temporary Phase-0 chunked upload; repeated aborts are safe."""
    return _asset_abort_ingest_upload(upload_id)


@mcp.tool()
async def asset_browser_upload_link(
    expected_bytes: int,
    expected_sha256: str = "",
    filename: str = "",
    mime_type: str = "application/octet-stream",
) -> str:
    """Create a short-lived browser upload URL; raw file bytes never enter the model context."""
    return _asset_create_browser_upload_link(expected_bytes, expected_sha256, filename, mime_type)


@mcp.tool()
async def asset_browser_upload_status(upload_id: str) -> str:
    """Return metadata-only status for a Phase-0 browser upload."""
    return _asset_browser_upload_status_payload(upload_id)


@mcp.custom_route("/rm/upload/{token}", methods=["GET", "POST"])
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
    expected_sha256: str = "",
    filename: str = "",
    mime_type: str = "application/octet-stream",
) -> str:
    """Create a short-lived browser upload URL for persistent Remember-Me assets."""
    return _rm_create_asset_upload_link(expected_bytes, expected_sha256, filename, mime_type)


@mcp.tool()
async def rm_asset_upload_status(upload_id: str) -> str:
    """Return metadata-only status for a persistent Remember-Me asset upload."""
    return _rm_asset_upload_status_payload(upload_id)


@mcp.tool()
async def rm_asset_get(asset_id: str) -> str:
    """Return persistent asset metadata without file bytes or disk paths."""
    asset = asset_store.get((asset_id or "").strip())
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
        asset = asset_store.update_metadata(
            asset_id,
            title=title,
            description=description,
            tags=tags,
        )
    except AssetStoreError as exc:
        return _asset_ingest_response(False, error=str(exc))
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
        result = asset_store.search(
            query=query,
            tags=tags,
            kind=kind,
            mime_type=mime_type,
            created_from=created_from,
            created_to=created_to,
            limit=limit,
            offset=offset,
        )
    except AssetStoreError as exc:
        return _asset_ingest_response(False, error=str(exc))
    if query.strip() and embedding_engine.enabled:
        try:
            semantic_scores = await asset_embedding_index.search(query)
            if semantic_scores:
                result = asset_store.search(
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
        result = await asset_embedding_index.reindex(
            asset_id=(asset_id or "").strip(),
            limit=limit,
        )
    except (AssetStoreError, ValueError) as exc:
        return _asset_ingest_response(False, error=str(exc))
    return _json_lib.dumps(
        {"ok": True, **result},
        ensure_ascii=False,
        sort_keys=True,
    )

@mcp.tool()
async def rm_asset_download_link(asset_id: str) -> str:
    """Create a five-minute signed download URL for one persistent asset."""
    return _rm_create_asset_download_link(asset_id)


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

@mcp.tool()
async def rm_asset_inspect(asset_id: str) -> CallToolResult:
    """Return the cleaned stored image for actual visual understanding.

    Call rm_asset_inspect when the model needs to read the image or text inside it.
    Call rm_asset_view when the goal is only to show the image to the user.
    Never guess image content from metadata. This tool does not update metadata
    or embeddings.
    """
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

@mcp.custom_route("/rm/asset-upload/{token}", methods=["GET", "POST"])
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
    temp_path = asset_store.create_temp_path()
    try:
        with temp_path.open("wb") as handle:
            streamed = await _asset_stream_browser_upload(request, handle.write)
        size_match = streamed["decoded_bytes"] == claim["expected_bytes"]
        hash_match = not claim["expected_sha256"] or hmac.compare_digest(
            streamed["sha256"], claim["expected_sha256"]
        )
        if not size_match or not hash_match:
            _rm_release_asset_upload(claim["upload_id"])
            return Response(status_code=422, headers=headers)
        asset = await asyncio.to_thread(
            asset_store.persist_upload,
            temp_path,
            streamed["sha256"],
            streamed["decoded_bytes"],
            claim["filename"],
            claim["mime_type"],
        )
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

    result = _rm_complete_asset_upload(claim["upload_id"], asset, streamed["sha256"])
    if result is None:
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
    _, path, headers = result
    if request.method.upper() == "HEAD":
        return Response(content=b"", headers=headers)
    return FileResponse(path, media_type=headers["Content-Type"], headers=headers)

@mcp.tool()
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


@mcp.tool()
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


@mcp.tool()
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


@mcp.tool()
async def asset_vision_verify(trial_id: str, answer_json: str) -> str:
    """Phase-0 blind vision verifier: score one submitted answer without returning the correct answer."""
    return _asset_score_vision_answer(trial_id, answer_json)


@mcp.tool()
async def asset_vision_export(trial_id: str) -> str:
    """Phase-0 file-view vision probe: export a live challenge PNG as JSON/base64 without revealing the answer."""
    return _asset_export_vision_trial(trial_id)


@mcp.tool()
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


@mcp.tool()
async def asset_vision_upload_challenge() -> str:
    """Phase-0 user-upload vision control: create a blind trial without returning ImageContent or base64."""
    trial = _asset_new_vision_trial()
    ok, error = _asset_store_vision_trial(trial)
    if not ok:
        return _asset_reject_vision_answer(error)
    return _asset_vision_upload_payload(trial["trial_id"], len(trial["png"]), trial["sha256"])


@mcp.tool()
async def digest(dry_run: bool = True, max_groups: int = 10, confirm_token: str = "") -> str:
    """Run automatic memory digestion. Defaults to dry-run and does not mutate data."""
    await decay_engine.ensure_started()
    try:
        return await _run_digest(dry_run=dry_run, max_groups=max_groups, confirm_token=confirm_token)
    except Exception as exc:
        logger.error("Digest failed: %s", exc)
        return f"自动消化失败: {exc}"


@mcp.tool()
async def related_backfill(dry_run: bool = True, limit: int = 100, threshold: float = -1) -> str:
    """Backfill semantic related links. Defaults to dry-run and skips sealed buckets."""
    await decay_engine.ensure_started()
    try:
        actual_threshold = None if threshold < 0 else threshold
        return await _run_related_backfill(dry_run=dry_run, limit=limit, threshold=actual_threshold)
    except Exception as exc:
        logger.error("Related backfill failed: %s", exc)
        return f"自动 related 回填失败: {exc}"


@mcp.tool()
async def breath(
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
    mailbox: bool = False,
    mailbox_limit: int = 1,
    feels: bool = False,
) -> str:
    """Public MCP wrapper for memory retrieval plus optional mailbox access."""
    if mailbox:
        return _with_response_seal(
            _format_mailbox(mailbox_limit, include_sealed=include_sealed)
        )
    if feels:
        domain = "feel"
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
        emotion_trend=emotion_trend,
        include_dormant=include_dormant,
        include_sealed=include_sealed,
        date_from=date_from,
        date_to=date_to,
        resonance=resonance,
    )
    return _with_response_seal(result)


# =============================================================
# Tool 2: hold — Hold on to this
# 工具 2：hold — 握住，留下来
# =============================================================
@mcp.tool()
async def hold(
    content: str,
    tags: str = "",
    importance: int = 5,
    pinned: bool = False,
    feel: bool = False,
    source_bucket: str = "",
    valence: float = -1,
    arousal: float = -1,
    trigger_date: str = "",
) -> str:
    """存储单条记忆,自动打标+合并。tags逗号分隔,importance 1-10。pinned=True创建永久钉选桶。feel=True存储你的第一人称感受(不参与普通浮现)。source_bucket=被消化的记忆桶ID(feel模式下,标记源记忆为已消化)。"""
    await decay_engine.ensure_started()

    # --- Input validation / 输入校验 ---
    if not content or not content.strip():
        return "内容为空，无法存储。"
    content = _apply_display_aliases(content)
    conflict_warning = await _detect_conflict_warning(content)
    try:
        trigger_date = _parse_optional_date(trigger_date, "trigger_date") or ""
    except ValueError as exc:
        return str(exc)
    should_record_emotion = 0 <= valence <= 1 and 0 <= arousal <= 1

    importance = max(1, min(10, importance))
    extra_tags = [t.strip() for t in tags.split(",") if t.strip()]

    # --- Feel mode: store as feel type, minimal metadata ---
    # --- Feel 模式：存为 feel 类型，最少元数据 ---
    if feel:
        # Feel valence/arousal = model's own perspective
        feel_valence = valence if 0 <= valence <= 1 else 0.5
        feel_arousal = arousal if 0 <= arousal <= 1 else 0.3
        try:
            feel_analysis = await dehydrator.analyze(content)
        except Exception as e:
            logger.warning(f"Feel auto-tagging failed, using defaults: {e}")
            feel_analysis = {"tags": []}
        feel_name = strip_wikilinks(content).strip().replace("\n", " ")[:20] or None
        bucket_id = await bucket_mgr.create(
            content=content,
            tags=feel_analysis.get("tags", []),
            importance=5,
            domain=[],
            valence=feel_valence,
            arousal=feel_arousal,
            name=feel_name,
            bucket_type="feel",
        )
        if should_record_emotion:
            _record_emotion_snapshot(valence, arousal, "hold")
        if trigger_date:
            await bucket_mgr.update(bucket_id, trigger_date=trigger_date, trigger_last_seen="")
        await _auto_link_related(bucket_id)
        # --- Mark source memory as digested + store model's valence perspective ---
        # --- 标记源记忆为已消化 + 存储模型视角的 valence ---
        if source_bucket and source_bucket.strip():
            try:
                update_kwargs = {"digested": True}
                if 0 <= valence <= 1:
                    update_kwargs["model_valence"] = feel_valence
                await bucket_mgr.update(source_bucket.strip(), **update_kwargs)
            except Exception as e:
                logger.warning(f"Failed to mark source as digested / 标记已消化失败: {e}")
        response = f"🫧feel→{bucket_id}"
        if conflict_warning:
            response += f"\nconflict: {conflict_warning}"
        return response

    # --- Step 1: auto-tagging / 自动打标 ---
    try:
        analysis = await dehydrator.analyze(content)
    except Exception as e:
        logger.warning(f"Auto-tagging failed, using defaults / 自动打标失败: {e}")
        analysis = {
            "domain": ["未分类"], "valence": 0.5, "arousal": 0.3,
            "tags": [], "suggested_name": "",
        }

    domain = analysis["domain"]
    auto_valence = analysis["valence"]
    auto_arousal = analysis["arousal"]
    auto_tags = analysis["tags"]
    suggested_name = analysis.get("suggested_name", "")

    # --- User-supplied valence/arousal takes priority over analyze() result ---
    # --- 用户显式传入的 valence/arousal 优先，analyze() 结果作为 fallback ---
    final_valence = valence if 0 <= valence <= 1 else auto_valence
    final_arousal = arousal if 0 <= arousal <= 1 else auto_arousal

    all_tags = list(dict.fromkeys(auto_tags + extra_tags))

    # --- Pinned buckets bypass merge and are created directly in permanent dir ---
    # --- 钉选桶跳过合并，直接新建到 permanent 目录 ---
    if pinned:
        bucket_id = await bucket_mgr.create(
            content=content,
            tags=all_tags,
            importance=10,
            domain=domain,
            valence=final_valence,
            arousal=final_arousal,
            name=suggested_name or None,
            bucket_type="permanent",
            pinned=True,
        )
        if should_record_emotion:
            _record_emotion_snapshot(valence, arousal, "hold")
        if trigger_date:
            await bucket_mgr.update(bucket_id, trigger_date=trigger_date, trigger_last_seen="")
        await _auto_link_related(bucket_id)
        response = f"📌钉选→{bucket_id} {','.join(domain)}"
        if conflict_warning:
            response += f"\nconflict: {conflict_warning}"
        return response

    # --- Step 2: merge or create / 合并或新建 ---
    result_name, is_merged = await _merge_or_create(
        content=content,
        tags=all_tags,
        importance=importance,
        domain=domain,
        valence=final_valence,
        arousal=final_arousal,
        name=suggested_name,
        trigger_date=trigger_date,
    )
    if should_record_emotion:
        _record_emotion_snapshot(valence, arousal, "hold")

    action = "合并→" if is_merged else "新建→"
    response = f"{action}{result_name} {','.join(domain)}"
    if conflict_warning:
        response += f"\nconflict: {conflict_warning}"
    return response


# =============================================================
# Tool 3: grow — Grow, fragments become memories
# 工具 3：grow — 生长，一天的碎片长成记忆
# =============================================================
@mcp.tool()
async def grow(content: str) -> str:
    """日记归档,自动拆分为多桶。短内容(<30字)走快速路径。"""
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
        try:
            analysis = await dehydrator.analyze(content)
        except Exception as e:
            logger.warning(f"Fast-path analyze failed / 快速路径打标失败: {e}")
            analysis = {
                "domain": ["未分类"], "valence": 0.5, "arousal": 0.3,
                "tags": [], "suggested_name": "",
            }
        result_name, is_merged = await _merge_or_create(
            content=content.strip(),
            tags=analysis.get("tags", []),
            importance=analysis.get("importance", 5) if isinstance(analysis.get("importance"), int) else 5,
            domain=analysis.get("domain", ["未分类"]),
            valence=analysis.get("valence", 0.5),
            arousal=analysis.get("arousal", 0.3),
            name=analysis.get("suggested_name", ""),
        )
        action = "合并" if is_merged else "新建"
        response = f"{action} → {result_name} | {','.join(analysis.get('domain', []))} V{analysis.get('valence', 0.5):.1f}/A{analysis.get('arousal', 0.3):.1f}"
        if conflict_warning:
            response += f"\nconflict: {conflict_warning}"
        return response

    # --- Step 1: let API split and organize / 让 API 拆分整理 ---
    try:
        items = await dehydrator.digest(content)
    except Exception as e:
        logger.error(f"Diary digest failed / 日记整理失败: {e}")
        return f"日记整理失败: {e}"

    if not items:
        return "内容为空或整理失败。"

    results = []
    conflicts = []
    created = 0
    merged = 0

    # --- Step 2: merge or create each item (with per-item error handling) ---
    # --- 逐条合并或新建（单条失败不影响其他）---
    for item in items:
        try:
            conflict_warning = await _detect_conflict_warning(item["content"])
            result_name, is_merged = await _merge_or_create(
                content=item["content"],
                tags=item.get("tags", []),
                importance=item.get("importance", 5),
                domain=item.get("domain", ["未分类"]),
                valence=item.get("valence", 0.5),
                arousal=item.get("arousal", 0.3),
                name=item.get("name", ""),
            )

            if is_merged:
                results.append(f"📎{result_name}")
                merged += 1
            else:
                results.append(f"📝{item.get('name', result_name)}")
                created += 1
            if conflict_warning:
                conflicts.append(f"{item.get('name', result_name)}: {conflict_warning}")
        except Exception as e:
            logger.warning(
                f"Failed to process diary item / 日记条目处理失败: "
                f"{item.get('name', '?')}: {e}"
            )
            results.append(f"⚠️{item.get('name', '?')}")

    response = f"{len(items)}条|新{created}合{merged}\n" + "\n".join(results)
    if conflicts:
        response += "\nconflict: " + "；".join(conflicts)
    return response


# =============================================================
# Tool 4: trace — Trace, redraw the outline of a memory
# 工具 4：trace — 描摹，重新勾勒记忆的轮廓
# Also handles deletion (delete=True)
# 同时承接删除功能
# =============================================================
@mcp.tool()
async def trace(
    bucket_id: str,
    name: str = "",
    domain: str = "",
    valence: float = -1,
    arousal: float = -1,
    importance: int = -1,
    tags: str = "",
    resolved: int = -1,
    pinned: int = -1,
    digested: int = -1,
    dormant: int = -1,
    sealed: int = -1,
    content: str = "",
    related: str = "",
    merge: str = "",
    append: bool = False,
    trigger_date: str = "",
    delete: bool = False,
) -> str:
    # MCP schema note: related must stay in the tool signature for bidirectional links.
    """修改记忆元数据或内容。resolved=1沉底/0激活,pinned=1钉选/0取消,digested=1隐藏(保留但不浮现)/0取消隐藏,content=替换桶正文,delete=True删除。只传需改的,-1或空=不改。"""

    if not bucket_id or not bucket_id.strip():
        return "请提供有效的 bucket_id。"

    bucket_ids = list(dict.fromkeys(_parse_csv_ids(bucket_id)))
    if not bucket_ids:
        return "请提供有效的 bucket_id。"
    if len(bucket_ids) > 1:
        if merge:
            return "批量 trace 不能与 merge 同时使用。"
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
                resolved=resolved,
                pinned=pinned,
                digested=digested,
                dormant=dormant,
                sealed=sealed,
                content="",
                related=related,
                merge="",
                append=append,
                trigger_date=trigger_date,
                delete=delete,
            )
            results.append(f"[{current_id}] {result}")
        return "\n".join(results)
    bucket_id = bucket_ids[0]

    if merge and delete:
        return "merge 不能与 delete 同时使用。"
    if merge:
        return await _merge_bucket_into_target(bucket_id, merge.strip())
    try:
        trigger_date = _parse_optional_date(trigger_date, "trigger_date") or ""
    except ValueError as exc:
        return str(exc)

    # --- Delete mode / 删除模式 ---
    if delete:
        success = await bucket_mgr.delete(bucket_id)
        if success:
            embedding_engine.delete_embedding(bucket_id)
        return f"已遗忘记忆桶: {bucket_id}" if success else f"未找到记忆桶: {bucket_id}"

    bucket = await bucket_mgr.get(bucket_id)
    if not bucket:
        return f"未找到记忆桶: {bucket_id}"

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
    if 1 <= importance <= 10:
        updates["importance"] = importance
    if tags:
        updates["tags"] = [t.strip() for t in tags.split(",") if t.strip()]
    if resolved in (0, 1):
        updates["resolved"] = bool(resolved)
    if pinned in (0, 1):
        updates["pinned"] = bool(pinned)
        if pinned == 1:
            updates["importance"] = 10  # pinned → lock importance
    if digested in (0, 1):
        updates["digested"] = bool(digested)
    if dormant in (0, 1):
        updates["dormant"] = bool(dormant)
    if sealed in (0, 1):
        updates["sealed"] = sealed
    if trigger_date:
        updates["trigger_date"] = trigger_date
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
    related_ids = _parse_csv_ids(related)
    if related_ids:
        current_related = _related_ids(bucket.get("metadata", {}))
        updates["related_buckets"] = ",".join(dict.fromkeys(current_related + related_ids))

    if "valence" in updates or "arousal" in updates:
        meta = bucket.get("metadata", {})
        next_valence = updates.get("valence", meta.get("valence", 0.5))
        next_arousal = updates.get("arousal", meta.get("arousal", 0.3))
        updates["emotion_history"] = _append_emotion_history(meta, next_valence, next_arousal)

    if not updates:
        return "没有任何字段需要修改。"
    if "dormant" not in updates:
        updates["dormant"] = False

    success = await bucket_mgr.update(bucket_id, **updates)
    if not success:
        return f"修改失败: {bucket_id}"

    if related_ids:
        for related_id in related_ids:
            if related_id == bucket_id:
                continue
            related_bucket = await bucket_mgr.get(related_id)
            if not related_bucket:
                continue
            current_related = _related_ids(related_bucket.get("metadata", {}))
            if bucket_id not in current_related:
                await bucket_mgr.update(
                    related_id,
                    related_buckets=",".join(dict.fromkeys(current_related + [bucket_id])),
                )

    changed = ", ".join(
        f"{k}={v}"
        for k, v in updates.items()
        if k not in ("content", "_history_change_type")
    )
    if "content" in updates:
        content_label = "content=已追加" if append else "content=已替换"
        changed += (f", {content_label}" if changed else content_label)
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
    return f"已修改记忆桶 {bucket_id}: {changed}"

@mcp.tool()
async def seal_letter(letter_id: int, sealed: int = 1) -> str:
    """Hide or unhide a handoff letter by id."""
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
) -> str:
    # MCP schema note: this function is intentionally registered as a tool.
    """Archive the current conversation summary into archive/session."""
    await decay_engine.ensure_started()
    if not summary or not summary.strip():
        return "summary 不能为空。"

    today = datetime.now().date().isoformat()
    all_buckets = await bucket_mgr.list_all(include_archive=True)
    existing = [
        b for b in all_buckets
        if "session" in b.get("metadata", {}).get("domain", [])
        and str(b.get("metadata", {}).get("name", "")).startswith(f"session_{today}_")
    ]
    session_name = f"session_{today}_{len(existing) + 1:02d}"

    parts = [f"# {session_name}", "", "## Summary", summary.strip()]
    if highlights.strip():
        parts.extend(["", "## Highlights", highlights.strip()])
    if mood.strip():
        parts.extend(["", "## Mood", mood.strip()])

    archive_valence = valence if 0 <= valence <= 1 else 0.5
    archive_arousal = arousal if 0 <= arousal <= 1 else 0.3

    bucket_id = await bucket_mgr.create(
        content="\n".join(parts),
        tags=["session", "archive"],
        importance=5,
        domain=["session"],
        valence=archive_valence,
        arousal=archive_arousal,
        bucket_type="dynamic",
        name=session_name,
        sealed=sealed,
    )
    await bucket_mgr.archive(bucket_id)
    if letter.strip():
        bucket_mgr.record_letter(letter.strip(), bucket_id, sealed=sealed)
    if 0 <= valence <= 1 and 0 <= arousal <= 1:
        _record_emotion_snapshot(valence, arousal, "archive")
    return f"已归档本次对话: {session_name} bucket_id:{bucket_id}"


# =============================================================
# Tool 6: pulse — Heartbeat, system status + memory listing
# 工具 5：pulse — 脉搏，系统状态 + 记忆列表
# =============================================================
@mcp.tool()
async def todos() -> str:
    """Return todos from every unresolved bucket, grouped by bucket."""
    await decay_engine.ensure_started()
    try:
        all_buckets = await bucket_mgr.list_all(include_archive=True)
    except Exception as exc:
        logger.error("Failed to list buckets for todos: %s", exc)
        return "待办汇总暂时无法读取。"

    groups = []
    for bucket in all_buckets:
        meta = bucket.get("metadata", {})
        if _is_sealed(bucket):
            continue
        if meta.get("resolved", False):
            continue
        items = _normalize_todos(meta.get("todos"))
        if not items:
            continue
        name = meta.get("name", bucket["id"])
        importance = meta.get("importance", "?")
        lines = [
            f"[bucket_id:{bucket['id']}] {name} | 重要度:{importance}",
            *(f"- {item}" for item in items),
        ]
        groups.append((int(meta.get("importance", 0) or 0), "\n".join(lines)))

    if not groups:
        return "当前没有未完成待办。"
    groups.sort(key=lambda item: item[0], reverse=True)
    return "\n---\n".join(text for _, text in groups)


@mcp.tool()
async def boot(pinned_chars: int = 2000, max_tokens: int = 8000) -> str:
    """One-shot startup context: pinned summaries, latest letter, sessions, todos."""
    await decay_engine.ensure_started()
    pinned_chars = max(80, min(int(pinned_chars or 2000), 2000))
    max_tokens = max(1000, min(int(max_tokens or 8000), 12000))

    try:
        active_buckets = await bucket_mgr.list_all(include_archive=False)
        archive_buckets = await bucket_mgr.list_all(include_archive=True)
    except Exception as exc:
        logger.error("Boot failed to list buckets: %s", exc)
        return _with_response_seal("boot 暂时无法读取记忆库。")

    trigger_text, trigger_ids = await _format_due_triggers(active_buckets)

    pinned = [
        b for b in active_buckets
        if (b.get("metadata", {}).get("pinned") or b.get("metadata", {}).get("protected"))
        and not _is_sealed(b)
    ]
    pinned.sort(
        key=lambda b: _bucket_date(b["metadata"], "updated_at", "last_active", "created"),
        reverse=True,
    )
    pinned_lines = []
    for bucket in pinned:
        meta = bucket.get("metadata", {})
        preview = strip_wikilinks(bucket.get("content", "")).strip()[:pinned_chars]
        pinned_lines.append(
            f"[bucket_id:{bucket['id']}] {meta.get('name', bucket['id'])}\n{preview}"
        )
    pinned_text = "=== boot: 开机索引 ===\n" + (
        "\n---\n".join(pinned_lines) if pinned_lines else "（暂无可见钉选桶）"
    )

    mailbox_text = _format_mailbox(1).replace("=== 信箱 ===", "=== boot: 最新信箱 ===", 1)
    echo_text = _format_feel_echo(active_buckets)

    sessions = [
        b for b in archive_buckets
        if "session" in b.get("metadata", {}).get("domain", [])
        and not _is_sealed(b)
    ]
    sessions.sort(
        key=lambda b: _bucket_date(b["metadata"], "updated_at", "created_at", "created"),
        reverse=True,
    )
    session_lines = []
    for bucket in sessions[:3]:
        meta = bucket.get("metadata", {})
        summary = _extract_session_summary(bucket.get("content", ""))
        session_lines.append(
            f"[bucket_id:{bucket['id']}] {meta.get('name', bucket['id'])}\n{summary}"
        )
    sessions_text = "=== boot: 最近 3 次归档 ===\n" + (
        "\n---\n".join(session_lines) if session_lines else "（暂无 session 归档）"
    )

    todos_text = "=== boot: 未完结 todos ===\n" + await todos()

    body = _fit_sections_to_budget(
        [
            ("triggers", trigger_text),
            ("pinned", pinned_text),
            ("mailbox", mailbox_text),
            ("echo", echo_text),
            ("sessions", sessions_text),
            ("todos", todos_text),
        ],
        max_tokens=max_tokens - 20,
    )
    today = datetime.now().date().isoformat()
    for bucket_id in trigger_ids:
        await bucket_mgr.update(bucket_id, trigger_last_seen=today)
    return _with_response_seal(body)


@mcp.tool()
async def pulse(include_archive: bool = False, show_all: bool = False, include_sealed: bool = False) -> str:
    """系统状态+记忆桶列表。include_archive=True含归档。"""
    await decay_engine.ensure_started()
    try:
        stats = await bucket_mgr.get_stats()
    except Exception as e:
        return f"获取系统状态失败: {e}"

    status = (
        f"=== Ombre Brain 记忆系统 ===\n"
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
        return status + f"\n列出记忆桶失败: {e}"

    if not buckets:
        return status + "\n记忆库为空。"

    await _mark_dormant_buckets(buckets)
    total_buckets = len(buckets)
    listable_buckets = [
        b for b in buckets
        if include_sealed or not _is_sealed(b)
    ]
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
        visible_buckets = sorted(
            listable_buckets,
            key=lambda b: (
                bool(b["metadata"].get("pinned") or b["metadata"].get("protected")),
                _bucket_date(b["metadata"], "updated_at", "last_active", "created"),
            ),
            reverse=True,
        )
        dynamic_count = len(listable_buckets) - len(pinned_buckets)
    else:
        dynamic_buckets = [
            b for b in listable_buckets
            if b["id"] not in pinned_ids
            and not b["metadata"].get("dormant", False)
        ]
        pinned_buckets.sort(key=pulse_score, reverse=True)
        dynamic_buckets.sort(key=pulse_score, reverse=True)
        dynamic_buckets = dynamic_buckets[:15]
        dynamic_count = len(dynamic_buckets)
        visible_buckets = pinned_buckets + dynamic_buckets

    lines = []
    for b in visible_buckets:
        meta = b.get("metadata", {})
        if int(meta.get("sealed", 0) or 0) == 1:
            icon = "🔒"
        elif meta.get("pinned") or meta.get("protected"):
            icon = "📌"
        elif meta.get("type") == "permanent":
            icon = "📦"
        elif meta.get("type") == "feel":
            icon = "🫧"
        elif meta.get("type") == "archived":
            icon = "🗄️"
        elif meta.get("resolved", False):
            icon = "✅"
        else:
            icon = "💭"
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
        created_at = _bucket_date(meta, "created_at", "created")
        updated_at = _bucket_date(meta, "updated_at", "last_active", "created")
        lines.append(
            f"{icon} [{meta.get('name', b['id'])}]{resolved_tag}{sealed_tag}{dormant_tag} "
            f"bucket_id:{b['id']} "
            f"主题:{domains} "
            f"情感:V{val:.1f}/A{aro:.1f} "
            f"重要:{meta.get('importance', '?')} "
            f"权重:{score:.2f} "
            f"created_at:{created_at} "
            f"updated_at:{updated_at} "
            f"标签:{','.join(meta.get('tags', []))}"
        )

    if show_all:
        breakdown = f"钉选{len(pinned_buckets)}个 + 全部非钉选{dynamic_count}个"
        display_stats = (
            f"\n共{total_buckets}个桶，当前显示{len(visible_buckets)}个"
            f"（{breakdown}）\n"
        )
    else:
        display_stats = (
            f"\n共{total_buckets}个桶，当前显示{len(visible_buckets)}个"
            f"（钉选{len(pinned_buckets)}个 + 动态Top15）\n"
        )
    return status + "\n=== 记忆列表 ===\n" + "\n".join(lines) + display_stats


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
async def dream(detail_ids: str = "") -> str:
    """做梦——默认返回最近 5 个记忆摘要；detail_ids 指定的桶返回全文。"""
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
            details.append(
                f"[{meta.get('name', bucket_id)}]{resolved_tag} "
                f"主题:{domains} V{val:.1f}/A{aro:.1f} 更新:{updated}\n"
                f"ID: {bucket_id}\n"
                f"{strip_wikilinks(bucket.get('content', ''))}"
            )
            await bucket_mgr.touch(bucket_id)
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
        await bucket_mgr.touch(bucket["id"])

    if not recent:
        return "没有需要消化的新记忆。"

    parts = []
    for b in recent:
        meta = b["metadata"]
        parts.append(_dream_summary_line(b))

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
@mcp.custom_route("/api/buckets", methods=["GET"])
async def api_buckets(request):
    """List all buckets with metadata (no content for efficiency)."""
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err: return err
    try:
        all_buckets = await bucket_mgr.list_all(include_archive=True)
        result = []
        for b in all_buckets:
            meta = b.get("metadata", {})
            result.append({
                "id": b["id"],
                "name": meta.get("name", b["id"]),
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
                "content_preview": strip_wikilinks(b.get("content", ""))[:200],
            })
        result.sort(key=lambda x: x["score"], reverse=True)
        return JSONResponse(result)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@mcp.custom_route("/api/bucket/{bucket_id}", methods=["GET"])
async def api_bucket_detail(request):
    """Get full bucket content by ID."""
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err: return err
    bucket_id = request.path_params["bucket_id"]
    bucket = await bucket_mgr.get(bucket_id)
    if not bucket:
        return JSONResponse({"error": "not found"}, status_code=404)
    meta = bucket.get("metadata", {})
    return JSONResponse({
        "id": bucket["id"],
        "metadata": meta,
        "content": strip_wikilinks(bucket.get("content", "")),
        "score": decay_engine.calculate_score(meta),
    })


@mcp.custom_route("/api/search", methods=["GET"])
async def api_search(request):
    """Search buckets by query."""
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err: return err
    query = request.query_params.get("q", "")
    if not query:
        return JSONResponse({"error": "missing q parameter"}, status_code=400)
    try:
        matches = await bucket_mgr.search(query, limit=10)
        result = []
        for b in matches:
            meta = b.get("metadata", {})
            result.append({
                "id": b["id"],
                "name": meta.get("name", b["id"]),
                "score": b.get("score", 0),
                "domain": meta.get("domain", []),
                "valence": meta.get("valence", 0.5),
                "arousal": meta.get("arousal", 0.3),
                "content_preview": strip_wikilinks(b.get("content", ""))[:200],
            })
        return JSONResponse(result)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


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
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@mcp.custom_route("/api/breath-debug", methods=["GET"])
async def api_breath_debug(request):
    """Debug endpoint: simulate breath scoring and return per-bucket breakdown."""
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err: return err
    query = request.query_params.get("q", "")
    q_valence = request.query_params.get("valence")
    q_arousal = request.query_params.get("arousal")
    q_valence = float(q_valence) if q_valence else None
    q_arousal = float(q_arousal) if q_arousal else None

    try:
        all_buckets = await bucket_mgr.list_all(include_archive=False)
        results = []
        w = {
            "topic": bucket_mgr.w_topic,
            "emotion": bucket_mgr.w_emotion,
            "time": bucket_mgr.w_time,
            "importance": bucket_mgr.w_importance,
        }
        w_sum = sum(w.values())

        for bucket in all_buckets:
            meta = bucket.get("metadata", {})
            bid = bucket["id"]
            try:
                topic = bucket_mgr._calc_topic_score(query, bucket) if query else 0.0
                emotion = bucket_mgr._calc_emotion_score(q_valence, q_arousal, meta)
                time_s = bucket_mgr._calc_time_score(meta)
                imp = max(1, min(10, int(meta.get("importance", 5)))) / 10.0

                raw_total = (
                    topic * w["topic"]
                    + emotion * w["emotion"]
                    + time_s * w["time"]
                    + imp * w["importance"]
                )
                normalized = (raw_total / w_sum) * 100 if w_sum > 0 else 0
                resolved = meta.get("resolved", False)
                if resolved:
                    normalized *= 0.3

                results.append({
                    "id": bid,
                    "name": meta.get("name", bid),
                    "domain": meta.get("domain", []),
                    "type": meta.get("type", "dynamic"),
                    "resolved": resolved,
                    "pinned": meta.get("pinned", False),
                    "scores": {
                        "topic": round(topic, 4),
                        "emotion": round(emotion, 4),
                        "time": round(time_s, 4),
                        "importance": round(imp, 4),
                    },
                    "weights": w,
                    "raw_total": round(raw_total, 4),
                    "normalized": round(normalized, 2),
                    "passed_threshold": normalized >= bucket_mgr.fuzzy_threshold,
                })
            except Exception:
                continue

        results.sort(key=lambda x: x["normalized"], reverse=True)
        passed = [r for r in results if r["passed_threshold"]]
        return JSONResponse({
            "query": query,
            "valence": q_valence,
            "arousal": q_arousal,
            "weights": w,
            "threshold": bucket_mgr.fuzzy_threshold,
            "total_candidates": len(results),
            "passed_count": len(passed),
            "results": results[:50],  # top 50 for debug
        })
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


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
async def api_config_update(request):
    """Hot-update runtime config. Optionally persist to config.yaml."""
    from starlette.responses import JSONResponse
    import yaml
    err = _require_auth(request)
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
        except Exception as e:
            return JSONResponse({"error": f"persist failed: {e}", "updated": updated}, status_code=500)

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
async def api_host_vault_set(request):
    """
    Persist OMBRE_HOST_VAULT_DIR to the project .env file.
    Body: {"value": "/path/to/vault"}  (empty string clears the entry)
    Note: container restart is required for docker-compose to pick up the new mount.
    """
    from starlette.responses import JSONResponse
    err = _require_auth(request)
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
    except Exception as e:
        return JSONResponse({"error": f"failed to write .env: {e}"}, status_code=500)

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

@mcp.custom_route("/api/import/upload", methods=["POST"])
async def api_import_upload(request):
    """Upload a conversation file and start import."""
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err: return err

    if import_engine.is_running:
        return JSONResponse({"error": "Import already running"}, status_code=409)

    content_type = request.headers.get("content-type", "")
    filename = ""

    try:
        if "multipart/form-data" in content_type:
            form = await request.form()
            file_field = form.get("file")
            if not file_field:
                return JSONResponse({"error": "No file field"}, status_code=400)
            raw_bytes = await file_field.read()
            filename = getattr(file_field, "filename", "upload")
            raw_content = raw_bytes.decode("utf-8", errors="replace")
        else:
            body = await request.body()
            raw_content = body.decode("utf-8", errors="replace")
            # Try to get filename from query params
            filename = request.query_params.get("filename", "upload")

        if not raw_content.strip():
            return JSONResponse({"error": "Empty file"}, status_code=400)

        preserve_raw = request.query_params.get("preserve_raw", "").lower() in ("1", "true")
        resume = request.query_params.get("resume", "").lower() in ("1", "true")

    except Exception as e:
        return JSONResponse({"error": f"Failed to read upload: {e}"}, status_code=400)

    # Start import in background
    async def _run_import():
        try:
            await import_engine.start(raw_content, filename, preserve_raw, resume)
        except Exception as e:
            logger.error(f"Import failed: {e}")

    asyncio.create_task(_run_import())

    return JSONResponse({
        "status": "started",
        "filename": filename,
        "size_bytes": len(raw_content.encode()),
    })


@mcp.custom_route("/api/import/status", methods=["GET"])
async def api_import_status(request):
    """Get current import progress."""
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err: return err
    return JSONResponse(import_engine.get_status())


@mcp.custom_route("/api/import/pause", methods=["POST"])
async def api_import_pause(request):
    """Pause the running import."""
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err: return err
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
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


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
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@mcp.custom_route("/api/import/review", methods=["POST"])
async def api_import_review(request):
    """Apply review decisions: mark buckets as important/noise/pinned."""
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err: return err
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    decisions = body.get("decisions", [])
    if not decisions:
        return JSONResponse({"error": "No decisions provided"}, status_code=400)

    applied = 0
    errors = 0
    for d in decisions:
        bid = d.get("bucket_id", "")
        action = d.get("action", "")
        if not bid or not action:
            continue
        try:
            if action == "important":
                await bucket_mgr.update(bid, importance=9)
            elif action == "pin":
                await bucket_mgr.update(bid, pinned=True)
            elif action == "noise":
                await bucket_mgr.update(bid, resolved=True, importance=1)
            elif action == "delete":
                file_path = bucket_mgr._find_bucket_file(bid)
                if file_path:
                    os.remove(file_path)
            applied += 1
        except Exception as e:
            logger.warning(f"Review action failed for {bid}: {e}")
            errors += 1

    return JSONResponse({"applied": applied, "errors": errors})


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
            "version": "1.3.0",
        })
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


# --- Entry point / 启动入口 ---
if __name__ == "__main__":
    transport = config.get("transport", "stdio")
    logger.info(f"Ombre Brain starting | transport: {transport}")

    if transport in ("sse", "streamable-http"):
        import threading
        import uvicorn
        from starlette.middleware.cors import CORSMiddleware

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
            _app = mcp.streamable_http_app()
        else:
            _app = mcp.sse_app()
        add_mcp_auth_middleware(_app)
        _app.add_middleware(
            CORSMiddleware,
            allow_origins=["*"],
            allow_methods=["*"],
            allow_headers=["*"],
            expose_headers=["*"],
        )
        logger.info("CORS middleware enabled for remote transport / 已启用 CORS 中间件")
        uvicorn.run(_app, host="0.0.0.0", port=OMBRE_PORT)
    else:
        mcp.run(transport=transport)

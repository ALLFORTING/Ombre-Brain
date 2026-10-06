# ============================================================
# Fragment: Dashboard API, host vault, import API and status (server_dashboard_api.py)
# 片段：Dashboard 接口、host vault、导入接口与状态页
#
# NOT an importable module. server.py executes this file in its own
# namespace, at the position where this code used to live, through
# _exec_server_fragment("server_dashboard_api.py"). Never import it.
# 这不是可以单独 import 的模块。server.py 在这段代码原来所在的位置，
# 通过 _exec_server_fragment("server_dashboard_api.py") 在 server 自己的命名空间里执行本文件。禁止 import。
#
# Why: tests unload and re-import server and keep using older server module
# objects, so each server module needs its own copies of these functions and
# of the state below; executing here gives exactly that.
# 原因：测试会卸载并重新加载 server，且继续使用旧的 server 模块对象，
# 每份 server 必须有自己的一套函数和下面的状态；在 server 命名空间里执行正好做到这一点。
#
# Contents: /api/buckets, /api/search, /api/network, /api/breath-debug, /api/assets*,
#   dashboard pages, /api/config, /api/host-vault, /api/import/*, /api/status.
# 内容：/api/buckets、/api/search、/api/network、/api/breath-debug、/api/assets*、
#   Dashboard 页面、/api/config、/api/host-vault、/api/import/*、/api/status。
# State: _IMPORT_BACKGROUND_TASKS (fresh for every server module).
# 状态：_IMPORT_BACKGROUND_TASKS（每份 server 模块各自全新的一套）。
#
# Every name used here comes from server.py's namespace. Tracebacks show this
# file and its own line numbers. A missing file stops server startup.
# 这里用到的名字都来自 server.py 的命名空间。报错堆栈显示本文件和它自己的行号。缺少本文件时服务无法启动。
# ============================================================
# --- end of fragment header ---

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

# ============================================================
# Fragment: Breath retrieval (server_breath.py)
# 片段：breath 检索实现
#
# NOT an importable module. server.py executes this file in its own
# namespace, at the position where this code used to live, through
# _exec_server_fragment("server_breath.py"), i.e. compile(source, path, "exec").
# Never `import server_breath`.
# 这不是可以单独 import 的模块。server.py 在这段代码原来所在的位置，通过
# _exec_server_fragment("server_breath.py")（即 compile(源码, 路径, "exec")）
# 在 server 自己的命名空间里执行。禁止 import server_breath。
#
# Why: tests unload and re-import server, keep using older server module
# objects, and patch names such as server._breath_cursor_scope; every server
# module needs its own copies of these functions, looking names up in that
# module's namespace. Executing here gives exactly that.
# 原因：测试会卸载并重新加载 server，继续使用旧的 server 模块对象，并给
# server._breath_cursor_scope 之类的名字打补丁；每份 server 必须有自己的一套函数，
# 并在自己的命名空间里查名字。在 server 命名空间里执行正好做到这一点。
#
# Contents: _BREATH_ALLOWED and parameter validation (_prepare_breath_request),
# filtered listings, query composition and scoring, query cursors, as_of
# historical retrieval, and _breath_impl. The breath MCP tool itself is
# registered later, from server_breath_tool.py, at its own position.
# 内容：_BREATH_ALLOWED 与参数校验（_prepare_breath_request）、筛选列表、
# query 检索组装与打分、query 翻页 cursor、as_of 历史版本检索，以及 _breath_impl。
# breath 这个 MCP 工具本身在后面由 server_breath_tool.py 在它自己的位置注册。
# The shared candidate filters used by breath, the Dashboard and pulse/dream
# (_recent_cutoff, _normalize_breath_filter, _filter_breath_candidates,
# _mark_dormant_buckets) stay in server.py.
# breath、Dashboard 和 pulse/dream 共用的候选筛选函数（_recent_cutoff、
# _normalize_breath_filter、_filter_breath_candidates、_mark_dormant_buckets）仍留在 server.py。
# State: no process state of its own. The cursor table and its limits
# (_BREATH_CURSOR_STATES, _BREATH_CURSOR_TTL_SECONDS, _BREATH_CURSOR_MAX_STATES)
# stay defined at the top of server.py; _BREATH_ALLOWED is a constant defined here.
# 状态：本文件不建进程内状态。cursor 表及其上限（_BREATH_CURSOR_STATES、
# _BREATH_CURSOR_TTL_SECONDS、_BREATH_CURSOR_MAX_STATES）仍定义在 server.py 顶部；
# _BREATH_ALLOWED 是在这里定义的常量。
#
# Every name used here comes from server.py's namespace. Tracebacks show this
# file and its own line numbers. A missing file stops server startup.
# 这里用到的名字都来自 server.py 的命名空间。报错堆栈显示本文件和它自己的行号。缺少本文件时服务无法启动。
# ============================================================
# --- end of fragment header ---

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

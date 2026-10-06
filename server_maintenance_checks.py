# ============================================================
# Fragment: related backfill, conflict check, similarity doorbell and digest
# scheduler (server_maintenance_checks.py)
# 片段：related 回填、冲突检测、相似提醒（门铃）与定时消化
#
# NOT an importable module. server.py executes this file in its own
# namespace, at the position where this code used to live, through
# _exec_server_fragment("server_maintenance_checks.py"), i.e.
# compile(source, path, "exec"). Never `import server_maintenance_checks`.
# 这不是可以单独 import 的模块。server.py 在这段代码原来所在的位置，通过
# _exec_server_fragment("server_maintenance_checks.py")（即 compile(源码, 路径, "exec")）
# 在 server 自己的命名空间里执行。禁止 import server_maintenance_checks。
#
# Why: tests unload and re-import server, keep using older server module
# objects, and patch names such as server._detect_conflict_warning,
# server._call_conflict_api and server._similarity_doorbell; every server
# module needs its own copies of these functions, looking names up in that
# module's namespace. Executing here gives exactly that.
# 原因：测试会卸载并重新加载 server，继续使用旧的 server 模块对象，并给
# server._detect_conflict_warning、server._call_conflict_api、
# server._similarity_doorbell 之类的名字打补丁；每份 server 必须有自己的一套函数，
# 并在自己的命名空间里查名字。在 server 命名空间里执行正好做到这一点。
#
# Contents: _run_related_backfill, the conflict detector (candidate recall,
# provider call, strict verdict parsing, warning text), the similarity
# doorbell and _digest_scheduler_loop. _auto_link_related, which hold/grow
# and merges call after writing, stays in server.py just above this include.
# 内容：_run_related_backfill、冲突检测（候选召回、模型调用、严格解析判定、提示文字）、
# 相似提醒和 _digest_scheduler_loop。hold/grow 与合并写入后调用的
# _auto_link_related 仍留在 server.py，就在本片段读入点的上方。
# State: none of its own.
# 状态：本文件自己不建状态。
#
# Every name used here comes from server.py's namespace. Tracebacks show this
# file and its own line numbers. A missing file stops server startup.
# 这里用到的名字都来自 server.py 的命名空间。报错堆栈显示本文件和它自己的行号。缺少本文件时服务无法启动。
# ============================================================
# --- end of fragment header ---

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

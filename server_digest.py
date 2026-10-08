# ============================================================
# Fragment: automatic digest (server_digest.py)
# 片段：自动消化
#
# NOT an importable module. server.py executes this file in its own
# namespace, at the position where this code used to live, through
# _exec_server_fragment("server_digest.py"), i.e. compile(source, path, "exec").
# Never `import server_digest`.
# 这不是可以单独 import 的模块。server.py 在这段代码原来所在的位置，通过
# _exec_server_fragment("server_digest.py")（即 compile(源码, 路径, "exec")）
# 在 server 自己的命名空间里执行。禁止 import server_digest。
#
# Why: tests unload and re-import server, keep using older server module
# objects, and patch names such as server._call_digest_api; every server
# module needs its own copies of these functions, looking names up in that
# module's namespace. Executing here gives exactly that.
# 原因：测试会卸载并重新加载 server，继续使用旧的 server 模块对象，并给
# server._call_digest_api 之类的名字打补丁；每份 server 必须有自己的一套函数，
# 并在自己的命名空间里查名字。在 server 命名空间里执行正好做到这一点。
#
# Contents: digest candidates and importance rebalance candidates, plan and
# confirmation payloads, the digest provider call, the dedupe scan, durable
# digest operations (claim, steps, resume) and _run_digest.
# 内容：消化候选与重要度回调候选、计划与确认内容、消化模型调用、查重扫描、
# 可续跑的消化操作（认领、分步执行、续跑）以及 _run_digest。
# State: none of its own. _digest_running_operations, _DIGEST_SOURCE_LIMIT_PER_GROUP
# and the confirmation token table/lock stay defined at the top of server.py.
# 状态：本文件自己不建状态。_digest_running_operations、_DIGEST_SOURCE_LIMIT_PER_GROUP
# 以及确认 token 表和锁仍定义在 server.py 顶部。
#
# Every name used here comes from server.py's namespace. Tracebacks show this
# file and its own line numbers. A missing file stops server startup.
# 这里用到的名字都来自 server.py 的命名空间。报错堆栈显示本文件和它自己的行号。缺少本文件时服务无法启动。
# ============================================================
# --- end of fragment header ---

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
        # Recorded so a digest can be undone; consolidation overwrites it.
        "source_bucket": metadata.get("source_bucket"),
    }


def _digest_state_matches(bucket: dict, state: dict) -> bool:
    # Plans persisted before a field joined the state still compare on theirs.
    current = _digest_bucket_state(bucket)
    return all(current.get(key) == value for key, value in state.items())


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
    if step not in operation["completed"] and not _digest_state_matches(bucket, state):
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

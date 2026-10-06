# ============================================================
# Fragment: the breath MCP tool (server_breath_tool.py)
# 片段：breath MCP 工具
#
# NOT an importable module. server.py executes this file in its own
# namespace, at the position where this code used to live, through
# _exec_server_fragment("server_breath_tool.py"), i.e. compile(source, path, "exec").
# Never `import server_breath_tool`.
# 这不是可以单独 import 的模块。server.py 在这段代码原来所在的位置，通过
# _exec_server_fragment("server_breath_tool.py")（即 compile(源码, 路径, "exec")）
# 在 server 自己的命名空间里执行。禁止 import server_breath_tool。
#
# Why: tests unload and re-import server, keep using older server module
# objects, and patch names such as server._breath_cursor_scope; every server
# module needs its own copies of these functions, looking names up in that
# module's namespace. Executing here gives exactly that.
# 原因：测试会卸载并重新加载 server，继续使用旧的 server 模块对象，并给
# server._breath_cursor_scope 之类的名字打补丁；每份 server 必须有自己的一套函数，
# 并在自己的命名空间里查名字。在 server 命名空间里执行正好做到这一点。
#
# Contents: the @mcp.tool() breath entry point (parameters, descriptions,
# dispatch to _prepare_breath_request and _breath_impl). Its include sits
# between related_backfill and hold, which fixes the tool registration order.
# 内容：@mcp.tool() 注册的 breath 入口（参数、说明，转交 _prepare_breath_request 与
# _breath_impl）。读入点位于 related_backfill 与 hold 之间，工具注册顺序由此固定。
# State: none.
# 状态：无。
#
# Every name used here comes from server.py's namespace. Tracebacks show this
# file and its own line numbers. A missing file stops server startup.
# 这里用到的名字都来自 server.py 的命名空间。报错堆栈显示本文件和它自己的行号。缺少本文件时服务无法启动。
# ============================================================
# --- end of fragment header ---

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

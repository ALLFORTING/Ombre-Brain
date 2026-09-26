# S-2：MCP stateless HTTP 候选实现与断连风险验证

基线：`main = origin/main = 57053c83bef162504a619a026eebf503394709f3`。
独立分支：`codex/s2-mcp-stateless-http`；worktree：`D:\Codex\projects\Ombre-Brain-S2-MCP-Stateless`。
验证日期：2026-09-26。仅 local commit；没有 push、merge、部署或修改线上环境变量。

## 判定

1. **stateless transport 与本次验证的 OB 请求、认证及应用状态兼容：通过。**
2. **当前所有 OB 写工具都已具备断连安全性：不通过。**
3. **当前不应在线上开启 `OMBRE_MCP_STATELESS_HTTP`。** 启用 embedding 且 provider 真正暂停时，主桶内容更新后的 embedding await 可被真实客户端断连取消，留下未完成的 archive 或单向 related。后者已通过真实 trace handler、临时磁盘和 TCP 断连复现，是数据关系完整性的明确阻断项。

这不意味着默认关闭的候选代码不能落地。本 Phase 保留 stateful 默认，并记录问题，未修业务工具、未实现 S-3。

## A / B：文件与开关

仅修改 `server.py`、`backup_entry.py`、`ENV_VARS.md`，新增 `tests/test_mcp_stateless_http.py` 与本报告。

共享 `server.build_streamable_http_app()` 供两个入口调用；其他 middleware 安装顺序保持原样。

- 默认 `false`，未设置、空、无效、false-like 值关闭。
- 与 OB 现有解析一致：`1`、`true`、`yes`、`on` 忽略大小写及首尾空白时开启。
- 只在 streamable-http app 启动构造时读取。SSE / stdio 入口不调用 builder。
- `true` 会禁用 transport session tracking；`json_response` 继续 false。
- auth、CORS、S-1.5 日志、工具/schema、业务实现保持不变。
- 不提供 exactly-once、请求幂等、重试去重或多进程 token/cursor 共享保障。

**SDK API 差异：** 实际安装并测试的 `mcp==1.29.1` 中，`FastMCP.streamable_http_app(self)` 没有 `stateless_http` 关键字参数。直接调用 `streamable_http_app(stateless_http=True)` 会抛 TypeError。因此 builder 在首次构造 manager 前设置 `mcp.settings.stateless_http`，再调用无参数方法。未更换 SDK、未 monkeypatch 生产 SDK。该版本的官方实现可见 [FastMCP v1.29.1](https://github.com/modelcontextprotocol/python-sdk/blob/v1.29.1/src/mcp/server/fastmcp/server.py)。这不是运行时热切换；SDK 会缓存首次构造的 manager。

## C：真实协议矩阵（MCP 1.29.1）

所有新 transport 测试都有 SDK 精确版本断言，使用真实 OB app、原 auth/CORS/diagnostic middleware、真实 Uvicorn、loopback TCP 与临时 buckets。业务存储及 SQLite 均为隔离 tmp；provider 调用不连接真实服务。

| 请求 | stateful（flag false / 默认） | stateless（flag true） |
|---|---|---|
| POST initialize | 200，返回 `Mcp-Session-Id` | 200，不返回 `Mcp-Session-Id` |
| POST notifications/initialized | 202 | 202 |
| POST tools/list | 200，有效 session 正常 | 200 |
| POST tools/call | 200，真实 trace handler 返回正常结果 | 200，同样正常 |
| POST + 任意旧 session ID | 404 | 200；旧 ID 不触发 session lookup，不返回新 ID |
| GET，Accept SSE，无 session ID | 400，`Bad Request: Missing session ID` | 200，`text/event-stream` |
| GET，Accept SSE，有效 stateful session | 200，`text/event-stream` | 无 transport session 概念 |
| GET，Accept SSE，旧 session ID | 404 | 200，`text/event-stream` |
| DELETE | 有效 session 200；随后同 ID POST 404 | 405，`Method Not Allowed: Session termination not supported` |

GET 的 **200 是实际观测结果**，不能假设 stateless GET 必为 405。测试取得真实 SSE 响应头后关闭连接，没有等待无限流结束。stateless GET 的 transport 与其他 POST 独立；本验证不宣称它提供跨请求通知投递或 replay。

Bearer 正确/错误、独立 query-token 正确/错误/flag 关闭、anonymous 明确 opt-in/默认关闭/仍有 Bearer 配置的拒绝规则，在两种模式下等价。S-1.5 的 `has_session` 描述请求头是否存在：stateless 无 ID 为 false、旧 ID 为 true；记录 hash，不泄露原 ID、Bearer 或 query secret。原 diagnostic/auth 回归通过。

## D：confirmation / cursor / durable operation

- 真实 `_issue_mutation_confirmation` / `_consume_mutation_confirmation` 经测试专用工具调用，跨独立 HTTP 请求成功；payload 不匹配拒绝，正确 token 只消费一次，过期拒绝。测试核验原 TTL 上限，仅失效隔离状态，不改变 transport 时钟。
- 真实 `_breath_cursor_scope` / encode / decode 跨请求保留 snapshot 和 position，query binding 与 TTL 保持。额外通过真实 `breath` handler，从临时桶读取两页、无重复，改 query / expired cursor 拒绝；`touch=False`。
- 真实 `trace(..., merge=...)`：目标正文已落盘后、删除源之前关闭 TCP；stateless 收到取消，磁盘 operation 仍为 running，已完成 target step 保留。下一个独立 HTTP 请求使用原 trace pair 恢复，源删除、operation complete，正文只合并一次。
- 真实 `digest` rebalance：durable apply 已将 importance 9→8、尚未记录上层 step 时关闭 TCP。磁盘 marker 保留；重新打开 BucketManager 能读到 operation。后续 HTTP preview 获取已有 resume token，完成恢复，importance 仍为 8，没有重复降到 7。
- 原 merge 重启恢复、digest consolidation / rebalance marker replay、confirmation、cursor 回归一起运行通过。

这些状态保持原来的进程内或磁盘所有权，不移入 transport/session。跨请求成功不代表跨进程或重启的 confirmation/cursor 也可用。

## E：断连取消实测

最小受控慢工具在 SSE POST 的 200 响应头发出后等待 gate。HTTP client 在完成前关闭未读响应，真实 TCP 连接关闭；测试没有主动对工具 task 调 `cancel()`。

| 模式 | 收到 CancelledError | gate 后代码 | transport 最终状态 |
|---|---|---|---|
| stateful | 否 | 释放 gate 后继续执行并完成 | 客户端连接已关闭，无法收到原结果；manager session 仍在，随后 tools/list 正常 |
| stateless | 是 | 不执行 | 请求 transport 结束，manager 不保留 session，随后独立 tools/list 正常 |

业务测试在启用的受控 embedding provider 内加入 gate，让真实 BucketManager 的原 refresh helper 发生暂停；merge/digest 在原持久化边界加入 gate；持久化步骤由原 handler / BucketManager 执行。此证据证明窗口存在，不量化线上发生率或 provider 延迟。

临时多个 Uvicorn server 的测试 harness 会恢复 sse-starlette 的全局 `AppStatus.should_exit`，避免一次测试的 shutdown 自动关闭后续测试 SSE；这是测试隔离，未改生产行为。

## F：OB 写工具分类（只读检查，未修业务）

A：单次近原子写，任务取消风险较低。B：多步骤但有 durable operation / resume。C：多步骤无完整请求幂等 / resume，真实调度点取消可能留下部分状态。分类按路径，不能给整工具统一贴 A。`await` 语法本身不等于实际暂停；同步 SQLite/文件写之间没有 await 时，普通 asyncio 取消不会在其中插入。

| 工具/路径 | 分类 | 持久化与取消窗口 | 严重程度、证据与上线影响 |
|---|---|---|---|
| archive_session | C | `create` 写正文和 boot event，随后 await embedding，再 archive，最后 letter / emotion。无请求级 key | **高，阻断。** create 后取消实测留下 dynamic session；handler 全部成功后响应丢失实测仍会产生第二条 session。不是字节损坏，是多记录业务状态不完整 |
| hold | A / C | target_id 单次 metadata 更新较低风险；新建/feel/pinned 路径 create → embedding → trigger / auto-link / source bucket / response | 中至高。正文已存在但后置操作可能未完成；匹配复用仅是启发式，不能视作请求幂等。只读确认 |
| grow | C | long input digest 拆分后逐项 `_merge_or_create`，每项 provider / embedding 可暂停；短路径同 hold | 高。只完成部分条目，无批次 durable receipt；重试可受不同 LLM 输出/匹配影响。只读确认 |
| trace 普通 metadata update | A | 单桶同步写；无 provider await 的路径不能仅凭 async 语法认定可半写 | 低（限任务取消，不涵盖进程崩溃/磁盘故障）。只读确认 |
| trace content/append + related | C | 主桶正文及 related 已写，await embedding；后续反向 related 尚未写 | **高，阻断，TCP 实测。** stateless 留单向关系；stateful 继续完成双向关系。重试同 append 在主桶留下两份 fragment |
| trace unrelate / supersession / 批量更新 | C，条件性 | 多桶更新；已有 False 返回时补偿，但不是 durable transaction。普通 metadata-only 内部可能不 yield；与 content/provider/异步 backend 组合才出现实际调度窗口 | 中至高，仅只读条件判断；没有宣称每个 await 必会导致半链或已经实测全部路径 |
| trace delete | A / C，条件性 | 单桶 history → vector cleanup → remove；默认 embedding delete 是同步 SQLite，任务取消窗口较低。多桶删除、反向 supersession cleanup 先于删除，无 durable batch plan；若 cleanup/backend 真正异步，取消可跳过 False 分支补偿 | 中至高，条件性只读判断。当前默认同步实现不能据 await 数目宣称已复现半删；仍不具备通用取消安全保证 |
| merge（trace.merge） | B | plan/journal + 每步 `apply_import_operation` atomic marker；finally 释放 running guard；CancelledError 不被 except Exception 转为 failed | 有可恢复 running 状态。HTTP 断连后 resume 实测通过；原重启回归通过。不是自动恢复，也不是所有时点永久零风险 |
| digest consolidation / rebalance | B | provider output 落 journal，桶写带 durable marker，step 可 replay，finally 释放 running guard | rebalance 真 TCP 取消后恢复通过；consolidation marker/restart 原回归通过。日志可能仍 running，需原 preview/confirm resume |
| dismiss_note | A | 预览 token 与确认是应用状态；消费后同步 CAS/SQLite dismiss，保留正文/history，无 provider await | 低，原回归通过；丢响应后的重试会读到已 dismiss 状态。只读取消边界检查 |
| legacy import | C | chunk 级进度；chunk 内多项 create/update 后才存进度，没有每项 durable key；取消可绕过 except Exception 和 `_running=False` | 高，但**不是本 MCP flag 新增加的直连取消路径**：当前 Dashboard 上传用 `asyncio.create_task` 后台执行，MCP tools/list 无 import 工具。只读判断，未运行真实导入 |
| raw-evidence durable import | B（数据）/ C（取消后的运行标记） | run/item journal、操作 key、memory marker、lineage reconciliation；数据可按已有流程恢复。CancelledError 可绕过正常/Exception 清理，`_running` 留 true | 中：数据有 durable 恢复；存活进程的运行标记可能需重启才能原流程恢复。当前后台 Dashboard 路径不受 MCP session flag 直接控制。未改 resume |
| RM / asset metadata mutation | A / C | RM core metadata/update/delete 为同步调用；legacy metadata 提交后 await index refresh | RM 同步元数据路径较低取消风险；legacy 索引可能缺失/陈旧，中、可原 reindex 重建。无 exactly-once 承诺 |
| RM / asset reindex | C（派生状态可重复重建，非 durable operation） | metadata/content-hash 判断可跳过已完成索引；逐项 provider await | 低至中，部分索引可重建。不能把它标作有完整 operation journal 的 B |
| browser asset upload / ingest | C（HTTP 协调） | stream 临时文件 → `to_thread` 持久化 → upload claim complete；取消 thread await 不停止 worker，finally 临时文件删除可能与 worker 生命周期交叠，claim completion 可能缺失 | 中至高，只读条件判断；同步存储有事务/去重，不代表整个 HTTP claim 原子。这是独立自定义 HTTP route，MCP stateless manager 不包裹它；不宣称该 flag 新引入此风险 |
| local RM asset import | A（同步单项 import） | adapter 的 IMPORTED / SKIPPED_IDEMPOTENT disposition 反映已有导入语义；不在本次 MCP tool task 中 | 不扩大为线上取消实测；保留现有路径，未做迁移、Data Repair 或 live RM mutation |

主要源码证据：`server.py::{archive_session,hold,grow,trace,_auto_link_related,_unlink_related,_apply_supersession,_execute_trace_delete,_execute_merge_operation,_execute_digest_operation,dismiss_note,api_import_upload,_rm_persist_remember_me_upload,rm_asset_upload_route,rm_asset_update_metadata}`；`bucket_manager.py::{create,update,delete,archive,apply_import_operation,_refresh_ordinary_embedding_best_effort}`；`embedding_engine.py::{get_embedding,delete_embedding}`；`import_memory.py::{start,start_raw_evidence,_process_chunks,_process_raw_evidence_chunks}`；`asset_store.py::{persist_upload,update_metadata,delete}`；`asset_embedding_index.py::{index_asset,reindex}`；`remember_me_core_adapter.py` / `remember_me_import_adapter.py`。

## G：archive_session 专项

真实临时桶和 SQLite，原 archive_session 实现未修改。

1. 第一次真实 archive_session 已完成 create + archive + letter；测试适配器在 handler 全部返回后、SDK 交付响应前加入 gate，随后关闭 TCP。该 gate 用于控制响应丢失，不是宣称生产 handler 在 archive 后原本有一个 await。
2. stateful 适配器不取消，stateless 适配器收到取消；两者首次 handler 写入均完整且 letter 均已存在，响应均无法交付。
3. 同样的 archive_session 参数通过第二独立 HTTP 请求重试。
4. 两种模式都得到两个不同 bucket ID，`session_<date>_01` / `_02`，两条均 archived、内容包含相同 summary。
5. 额外在 create 写正文后的 embedding await 取消 stateless：第一个桶仍 dynamic，重试第二个桶 archived。

**原有 retry duplicate 风险：** handler 按现存 session 数量起名、create 生成新 ID，没有请求级 idempotency key；即使 stateful 写成功但丢响应也会重复。stateless 不能阻止它。

**stateless 扩大的窗口：** 在真实 create 后、启用的 embedding provider await 中断连，原本 stateful 会继续完成的 embedding→archive→letter 后半段被截断；造成部分状态，并让客户端更可能在没有完成结果时重试。没有证明 stateless 增加并发、没有量化重复率，也没有把原有非幂等误称为新引入问题。

## H：上线阻断项

当前明确阻断：trace 主写后的双向 related 不完整；archive create 后未归档。grow/hold 的多步骤写进一步支持不能保证全部工具安全，但不需要依赖未实测路径就能作出不上线判定。

仅协议兼容不够：旧 session POST 不再 404，不等于写工具有请求幂等；GET 200 SSE 不等于保留 stateful 推送/重放语义；durable merge/digest 能恢复，也不覆盖其他工具。

保持 flag 默认 false。后续若做 S-3，必须由独立授权处理具体业务完整性/幂等策略及复测；本 Phase 没有实施这些修复。

## I / J：验证与 self-review

WSL Ubuntu，`/home/ting/.venvs/ombre/bin/python`（3.12.14），SDK `mcp==1.29.1`。pytest 使用 Linux 默认 `/tmp`，未设置 TMP/TEMP，未传 basetemp。显式匹配的 GIT_DIR/GIT_WORK_TREE 仅用于 WSL 正确识别 Windows 创建的独立 worktree，不修改 Git 配置。

Targeted：133 passed，覆盖新 S-2 transport/cancellation 测试及既有 auth、diagnostic、cursor、merge/digest、confirmation、archive topics/write safety。

Full pytest（最终完整重跑）：1966 passed，9 skipped，41 warnings，376.16 秒，退出码 0。首轮仅新增 cursor TTL 的浮点精确相等断言失败；已改为绝对 1e-7、相对 0 的容差，生产 TTL 未修改。

Targeted 命令：`python -m pytest tests/test_mcp_stateless_http.py tests/test_mcp_auth.py tests/test_mcp_session_diagnostics.py tests/test_breath_output_accounting.py tests/test_r791416_write_semantics.py tests/test_digest_tool.py tests/test_phase3_write_safety.py tests/test_archive_session_topics.py -q`（实际使用上面的指定 WSL interpreter）。最终全量：`/home/ting/.venvs/ombre/bin/python -m pytest -q`。

Self-review：生产 diff 仅共享 builder 与两入口调用；保留混合换行，未改业务函数、MCP schemas 或 middleware 实现。测试专用工具只存在 tests。鉴别真实调度点与纯同步 async 包装，修正 GET 实测预期，隔离 SSE 全局 shutdown；未把 HTTP 自定义后台流程归因于 MCP flag。只读 AST 比较确认，去掉新增 builder 并还原两个调用点后，两个入口的原 AST 与基线完全一致。git diff --check 通过；未发现未授权文件或业务改动。已发现的取消风险作为候选开关的已知上线阻断项保留。

## K / L：提交与状态

Local commit message：`feat: add opt-in MCP stateless HTTP candidate and cancellation validation (S-2)`。
Commit SHA 与最终 clean status 在任务交付中报告；本文件不嵌入自身 commit SHA。

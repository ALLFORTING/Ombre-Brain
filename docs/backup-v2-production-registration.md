> 2026-10-07：operator-status 增量已移植到 fresh-fetch main 的独立收尾 worktree，并适配当前 HTTP 安全模块。以下 2026-10-05 停点和测试记录保留为历史；当前实施、待填部署 SHA、报告与发布方案见 backup-v2-closeout.md。仍仅本地，未推送或部署。

# Backup v2：入口、版本来源与离线恢复

本 Phase 仅实施本地代码和聚焦验证。未配置正式服务、未 push、未部署、
未生成正式密钥、未触发 workflow，也未采集正式数据。正式备份尚未完成。

## 当前入口与默认关闭

保留 Dockerfile 的 python server.py 入口、基础镜像、依赖和
/app/buckets 卷路径。server 在启动后台线程和构建 HTTP app 之前，
向正在运行的模块注册 v2；使用 sys.modules[__name__]，不会再次 import server。
本入口注册以下四条采集路由及一条独立只读状态路由，不引入 /api/backup/export：

- POST /api/backup/v2/captures
- GET /api/backup/v2/captures/{request_id}
- GET /api/backup/v2/captures/{request_id}/bundle
- POST /api/backup/v2/captures/{request_id}/ack
- GET /api/backup/v2/operator-status/{request_id}

OMBRE_BACKUP_V2_ENABLED 未设置、为空或精确为 false 时保持原有懒初始化；
不读取版本文件，不创建备份 workspace，不创建 Controller，不注册 v2。
只有精确的 true 可启用；其他值拒绝启动。启用只支持 streamable-http、
单实例、单进程和单 Uvicorn worker；禁止 reload。WEB_CONCURRENCY 与
UVICORN_WORKERS 必须未设置、为空或为 1。backup_entry.py 未修改。

## 同一写入边界

启用时，桶目录创建、资产库、embedding、桶历史及关系库、dehydrator、
RM runtime 和资产 registry 的整个初始化都在默认协调器 writer_scope 内完成；
只有初始化全部成功后才发布 runtime components 并注册采集路由。
初始化失败不会发布半初始化组件，writer_scope 会退出。

Controller 使用运行模块 bucket_mgr.write_coordinator。
注册、请求处理和采集 preflight 都核对桶、关系库、资产库、embedding、
资产 embedding index、dehydrator、后台 decay/import 引用、启用的
RM Core Adapter，以及存在的迁移/Cutover state store。还核对现有
HTTP mutation 门禁使用的默认协调器。身份不同即拒绝采集。
重复注册不得悄悄替换已注册 Controller 或配置。

仍复用既有排空、冻结、SQLite WAL snapshot、取消/超时退出后解冻机制。
未修改 RM Core、schema、业务工具契约或自动定时策略。

## 可核验的实际部署版本

Dockerfile 接收构建参数 ZEABUR_GIT_COMMIT_SHA，仅接受 40 位小写十六进制 SHA。
构建命令写入镜像内 /app/.backup-v2-build.json，位于持久化卷之外：
source=zeabur-build、status=valid/missing/invalid 和合法的 commit 或 null。
非法输入原值不会写入镜像；不写入凭据。缺失/非法输入不阻止默认关闭模式启动。

runtime 的固定读取路径位于 backup_v2_runtime.py 旁；没有可手填的通用 SHA override。
合法镜像记录可以作为来源；Render 保留平台注入的 RENDER_GIT_COMMIT 兼容。
缺失记录且无可信 provider 来源时拒绝启用。非法记录拒绝启用，即使同时有 Render SHA。
Zeabur 服务不得用 RENDER_GIT_COMMIT 冒充缺失的构建记录。
若 runtime 也提供 ZEABUR_GIT_COMMIT_SHA，它只能与合法镜像记录一致。
多个来源不一致、非法 SHA、后续版本漂移都拒绝采集，不静默选择某一来源。

[Zeabur 官方说明 Git 信息变量仅在构建阶段出现](https://zeabur.com/docs/en-US/deploy/config/environment-variables)。

本地测试逐项执行 Dockerfile 中实际的注入命令，并通过 runtime reader 验证正常、
缺失、非法和冲突输入。未完成容器镜像构建或 Zeabur 部署验证。
后续仍须核对 Zeabur 是否实际把构建变量传给 Docker ARG，并在新部署中核对
镜像记录 SHA、面板 Running deployment、GitHub deployment SHA 三者一致。
不得将远端 main SHA 手工填写为“实际部署版本”；未传参时保持 v2 关闭。

## 启用配置（后续阶段）

除实际部署版本来源外，启用需要：

- OMBRE_BACKUP_V2_PUBLIC_KEY_B64：32 字节 X25519 公钥的 canonical base64。
- OMBRE_BACKUP_V2_RECIPIENT_FINGERPRINT：匹配公钥的 x25519-sha256 指纹。
- OMBRE_BACKUP_V2_REPOSITORY_ID、OMBRE_BACKUP_V2_REPOSITORY_OWNER_ID：批准仓库及 owner 的十进制 ID。
- OMBRE_BACKUP_V2_WORKSPACE_ROOT：绝对路径，不与配置中的 buckets_dir 相等、互相包含或通过链接越界。
- OMBRE_BACKUP_V2_FREEZE_TIMEOUT_SECONDS：1..600。
- OMBRE_BACKUP_V2_MAX_FREEZE_SECONDS：2..1800，且大于 freeze timeout。
- OMBRE_BACKUP_V2_MAX_SOURCE_BYTES、OMBRE_BACKUP_V2_MAX_BUNDLE_BYTES、
  OMBRE_BACKUP_V2_MINIMUM_FREE_BYTES：1..10737418240。
- OMBRE_BACKUP_V2_READY_TTL_SECONDS：1..86400。

source root 始终取自正常 server.config["buckets_dir"]。
启用前核对桶、历史、资产、RM 与迁移库均在该根中。workspace 不能位于该根。
私钥永不进入正式服务、GitHub Actions、环境变量、Git、日志或工件。
OIDC 保留 RS256、GitHub issuer/JWKS、固定 audience、批准仓库/owner ID、
main、workflow path、workflow_dispatch、run ID 和 run attempt 限制。
JWK client 为懒加载，启动时不联网；禁止 query/cookie/body token。
现有单实例限制不提供分布式锁。

## 加密 PEM 离线恢复

scripts/backup_v2_recovery.py 仅在本机终端通过 getpass 隐藏输入口令。
无安全终端时拒绝回退到回显输入；没有口令参数或口令环境变量。
只接受 encrypted PKCS8 PEM，私钥在内存解密，不导出明文私钥，
复用 verify_bundle 和 restore_bundle。

先在 D:\Codex\projects 下准备独立恢复 workspace：
python offline_backup_bundle.py prepare <new-workspace>。
将已绑定 run/artifact 且校验过的唯一加密包以不覆盖方式放入 workspace/bundles，
保留原 <32位bundle-id>.obbackup 文件名。以下命令只在后续明确授权恢复时运行：

~~~text
python scripts/backup_v2_recovery.py verify --workspace <workspace> --bundle <bundle-id>.obbackup --private-key <encrypted-pem>
python scripts/backup_v2_recovery.py restore --workspace <workspace> --bundle <bundle-id>.obbackup --private-key <encrypted-pem> --target <absolute-workspace>/restored/<new-32-lowercase-hex-id>
~~~

target 必须是 workspace/restored 下不存在的新目录，拒绝现有目录、链接、
相对路径及其他位置。底层 core 在完全认证后以 no-replace 方式发布；
失败不覆盖已有目录。verify 不发布恢复目录，但会使用并清理隔离临时解密文件。
“私钥在内存”不表示恢复明文数据从不写入临时磁盘。

## 本地验证环境与回退

OB 测试只在 Ubuntu WSL 使用 /home/ting/.venvs/ombre/bin/python（3.12.14）。
使用 constraints-py312-linux.txt 的锁定依赖；不设置 TMP/TEMP 或 --basetemp。
现有解释器中的 RM dev7 与仓库 pin 不符，因此在本 worktree 的 ignored
.venv/backup-v2-validation 中隔离安装 0.1.0，不改全局 venv。
release archive 按 requirements.txt 的 SHA256 验证；离线安装记录的来源为实际
下载的固定 release URL 和已验证 SHA，RM source 未修改。
执行时通过进程内 PYTHONPATH 选中该隔离包。所有数据和密钥均为合成测试材料。

未启用或部署时，只需保留本地分支即可，无正式服务回退操作。
后续若已经启用，回退应先将备份仓库 ARMED 改为 false，再将服务
OMBRE_BACKUP_V2_ENABLED 改为 false，并按另行批准的部署流程发布。
必要时回到本 Phase 前的已验证版本。不要删除密钥、加密包或正式卷数据。

## 2026-10-04 本地验证记录

最终仅运行 runtime、recovery、quiesced capture、offline bundle、key tool 五个相关测试文件：
220 passed，1 skipped（Windows ACL 专用测试），2 个既有依赖/合成 JWT warning。
覆盖初始化排空、实际 RM/后台桶写入、协调器分裂拒绝、WAL snapshot、
失败/取消/冻结超时后的解冻、加密 PEM 恢复及构建版本注入/冲突。
未重跑全套正式验收。两个来源库基线分别为
885807cf460bec47af09812a523677d9dbb33eba 与 8ad77d5c1301df849ed9b9e7126d5fdcece0200b。

### 生产目录策略与本地验证边界

启用后的生产注册将配置中的 buckets 根与 workspace 根绑定到同一个确切目录策略，
并记录目录的设备号与 inode；prepare、load、控制器预检和冻结采集共享此策略。
源码位于 /app 时，/app/buckets 与 /app/backup-v2-workspace 是允许的独立根。
生产路径、入口、卷和数据无需迁移。仍拒绝源码目录本身及其祖先、两根重叠、
符号链接／重解析路径、目录替换以及 workspace 内部越界。
离线 CLI 默认 repository 隔离保留，无通用跳过开关。
恢复验证继续使用另行准备的离线 workspace。

本补丁仅做本地合成数据验证，不代表正式业务验收。
部署前先审查本地提交，核对镜像构建来源及确切目录配置，并处理验证报告中的既有阻塞。
保持 backup-v2 disabled 与 GitHub ARMED=false；发布、启用和正式验收需另行批准。
本阶段不授权 push、部署、启用、Dispatch、正式采集或恢复。

本地验证记录（2026-10-04，基线 9fef5cbf41801a132688b7c289654967cc2baa60）：

- WSL Ubuntu，/home/ting/.venvs/ombre/bin/python 3.12.14；
  constraints-py312-linux.txt 中 53 个锁定版本均匹配。未修改环境或依赖。
- 相关测试文件：test_backup_v2_directory_policy、test_stage8h_g1d_backup_v2_runtime、
  test_stage8h_g1c_quiesced_capture、test_offline_backup_bundle、
  test_backup_v2_recovery、test_backup_v2_key_tool。
  结果为 241 passed、2 failed、1 skipped；跳过项需要 Windows ACL。
- 新增 23 项目录策略用例全部通过，包括合成 /app 布局下旧默认规则拒绝、
  生产 prepare → load → 实际冻结采集 → 离线验证加密包、SQLite 合成快照、
  默认离线隔离与拒绝边界。既有 disabled/lazy-startup 及写覆盖回归通过。
- 两个失败为 test_enabled_real_initialization_and_rm_share_boundary 与
  test_real_rm_and_background_writes_are_blocked_then_thaw，
  均报 remember_me_host_bootstrap_failed。
  从指定基线读取三个改动模块并在内存加载后复跑这两项，得到相同失败；
  未将其计为本补丁新回归，也未扩大范围修复 Remember-Me。
- 部署前仍需处理上述既有初始化阻塞、审查补丁并另行批准发布与正式验收。
  未读取正式数据或真实私钥，未执行任何云端变更。


## 2026-10-05：只读 operator-status（仅本地，待独立审查）

固定基线 7d5de85efdcc5313d7bd22f561c54c7c1e798789；开始时指定参考 clean，
使用 ALLFORTING 进程内凭据核对远端 main 为同一 SHA，没有切换全局账号。
独立 worktree 为 D:\Codex\projects\Ombre-Brain-backup-v2-operator-status-20261005，
分支 codex/backup-v2-operator-status-20261005。原参考和原脏 checkout 未修改。

### 认证、参数及错误

接口只提供 GET：

~~~text
/api/backup/v2/operator-status/{request_id}?original_run_id=...&original_run_attempt=...
~~~

专用 OMBRE_BACKUP_V2_STATUS_TOKEN 仅供该只读接口使用：随机 32 字节的
canonical、无 padding base64url（43 ASCII 字符）。本阶段没有生成或配置生产 token。
不 trim 配置，不回退到 MCP、dashboard 或 OIDC 凭据；query/cookie/body 不能认证。
原始 ASGI Authorization 必须恰好一条，包括不同大小写的重复头也拒绝。
Bearer scheme 大小写不敏感，必须后接一个 ASCII 空格和精确凭据；
合并头、逗号列表、空值和多余空白拒绝。合法候选及配置先解码并检查 canonical，
再对 SHA-256 固定长度摘要使用 secrets.compare_digest。凭据不写入响应、日志或异常。

整个接口共享进程内 token bucket，容量 5，每 2 秒补一个 token；所有尝试（包括
错误认证和非 GET）计入，使用 monotonic 和独立短锁。此锁先释放，再认证、再进入
业务锁序；不按 IP 分桶，不存凭据。建议查询间隔至少 5 秒。
HEAD 及其他方法不返回状态；通过已注册路由的专用 rejection handler，
认证后统一返回 405 method_not_allowed。FastMCP 注册仍仅声明 GET，
保留专用 handler 的行为已用真实 FastMCP app 验证。

路径 request_id 必须是 36 字符 canonical 小写 UUID，沿用现有 UUID 规则，不额外要求 v4。
query 解码后通过 multi_items() 检查：必须且只能有 original_run_id、original_run_attempt，
各一次，值精确匹配 [1-9][0-9]{0,19}。缺失、重复、未知、空值、前导零、符号、空白及
非 ASCII 数字拒绝；query 中 request_id 或 token 同样是未知参数。
原 run/attempt 必须取自原关联证据，不能用新 attempt 替代。

全部响应 Cache-Control: no-store；失败只返回单一稳定 status 字段：

| HTTP | status | 含义 |
| --- | --- | --- |
| 401 | unauthorized | 缺失、重复、格式错误或错误认证；WWW-Authenticate: Bearer |
| 400 | request_invalid | 路径或 query 不符 |
| 405 | method_not_allowed | 已认证但不是 GET |
| 429 | rate_limited | 整个接口共享限速；Retry-After 为向上取整秒数 |
| 503 | status_unavailable | token 未配置/非法、正式实例缺失、身份分裂、版本来源不可用/冲突 |
| 409 | snapshot_conflict | 协调器内部状态违反快照不变量 |
| 500 | internal_error | 未预期错误；不返回异常原文 |

### 一次组合内存快照

只查询已注册的正式 controller。查询不调用 _get_runtime_components，不解引用 lazy
proxy 初始化服务；只从已发布组件解析已知 proxy，同时保留显式 module override
的身份检查。首先核对 controller 和正式 DEFAULT_WRITE_COORDINATOR，锁外验证既有
provenance 并比较已注册 runtime commit。取得锁后再次核对已注册引用和协调器身份。

固定锁序为异步 _job_lock → _status_lock（RLock）→ coordinator._condition。
_jobs、job 字段、active request、delivery 集合及相关 ownership bookkeeping 均通过
共享状态锁读取/发布。协调器只读 operator_snapshot_scope 保持原 status() 契约不变。
所有同步锁内禁止 await、文件/网络操作和反向锁调用；时钟 callback、版本读取、
哈希、chmod、unlink 均在同步锁外完成，再短锁成组发布。
完成的大小/摘要/ready、失败的 orphan/failure/state/timestamp、ack/stale 状态成组发布。
没有新增历史存储、重建、清理或生产恢复入口。

HTTP 200 顶层严格为：schema_version=1、status=ok、runtime_commit、observed_at
（UTC RFC3339 微秒）、snapshot_consistent=true、controller_busy、job_lookup、job、
coordinator、original_job_lease_release=unknown。
controller_busy 精确定义为 active request 不为空或 delivery 集合不为空，覆盖 preflight、
worker 等待、失败清理以及下载/hash/release。job 及协调器有各自生命周期；
快照只证明观察时刻的一次组合内存读取，不证明文件完整或持续开放。

request_id、原 run、原 attempt 精确匹配时 job_lookup=matched，job 为现有 public()
13 字段对象。缺失或身份不符统一 job_lookup=not_found、job=null，仍返回当前正式
协调器快照，不泄漏实际 job 身份、包信息、bundle_name、路径、claims 或 lease capability。
新进程的旧 job 内存缺失同样使用该响应，不扫描 bucket/workspace 来补建任务。

coordinator 固定字段：state、active_writers、generation、lease_present、
freeze_started_at、freeze_deadline、freeze_reason。reason 仅为 encrypted_backup_capture、
other 或 null。open 必须无 lease 且 freeze 字段为空；draining 允许无 lease；
frozen 必须存在 lease 且 active_writers=0；writer/generation 为非负 integer。
lease_present 仅表示对象存在：过期对象仍可为 true，不校验、更新或释放它。
generation 仅是 writer 退出计数；freeze/release 不递增，不能证明历史释放或快照版本。
original_job_lease_release 永远 unknown。

### 本地验证与既有阻塞

仅在 Ubuntu WSL 的既有 /home/ting/.venvs/ombre/bin/python（3.12.14）执行下面三份文件；
没有安装/修改依赖，没有设置 TMP/TEMP 或 --basetemp。所有凭据、job、数据和包均为临时合成材料。

~~~text
cd /mnt/d/Codex/projects/Ombre-Brain-backup-v2-operator-status-20261005
/home/ting/.venvs/ombre/bin/python -m pytest -q tests/test_backup_v2_operator_status.py tests/test_stage8h_g1c_quiesced_capture.py tests/test_stage8h_g1d_backup_v2_runtime.py --tb=short --color=no
~~~

新增认证/参数/schema/权限/限速/零副作用测试；以少量 barrier 交错验证真实 helper
跨线程发布 ready/failed 的原子性、锁序、取消时等待 preflight worker、下载 busy、
ack/stale、draining 无 lease、过期对象仍存在、连续 freeze 共用 generation。
覆盖重启后的旧 job 缺失、等待 job lock 后正式实例替换拒绝、真实 FastMCP 第五路由、
原四条采集路由回归和既有注册写覆盖检查。

最终完整三文件结果：174 passed、2 failed、1 warning（10.26 秒，退出 1）。
两个失败为下面列出的基线初始化用例；没有将其改为 skip/xfail。
另以同样三文件明确排除这两个既有阻塞，验证功能子集：

~~~text
/home/ting/.venvs/ombre/bin/python -m pytest -q tests/test_backup_v2_operator_status.py tests/test_stage8h_g1c_quiesced_capture.py tests/test_stage8h_g1d_backup_v2_runtime.py -k 'not test_enabled_real_initialization_and_rm_share_boundary and not test_real_rm_and_background_writes_are_blocked_then_thaw' --tb=short --color=no
~~~

结果：174 passed、2 deselected、1 warning（10.00 秒，退出 0）。
warning 为既有合成 JWT HMAC key 长度提示。diff --check 与八文件范围核对通过。
提交前再次以 ALLFORTING 进程内凭据回查远端 main，仍与固定基线匹配。
实施前，在固定基线未改代码时单独运行以下两个用例仍得到 2 failed：

~~~text
/home/ting/.venvs/ombre/bin/python -m pytest -q tests/test_stage8h_g1d_backup_v2_runtime.py::test_enabled_real_initialization_and_rm_share_boundary tests/test_stage8h_g1d_backup_v2_runtime.py::test_real_rm_and_background_writes_are_blocked_then_thaw
~~~

两者均在开启 RM 的合成配置、_get_runtime_components → _bootstrap_remember_me_host
阶段报 remember_me_host_bootstrap_failed，未到采集或后台写入断言。
本次进一步核验：安装版 0.1.0.dev7 与仓库 pin 0.1.0 不符；
validate_remember_me_contract 报 remember_me_contract_mismatch:package_version。
失败后 runtime components 未发布，协调器 open、active_writers=0。
只读状态接口通过不能代表生产初始化健康；未顺带修复适配器或更改环境。

### 后续上线和继续采集门槛（未执行）

独立审查后仍需另行授权发布。上线前回查 ARMED 并保持 false；核对原关联证据。
从平台确定旧 deployment/实例，确认旧 server 及相关执行单元退出且新旧实例不重叠。
源码采集使用 to_thread，没有启动采集子进程；若部署 wrapper 有后代，核对确切进程树。
在已确认 workspace 内仅查固定 marker、目录/链接边界及 temp/bundles 顶层元数据；
发现未归属残留则停止自动复用，另行批准保留隔离或选择独立合法 workspace，不自动删除。
核对新 Running deployment、构建 provenance 和接口 runtime commit，以及单实例/进程/worker。
部署后旧 job 缺失可以返回 not_found/null；历史释放仍 unknown。
继续采集须同时满足旧实例已退出、workspace 可用或已获批隔离替换、版本/目录/指纹/OIDC 正确、
当前 open/no lease/freeze 字段为空/controller_busy=false、正式初始化健康，并获婷另行授权。
使用新 request ID 和实际新 run/attempt，不自动重发旧 POST；新 POST 仍执行现有前置检查。

本阶段 ARMED 未回查；原任务释放始终 unknown。没有 push、生产配置、部署、重启、
Dispatch/rerun、正式采集、真实密钥读取或子代理；未触碰旧日雪及隔离服务。

## 2026-10-05：环境核正及 CORS 预检契约修复（仅本地）

独立复审基线为 05ae1cf69c991f5818a616972b06a588883dd889，parent 为
7d5de85efdcc5313d7bd22f561c54c7c1e798789；复审及修复开始时 worktree clean，
上一提交恰好修改指定八文件。本次只修改 server.py、本测试文件及本文档。

### 匹配仓库 pin 的 WSL 测试来源

原 /home/ting/.venvs/ombre/bin/python 为 Linux Python 3.12.14，但其原生安装 RM
为 0.1.0.dev7，来源 file:///mnt/d/Codex/_wheels/remember-me.tar.gz，不匹配正式 pin。
此前已修复的 Windows 备份恢复环境 OB-Backup-Recovery-20261004/recovery-venv
是另一环境；其 pywin32 初始化修复不改变这个 WSL 安装。

本次继续使用此前独立复审已核验的现有正式 RM target 安装：
/mnt/d/Codex/projects/Ombre-Brain-backup-v2-directory-fix-20261004/.venv/backup-v2-validation/site-packages。
由 PYTHONPATH 选择它，原 venv、仓库依赖声明及生产环境均未修改。
版本 0.1.0、release tag v0.1.0，仓库 pin 的来源为：

~~~text
https://github.com/peanutsuee/Remember-Me/releases/download/v0.1.0/remember_me-0.1.0.tar.gz
SHA256: 93d1514f940bde00a43b34b61681fe7f64da130313840f247869157d6e250485
source commit: d50b27074f194f299813789d3874d7a5fc83bda4
source tree: 0974890e5b77a6ec77813bc6d3e75bd31a15906c
~~~

此前复审现场已核验归档 SHA256、安装包 44 个文件与归档逐字节一致、
constraints-py312-linux.txt 的 53 项版本匹配，以及完整 RM 契约通过。
既有 installation-evidence.json 保留实际本地归档安装 transport 和上游来源证据；
不把仅修改来源元数据视作安装来源证明。本次再次确认解释器、实际 import 路径、
安装版 0.1.0、direct_url 的上述来源/散列和 validate_remember_me_contract 全部通过。

此前在该匹配 pin 的环境不 deselect 重跑三文件，结果为
176 passed、2 warnings（30.44 秒，退出 0）。
两个真实 RM 初始化用例 test_enabled_real_initialization_and_rm_share_boundary、
test_real_rm_and_background_writes_are_blocked_then_thaw 均通过；
remember_me_host_bootstrap_failed 在这个正式版本环境未复现。
上一节 dev7 环境中的失败保留为历史验证记录，不再作为匹配 pin 环境的当前阻塞。
这些是本地合成配置下的初始化证据，不能说明生产初始化健康。

### CORS 前的路径专用入口

原生产入口调用顺序为 MCP auth、CORS、diagnostic，Starlette 最后添加者先运行，
因此实际为 diagnostic → CORS → MCP auth → route。
全局 CORS 会提前响应允许 origin 的 OPTIONS 预检，绕过路由内部认证和限速。

现在 add_http_cors_middleware 在 CORS 外加入仅匹配
/api/backup/v2/operator-status/{单一路径段} 的 ASGI 入口保护。
生产共用 add_http_transport_middleware 装配函数，执行顺序为
 diagnostic → operator-status entry → CORS → MCP auth → route。
其他路径继续使用原 CORS 行为，没有扩成全站认证。
入口先调用原专用认证/共享限速，再对已认证非 GET 返回
405 method_not_allowed/no-store；OPTIONS/HEAD/其他方法均不读取状态数据。
认证成功在当前 ASGI scope 内保存函数身份标记；路由再次调用认证时复用这次成功，
每个请求只扣一次。标记不能从请求头/query/cookie 注入；裸路由仍执行原认证。
原始重复 Authorization 头拒绝、canonical 凭据及稳定错误码规则保持不变。

新增测试直接使用真实 FastMCP app 和 __main__ 共用的生产装配函数，覆盖允许 origin
未认证预检、五次拒绝后共享限速耗尽、已认证 OPTIONS/HEAD/POST、五次正常 GET
只各扣一次、重复原始 Authorization 头，以及桶耗尽时其他接口 CORS 仍正常。

本次完整命令（同此前环境核正重跑命令）：

~~~bash
cd /mnt/d/Codex/projects/Ombre-Brain-backup-v2-operator-status-20261005
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=/mnt/d/Codex/projects/Ombre-Brain-backup-v2-directory-fix-20261004/.venv/backup-v2-validation/site-packages /home/ting/.venvs/ombre/bin/python -m pytest -q -p no:cacheprovider tests/test_backup_v2_operator_status.py tests/test_stage8h_g1c_quiesced_capture.py tests/test_stage8h_g1d_backup_v2_runtime.py --tb=short --color=no
~~~

结果：182 passed、2 warnings（29.61 秒，退出 0），无 deselect，未扩大全量。
两项初始化测试均通过；warning 为合成 JWT HMAC 长度和正式 RM 的 Pillow API 弃用提示。
没有设置 TMP/TEMP 或 --basetemp；字节码及 pytest 缓存关闭。
最终 diff --check 和指定八文件范围核对通过，本次实际仅三个文件变更，创建新本地提交。

ARMED 未回查，原任务历史释放仍 unknown。生产初始化健康、部署版本、旧实例退出、
workspace 状态及生产接口行为均未取得新证据，后续上线门槛仍按上一节执行。
没有 push、生产配置、部署、重启、Dispatch/rerun、正式采集、真实密钥读取或子代理。
修复完成后停止，待聚焦复审。

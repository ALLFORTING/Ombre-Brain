# Backup v2：入口、版本来源与离线恢复

本 Phase 仅实施本地代码和聚焦验证。未配置正式服务、未 push、未部署、
未生成正式密钥、未触发 workflow，也未采集正式数据。正式备份尚未完成。

## 当前入口与默认关闭

保留 Dockerfile 的 python server.py 入口、基础镜像、依赖和
/app/buckets 卷路径。server 在启动后台线程和构建 HTTP app 之前，
向正在运行的模块注册 v2；使用 sys.modules[__name__]，不会再次 import server。
本入口只新增以下四条 v2 路由，不引入 /api/backup/export：

- POST /api/backup/v2/captures
- GET /api/backup/v2/captures/{request_id}
- GET /api/backup/v2/captures/{request_id}/bundle
- POST /api/backup/v2/captures/{request_id}/ack

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

# OB 备份收尾：2026-10-07 本地实施与发布准备

## 本地基线与范围

fresh fetch 的 OB main 为 69deb270d694776887a000f39ca4a0fbae6e3bc9。
独立 worktree：D:\Codex\projects\Ombre-Brain-backup-closeout-20261007。
分支：codex/backup-closeout-20261007。原脏 checkout 和旧状态 worktree 未修改。
复用 05ae1cf69c991f5818a616972b06a588883dd889、9d2ce0d90c93e562560ec9374b333b9b2d1978b7 的备份增量。
当前 HTTP 安全模块拆分、CORS、stateless 构建器和现有业务实现保持；只添加状态入口保护。
客户端固定为已发布 a78711f63609552fddb18f2d7128194476a9aff8，不修改客户端源码或 workflow。

本 Phase 只本地实施、合成测试、提交。ARMED 只读回查 false，不改云端。
没有 push、部署、正式采集、真实私钥读取或外部介质访问。
历史 original_job_lease_release 永远 unknown，不重建旧 job 或追补旧释放证明。

## 本地入口与待填门槛

可提交源码位于 scripts/backup_closeout/；其中 Later-Steps.ps1、local_prepare.py
同步到 D:\Codex\projects\OB-Backup-Recovery-20261004 下的同名入口。
准备目录并非 Git 仓库；仓库中的副本是审查与版本留档来源。
新增 backup-closeout-plan.json 与仓库 template 对应，密钥和旧下载/恢复收据未覆盖。
提交后仅本地计划的 ob_source_commit 填本次实现 SHA；final_deployment_sha
始终保留 PENDING_FINAL_DEPLOYMENT_SHA，直到未来正式镜像与接口核验。
不能把 7d5de85、69deb270 或本地候选提交冒充实际部署 SHA。
部署核验后 final_deployment_sha 必须等于实际使用的 clean OB source HEAD。
若未来需要重新整合当前 main，重新审查 SHA 和本地源码绑定。

session_name 是模板中预生成的 closeout-<32hex>，本轮不创建该会话目录。
Dispatch 创建新会话并记录 preflight/intent，旧 session 存在则拒绝 Dispatch；
download、每次显式 ResumeDownload 的新目录、recovery 及报告均归本次会话。
任何待填 SHA 在 Git/HTTP/密钥/目录写入之前失败。
入口不设置 ARMED、EXPECTED_COMMIT 或其他云端变量；未来 Dispatch 前只读核对
client main、EXPECTED_COMMIT、ARMED=true（须另行批准）以及当前正式状态快照。
preflight 的占位 UUID/run=1/attempt=1 仅用于当前协调器查询，不作为旧任务绑定。

Dispatch 仅调用既有 helper 一次。失败不 Dispatch/rerun，也不自动恢复下载；
保留返回的 run-binding，从原 run/attempt 日志中精确提取唯一 request JSON。
request 不可得则报告 unknown，不能模糊匹配或另发 POST。
ResumeDownload 只允许原 run，发现 attempt 改变立即停止；既有 helper 仍要求 run success，
因此 ack 失败但存在 artifact 时应先只读检查该 artifact，不能把新采集当作补救。
Status 从本会话 request.json 读取精确身份，用进程内专用状态凭据查询并保存新快照。
若原 request 证据不可得，显式标记 unknown_query_placeholder 只查询当前正式协调器，不能宣称查询到了原任务。
凭据不放 URL、计划、收据、命令参数或日志；原 OIDC 写授权策略保持。

## 独立保存与恢复报告

independent_bundle_path=PENDING_INDEPENDENT_BUNDLE_PATH、independent_medium_confirmed=false。
这些项目在未来新包保存阶段由婷明确指定准确路径及授权后才填写；本轮不访问介质。
StageRecovery 比对已下载包及独立副本的 SHA256/大小，Windows 拒绝同盘副本。
从核验过的独立副本建立全新恢复 workspace，留存 independent-save-receipt、
recovery-binding，绑定 request/run/attempt/artifact/ZIP digest/client commit/runtime commit/bundle digest。
任何复制或收据失败停止并保留本次文件，禁止覆盖旧 workspace 或旧 receipt。

恢复 core 新增可选 report_name、association；无报告的旧调用行为继续可用。
报告仅在 GCM 认证、manifest、一一对应的文件大小/哈希和 SQLite 校验全通过后构建。
报告目录为 recovery/reports/<bundle-id>.verify 或 <bundle-id>.restore，包含：

- manifest.json：认证后完整 manifest，包括条目、排除清单、来源身份和 commit。
- verification.json：实际逐文件 SHA256/大小、SQLite quick_check、页大小/页数、user_version、schema hash；按既有类别列覆盖路径与排除原因。
- association.json：经过包 ID、runtime SHA、包大小/哈希核对的关联收据；历史 lease 释放 unknown。

core 的 verification.json 只声明 validated，complete_acceptance=false：
它不能单独证明独立保管。只有本地入口已核验独立副本、成功读取全部报告并保存
restore-receipt.json 后，才能输出完整验收成功。Verify receipt 不声明完整验收。
完整验收仅指本次包取回、独立保存及隔离恢复；不声明业务运行、正式卷恢复或历史 lease 释放。
覆盖以认证 manifest 为依据，不新增权威数据类别、不要求不存在的数据必然存在。

报告先在 workspace/temp 中生成，再无替换发布；已有报告/恢复目标在解密前拒绝。
报告写入失败不发布恢复目录；恢复已发布后若报告发布失败，完整恢复目录保留但操作失败，
不得重跑恢复覆盖该目录，也不得报告完整成功。完成收据保存失败同样失败关闭。

## 必要测试与结果

环境：WSL Ubuntu，/home/ting/.venvs/ombre/bin/python 3.12.14。
使用已有正式 Remember-Me 0.1.0 target（与旧复审相同），没有安装或修改依赖。
不设置 TMP/TEMP，不用 --basetemp；临时数据使用 Linux 默认 /tmp。

```sh
cd /mnt/d/Codex/projects/Ombre-Brain-backup-closeout-20261007
export PYTHONDONTWRITEBYTECODE=1
export PYTHONPATH=/mnt/d/Codex/projects/Ombre-Brain-backup-v2-directory-fix-20261004/.venv/backup-v2-validation/site-packages
/home/ting/.venvs/ombre/bin/python -m pytest -q -p no:cacheprovider tests/test_backup_v2_operator_status.py tests/test_stage8h_g1c_quiesced_capture.py tests/test_stage8h_g1d_backup_v2_runtime.py tests/test_backup_v2_recovery.py tests/test_backup_closeout.py --tb=short --color=no
/home/ting/.venvs/ombre/bin/python -B maintenance_write_coverage.py
```

聚焦测试：200 passed、2 warnings、29.36s，退出 0，无 deselect/skip。
警告为原 JWT 合成短 HMAC key 与 Pillow getdata 弃用提示。
写覆盖审计：Write coverage audit passed，退出 0；三文件中也包含指定写覆盖回归。
首轮 9 failed/190 passed 由新增 fixture 错用 CaptureResult 字段和路径字符串 replace 扫描造成；
修正后相同范围全通过，没有删减用例。
最终边界/报告兼容性修改后，复跑新增收尾、既有恢复和指定写覆盖回归：20 passed，5.35s，退出 0。
包含新增的完成收据 fsync 失败测试；认证 manifest 留档保持原 canonical bytes，能再次直接通过 manifest validator。

客户端旧两份聚焦 unittest 在同一 WSL 环境运行：test_backup_v2_client.py 39 项、
test_backup_v2_server_contract.py 15 项；OB_BACKUP_V2_REFERENCE_REPO 指向固定旧参考
Ombre-Brain-backup-v2-directory-fix-20261004，保持既有契约测试 pin。
PowerShell tests/test_backup_closeout.ps1：7 组断言通过，无真实 GitHub/HTTP/Dispatch/密钥操作。
原客户端 tests/test_backup_v2_first_backup.ps1：24 offline assertions passed。
新增测试复用合成源与即时测试密钥，覆盖实际数据库报告、损坏包、报告写入/发布失败、
关联 commit 不符、完成收据失败、已有目标保护、新会话独立副本→verify→restore→receipt。
状态测试使用当前真实 build_streamable_http_app，再组装生产 middleware，保留 stateless 行为。

## 下一 Phase 精确发布顺序（本轮不执行）

1. 独立审查本次提交。fresh fetch 再核对 OB main 仍为本次 parent、worktree clean、ARMED=false。
   若 main 漂移，先保留本实现，整合备份增量到新基线并复测受影响部分；不能 force push。
2. 确定维护窗口，记录旧 Running deployment/实例、镜像 build SHA、启动进程模式和 workspace
   顶层归属。以受控停机确保旧实例全部退出，不能把切流 success 当作无实例重叠。
3. 安全供应专用 OMBRE_BACKUP_V2_STATUS_TOKEN；维持单实例/单进程/单 worker、原公钥与限额。
   保留原 workspace 和正式卷；若归属不明，另行批准新的合法 workspace，不清理残留。
4. 获准后正常非 force 推送审查过的精确实现 SHA 到 OB main。记录新 deployment。
   发布前停在这些操作处；本轮任何脚本均未执行 push 或平台变更。
5. 核对 Running deployment、镜像 /app/.backup-v2-build.json、operator runtime_commit
   全部匹配实际发布 SHA；正式初始化健康、实例模式正确；无凭据拒绝、专用凭据成功/no-store。
   当前 snapshot_consistent=true、open、lease_present=false、freeze 三字段 null、controller_busy=false。
   active_writers 可以非负，不要求持续为零；历史释放仍 unknown。
6. 核验通过后，才填本地 final_deployment_sha 并更新云端 EXPECTED_COMMIT，ARMED 仍 false。
   独立介质字段继续 pending，直到新包保存阶段确定。另行批准一次采集时才短暂 ARMED=true。
7. 一次 Dispatch→确切 artifact 完整下载；成功或失败都解除 ARMED。失败只查询原 request 和
   当前快照，不自动重发。随后独立保存、核验副本、StageRecovery/Verify/Restore，报告完整留档。

本地测试不构成生产初始化、真实 OIDC、正式采集、独立介质保管或真实密钥恢复验收。
不涉及 OB 业务大改动、正式卷覆盖或旧日雪。

客户端精确命令与结果（既有固定参考，不修改参考目录）：

```sh
cd /mnt/d/Codex/projects/ob-backup-client-offline-20261005
OB_BACKUP_V2_REFERENCE_REPO=/mnt/d/Codex/projects/Ombre-Brain-backup-v2-directory-fix-20261004 /home/ting/.venvs/ombre/bin/python -m unittest discover -s tests -p 'test_backup_v2_client.py' -q
OB_BACKUP_V2_REFERENCE_REPO=/mnt/d/Codex/projects/Ombre-Brain-backup-v2-directory-fix-20261004 /home/ting/.venvs/ombre/bin/python -m unittest discover -s tests -p 'test_backup_v2_server_contract.py' -q
```

39 tests/0.729s/OK；15 tests/0.538s/OK，退出均 0，无 skip。
## 2026-10-07：独立审查后原 attempt 最小修复

基线为 250bd914cdef2fcf45ad7a6245806e26dd03e931，新增独立提交，不 amend。
只修改 Later-Steps.ps1、既有 PowerShell 收尾测试及本文档；服务器、客户端、
恢复 core 和 local_prepare.py 均保持原提交内容。

ResumeDownload 在调用 helper 前，从本会话原 download/run-binding.json、
已有 run-identity.json 和 request.json 取得确切原 run/attempt，并交叉核对。
原下载收据只有 run_id 时，必须另有一致的既存身份或 request 证据；
没有确切 attempt 就停止并报告 unknown，不默认 1，不用 API 当前 attempt 补建原身份。
API 只比较当前值是否仍与原证据一致；缺证据或变化时 helper 调用次数为零。
下载前取得的身份继续用于下载后比较，返回收据必须包含同一 run/attempt。

Save-RequestEvidence 在首次建档前检查原证据、下载收据、原 attempt 日志中的唯一
request 及既有 request 全部一致；日志读取前后各核对当前 attempt。
不一致不发布新的身份/request 文件，不覆盖已有记录。只有下载收据、workflow
commit、request 和最终 attempt 核对全部通过，才建立 download-selection.json。
失败保留本次材料，零自动重发，不进入恢复或声明成功；历史 lease 仍 unknown。
初次 Dispatch 失败后若仅有 run_id 而缺原 attempt，取证保持 unknown，不能采用当前
API 值来恢复下载。另有确切原证据时才允许以后显式 ResumeDownload。

必要验证使用 PowerShell 7.6.5：

```powershell
& D:\Codex\projects\Ombre-Brain-backup-closeout-20261007\tests\test_backup_closeout.ps1
& D:\Codex\projects\ob-backup-client-offline-20261005\tests\test_backup_v2_first_backup.ps1
```

结果：收尾 15 组断言通过，helper 24 offline assertions passed，退出均 0。
新增回归包括原 run42/attempt1 且无身份文件时 API attempt2 拒绝且零下载、
缺原 attempt 时 unknown/零下载、收据或 request 不一致、日志读取期间 rerun、
下载期间 attempt 变化，以及一致时成功且已有身份文件 SHA256 不变。
所有测试仅使用合成文件与桩，没有实际 GitHub、HTTP、Dispatch 或密钥操作。

尝试受影响 Python 回归：

```powershell
wsl -d Ubuntu -- /home/ting/.venvs/ombre/bin/python -B -m pytest -q -p no:cacheprovider tests/test_backup_closeout.py tests/test_backup_v2_recovery.py --tb=short --color=no
```

WSL 启动阶段返回 Wsl/Service/E_ACCESSDENIED，退出 1；pytest 未执行。
未改用 Windows Python 跑 OB 套件；此前 200/20 passed 仅保留为原提交的历史记录。
没有新增 Python 生产改动。git diff --check 通过。

准备目录 Later-Steps.ps1 同步仓库字节，local_prepare.py 继续核对一致；
本地 ob_source_commit 更新为新提交。final_deployment_sha 仍为
PENDING_FINAL_DEPLOYMENT_SHA，独立介质仍 pending，历史 lease unknown。
未推送、部署、采集或改云端配置；提交后停止待复审。

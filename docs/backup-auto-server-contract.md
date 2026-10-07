# OB 长期自动备份：服务端契约 v1

本阶段仅本地实施服务端。基线为 fresh fetch 核实的
`f74785dfab4e3a1a24133c4459d5995b1fff8113`。没有推送、部署、正式采集，
没有修改客户端 workflow、Release 保留、TG bridge、旧包/密钥/验收会话。
旧人工 v2 和加密恢复继续兼容；旧 `OMBRE_BACKUP_V2_ARMED=false` 不变。

## 启用条件

服务默认关闭自动入口；单进程、单 worker、`streamable-http`。
显式设置 `OMBRE_BACKUP_AUTO_ENABLED=true` 才注册，不需要每日人工 arming。
保持现有 stateless 默认。入口注册接入 `server.py` 和 `backup_entry.py`。

必需配置（前缀均为 `OMBRE_BACKUP_AUTO_`）：

| 后缀 | 约束 |
|---|---|
| `WORKSPACE_ROOT` | 专用绝对目录，不能与 source root 或 v2 workspace 重叠 |
| `FREEZE_TIMEOUT_SECONDS` | 整数 1–600 |
| `MAX_FREEZE_SECONDS` | 整数 2–1800，必须大于排空期限 |
| `MAX_SOURCE_BYTES` | 整数 1–10 GiB |
| `MAX_BUNDLE_BYTES` | 整数 1–10 GiB；客户端发布限制可要求更低上限 |
| `MINIMUM_FREE_BYTES` | 整数 1–10 GiB |
| `READY_TTL_SECONDS` | 整数 1–86400 |

实际运行 SHA 复用 `resolve_runtime_commit`：Zeabur 镜像构建元数据及可选
provider SHA 必须一致；Render 使用 provider-issued `RENDER_GIT_COMMIT`。
不接受客户端配置的固定旧 SHA、GitHub 最新 main 或 OIDC workflow SHA 代替运行 SHA。
无需公钥、私钥或 recipient fingerprint 配置。

首次 workspace 必须不存在，使用现有固定目录、随机 workspace ID/nonce 和安全路径策略创建，
再写自动入口专用归属标记。既有 workspace 必须同时有匹配的自动标记；普通恢复/v2
workspace 不可借用。相关 buckets、letters、notes 必须位于配置 source root；不扫描其他卷。

## OIDC 和运行所有权

Bearer 只在 Authorization header；沿用 RS256/JWKS、issuer、exp/iat/nbf 校验，
拒绝 query/body/cookie token。专用 audience 为 `ombre-brain-backup-auto-v1`。

精确绑定：

- repository `ALLFORTING/ob-backup`，repository ID `1266342286`。
- owner `ALLFORTING`，owner ID `281855397`，visibility `private`。
- ref `refs/heads/main`。
- workflow_ref `ALLFORTING/ob-backup/.github/workflows/backup-auto.yml@refs/heads/main`。
- event `schedule` 或 `workflow_dispatch`；不允许 environment 或 reusable workflow。
- run_id 和 run_attempt 必须为正整数字符串；每个请求与两者绑定。

Claim 含义参考 [GitHub OIDC reference](https://docs.github.com/en/actions/reference/security/oidc)。
旧 v2 token 不能使用自动入口，自动 token 不能使用旧 v2。

## HTTP 契约

以下请求均使用上述 OIDC，响应 `Cache-Control: no-store`。

| 方法和路径 | 契约 |
|---|---|
| `GET /api/backup/auto/v1/metadata` | 返回 `format`, `runtime_commit`, `run_id`, `run_attempt` |
| `POST /api/backup/auto/v1/captures` | JSON 仅 `request_id`（规范 UUID）, `expected_runtime_commit`（metadata SHA）；202 |
| `GET /api/backup/auto/v1/captures/{request_id}` | 所属 run/attempt 的状态 |
| `GET /api/backup/auto/v1/captures/{request_id}/bundle` | ready 时下载；同一请求只允许一个 active delivery |
| `POST /api/backup/auto/v1/captures/{request_id}/ack` | ready 且无下载占用时删除本请求包，转 consumed |

状态沿用 `accepted → draining → capturing → ready → consumed/stale`，错误转 `failed`。
明文状态字段使用 `bundle_size`、`bundle_sha256`，同时返回 `bundle_id`、`runtime_commit`、
`oidc_run_id`、`oidc_run_attempt`、时间、稳定 failure_code；不返回服务器路径。
下载 Content-Disposition 为 `<bundle_id>.obplain.tar`；Content-Length 与状态大小一致，
`X-Backup-Bundle-Id` 和 `X-Backup-SHA256` 供交叉校验。
共享下载器保留 `X-Backup-Recipient-Fingerprint: none` 兼容字段，它不要求密钥。

客户端每次先认证获取 metadata，然后绑定实际 SHA 创建请求。
相同 UUID/参数/run/attempt 幂等；更改任一参数拒绝；不同请求不能同时采集。
采集接收、预检、worker 开始和完成都核对实际运行 SHA；变动失败并释放。
run/attempt 不一致不能读状态、下载或 ack。

冻结排空、lease deadline、合作中止、worker join、finally 释放复用 v2。
HTTP 创建返回后后台 task 继续；断线/取消等待不取消采集。
ASGI 下载 finally 释放 delivery；下载未完成可重新下载，不能算成功或 ack。
客户端不得把失败自动转成重新采集。失败包只能在已证明归属时清理。

## 明文格式及恢复

格式名 `ob-backup-plain-v1`；后缀 `.obplain.tar`，未压缩 tar，首项 `manifest.json`，
其余为严格排序的 `data/<relative_path>`。不借用旧加密 profile/version 来表示明文。
清单包含独立 format、schema/version、bundle/workspace 身份、UTC 时间、实际 OB SHA、
Remember-Me 版本、源身份摘要、逐文件大小/SHA256、SQLite schema/page 元数据、排除清单，
以及自身 canonical JSON 摘要和 `reconciliation`。

一致性采集复用同一冻结 staging、源前后稳定性检查、SQLite backup API 和容量限制。
从 staging 生成 bucket ID 集合、总数、sealed 数及每项内容/完整 metadata 摘要。
遍历所有 SQLite 快照中的 letters/notes 表，覆盖 sealed、未来 notes、投递等完整状态。
不初始化 BucketManager，不使用 aliases、touch、迁移或普通检索过滤。
报告只含 ID/路径、数量和摘要，不输出正文；清单摘要不是签名。

隔离恢复 API（导入 `offline_backup_bundle`）：

```python
restore_plain_bundle(workspace_path, bundle_name,
    expected_sha256=trusted_download_sha256,
    expected_size=trusted_download_size,
    restore_name=new_32_hex_id,
    maximum_bytes=client_limit)
```

包放在独立恢复 workspace 的 bundles 目录；信任的 size/hash 必须来自认证下载元数据。
先稳定复制包、校验外层大小/哈希，再校验清单、成员、安全路径、碰撞、逐文件哈希，
拒绝链接/特殊成员/稀疏文件，运行 SQLite quick_check/schema 对账。
恢复树重新生成 reconciliation 并严格比较，全部通过后 no-replace 发布到 restored 目录。
该树内 `.ob-plain-restore-receipt.json` 保存数量/摘要和匹配结果；失败不发布。
已有恢复目标不覆盖；正式卷绝不作为恢复目标。
旧 `.obbackup` 继续使用原 `restore_bundle` 和旧私钥，不改变旧材料。

## TTL 和重启

HTTP app lifespan 启动清理任务，间隔为 min(60 秒, TTL)。
只删除本进程 job 明确拥有、ready 已超过 TTL、无后台任务/active request/下载占用的包，
校验专用 workspace、安全 parent、bundle ID/格式和已记录大小/哈希后删除。
没有全 workspace 清空、基于文件 mtime 猜测过期或无差别 rmtree。
临时 staging 在创建它的 worker finally 内释放；worker 未退出之前不释放冻结。
进程重启后失去 job/task 归属证据，遗留包/temp 全部保留并仅计数报告，交独立审查；
不根据可伪造的名称、mtime 或普通 workspace marker 推断可删除。
shutdown 停清理任务并等待采集 worker 完成，强制终止进程不提供完成保证。

## 下一阶段消费顺序

metadata → create → poll ready → download 校验 → 私有 Release 上传及回读核验
→ 发布成功 receipt → ack → 保留算法。上传未完成不能标记成功。
客户端 workflow、Release 保留及 TG 失败/漏跑守护均留下一阶段，不在本提交内。

本地测试只使用合成数据和 Linux 默认 `/tmp`，指定 Python 3.12.14。
这些结果不代表部署成功、正式采集成功或真实生产恢复验收。

2026-10-07 本地限定验证：102 passed、64 deselected，9.72 秒。
新增服务端测试 33 项；受影响 v2 OIDC/运行 SHA/注册/冻结/释放/断线和写入覆盖回归
66 项；旧加密 round-trip 与历史格式兼容 3 项。一个既有负向 JWT HMAC 短密钥警告。
测试通过 `wsl -d Ubuntu` 交互 shell，使用 `/home/ting/.venvs/ombre/bin/python`，
默认 `/tmp`，未设置 TMP/TEMP 或 basetemp，未运行全量 suite。

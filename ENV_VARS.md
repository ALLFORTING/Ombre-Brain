# 环境变量参考

| 变量名 | 必填 | 默认值 | 说明 |
|--------|------|--------|------|
| `OMBRE_API_KEY` | 是 | — | Gemini / OpenAI-compatible API Key，用于脱水(dehydration)和向量嵌入 |
| `OMBRE_BASE_URL` | 否 | `https://generativelanguage.googleapis.com/v1beta/openai/` | API Base URL（可替换为代理或兼容接口） |
| `OMBRE_TRANSPORT` | 否 | `stdio` | MCP 传输模式：`stdio` / `sse` / `streamable-http` |
| `OMBRE_PORT` | 否 | `8000` | HTTP/SSE 模式监听端口（仅 `sse` / `streamable-http` 生效） |
| `OMBRE_AUTH_TOKEN` | 否 | 无 | HTTP MCP（`/mcp`、`/mcp/*`、SSE 的 `/sse` 与 `/messages`）的首选认证；只接受 `Authorization: Bearer <token>`，Bearer 未设置或不匹配时拒绝访问 |
| `OMBRE_MCP_ALLOW_QUERY_TOKEN` | 否 | 关闭 | URL-only MCP 客户端的显式 query-token 兼容开关；默认关闭。启用后 URL 凭据可能被客户端、代理、历史记录或访问日志保留 |
| `OMBRE_MCP_QUERY_TOKEN` | 否 | 无 | 与 `OMBRE_AUTH_TOKEN` 独立的专用 query token；仅在 `OMBRE_MCP_ALLOW_QUERY_TOKEN=true` 时用于 `?token=<dedicated-query-token>`，不会回退或复用 `OMBRE_AUTH_TOKEN` |
| `OMBRE_MCP_ALLOW_ANONYMOUS_HTTP` | 否 | 关闭 | 显式允许匿名 HTTP MCP；默认关闭，启用会输出强安全警告，仅适合刻意的本地/受控场景，绝不要用于公网部署 |
| `OMBRE_HTTP_ALLOWED_ORIGINS` | 否 | 空 | 逗号分隔的明确浏览器 origin；为空时不允许跨 origin 浏览器访问，不要使用 `*`。CORS 是浏览器策略，不是 MCP 认证 |
| `OMBRE_BUCKETS_DIR` | 否 | `./buckets` | 记忆桶文件存放目录（绑定 Docker Volume 时务必设置） |
| `OMBRE_RAW_EVIDENCE_ROOT` | 否 | 无 | O5B 明确启用后使用的 Raw Evidence 绝对路径；不得与 `OMBRE_BUCKETS_DIR` 或 Remember-Me 根目录重叠。默认关闭时不要求配置，也不会创建该目录。 |
| `OMBRE_RAW_EVIDENCE_RETENTION_DAYS` | 否 | `30` | O5D 显式 lifecycle invocation 使用的有限证据 retention 天数；仅允许 `1..365`，不改变已保存 revision deadline。 |
| `OMBRE_RAW_EVIDENCE_AUDIT_RETENTION_DAYS` | 否 | `365` | O5D metadata-only lifecycle audit 保留天数；仅允许 `30..3650`。 |
| `OMBRE_RAW_EVIDENCE_PURGE_BATCH_SIZE` | 否 | `100` | O5D 单次显式 lifecycle pass 的 bounded purge batch；仅允许 `1..1000`。 |
| `OMBRE_RAW_EVIDENCE_BACKUP_ROOT` | 否 | 无 | O5E 显式 operator backup/restore repository 的绝对路径；不得与代码仓库、live Raw Evidence、bucket 或 Remember-Me 根目录重叠。未配置时不创建、不要求 backup repository。 |
| `OMBRE_RAW_EVIDENCE_BACKUP_RETENTION_DAYS` | 否 | `7` | O5E bundle retention 起算于 immutable `created_at`；仅允许 `1..30`。无自动生产 scheduler，需显式 operator prune。 |
| `OMBRE_RAW_EVIDENCE_BACKUP_ROOT` | 否 | 无 | O5E 显式 operator backup/restore repository 的绝对路径；不得与代码仓库、live Raw Evidence、bucket 或 Remember-Me 根目录重叠。未配置时不创建、不要求 backup repository。 |
| `OMBRE_RAW_EVIDENCE_BACKUP_RETENTION_DAYS` | 否 | `7` | O5E bundle retention 起算于 immutable `created_at`；仅允许 `1..30`。无自动生产 scheduler，需显式 operator prune。 |
| `OMBRE_HOOK_URL` | 否 | — | Breath/Dream Webhook 推送地址（POST JSON），留空则不推送 |
| `OMBRE_HOOK_TOKEN` | 否 | — | `/breath-hook` 与 `/dream-hook` 的独立 Bearer token；未配置时端点返回 503。使用 `secrets.token_urlsafe(32)` 或等价安全随机源生成，不要提交到仓库 |
| `OMBRE_HOOK_SKIP` | 否 | `false` | 设为 `true`/`1`/`yes` 跳过 Webhook 推送（即使 `OMBRE_HOOK_URL` 已设置） |
| `OMBRE_DASHBOARD_PASSWORD` | 否 | — | 预设 Dashboard 访问密码；设置后覆盖文件存储的密码。auth store 损坏或不可读时可用于登录恢复，正常文件仍不能通过 Dashboard 修改密码 |
| `OMBRE_DIAG_TOOLS` | 否 | 关闭 | 仅供开发与验收临时启用 15 个 Stage 0 / 诊断 MCP 工具；只有 `1`、`true`、`yes`、`on`（忽略大小写和首尾空白）表示开启 |
| `OMBRE_RM_RUNTIME_ENABLED` | 否 | 关闭 | Stage 8F-A Remember-Me host runtime bootstrap 开关；仅 `1`、`true`、`yes`、`on`（忽略大小写和首尾空白）表示开启；当前仅供开发验收，禁止在当前生产 Render 中开启 |
| `OMBRE_RM_DATA_ROOT` | 否 | 无 | Stage 8F-A 启用时必须显式提供的 Remember-Me 绝对数据目录；关闭时完全忽略；不得隐式使用 `OMBRE_BUCKETS_DIR` 或现有 buckets_dir；当前仅供开发验收，禁止在当前生产 Render 中配置 |
| `OMBRE_DEHYDRATION_MODEL` | 否 | `deepseek-chat` | 脱水/打标/合并/拆分用的 LLM 模型名（覆盖 `dehydration.model`） |
| `OMBRE_DEHYDRATION_BASE_URL` | 否 | `https://api.deepseek.com/v1` | 脱水模型的 API Base URL（覆盖 `dehydration.base_url`） |
| `OMBRE_MODEL` | 否 | — | `OMBRE_DEHYDRATION_MODEL` 的别名（前者优先） |
| `OMBRE_EMBEDDING_MODEL` | 否 | `gemini-embedding-001` | 向量嵌入模型名（覆盖 `embedding.model`） |
| `OMBRE_EMBEDDING_BASE_URL` | 否 | — | 向量嵌入的 API Base URL（覆盖 `embedding.base_url`；留空则复用脱水配置） |
| `OMBRE_EMBEDDING_API_KEY` | 否 | — | 独立的向量 API key；设置后不会复用主 LLM key |

## HTTP MCP authentication

`OMBRE_AUTH_TOKEN` 是 HTTP MCP 的首选认证方式：客户端发送 `Authorization: Bearer <token>`。远程/网络 HTTP MCP 默认 fail-closed；未配置认证时拒绝访问。`OMBRE_TRANSPORT=stdio` 的本地 stdio 行为保持不变，Dashboard 认证也与 MCP 认证分开。

对于只能填写 URL、不能发送 Bearer 请求头的 MCP 客户端，可明确启用兼容模式：同时设置 `OMBRE_MCP_ALLOW_QUERY_TOKEN=true` 和独立的 `OMBRE_MCP_QUERY_TOKEN`，然后使用 `https://<host>/mcp?token=<dedicated-query-token>`。该 token 不会回退或复用 `OMBRE_AUTH_TOKEN`；flag 未开启、专用 token 未设置或 token 错误时仍拒绝访问。

Query credentials 可能被客户端、代理、浏览器历史或访问日志保留。只在确实需要 URL-only 客户端时启用，使用可独立轮换的专用 token；客户端支持 Bearer 时仍应优先使用 Bearer。该兼容模式可用于特定 URL-only custom connector，不代表所有 Claude 产品或 MCP 客户端都需要它。匿名 HTTP 仍由独立的 `OMBRE_MCP_ALLOW_ANONYMOUS_HTTP` 控制，默认关闭并不因 query-token 设置而自动开启。

## Dashboard auth store and first deployment

The Dashboard auth file is `{buckets_dir}/.dashboard_auth.json`. Its four states are:

- `missing`: the path is truly absent. This is the only state that permits setup.
- `valid`: a regular file containing a valid `password_hash`.
- `corrupt`: the file exists but is empty, invalid JSON, or has a missing or wrong-type `password_hash`.
- `unreadable`: the path is a symlink, dangling symlink, directory, non-regular node, or cannot be read.

`corrupt` and `unreadable` states fail closed: setup returns `503` and no startup token is generated. When `OMBRE_DASHBOARD_PASSWORD` is configured, it can be used to log in and then the existing `/auth/change-password` route can explicitly rebuild the auth file. Without the env password, recovery is limited to an operator repairing the file at the filesystem layer. The service never automatically rebuilds a corrupt or unreadable file.

During env-password recovery, the env password remains higher priority than the file password. The new file password is therefore not a login credential while `OMBRE_DASHBOARD_PASSWORD` remains set. After verifying recovery, delete or rotate the env password and restart the service. Leaving it configured permanently preserves an additional login channel.

For an existing `.dashboard_auth.json`, deployment operators must verify and tighten its permissions to `0600`. This is a manual check; the application does not claim to migrate permissions on every pre-existing file.

Clean first deployment requires the operator-configured `OMBRE_DASHBOARD_SETUP_TOKEN` in `X-Ombre-Setup-Token`, plus same-origin setup and a password without leading or trailing whitespace. The application does not generate or print a startup token. The configured token is consumed only after the auth file is published. `setup_completed_login_required` means the file was published but the setup request could not create a session; use normal login. `409` means another setup request won the create-if-absent race; do not overwrite the winner.

## 说明

- `OMBRE_API_KEY` 也可在 `config.yaml` 的 `dehydration.api_key` / `embedding.api_key` 中设置，但**强烈建议**通过环境变量传入，避免密钥写入文件。
- `OMBRE_DASHBOARD_PASSWORD` 设置后，valid auth store 的 Dashboard "修改密码"功能仍禁用（显示提示，建议直接修改环境变量）；仅 corrupt/unreadable auth store 允许用 env 密码登录后通过现有 `/auth/change-password` 显式恢复。未设置则密码存储在 `{buckets_dir}/.dashboard_auth.json`（SHA-256 + salt）。
- `OMBRE_HOOK_TOKEN` 只用于两个 HTTP hook 的 `Authorization: Bearer` 认证，与 `OMBRE_AUTH_TOKEN` 独立，不支持 query token 或匿名回退；未配置时 hook 返回 `503 hook_not_configured`。

- 默认 MCP 工具面只注册 22 个正式工具。15 个诊断工具仍保留在代码和测试中，只有显式设置 `OMBRE_DIAG_TOOLS` 为开启值时才注册；普通用户不应开启。

- `OMBRE_RM_RUNTIME_ENABLED` 默认关闭。关闭时 `OMBRE_RM_DATA_ROOT` 即使是非法值也不会被读取或验证。开启时 `OMBRE_RM_DATA_ROOT` 必须是绝对路径，例如 `C:\example\remember-me-data` 或 `/tmp/remember-me-data`；启动失败会 fail closed，不会回退到旧 AssetStore。

## Hook deployment order

For a live deployment, use this order: configure `OMBRE_HOOK_TOKEN`; update every real caller or trusted proxy to send `Authorization: Bearer <OMBRE_HOOK_TOKEN>`; then deploy the backend that enforces the token. After deployment, observe a complete caller cycle and confirm that breath/dream surfacing still succeeds. A caller must not be updated after the backend has already been made mandatory.

## Webhook 推送格式 (`OMBRE_HOOK_URL`)

设置 `OMBRE_HOOK_URL` 后，Ombre Brain 会在以下事件发生时**异步**（fire-and-forget，5 秒超时）`POST` JSON 到该 URL：

| 事件名 (`event`) | 触发时机 | `payload` 字段 |
|------------------|----------|----------------|
| `breath` | MCP 工具 `breath()` 返回时 | `mode` (`ok`/`empty`), `matches`, `chars` |
| `dream` | MCP 工具 `dream()` 返回时 | `recent`, `chars` |
| `breath_hook` | HTTP `GET /breath-hook` 命中（SessionStart 钩子） | `surfaced`, `chars` |
| `dream_hook` | HTTP `GET /dream-hook` 命中 | `surfaced`, `chars` |

请求体结构（JSON）：

```json
{
  "event": "breath",
  "timestamp": 1730000000.123,
  "payload": { "...": "..." }
}
```

Webhook 推送失败仅在服务日志中以 WARNING 级别记录，**不会影响 MCP 工具的正常返回**。

## Backup v2 production registration placeholders

Stage 8H-G1D-B adds a disabled-by-default registration path for the encrypted
backup-v2 capture API. These variables are documented as placeholders only.
This PR does not configure Render, GitHub secrets, GitHub variables, endpoint
values, production keys, or production paths.

| Variable | Required when enabled | Default | Description |
|----------|-----------------------|---------|-------------|
| `OMBRE_BACKUP_V2_ENABLED` | No | disabled | Backup-v2 remains disabled when unset, empty, or exact `false`. Exact lowercase `true` is required to enable. Any other non-empty value fails startup. |
| `OMBRE_BACKUP_V2_PUBLIC_KEY_B64` | Yes | none | Canonical base64 for the 32 raw X25519 recipient public-key bytes. Placeholder only; do not commit a production key. |
| `OMBRE_BACKUP_V2_RECIPIENT_FINGERPRINT` | Yes | none | `x25519-sha256:` plus 64 lowercase hex characters, matching the public key. Placeholder only; do not commit a production fingerprint. |
| `OMBRE_BACKUP_V2_REPOSITORY_ID` | Yes | none | Decimal GitHub repository ID for the approved backup transport repository. Placeholder only. |
| `OMBRE_BACKUP_V2_REPOSITORY_OWNER_ID` | Yes | none | Decimal GitHub owner ID for the approved backup transport owner. Placeholder only. |
| `OMBRE_BACKUP_V2_WORKSPACE_ROOT` | Yes | none | Absolute backup-v2 workspace directory outside the repository and outside the source bucket tree. Placeholder only. |
| `OMBRE_BACKUP_V2_FREEZE_TIMEOUT_SECONDS` | Yes | none | Positive integer from 1 through 600. Must be less than `OMBRE_BACKUP_V2_MAX_FREEZE_SECONDS`. |
| `OMBRE_BACKUP_V2_MAX_FREEZE_SECONDS` | Yes | none | Positive integer from 2 through 1800. |
| `OMBRE_BACKUP_V2_MAX_SOURCE_BYTES` | Yes | none | Positive integer up to 10737418240. |
| `OMBRE_BACKUP_V2_MAX_BUNDLE_BYTES` | Yes | none | Positive integer up to 10737418240. |
| `OMBRE_BACKUP_V2_MINIMUM_FREE_BYTES` | Yes | none | Positive integer up to 10737418240. |
| `OMBRE_BACKUP_V2_READY_TTL_SECONDS` | Yes | none | Positive integer from 1 through 86400. |
| `RENDER_GIT_COMMIT` | Yes | none | Runtime commit supplied by the production platform; exact 40-character lowercase hex. There is no separate production override. |

Numeric values reject whitespace, signs, booleans, decimals, zero, negatives,
overflow, scientific notation, and trailing junk. Invalid enabled configuration
aborts startup instead of silently falling back to disabled mode.

The backup-v2 source root is the existing `server.config["buckets_dir"]`; there
is no backup-v2 source-root environment override. Backup-v2 supports only
`streamable-http` with one Python process and one Uvicorn worker.

Offline key preparation must happen on a trusted local machine outside the
repository:

```powershell
python scripts/backup_v2_key_tool.py generate --output-dir <absolute-new-directory-outside-repo>
python scripts/backup_v2_key_tool.py verify-keyset --key-dir <absolute-key-directory>
python scripts/backup_v2_key_tool.py inspect-public --public-key <absolute-key-directory>\recipient-public-key.b64
```

The private key is encrypted PKCS#8 PEM and must never be placed in Render,
GitHub Actions, Git, logs, artifacts, job summaries, shell history, ChatGPT,
Codex, issues, pull requests, or environment exports. Only the public key and
fingerprint may later be copied into production after an explicitly approved
activation stage.

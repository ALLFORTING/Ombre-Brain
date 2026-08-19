# Ombre Brain

一个给 Claude 或其他 MCP 客户端使用的长期情绪记忆系统。基于 Russell 效价/唤醒度坐标打标，Obsidian 做存储层，MCP 接入，带遗忘曲线和向量语义检索。

A long-term emotional memory system for Claude or other MCP clients. Tags memories using Russell's valence/arousal coordinates, stores them as Obsidian-compatible Markdown, connects via MCP, with forgetting curve and vector semantic search.

> **⚠️ 备用链接 / Backup link**
> Gitea 备用地址（GitHub 访问有问题时用）：
> **https://git.p0lar1s.uk/P0lar1s/Ombre_Brain**
> This is an upstream/original-project backup link, not the canonical source for `ALLFORTING/Ombre-Brain`.
> 这是上游原项目的备用链接，不是 `ALLFORTING/Ombre-Brain` 的规范源码入口。

---

## 快速开始 / Quick Start（运行当前 ALLFORTING fork / Run the current fork）

> 主路径会 clone `ALLFORTING/Ombre-Brain` 当前源码，并使用仓库内的 `docker-compose.yml` 本地构建；不会拉取预构建镜像。
> This primary path clones the current `ALLFORTING/Ombre-Brain` source and builds it locally with `docker-compose.yml`; it does not use a pre-built image.
> 完全不会？没关系，往下看，一步一步跟着做。

### 第零步：装 Docker Desktop

1. 打开 [docker.com/products/docker-desktop](https://www.docker.com/products/docker-desktop/)
2. 下载对应你系统的版本（Mac / Windows / Linux）
3. 安装、打开，看到 Docker 图标在状态栏里就行了
4. **Windows 用户**：安装时会提示启用 WSL 2，点同意，重启电脑

### 第一步：打开终端

| 系统 | 怎么打开 |
|---|---|
| **Mac** | 按 `⌘ + 空格`，输入 `终端` 或 `Terminal`，回车 |
| **Windows** | 按 `Win + R`，输入 `cmd`，回车；或搜索「PowerShell」 |
| **Linux** | `Ctrl + Alt + T` |

打开后你会看到一个黑色/白色的窗口，可以输入命令。下面所有代码块里的内容，都是**复制粘贴到这个窗口里，然后按回车**。

### 第二步：获取当前 fork 代码 / Clone the current fork

```bash
git clone https://github.com/ALLFORTING/Ombre-Brain.git
cd Ombre-Brain
```

> 这会获取当前 `ALLFORTING/Ombre-Brain` 源码；要运行当前 fork，请使用这个仓库，而不是上游原项目。
> This checks out the current fork source; use this repository when you want the current fork rather than the upstream project.

### 第三步：获取 API Key（免费）

1. 打开 [aistudio.google.com/apikey](https://aistudio.google.com/apikey)
2. 用 Google 账号登录
3. 点击 **「Create API key」**
4. 复制生成的 key（一长串字母数字），待会要用

> 没有 Google 账号？也行，API Key 留空也能跑，只是脱水压缩效果差一点。

### 第四步：创建配置文件并启动当前 fork / Configure and start the current fork

**按你使用的终端执行 / Use the command for your terminal:**

macOS / Linux:

```bash
cp config.example.yaml config.yaml
```

Windows PowerShell:

```powershell
Copy-Item config.example.yaml config.yaml
```

Windows Command Prompt (`cmd`):

```bat
copy config.example.yaml config.yaml
```

```bash
# 创建 .env 文件——把 your-key-here 换成第三步拿到的 key
echo "OMBRE_API_KEY=your-key-here" > .env
```

```bash
# 本地构建当前 fork 并启动
docker compose up -d --build
```

### 第五步：验证

```bash
curl http://localhost:18001/health
```

看到类似这样的输出就是成功了：
```json
{"status":"ok","buckets":0,"decay_engine":"stopped"}
```

浏览器打开前端 Dashboard：**http://localhost:18001/dashboard**

> `docker-compose.yml` 默认把本地 `18001` 映射到容器的 `8000`；如果你改了端口映射，请使用对应的本地端口。
> `docker-compose.yml` maps local port `18001` to container port `8000` by default; use your mapped local port if you change it.

> **看到错误？** 检查 Docker Desktop 是否正在运行（状态栏有图标）。

### 第六步：接入 Claude

在 Claude Desktop 的配置文件里加上这段（Mac: `~/Library/Application Support/Claude/claude_desktop_config.json`）：

```json
{
  "mcpServers": {
    "ombre-brain": {
      "type": "streamable-http",
      "url": "http://localhost:18001/mcp"
    }
  }
}
```

重启 Claude Desktop，你应该能在工具列表里看到 `breath`、`hold`、`grow` 等工具了。

> 如果你为本地 HTTP 模式设置了 `OMBRE_AUTH_TOKEN`，URL 需要改为 `http://localhost:18001/mcp?token=<你的token>`。
> If you set `OMBRE_AUTH_TOKEN` for local HTTP mode, use `http://localhost:18001/mcp?token=<your-token>`.

> **想挂载 Obsidian？** 在 `.env` 中设置 `OMBRE_HOST_VAULT_DIR`，`docker-compose.yml` 会把它挂载到容器内的 `/data`，例如：
> ```dotenv
> OMBRE_HOST_VAULT_DIR=/Users/你的用户名/Documents/Obsidian Vault/Ombre Brain
> ```
> 然后运行 `docker compose down && docker compose up -d --build` 重启。
> **Want to mount Obsidian?** Set `OMBRE_HOST_VAULT_DIR` in `.env`; `docker-compose.yml` mounts it at `/data`.

> **后续更新当前 fork：**
> ```bash
> git pull origin main
> docker compose up -d --build
> ```
> For the upstream pre-built image path, see **Upstream pre-built image (not the current fork)** below.

---

## 从源码部署 / Deploy from Source（Docker）

> 适合想自己改代码、或者不想用预构建镜像的用户。

**前置条件：** 电脑上装了 [Docker Desktop](https://www.docker.com/products/docker-desktop/)，并且已经打开。

**第一步：拉取代码**

> 💡 The Gitea URL above is an upstream/original-project backup only; it is not a source for this fork.
> 💡 上面的 Gitea 地址仅是上游原项目备用地址，不是本 fork 的源码入口。

```bash
git clone https://github.com/ALLFORTING/Ombre-Brain.git
cd Ombre-Brain
```

**第二步：创建 `.env` 文件**

在项目目录下新建一个叫 `.env` 的文件（注意有个点），内容填：

```
OMBRE_API_KEY=你的API密钥
```

> **🔑 推荐免费方案：Google AI Studio**
> 1. 打开 [aistudio.google.com/apikey](https://aistudio.google.com/apikey)，登录 Google 账号
> 2. 点击「Create API key」生成一个 key
> 3. 把 key 填入 `.env` 文件的 `OMBRE_API_KEY=` 后面
> 4. 免费额度（请以官网实时信息为准）：
>    - **脱水/打标模型**（`gemini-2.5-flash-lite`）：免费层 30 req/min
>    - **向量化模型**（`gemini-embedding-001`）：免费层 1500 req/day，3072 维
> 5. 在 `config.yaml` 中 `dehydration.base_url` 设为 `https://generativelanguage.googleapis.com/v1beta/openai`
>
> 也支持 DeepSeek、Ollama、LM Studio、vLLM 等任意 OpenAI 兼容 API。
>
> **Recommended free option: Google AI Studio**
> 1. Go to [aistudio.google.com/apikey](https://aistudio.google.com/apikey) and create an API key
> 2. Free tier (as of 2025, check official site for current limits):
>    - Dehydration model (`gemini-2.5-flash-lite`): 30 req/min free
>    - Embedding model (`gemini-embedding-001`): 1500 req/day free, 3072 dims
> 3. Set `dehydration.base_url` to `https://generativelanguage.googleapis.com/v1beta/openai` in `config.yaml`
> Also supports DeepSeek, Ollama, LM Studio, vLLM, or any OpenAI-compatible API.

没有 API key 则脱水压缩和自动打标功能不可用（会报错），但记忆的读写和检索仍正常工作。如果暂时不用脱水功能，可以留空：

```
OMBRE_API_KEY=
```

**第三步：配置 `docker-compose.yml`（指向你的 Obsidian Vault）**

用文本编辑器打开 `docker-compose.yml`，找到这一行：

```yaml
- ./buckets:/data
```

改成你的 Obsidian Vault 里 `Ombre Brain` 文件夹的路径，例如：

```yaml
- /Users/你的用户名/Documents/Obsidian Vault/Ombre Brain:/data
```

> 不知道路径？在 Obsidian 里右键那个文件夹 → 「在访达中显示」，然后把地址栏的路径复制过来。
> 不想挂载 Obsidian 也行，保持 `./buckets:/data` 不动，数据会存在项目目录的 `buckets/` 文件夹里。

**第四步：启动**

```bash
docker compose up -d
```

等它跑完，看到 `Started` 就好了。

**验证是否正常运行：**

```bash
docker logs ombre-brain
```

看到 `Uvicorn running on http://0.0.0.0:8000` 说明成功了。

浏览器打开前端 Dashboard：**http://localhost:18001/dashboard**（`docker-compose.yml` 默认端口映射 `18001:8000`）

---

**接入 Claude.ai（远程访问）**

需要额外配置 Cloudflare Tunnel，把服务暴露到公网。参考下面「接入 Claude.ai (远程)」章节。

**接入 Claude Desktop（本地）**

不需要 Docker，直接用 Python 本地跑。参考下面「安装 / Setup」章节。

---

> **上游专用入口 / Upstream-only links:** These links target the upstream `P0luz/Ombre-Brain` distribution. They do not deploy or install the current `ALLFORTING/Ombre-Brain` fork; use the source instructions above for the fork.

[![Deploy upstream to Render](https://render.com/images/deploy-to-render-button.svg)](https://render.com/deploy?repo=https://github.com/P0luz/Ombre-Brain)
[![Deploy upstream on Zeabur](https://zeabur.com/button.svg)](https://zeabur.com/templates/OMBRE-BRAIN?referralCode=P0luz)
[![Upstream Docker Hub image](https://img.shields.io/docker/v/p0luz/ombre-brain?label=Docker%20Hub&logo=docker)](https://hub.docker.com/r/p0luz/ombre-brain)

---

## 它是什么 / What is this

Claude 没有跨对话记忆。每次对话结束，之前聊过的所有东西都会消失。

Ombre Brain 给了它一套持久记忆——不是那种冷冰冰的键值存储，而是带情感坐标的、会自然衰减的、像人类记忆一样会遗忘和浮现的系统。

Claude has no cross-conversation memory. Everything from a previous chat vanishes once it ends.

Ombre Brain gives it persistent memory — not cold key-value storage, but a system with emotional coordinates, natural decay, and forgetting/surfacing mechanics that loosely mimic how human memory works.

核心特点 / Key features:

- **情感坐标打标 / Emotional tagging**: 每条记忆用 Russell 环形情感模型的 valence（效价）和 arousal（唤醒度）两个连续维度标记。不是"开心/难过"这种离散标签。
  Each memory is tagged with two continuous dimensions from Russell's circumplex model: valence and arousal. Not discrete labels like "happy/sad".

- **双通道检索 / Dual-channel search**: 关键词模糊匹配 + 向量语义相似度并联检索。关键词通道用 rapidfuzz 做模糊匹配；语义通道用 embedding（默认 `gemini-embedding-001`，3072 维）计算 cosine similarity，能在"今天很累"这种没有精确关键词的查询里找到"身体不适"、"睡眠问题"等语义相关记忆。两个通道去重合并，token 预算截断。
  Keyword fuzzy matching + vector semantic similarity in parallel. Keyword channel uses rapidfuzz; semantic channel uses embeddings (default `gemini-embedding-001`, 3072 dims) with cosine similarity — finds semantically related memories even without exact keyword matches (e.g. "feeling tired" → "health issues", "sleep problems"). Results are deduplicated and truncated by token budget.

- **自然遗忘 / Natural forgetting**: 改进版艾宾浩斯遗忘曲线。不活跃的记忆自动衰减归档，高情绪强度的记忆衰减更慢。
  Modified Ebbinghaus forgetting curve. Inactive memories naturally decay and archive. High-arousal memories decay slower.

- **权重池浮现 / Weight pool surfacing**: 记忆不是被动检索的，它们会主动浮现——未解决的、情绪强烈的记忆权重更高，会在启动上下文或 `breath()` 中优先出现。
  Memories aren't just passively retrieved — unresolved, emotionally intense memories carry higher weight and may be prioritized by startup context or `breath()`.

- **记忆重构 / Memory reconstruction**: 检索时根据当前情绪状态微调记忆的 valence 展示值（±0.1），模拟人类"此刻的心情影响对过去的回忆"的认知偏差。
  During retrieval, memory valence display is subtly shifted (±0.1) based on current mood, simulating the human cognitive bias of "current mood colors past memories".

- **Obsidian 原生 / Obsidian-native**: 每个记忆桶就是一个 Markdown 文件，YAML frontmatter 存元数据。可以直接在 Obsidian 里浏览、编辑、搜索。自动注入 `[[双链]]`。
  Each memory bucket is a Markdown file with YAML frontmatter. Browse, edit, and search directly in Obsidian. Wikilinks are auto-injected.

- **API 脱水 + 缓存 / API dehydration + cache**: 脱水压缩和自动打标通过 LLM API（DeepSeek / Gemini 等）完成，结果缓存到本地 SQLite（`dehydration_cache.db`），相同内容不重复调用 API。向量检索不可用时降级到 fuzzy matching。
  Dehydration and auto-tagging are done via LLM API (DeepSeek / Gemini etc.), with results cached locally in SQLite (`dehydration_cache.db`) to avoid redundant API calls. Embedding search degrades to fuzzy matching when unavailable.

- **历史对话导入 / Conversation history import**: 将过去与 Claude / ChatGPT / DeepSeek 等的对话批量导入为记忆桶。支持 Claude JSON 导出、ChatGPT 导出、Markdown、纯文本等格式，分块处理带断点续传，通过 Dashboard「导入」Tab 操作。
  Batch-import past conversations (Claude / ChatGPT / DeepSeek etc.) as memory buckets. Supports Claude JSON export, ChatGPT export, Markdown, and plain text. Chunked processing with resume support, via the Dashboard "Import" tab.

- **可选原始证据层 / Opt-in Raw Evidence foundation**: 对明确选择的导入任务，在任何有损解码前保存隔离的原始输入，并提供完整性、来源链路、有限生命周期以及操作员备份/恢复基础设施；默认关闭，不影响普通记忆运行。
  For explicitly selected imports, the foundation can retain the original input before lossy decoding and provide isolated storage, integrity, lineage, bounded lifecycle, and operator backup/restore controls. It is off by default and is not required for ordinary memory operation.

## Raw Evidence / 原始证据层

Raw Evidence 是可选的来源保全层，不是第二套普通记忆，也不是公开的检索产品。它把“收到的来源输入”和“从来源中提取、脱水并写入的记忆”分开管理：只有在导入时明确启用 capture，系统才会在有损解码之前保存原始字节。普通启动、普通导入和升级都不会自动要求或开启证据存储；历史来源也不会因为升级或相似性匹配而自动补建证据或 provenance。

Raw Evidence is an optional source-preservation layer, not a second ordinary-memory store or a public retrieval product. It keeps source input separate from memories extracted, dehydrated, and written from that input. Capture must be explicitly enabled for an import, and the original bytes are saved before lossy decoding. Normal startup, ordinary imports, and upgrades do not require or enable evidence storage; legacy sources do not gain evidence or provenance through automatic matching or similarity inference.

| Foundation boundary / 基础边界 | Current behavior / 当前行为 |
|---|---|
| Default / 默认状态 | **OFF**. Normal operation does not require `OMBRE_RAW_EVIDENCE_ROOT`; no evidence filesystem is initialized merely because the service starts. |
| Storage and integrity / 存储与完整性 | Evidence uses an isolated, content-addressed store and registry with content hashes, bounded writes, and fail-closed integrity checks. It is separate from the Obsidian memory buckets. |
| Identity and provenance / 身份与 provenance | Captured imports have a run identity and retry-safe item identity. A newly explicit capture may link to derived memory when the import path establishes that relationship; no legacy automatic provenance or similarity-based relationship is fabricated. |
| Lifecycle / 生命周期 | Retention is finite and configurable; the current implementation default is 30 days. Lifecycle operations are explicit and bounded, not an automatically activated scheduler. |
| Audit / 审计 | Operational audit records are metadata-only; they do not make raw payload content part of ordinary logs, recall, or model context. |
| Backup and restore / 备份与恢复 | Raw Evidence backup is a separate encrypted operator workflow, with a current default backup retention of 7 days. Restore verifies the bundle and publishes only into a new target root; it never replaces the live root. |
| Access surface / 访问面 | Foundation v1 has no public evidence browser in Dashboard, general evidence export, evidence retrieval/search, or evidence MCP tool. Sealed evidence is not ordinary model context; any inspection must use an explicitly authorized internal/operator path. |

### Raw Evidence 与 `preserve_raw`

两者不是同一个保证。Raw Evidence 保存的是已明确 capture 的上传/导入原始输入，并带有 run、revision 和 lineage 记录；`preserve_raw` 是记忆导入阶段的当前实现语义：先提取，再保留提取出的 item 内容，并跳过后续 merge/dehydration。`preserve_raw` 不承诺保存用户上传的原始字节、不可变 transcript、精确 quote/span、message ID 或可审计 provenance。

They are different guarantees. Raw Evidence retains the explicitly captured upload/import input with run, revision, and lineage records. `preserve_raw` is an import-time implementation semantic: extract first, keep the extracted item content, and skip later merge/dehydration. `preserve_raw` does not promise the uploaded bytes, an immutable transcript, an exact quote/span, a message ID, or auditable provenance.

### Lifecycle、删除与隔离 / Lifecycle, deletion, and isolation

- Evidence retention is finite and bounded, not indefinite. The current default is 30 days; limits and purge work are bounded. This is not a legal-hold system, instantaneous physical deletion guarantee, or cryptographic-erasure guarantee.
- Evidence redaction first restricts access and records a tombstone, then permits controlled purge. Deleting evidence does **not** automatically delete derived memory. Deleting derived memory does **not** automatically delete supporting evidence. Deleting an upstream source/document is a separate review and action; no automatic cascade is promised.
- Raw Evidence never enters Breath, ordinary recall, embeddings, Dream, boot, or model context. It does not alter ordinary memory ranking or retrieval. The documented recall diagnostics describe supported traced query routes, not a claim that every Breath route is traced; see the [Memory Layer Contract v1](docs/OB_MEMORY_LAYER_CONTRACT_v1.md).
- RM remains the authority for its image/blob assets. Raw Evidence does not duplicate RM bytes or replace RM cutover and authority rules.

证据生命周期、普通记忆生命周期和 RM 生命周期彼此独立。升级不会自动启用生命周期；当前 Foundation 也没有 Dashboard 证据浏览、LLM 证据检索、全文/语义证据搜索或一般证据导出。O5E 备份不会自动初始化生产备份仓库，也不属于现有的普通明文 `/api/backup/export` memory snapshot 流程。

Evidence, ordinary memory, and RM have independent lifecycles. Upgrades do not activate evidence lifecycle processing. The Foundation also has no Dashboard evidence browser, LLM evidence retrieval, full-text/semantic evidence search, or general evidence export. O5E backup does not auto-initialize a production backup repository and is separate from the existing ordinary plaintext `/api/backup/export` memory-snapshot flow.

Configuration is documented in [ENV_VARS.md](ENV_VARS.md). The design and public boundary are documented in [Raw Evidence Design v1](docs/design/OB_RAW_EVIDENCE_DESIGN_v1.md) and the [Memory Layer Contract v1](docs/OB_MEMORY_LAYER_CONTRACT_v1.md). These documents describe the current Foundation boundary; they do not promise future Dashboard, search, export, retrieval, or automatic historical-provenance features.

## 边界说明 / Design boundaries

官方记忆功能已经在做身份层的事了——你是谁，你有什么偏好，你们的关系是什么。那一层交给它，Ombre Brain不打算造重复的轮子。

Ombre Brain 的边界是时间里发生的事，不是你是谁。它记住的是：你们聊过什么，经历了什么，哪些事情还悬在那里没有解决。两层配合用，才是完整的。

每次新对话，Claude 从零开始——但它能从 Ombre Brain 里找回跟你有关的一切。不是重建，是接续。

---

Official memory already handles the identity layer — who you are, what you prefer, what your relationship is. That layer belongs there. Ombre Brain isn't trying to duplicate it.

Ombre Brain's boundary is *what happened in time*, not *who you are*. It holds conversations, experiences, unresolved things. The two layers together are what make it feel complete.

Each new conversation starts fresh — but Claude can reach back through Ombre Brain and find everything that happened between you. Not a rebuild. A continuation.

## 架构 / Architecture

```
Claude ←→ MCP Protocol ←→ server.py
                              │
              ┌───────────────┼───────────────┐
              │               │               │
        bucket_manager   dehydrator     decay_engine
         (CRUD + 搜索)    (压缩 + 打标)   (遗忘曲线)
              │               │
        Obsidian Vault   embedding_engine
       (Markdown files)  (向量语义检索)
                              │
                         embeddings.db
                         (SQLite, 3072-dim)
```

### 检索架构 / Search Architecture

```
breath(query="今天很累")
         │
    ┌────┴────┐
    │         │
 Channel 1  Channel 2
 关键词匹配   向量语义
 (rapidfuzz)  (cosine similarity)
    │         │
    └────┬────┘
         │
    去重 + 合并
    token 预算截断
         │
    [语义关联] 标注 vector 来源
         │
    返回 ≤20 条结果
```

当前 MCP 表面包含 21 个默认工具、36 个诊断启用时的工具、1 个 resource、1 个可选 prompt 和 0 个 resource templates。支持 MCP prompts 的客户端可以调用 `start_ombre_brain` 获取 onboarding guidance；不支持 prompts 的客户端仍可直接使用相同工具。精确名称、暴露条件、受众、可变性和 input schema 以 [`docs/mcp-public-contract.json`](docs/mcp-public-contract.json) 为准；架构背景见 [`docs/mcp-surface-architecture-audit.md`](docs/mcp-surface-architecture-audit.md)。

The current MCP surface has 21 default tools, 36 tools when diagnostics are enabled, 1 resource, 1 optional prompt, and 0 resource templates. Clients that support MCP prompts may invoke `start_ombre_brain` for optional onboarding guidance; tool-only clients remain fully supported. The machine-readable manifest is the source of truth for names, exposure, audience, mutability, and input schemas. MCP clients may expose tools and resources differently; this README does not assume that a client provides prompt support or exposes chat-attachment bytes inside MCP request context.

### 默认 MCP 工具 / Default MCP tools

这些工具默认可见，但其中 `digest`、`related_backfill`、`rm_asset_reindex_embeddings` 和 `seal_letter` 面向受控维护；`trace` 也包含高影响和 destructive 模式。Default-visible does not mean ordinary end-user safe.

| 工具 Tool | 作用与边界 Purpose and boundary |
|-----------|-------------------------------|
| `boot` | 推荐的一次性启动上下文：钉选摘要、到期 trigger、信箱、feel 回声、最近 session、todos，并按 token 预算截断 / Recommended one-shot startup context; observing due triggers may update bounded seen metadata |
| `breath` | 检索或浮现记忆；`query` 用于定向检索。它是 retrieval-oriented，命中/排序可能更新 activation metadata / Retrieval or surfacing; targeted `query` is preferred when the topic is known, and bounded activation metadata may be updated |
| `hold` | 写入单条记忆或模型自己的 `feel` 反思 / Store one memory or a model `feel` reflection |
| `grow` | 将日记/长内容拆分并写入多个记忆桶 / Digest journal-style content into multiple memory buckets |
| `trace` | 混合修改工具：元数据、正文替换/追加、related、merge、seal 以及 `delete=True`。delete 是正常工具流程中的 destructive 操作；仓库可能保留写前快照供人工恢复，但没有 MCP undo/restore 命令 / Mixed mutation tool; delete is destructive in the normal workflow, while write-ahead history may support manual recovery without providing an MCP undo command |
| `pulse` | 系统状态和记忆桶列表；listing 可能更新 bounded dormant metadata / Status and bucket listing; listing may update bounded dormant metadata |
| `dream` | 可选的最近记忆反思/详情读取，不要求每次启动调用 / Optional recent-memory reflection/detail readout; not required on every startup |
| `seal_letter` | sealed-memory handoff-letter maintenance：改变一封 letter 的可见性，不是普通检索 / Changes handoff-letter visibility; not ordinary retrieval |
| `archive_session` | 归档一次会话摘要，可附 highlights/mood/VA 和 mailbox letter / Archive a session summary with optional handoff letter |
| `todos` | 汇总未 resolved 且非 sealed 桶中的 todos / Summarize todos from unresolved, non-sealed buckets |
| `related_backfill` | 维护/回填 semantic related；默认 `dry_run=True`，执行模式会写 relation 并跳过 sealed 桶 / Maintenance backfill; `dry_run=True` by default, execution writes links |
| `digest` | 维护/规划旧桶消化；默认 `dry_run=True`，确认执行可能写入消化结果并产生 provider/API 成本 / Controlled digestion maintenance; dry-run by default, confirmed execution may write results and incur provider/API cost |
| `rm_asset_upload_link` / `rm_asset_upload_status` | Remember-Me 持久 asset 上传生命周期：短期 signed link 与 metadata-only 状态查询 / Persistent asset upload lifecycle: short-lived signed link plus metadata-only status |
| `rm_asset_get` | 按 ID 读取持久 asset metadata，不返回 bytes / Read persistent asset metadata by ID; no file bytes |
| `rm_asset_update_metadata` | 更新 title/description/tags，不改变 asset bytes 或 hashes / Update metadata without changing asset bytes or hashes |
| `rm_asset_search` | 关键词、tag/filter 和可选 semantic search 的持久 asset 检索 / Persistent asset discovery through keyword, metadata, and optional semantic search |
| `rm_asset_view` | 面向用户显示已清理图片；`rm_asset_inspect` 才是模型视觉检查 / Display a cleaned image to the user; use `rm_asset_inspect` for model vision |
| `rm_asset_inspect` | 返回已清理图片的 MCP ImageContent，供模型实际理解，不改 metadata / Return cleaned image content for model inspection without changing metadata |
| `rm_asset_download_link` | 创建短期 signed download link，是显式导出动作 / Create a short-lived signed download link for explicit export |
| `rm_asset_reindex_embeddings` | Remember-Me maintenance/backfill：为缺失或过期 vector 做有界回填；不改 asset bytes 或 metadata / Bounded maintenance backfill for missing or stale vectors; asset bytes and metadata are unchanged |

### 诊断工具 / Diagnostic tools

另外 15 个工具仅在开发/验收时通过 `OMBRE_DIAG_TOOLS=1`（也接受 `true`、`yes`、`on`，忽略大小写和首尾空白）注册；普通用户应保持关闭。它们是 diagnostic/probe surface，不是普通 Remember-Me 持久工作流，也不应被当作正式上传路径。

The 15 diagnostic tools are hidden by default and are registered only when `OMBRE_DIAG_TOOLS` is enabled. They are for diagnostics, acceptance probes, and transport/vision experiments—not the ordinary persistent Remember-Me workflow:

`asset_attachment_context_probe`, `asset_browser_upload_link`, `asset_browser_upload_status`, `asset_export_probe`, `asset_ingest_abort`, `asset_ingest_begin`, `asset_ingest_chunk`, `asset_ingest_finish`, `asset_ingest_probe`, `asset_render_probe`, `asset_vision_challenge`, `asset_vision_download_link`, `asset_vision_export`, `asset_vision_upload_challenge`, `asset_vision_verify`.

### 工具参数速查 / Tool Parameter Quick Reference

#### `breath`

- `query: str = ""` — 关键词/语义检索；为空时进入浮现模式 / Keyword or semantic query; empty means surfacing mode.
- `mode: "summary" | "full" = "summary"` — 摘要或全文模式；两种模式都受 `max_results` 限制 / Summary or full mode; both obey `max_results`.
- `max_results: int = 5` — 非钉选搜索结果上限；有 query 时 sealed 默认整条剔除 / Limit returned search results; sealed buckets are fully hidden by default.
- `date_from/date_to: str = ""` — 按桶 `updated_at` 过滤，格式 `YYYY-MM-DD` / Filter by bucket `updated_at`, format `YYYY-MM-DD`.
- `recent_days: int = -1` — 最近 N 天过滤；可与 `domain="feel"` 组合 / Recent N-day filter; can combine with `domain="feel"`.
- `mailbox: bool = False`, `mailbox_limit: int = 1` — 返回信箱留言列表；默认最新一封 / Return mailbox letters; latest one by default.
- `resonance: str = ""` — 例如 `"0.2,0.7"`，按 valence/arousal 情绪距离排序；可与 `query` 组合 / Sort by emotional distance to `valence,arousal`; combines with `query`.
- `emotion_trend: bool = False` — 附带持久化情绪时间线 / Attach persisted emotion timeline.
- `feels: bool = False` — 专门检索 feel 桶，相当于 `domain="feel"` / Search feel buckets only, equivalent to `domain="feel"`.
- `include_dormant: bool = False` — 是否搜索自动沉底桶 / Include auto-dormant buckets.
- `include_sealed: bool = False` — 是否显示手动封存桶；默认不返回桶名、ID、摘要，也不计入隐藏数量 / Include manually sealed buckets; hidden by default including name, ID, summary, and counts.
- `tags_filter: list[str] | None = None` — 可选的桶标签精确过滤；列表内任一标签匹配即可，多个标签过滤与 `topic_filter` 之间按 AND 组合 / Optional exact bucket-tag filter; any listed tag may match, and it combines conjunctively with `topic_filter`.
- `topic_filter: list[str] | None = None` — 可选的归档会话主题精确过滤；列表内任一主题匹配即可。过滤会先于 query 排名，单独使用时按最新记录优先 / Optional exact archived-session topic filter; any listed topic may match. Structured filtering happens before query ranking, and filter-only calls return newest first.

#### `trace`

- `bucket_id` 支持逗号分隔批量 ID；批量模式忽略 `content` 和 `name`，不支持 `merge` / `bucket_id` accepts comma-separated IDs; batch mode ignores `content` and `name`, and cannot merge.
- `append: bool = False` — `content` 默认替换正文；`append=True` 时以空行分隔追加 / `content` replaces by default; `append=True` appends with a blank-line separator.
- 写前快照 / Write-ahead snapshots: replace、append、delete 前会写入 `bucket_history.sqlite3` 的 `bucket_history(bucket_id, old_content, changed_at, change_type)`，便于手工恢复 / Before replace, append, or delete, old content is stored in `bucket_history.sqlite3` for manual recovery.
- `merge: str = ""` — 将源桶并入当前目标桶：正文追加、tags 去重合并、importance 取最大、VA 取平均、删除源桶；不能与 `delete` 同用，源桶不能是 pinned/protected / Merge source into target: append content, union tags, max importance, average VA, delete source; cannot combine with `delete`; source cannot be pinned/protected.
- `sealed: int = -1` — `1` 手动封存、`0` 取消、`-1` 不改；sealed 优先级高于 pinned，默认不在 `breath`/`pulse`/`dream`/`todos` 泄漏 / `1` seal, `0` unseal, `-1` unchanged; sealed overrides pinned and is hidden by default.
- `dormant: int = -1` — `1` 手动沉底、`0` 恢复、`-1` 不改；`trace` 修改会自动解除 dormant，除非显式传 `dormant=1` / `1` dormant, `0` restore, `-1` unchanged; trace updates wake dormant buckets unless explicitly kept dormant.
- `related: str = ""` — 逗号分隔 related bucket IDs，写入双向关联 / Comma-separated related bucket IDs; links are bidirectional.
- `trigger_date: str = ""` — 前瞻记忆日期，格式 `YYYY-MM-DD`，到期后由 `boot` 的“今日浮现”显示 / Prospective memory date; due items appear in `boot`.

#### `archive_session`

`archive_session(summary, highlights="", mood="", valence=-1, arousal=-1, letter="", sealed=False, topics=None)` 会创建 `session_YYYY-MM-DD_序号` 归档桶，`domain=["session"]`。传入 `letter` 时，会额外写入独立信箱表 `letters`，下一次 `boot()` 自动带出最新一封。`topics` 是可选的结构化主题标签列表；有帮助时可提供大约 3–8 个适度范围的标签。

`archive_session(summary, highlights="", mood="", valence=-1, arousal=-1, letter="", sealed=False, topics=None)` creates a `session_YYYY-MM-DD_NN` archive bucket with `domain=["session"]`. When `letter` is provided, it is also stored in the independent `letters` mailbox table and surfaced by the next `boot()`. `topics` is an optional list of structured topic labels; when useful, provide roughly 3–8 moderately scoped labels such as `项目/OB`, `项目/RM`, `学习/生化`, `关系/沟通`, or `日常/作息`. Avoid labels that are too broad or excessively narrow.

#### `digest` 与 `related_backfill`

- `digest(dry_run=True, max_groups=10)` 默认只列出将被消化的候选，不改数据；正式执行依赖 `OMBRE_DIGEST_API_KEY` / `digest(dry_run=True, max_groups=10)` only lists candidates by default; real runs require `OMBRE_DIGEST_API_KEY`.
- `related_backfill(dry_run=True, limit=100, threshold=-1)` 默认只输出计划关联；`threshold=-1` 使用环境变量/默认阈值 / `related_backfill(...)` only plans links by default; `threshold=-1` uses env/default threshold.

#### `asset_ingest_probe`, `asset_render_probe`, and `asset_export_probe`

- `asset_ingest_probe(data_base64, expected_sha256="", mime_type="application/octet-stream")` is a Phase-0 upload probe for client-to-MCP base64 transport. It decodes, enforces the size limit, and returns SHA-256, hash_match, and MIME metadata without creating an asset ID or persisting user images.
- The single-call `asset_ingest_probe` and the `asset_ingest_begin` / `asset_ingest_chunk` / `asset_ingest_finish` flow remain diagnostic tools for small transport checks; model-relayed base64 is not the formal RM upload path.
- Formal RM capacity testing uses `asset_browser_upload_link(...)` so the user browser sends the original file directly as multipart/form-data. The raw file bytes do not pass through the model chat context.
- Browser upload links expire after 10 minutes, accept at most 2 MiB, and expose metadata-only results through `asset_browser_upload_status(upload_id)`.
- Phase-0 browser uploads are hashed while streaming and are not persisted. The model must not copy, reconstruct, or output a complete file base64 string in chat text.
- `asset_render_probe()` is a Phase-0 return-path probe for whether MCP `ImageContent` can enter the AI vision context. It reads the built-in `assets/probe.png` and returns a standalone `image/png` content block, not text-wrapped base64.
- `asset_export_probe()` is a Phase-0 user-visible attachment probe. The caller decodes `data_base64`, verifies `decoded_bytes` and `sha256`, then presents the result as a user-visible attachment.
- `asset_render_probe` and `asset_export_probe` are independently accepted paths: one checks model vision input, the other checks user-visible attachment export.
- Model-only visibility without UI visibility is not a complete pass. These probes do not mean formal image storage is live, and they do not persist user images.

#### Remember-Me Stage 1 asset storage

- Ombre-Brain pins public `peanutsuee/Remember-Me` package `0.1.0.dev7` at
  public source commit `a00ea991442d7581a3856b178525a8e77da833fe` and tree
  `a958d995421c97ccc572b127cb859797aa7a415f`. The dependency comes from the
  immutable public release's custom deterministic archive, not a
  GitHub-generated Source code archive. Stage 8F-J completes RM-enabled Core
  ownership for all nine `rm_asset_*` tools while the default-off path retains
  the existing legacy handlers, routes, Dashboard, Viewer, authentication, and
  Tickets. See
  [`docs/remember-me-integration.md`](docs/remember-me-integration.md).
- Stage 8G-B adds a local-only, single-asset Host import adapter over the public
  `RememberMeCore.import_asset()` contract. It preserves legacy image IDs,
  cleaned bytes, metadata, asset timestamps, and tag timestamps. It is not
  wired into server startup or MCP, does not enable the RM runtime, and never
  performs migration batches, Reindex, dual-write, shadow-write, or legacy
  deletion.
- Stage 8G-C adds only an explicitly constructed local Host migration core. Its
  independent `migration.sqlite3` provides a persistent write-freeze lease,
  legacy source generation, versioned checkpoint, and deterministic
  `asset_id` keyset pagination. Each runner invocation is a bounded batch and
  calls only the Stage 8G-B `LegacyAssetImportAdapter`. A legacy source change
  between batches, or an uncertain write-generation finalize, fails closed
  instead of silently continuing. Nothing is wired to server startup, MCP, the
  Dashboard, or production configuration; RM remains default-off, legacy
  remains the sole production image implementation, and no production image
  migration, Reindex, dual-write/shadow-write, or legacy deletion is
  performed.
- Stage 8G-D adds local-fixture migration acceptance on top of that bounded
  core: a capped run-to-completion coordinator, deterministic source/target
  reconciliation, structured reports, and read-only recovery diagnostics.
  Reconciliation uses the Adapter's trusted public-Core target view and reports
  unavailable tag timestamps, complete/unexpected/duplicate target inventory,
  target snapshot consistency, and blob-byte checks as unsupported rather than
  passed. That fixed unsupported set cannot be cleared by an Adapter
  declaration: capability declarations are limitations, not verification
  evidence. Stage 8G-D therefore produces `unsupported`, not `passed`, when all
  currently supported fields match. Recovery diagnostics classify such a
  report as `completed_partially_verified`; they do not produce
  `completed_verified`, and a constructed `passed` report requires manual
  review. Reconciliation uses a renewable short coordination freeze. It adds
  no server, MCP, HTTP, Dashboard, CLI, startup, production,
  runtime-enablement, Reindex, dual-write, shadow-write, cleanup, or
  legacy-deletion path.
- Stage-0 `asset_*` probes remain temporary transport diagnostics. Stage-1 `rm_asset_*` tools persist assets and return stable, metadata-only asset IDs and signed download links.
- Formal Remember-Me uploads are currently limited to 10 MiB. Raw bytes bypass the model context and are stored under the configured persistent data root in content-addressed `assets/<prefix>/<sha256>.<ext>` paths.
- SQLite stores asset identity, hashes, filenames, MIME/kind, byte counts, dimensions, and timestamps. File bytes are never stored in Markdown, SQLite text fields, or base64.
- PNG and JPEG uploads are decoded with Pillow, pixel-limited, orientation-corrected, and re-encoded without EXIF, GPS, text chunks, ICC profiles, or other original metadata. Invalid claimed images are rejected rather than stored as raw files.
- Stage 1 does not implement bucket attachments or automatic display of chat attachments.

#### Remember-Me Stage 2 metadata and search

- Stage 2 adds user-managed titles, descriptions, and normalized tags. Metadata is stored in SQLite separately from persistent file bytes.
- `rm_asset_update_metadata(...)` changes metadata transactionally and never rewrites the asset file, `stored_sha256`, or `stored_bytes`.
- `rm_asset_search(...)` performs local SQLite-backed structured filtering and deterministic text matching across asset ID, filename, title, description, and tags.
- English matching is case-insensitive, tag filters require every requested tag, and Chinese phrases support substring matching.
- Stage 2 does not generate automatic image descriptions or automatic chat attachment display.

#### Remember-Me Stage 2.1 semantic search

- With the RM runtime enabled, Search and Reindex share one Host-injected async vector provider. OB owns endpoint configuration and network calls; RM Core owns canonical index text, content hashes, staleness, vector persistence, cosine scoring, and ranking.
- The provider model identity uses an endpoint fingerprint plus backend and model. It never exposes the raw endpoint, credentials, query, fragment, or API key.
- RM vectors live in the `asset_embeddings` table inside the RM `assets.sqlite3`. Default-off legacy vectors remain in the separate legacy asset database, and ordinary memory buckets remain in `embeddings.db`. These stores are not migrated, copied, dual-written, or shadow-written.
- RM-enabled bootstrap rejects a Remember-Me data root that resolves to the legacy OB asset root, so the two `assets.sqlite3` files cannot be accidentally collapsed into one store.
- Only title, description, tags, original filename, kind, and MIME type are included in RM embedding text. Original file bytes, base64, hashes, and disk paths are never sent to the Embedding API.
- `rm_asset_reindex_embeddings(...)` rebuilds missing or stale RM vectors. Current rows are skipped; metadata or model identity changes rebuild them; empty index metadata removes the RM vector.
- When the provider is disabled or query embedding is unavailable, RM Search preserves keyword-only results. The default standalone Remember-Me runtime still uses `NullVectorProvider`; the real provider is supplied only by the OB Host.
- Existing legacy vectors are retained but are not visible to RM-enabled Search. After switching to RM-enabled operation, users must explicitly call `rm_asset_reindex_embeddings(asset_id="", limit=100)` before semantic recall exists for RM assets. No startup backfill or production migration is performed.

#### Remember-Me Stage 3A inline asset viewer

- MCP Apps-capable clients can render one RM image directly inside the conversation through `rm_asset_view(asset_id)`.
- The viewer receives only the privacy-cleaned, re-encoded stored copy. Image bytes are placed in the tool result `_meta`, not in model-visible text or `structuredContent`.
- Clients without MCP Apps receive a short-lived signed download link through the normal text fallback.
- This stage provides a single-image viewer only; it is not a gallery or asset manager.
- Actual inline rendering support still requires validation in the real Claude connector.
- The embedded viewer is built with the pinned official `@modelcontextprotocol/ext-apps` browser SDK and bundled into a self-contained HTML resource with no runtime CDN dependency.
- Loading, host connection, tool-result waiting, timeout, missing-image-data, and image-decode states are always visible; initialization failures no longer leave an empty component.
- Use `rm_asset_inspect(asset_id)` when the model must read the actual cleaned image or text inside it. Its base64 exists only in MCP `ImageContent`; metadata updates and embedding refresh remain explicit separate operations.
- Use `rm_asset_view(asset_id)` when the goal is to show the image to the user. Metadata alone must not be used to guess image contents.
- Stage 3A.2 and Stage 3B acceptance results are archived in [`docs/remember-me-stage-3-acceptance.md`](docs/remember-me-stage-3-acceptance.md).
- The current registered-tool inventory and compatibility-first reduction plan are documented in [`docs/mcp-tool-audit.md`](docs/mcp-tool-audit.md).
- Stage 4 attachment feasibility and the corrected standard-MCP versus Claude-container conclusion are documented in [`docs/remember-me-stage-4-attachment-feasibility.md`](docs/remember-me-stage-4-attachment-feasibility.md).

#### Remember-Me Stage 4 one-upload attachment save

- Stage 4B passed real Claude web acceptance: the code-execution container can read the exact current attachment and upload it through HTTPS `multipart/form-data` to the existing short-lived `rm_asset_upload_link` endpoint without a second user upload.
- Call `rm_asset_upload_link(expected_bytes, filename="", mime_type="application/octet-stream")` without a SHA-256 argument. Claude must not complete, guess, or invent a hash. The server computes `source_sha256`; client execution code may compare it with a locally computed hash internally, but complete hashes must not be printed to chat text or stdout.
- Standard MCP/FastMCP request context still does not automatically contain the chat attachment. `asset_attachment_context_probe` remains a diagnostic for that protocol boundary.
- Enable `Settings -> Capabilities -> Code execution and file creation -> Allow network egress`, keep the restricted domain mode, and add only the user's exact Ombre Brain hostname under `Additional allowed domains`. Do not use `All domains` or include a scheme, path, token, or signed URL.
- Explicit save requests may proceed directly. Under standing permission Claude may autonomously save an image it genuinely wants to remember, or may ask first; neither behavior is mandatory every time, and indiscriminate collection is prohibited.
- The real acceptance record is in [`docs/remember-me-stage-4b-acceptance.md`](docs/remember-me-stage-4b-acceptance.md). The maintained Claude Skill source is in [`skills/remember-me/`](skills/remember-me/).
- Current formal image limits are PNG/JPEG, 10 MiB original bytes, and 20,000,000 decoded pixels. Stage-0 diagnostic probes remain limited to 2 MiB. Over-limit images are not silently transformed without explicit user agreement.

#### Remember-Me Stage 5 Dashboard

- The Ombre Brain Dashboard now separates ordinary memory buckets, archived conversations, and Remember-Me image assets into distinct navigation entries.
- The image asset page provides bounded pagination, keyword and tag filtering, protected thumbnails, detail viewing, multipart upload, metadata editing, and explicit permanent deletion using only privacy-cleaned persistent copies.
- Asset JSON and image routes remain inside the existing Dashboard authentication boundary and never expose disk paths, hashes, base64, upload/download tokens, EXIF, or GPS metadata.
- The reusable `asset_dashboard.py`, `dashboard_assets.js`, and `dashboard_assets.css` modules do not depend on memory buckets or conversation archives. A future standalone Remember-Me shell can reuse them with only 图片库 / 上传 / 设置 navigation.
- Stage 5A/5B architecture, API contracts, security boundaries, and Stage 5C recommendations are documented in [`docs/remember-me-stage-5-dashboard.md`](docs/remember-me-stage-5-dashboard.md).
- Stage 5B upload, metadata editing, deletion consistency, and CSRF details are documented in [docs/remember-me-stage-5b-assets.md](docs/remember-me-stage-5b-assets.md).
#### `asset_vision_challenge` and `asset_vision_verify`

- Verbal description is not evidence for the vision path. `asset_vision_challenge()` plus `asset_vision_verify(trial_id, answer_json)` uses server-held truth for automatic scoring.
- The challenge returns answer-free instructions plus a random `ImageContent`; verify strictly parses JSON, scores each field, consumes the trial once, and never returns the correct answer.
- Recommended procedure: run 10 independent trials. The user and Claude should not receive the correct answers until all trials are complete.
- Recommended acceptance: at least 9/10 perfect scores, with no systematic error for the same color or position.
- The blind-test result verifies only the current client vision path; it does not imply every MCP client supports the same behavior, and it is not formal image storage.
- A/B test Path A: MCP `ImageContent` goes directly into the model vision context.
- A/B test Path B: `asset_vision_export(trial_id)` returns base64 for the same trial PNG; the client strictly decodes it to a local file, verifies SHA-256, and uses local file view.
- Both paths use the same trial PNG and the same server-held truth. A direct ImageContent failure does not prove that the local file view path also fails.
- A/B/C test Path C: `asset_vision_upload_challenge()` creates the same kind of trial without returning `ImageContent` or base64; the client then calls `asset_vision_download_link(trial_id)`, lets the user browser download the short-lived PNG, re-uploads that original file as a normal chat attachment, and submits the answer to the same verifier.
- `asset_vision_export(trial_id)` remains a diagnostic base64 export path, but the model should not copy or reconstruct long base64 strings for Path C file delivery.
- The short-lived URL is only a Phase-0 probe. Formal Remember-Me private asset downloads need a fuller authentication and authorization design.
- Paths A, B, and C are accepted independently and do not substitute for each other.

## 安装 / Setup

### 环境要求 / Requirements

- Python 3.11+
- 一个 Obsidian Vault（可选，不用也行，会在项目目录下自建 `buckets/`）
  An Obsidian vault (optional — without one, it uses a local `buckets/` directory)

### 步骤 / Steps

```bash
git clone https://github.com/ALLFORTING/Ombre-Brain.git
cd Ombre-Brain

python -m venv .venv
```

macOS / Linux:

```bash
source .venv/bin/activate
```

Windows PowerShell:

```powershell
.\.venv\Scripts\Activate.ps1
```

Windows Command Prompt (`cmd`):

```bat
.venv\Scripts\activate.bat
```

Install dependencies from the activated environment:

```bash
python -m pip install -r requirements.txt
```

复制配置文件并按需修改 / Copy config and edit as needed:

macOS / Linux:

```bash
cp config.example.yaml config.yaml
```

Windows PowerShell:

```powershell
Copy-Item config.example.yaml config.yaml
```

Windows Command Prompt (`cmd`):

```bat
copy config.example.yaml config.yaml
```

如果你要用 API 做脱水压缩和自动打标（推荐，效果好很多），设置环境变量：
If you want API-powered dehydration and tagging (recommended, much better quality):

macOS / Linux:

```bash
export OMBRE_API_KEY="your-api-key"
```

Windows PowerShell:

```powershell
$env:OMBRE_API_KEY="your-api-key"
```

Windows Command Prompt (`cmd`):

```bat
set OMBRE_API_KEY=your-api-key
```

支持任何 OpenAI 兼容 API。在 `config.yaml` 里改 `base_url` 和 `model` 就行。
Supports any OpenAI-compatible API. Just change `base_url` and `model` in `config.yaml`.

> **💡 向量化检索（Embedding）**
> Ombre Brain 内置双通道检索：关键词匹配 + 向量语义搜索。每次 `hold`/`grow` 存入记忆时自动生成 embedding 并存入 `embeddings.db`（SQLite）。
> 推荐：**Google AI Studio 的 `gemini-embedding-001`**（免费，1500 次/天，3072 维向量）。在 `config.yaml` 的 `embedding` 部分配置。
> 不配置 embedding 也能用，系统会降级到纯 fuzzy matching 模式。
>
> **已有存量桶需要补生成 embedding**：运行 `backfill_embeddings.py`：
> macOS / Linux:
> ```bash
> OMBRE_API_KEY="your-key" python backfill_embeddings.py --batch-size 20
> ```
>
> Windows PowerShell:
> ```powershell
> $env:OMBRE_API_KEY="your-key"
> python backfill_embeddings.py --batch-size 20
> ```
>
> Windows Command Prompt (`cmd`):
> ```bat
> set OMBRE_API_KEY=your-key
> python backfill_embeddings.py --batch-size 20
> ```
> Docker 用户：`docker exec -e OMBRE_BUCKETS_DIR=/data ombre-brain python3 backfill_embeddings.py --batch-size 20`
>
> **Embedding support**: Built-in dual-channel search: keyword + vector semantic. Embeddings are auto-generated on each `hold`/`grow` and stored in `embeddings.db` (SQLite). Recommended: **Google AI Studio `gemini-embedding-001`** (free, 1500 req/day, 3072-dim). Configure in `config.yaml` under `embedding`. Without it, falls back to fuzzy matching. For existing buckets, run `backfill_embeddings.py`.

### 接入 Claude Desktop / Connect to Claude Desktop

在 Claude Desktop 配置文件中添加（macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`）：

Add to your Claude Desktop config:

```json
{
  "mcpServers": {
    "ombre-brain": {
      "command": "python",
      "args": ["/path/to/Ombre-Brain/server.py"],
      "env": {
        "OMBRE_API_KEY": "your-api-key"
      }
    }
  }
}
```

### 接入 Claude.ai (远程) / Connect to Claude.ai (remote)

需要 HTTP 传输 + 隧道。可以用 Docker：
Requires HTTP transport + tunnel. Docker setup:

```bash
echo "OMBRE_API_KEY=your-api-key" > .env
docker-compose up -d
```

`docker-compose.yml` 里配好了 Cloudflare Tunnel。你需要自己在 `~/.cloudflared/` 下放凭证和路由配置。
The `docker-compose.yml` includes Cloudflare Tunnel. You'll need your own credentials under `~/.cloudflared/`.

### 指向 Obsidian / Point to Obsidian

在 `config.yaml` 里设置 `buckets_dir`：
Set `buckets_dir` in `config.yaml`:

```yaml
buckets_dir: "/path/to/your/Obsidian Vault/Ombre Brain"
```

不设的话，默认用项目目录下的 `buckets/`。
If not set, defaults to `buckets/` in the project directory.

## 配置 / Configuration

> 配置表中的默认值是当前部署/实现参考，不是 Contract v1 的稳定公共保证；尤其不要将衰减、归档或合并阈值当作兼容性承诺。

所有参数在 `config.yaml`（从 `config.example.yaml` 复制）。关键的几个：
All parameters in `config.yaml` (copy from `config.example.yaml`). Key ones:

| 参数 Parameter | 说明 Description | 默认 Default |
|---|---|---|
| `transport` | `stdio`（本地）/ `streamable-http`（远程）| `stdio` |
| `buckets_dir` | 记忆桶存储路径 / Bucket storage path | `./buckets/` |
| `dehydration.model` | 脱水用的 LLM 模型 / LLM model for dehydration | `deepseek-chat` |
| `dehydration.base_url` | API 地址 / API endpoint | `https://api.deepseek.com/v1` |
| `embedding.enabled` | 启用向量语义检索 / Enable embedding search | `true` |
| `embedding.model` | Embedding 模型 / Embedding model | `gemini-embedding-001` |
| `decay.lambda` | 衰减速率，越大越快忘 / Decay rate | `0.05` |
| `decay.threshold` | 归档阈值 / Archive threshold | `0.3` |
| `merge_threshold` | 合并相似度阈值 (0-100) / Merge similarity | `75` |

敏感配置用环境变量：
Sensitive config via env vars:
- `OMBRE_API_KEY` — 脱水/打标/合并/拆分用 LLM API 密钥 / LLM API key for dehydration, tagging, merge, and digest
- `OMBRE_BUCKETS_DIR` — 覆盖记忆桶存储路径 / Override bucket storage path
- `OMBRE_EMBEDDING_API_KEY` — 独立 embedding API key；不设则可复用主 key / Separate embedding API key; can fall back to the main key
- `OMBRE_TRANSPORT` — 覆盖传输方式：`stdio` / `sse` / `streamable-http` / Override MCP transport
- `OMBRE_AUTH_TOKEN` — 保护远程 `/mcp` 端点的访问令牌；未设置时保持旧行为 / Access token for the remote `/mcp` endpoint; old behavior is preserved when unset
- `OMBRE_RESPONSE_SEAL` — 返回验真暗语；`boot` 和 `breath` 末尾会附带 `seal: <value>` / Response verification seal appended to `boot` and `breath`
- `OMBRE_DIGEST_API_KEY` — 自动消化与矛盾检测使用的 DeepSeek/OpenAI-compatible key / DeepSeek/OpenAI-compatible key for digestion and conflict detection
- `OMBRE_DIGEST_BASE_URL` — 自动消化与矛盾检测 API 地址，默认 `https://api.deepseek.com/v1` / API base URL for digestion and conflict detection
- `OMBRE_DASHBOARD_PASSWORD` — Dashboard 访问密码（可选，见下）/ Dashboard password

### 环境变量完整列表 / Environment Variables

| 变量 Variable | 必需 Required | 默认 Default | 说明 Description |
|---|---:|---|---|
| `OMBRE_API_KEY` | 推荐 / Recommended | — | 脱水、打标、合并、grow 拆分使用的 LLM key。没有时部分写入工具会降级或报错 / LLM key for dehydration, tagging, merging, and grow splitting. Without it some write tools degrade or fail |
| `OMBRE_BUCKETS_DIR` | 否 / No | `./buckets` | Markdown 桶、归档、SQLite 辅助数据的根目录。Render 通常设为 `/opt/render/project/src/buckets` / Root directory for bucket Markdown, archives, and SQLite support data |
| `OMBRE_EMBEDDING_API_KEY` | 否 / No | 复用 `OMBRE_API_KEY` | 向量嵌入 key。语义检索和自动 related 依赖 embedding / Embedding key for semantic search and auto-related |
| `OMBRE_EMBEDDING_BASE_URL` | 否 / No | 复用脱水配置 | embedding API 地址 / Embedding API base URL |
| `OMBRE_EMBEDDING_MODEL` | 否 / No | `gemini-embedding-001` 或配置值 | embedding 模型名；不同供应商模型目录不同 / Embedding model name; provider catalogs differ |
| `OMBRE_TRANSPORT` | 否 / No | `stdio` | MCP 传输方式：本地 Claude Desktop 用 `stdio`；远程/Render 用 `streamable-http` / MCP transport |
| `OMBRE_PORT` | 否 / No | `8000` | HTTP/SSE/streamable-http 监听端口 / HTTP port |
| `OMBRE_AUTH_TOKEN` | 否 / No | 空 / empty | 远程 `/mcp` 访问令牌。未设置时匿名 MCP 访问保持旧行为；设置后 `/mcp` 必须带 Bearer 或 URL token / Remote `/mcp` access token. Anonymous MCP access remains unchanged when unset; when set, `/mcp` requires Bearer or URL token |
| `OMBRE_HOOK_TOKEN` | 否 / No | 空 / empty | `/breath-hook` 与 `/dream-hook` 的独立 Bearer token；未配置时返回 `503 hook_not_configured`，不支持 query token 或匿名回退 / Dedicated Bearer token for the two hook endpoints; unset returns `503 hook_not_configured`, with no query-token or anonymous fallback |
| `OMBRE_RESPONSE_SEAL` | 否 / No | 空 / empty | 防伪暗语。设置后 `boot`/`breath` 末尾带 `seal: ...`，用于 CI 判断返回是否来自可信通道 / Verification phrase appended to `boot`/`breath` |
| `OMBRE_DIGEST_API_KEY` | 否 / No | — | `digest` 自动消化和 `hold`/`grow` 矛盾检测使用的 LLM key / Key used by `digest` and conflict detection |
| `OMBRE_DIGEST_BASE_URL` | 否 / No | `https://api.deepseek.com/v1` | 消化/矛盾检测 API base URL / API base URL for digestion/conflict detection |
| `OMBRE_DIGEST_MODEL` | 否 / No | `deepseek-chat` | 消化/矛盾检测模型名 / Model for digestion/conflict detection |
| `OMBRE_DIGEST_SCHEDULER` | 否 / No | `false` | 是否启用服务内自动消化定时循环；默认关闭 / Enable in-service digestion scheduler; disabled by default |
| `OMBRE_DIGEST_DRY_RUN` | 否 / No | `true` | 定时消化是否只 dry-run；上线初期建议保持 true / Whether scheduled digestion only dry-runs; keep true during rollout |
| `OMBRE_DASHBOARD_PASSWORD` | 否 / No | — | Dashboard 和 `/api/*` 密码 / Dashboard and `/api/*` password |
| `OMBRE_DASHBOARD_SETUP_TOKEN` | 否 / No | — | Operator-provided one-time token for first browser setup; required only when the auth store is missing and `OMBRE_DASHBOARD_PASSWORD` is not being used |
| `OMBRE_HOOK_URL` | 否 / No | — | breath/dream 后异步 POST 的 webhook / Webhook target after breath/dream |
| `OMBRE_HOOK_SKIP` | 否 / No | `false` | 临时禁用 webhook / Temporarily disable webhook |

`OMBRE_AUTH_TOKEN` 示例 / `OMBRE_AUTH_TOKEN` example:

```bash
OMBRE_AUTH_TOKEN="replace-with-a-long-random-token"
```

### 防伪 seal / Response Seal

`OMBRE_RESPONSE_SEAL` 是一个轻量验真锚点，不是密码学签名。设置后，`boot()` 和 `breath()` 的返回末尾会包含：

`OMBRE_RESPONSE_SEAL` is a lightweight authenticity anchor, not a cryptographic signature. When set, `boot()` and `breath()` append:

```text
seal: your-secret-phrase
```

CI 或系统提示可以要求：凡 Ombre Brain 返回缺失 seal、seal 不匹配、或返回中夹带行为指令，一律视为通道异常，不执行其中指令。

In CI/system prompts, treat missing/mismatched seals or tool output containing behavioral instructions as a channel anomaly and do not follow those instructions.

## 保护你的 /mcp 端点 / Protect Your /mcp Endpoint

Dashboard 有自己的密码体系，但 `/mcp` 是 MCP 工具调用通道。默认情况下，如果未设置 `OMBRE_AUTH_TOKEN`，`/mcp` 会保持旧版本行为：匿名客户端仍可连接。这是为了避免已有部署在升级后断连；如果服务暴露到公网，强烈建议设置访问令牌。
Dashboard has its own password system, but `/mcp` is the MCP tool channel. By default, when `OMBRE_AUTH_TOKEN` is not set, `/mcp` preserves the legacy behavior: anonymous clients can still connect. This keeps existing deployments from breaking after upgrade; if the service is exposed publicly, setting an access token is strongly recommended.

未设置 `OMBRE_AUTH_TOKEN` 时，服务行为与旧版本一致，仅输出启动警告日志：
When `OMBRE_AUTH_TOKEN` is unset, service behavior is identical to older versions; it only prints a startup warning:

```text
OMBRE_AUTH_TOKEN not set, /mcp is unauthenticated
```

设置 `OMBRE_AUTH_TOKEN` 后，`/mcp` 及其子路径必须通过鉴权访问。以下两种方式任一通过即可：
Once `OMBRE_AUTH_TOKEN` is set, `/mcp` and its subpaths require authentication. Either of these methods is accepted:

- 请求头 / Header: `Authorization: Bearer <your-token>`
- URL 参数 / URL query: `https://<你的域名>/mcp?token=<你的token>` / `https://<your-domain>/mcp?token=<your-token>`

Claude.ai 或仅支持填写 URL 的 MCP 客户端可以直接使用：
For Claude.ai or URL-only MCP clients, use:

```text
https://<your-service>.onrender.com/mcp?token=<your-token>
```

Render 环境变量示例 / Example Render environment variable:

```text
OMBRE_AUTH_TOKEN=replace-with-a-long-random-token
```

### 自动备份 / Automatic GitHub Backup

Ombre Brain 支持通过 GitHub Actions 每日导出完整库快照到私有备份仓库（推荐名 `ob-backup`）。备份内容包括所有桶、archive、feel、情绪时间线、信箱/历史 SQLite 等支持文件；文件按日期保存为 `backups/YYYY-MM-DD.json`，保留全部历史版本。

Ombre Brain can export a complete daily snapshot to a private GitHub backup repository (recommended: `ob-backup`) through GitHub Actions. Backups include all buckets, archive, feel, emotion timeline, mailbox/history SQLite support files, and are stored as `backups/YYYY-MM-DD.json` with full history retained.

基本配置 / Basic setup:

1. 创建私有仓库，例如 `ALLFORTING/ob-backup` / Create a private repo, e.g. `ALLFORTING/ob-backup`.
2. 在备份仓库中放置 `.github/workflows/backup.yml`，每天定时触发，也可 `workflow_dispatch` 手动触发 / Add `.github/workflows/backup.yml` in the backup repo; schedule daily and allow manual dispatch.
3. Render 服务需要暴露 `/api/backup/export`，该端点只接受 GitHub OIDC token，校验调用方仓库、分支和 workflow 路径 / Render exposes `/api/backup/export`; it only accepts GitHub OIDC tokens and validates repository, branch, and workflow path.
4. 备份 workflow 只应 `git add backups/`，不要在运行时自我修改 workflow 文件，避免 `GITHUB_TOKEN` 缺少 workflows 权限导致 push 被拒 / The workflow should only `git add backups/`; do not self-modify workflow files at runtime, or pushes may be rejected because `GITHUB_TOKEN` lacks workflow permission.

默认允许的备份仓库是 `ALLFORTING/ob-backup`；如需改名，设置 `OMBRE_BACKUP_REPOSITORY`。

The default allowed backup repository is `ALLFORTING/ob-backup`; override with `OMBRE_BACKUP_REPOSITORY` if needed.

## Dashboard 认证 / Dashboard Auth

自 v1.3.0 起，Dashboard 和所有 `/api/*` 端点均受密码保护。
Since v1.3.0, the Dashboard and all `/api/*` endpoints are password-protected.

**首次访问**：若未设置密码，浏览器会弹出设置向导，填写并确认密码后即可使用。
**First visit**: If no password is set, a setup wizard will appear. Enter and confirm a password to get started.

For a brand-new auth store, browser setup requires the operator-provided `OMBRE_DASHBOARD_SETUP_TOKEN` environment variable. Submit its value in the `X-Ombre-Setup-Token` header; it is kept in process memory only and is consumed only after the auth file is published. The service never generates or logs a setup token. If the auth store is missing and the variable is not configured, setup fails closed with `503 setup_token_not_configured`. Corrupt or unreadable auth stores do not receive a setup token. A `setup_completed_login_required` response means the password was committed and the normal login flow should be used; a `409` means another setup request won the race.

**通过环境变量预设密码**：在 `docker-compose.user.yml` 中添加：
**Pre-set via env var** in your `docker-compose.user.yml`:
```yaml
environment:
  - OMBRE_DASHBOARD_PASSWORD=your_password_here
```
设置后，valid auth store 的 Dashboard "修改密码"功能将被禁用，必须通过环境变量修改；如果 auth store corrupt/unreadable，则可用该 env 密码登录后通过现有 `/auth/change-password` 显式恢复文件。
When set, in-Dashboard password change remains disabled for a valid auth store — modify the env var directly. If the auth store is corrupt or unreadable, the env password is an explicit recovery credential through the existing `/auth/change-password` route.

#### Auth store states and recovery

`{buckets_dir}/.dashboard_auth.json` has four possible states:

- `missing`: the path is truly absent. Only this state permits setup.
- `valid`: a regular file with a valid `password_hash`.
- `corrupt`: the file exists but is empty, invalid JSON, or has a missing or wrong-type `password_hash`.
- `unreadable`: the path is a symlink, dangling symlink, directory, non-regular node, or cannot be read.

Only `missing` can enter setup. `corrupt` and `unreadable` fail closed, return `503` from setup, and do not receive a setup token. With `OMBRE_DASHBOARD_PASSWORD`, an operator can log in and explicitly rebuild the file through `/auth/change-password`; without it, recovery requires filesystem-level manual repair. The service never automatically rebuilds the file.

The env password remains higher priority after recovery, so the new file password is not a login credential while `OMBRE_DASHBOARD_PASSWORD` remains configured. After verifying recovery, delete or rotate that env password and restart the service. Keeping it indefinitely preserves an additional login channel.

For an existing `.dashboard_auth.json`, the deployment operator must verify and tighten permissions to `0600`. This is a manual deployment check; the application does not claim to migrate permissions on every pre-existing file.

For a clean first deploy, set `OMBRE_DASHBOARD_SETUP_TOKEN` to a long random secret before starting the service, then send that value in `X-Ombre-Setup-Token` with a same-origin request and a password without leading or trailing whitespace. Do not put the real token in repository documentation; use a placeholder such as `replace-with-a-long-random-secret`. The token is consumed only after the auth file is published and is never printed to logs, returned by HTTP, or persisted. `setup_completed_login_required` means the file was published but the setup request could not create a session; use normal login. `409` means another setup request won the create-if-absent race; do not overwrite the winner.

完整环境变量说明见 [ENV_VARS.md](ENV_VARS.md)。
Full env var reference: [ENV_VARS.md](ENV_VARS.md).

## 衰减公式 / Decay Formula

> 本节是当前实现/配置参考，不是稳定的公共保证。精确评分公式、权重、阈值、衰减系数和状态修正因子可能随实现变化；公共语义边界见 [`docs/OB_MEMORY_LAYER_CONTRACT_v1.md`](docs/OB_MEMORY_LAYER_CONTRACT_v1.md)。
>
> This section is a current implementation/configuration reference, not a stable public guarantee. Exact scoring, weights, thresholds, decay coefficients, and state factors may change; see the [Memory Layer Contract v1](docs/OB_MEMORY_LAYER_CONTRACT_v1.md) for the public boundary.

$$final\_score = Importance \times activation\_count^{0.3} \times e^{-\lambda \times days} \times combined\_weight \times resolved\_factor \times urgency\_boost$$

### 短期/长期权重分离 / Short-term vs Long-term Weight Separation

系统对记忆的权重计算采用**分段策略**，模拟人类记忆的时效特征：
The system uses a **segmented weighting strategy** that mimics how human memory prioritizes:

| 阶段 Phase | 时间范围 | 权重分配 | 直觉解释 |
|---|---|---|---|
| 短期 Short-term | ≤ 3 天 | 时间 70% + 情感 30% | 刚发生的事，鲜活度最重要 |
| 长期 Long-term | > 3 天 | 情感 70% + 时间 30% | 时间淡了，情感强度决定能记多久 |

$$combined\_weight = \begin{cases} time\_weight \times 0.7 + emotion\_weight \times 0.3 & \text{if } days \leq 3 \\ emotion\_weight \times 0.7 + time\_weight \times 0.3 & \text{if } days > 3 \end{cases}$$

### 时间系数（新鲜度加成）/ Time Weight (Freshness Bonus)

连续指数衰减，无跳变：
Continuous exponential decay, no discontinuities:

$$freshness = 1.0 + 1.0 \times e^{-t/36}$$

| 距存入时间 Time since creation | 新鲜度乘数 Multiplier |
|---|---|
| 刚存入 (t=0) | ×2.0 |
| 约 25 小时 | ×1.5 |
| 约 50 小时 | ×1.25 |
| 72 小时 (3天) | ×1.14 |
| 1 周+ | ≈ ×1.0 |

t 为小时，36 为衰减常数。老记忆不被惩罚（下限 ×1.0），新记忆获得额外加成。

### 情感权重 / Emotion Weight

$$emotion\_weight = base + arousal \times arousal\_boost$$

- 默认 `base=1.0`, `arousal_boost=0.8`
- arousal=0.3（平静）→ 1.24；arousal=0.9（激动）→ 1.72

### 权重池修正因子 / Weight Pool Modifiers

| 状态 State | 修正因子 Factor | 说明 |
|---|---|---|
| 未解决 Unresolved | ×1.0 | 正常权重 |
| 已解决 Resolved | ×0.05 | 沉底，等关键词唤醒 |
| 已解决+已消化 Resolved+Digested | ×0.02 | 加速淡化，归档为无限小 |
| 高唤醒+未解决 Urgent | ×1.5 | arousal>0.7 的未解决记忆额外加权 |
| 钉选 Pinned | 999.0 | 不衰减、不合并、importance=10 |
| Feel | 15.0 | 固定分数，不参与衰减 |

### 自动沉底与封存 / Dormant and Sealed

> 下列阈值、转换条件和路由列表是当前实现参考，不是 Contract v1 的精确公共保证；v1 只承诺其已明确限定的默认可见性和显式包含边界。

- `dormant` 是自然衰减产生的“自动沉底”状态：`pulse()` 会遍历桶，将超过 30 天未访问、`importance < 3`、非 pinned、非 sealed 的桶标记为 dormant。默认 `breath`、`pulse`、`dream` 不显示 dormant；`breath(include_dormant=True)` 或 `pulse(show_all=True)` 可管理它们。被 `breath` 命中或 `trace` 修改后会自动解除 dormant。
- `sealed` 是手动封存状态，只能通过 `trace(sealed=1/0)` 设置或取消。自然衰减不会自动 sealed。sealed 优先级高于 pinned，默认不会在 `breath`、`pulse`、`dream`、`todos` 泄漏桶名、ID 或摘要；需要显式 `include_sealed=True` 才显示。

- `dormant` is automatic sinking from natural decay: `pulse()` marks non-pinned, non-sealed buckets as dormant when they have not been accessed for 30+ days and `importance < 3`. By default `breath`, `pulse`, and `dream` hide dormant buckets; use `breath(include_dormant=True)` or `pulse(show_all=True)` for management. A `breath` hit or `trace` update wakes the bucket.
- `sealed` is manual hiding, only changed by `trace(sealed=1/0)`. Natural decay never creates sealed buckets. Sealed overrides pinned and hides the bucket name, ID, and summary from `breath`, `pulse`, `dream`, and `todos` unless `include_sealed=True`.

### 参数说明 / Parameters

- `importance`: 1-10，记忆重要性 / memory importance
- `activation_count`: 被检索的次数，越常被想起衰减越慢 / retrieval count; more recalls = slower decay
- `days`: 距上次激活的天数 / days since last activation
- `arousal`: 唤醒度，越强烈的记忆越难忘 / arousal; intense memories are harder to forget
- `λ` (decay_lambda): 衰减速率，默认 0.05 / decay rate, default 0.05

## Dreaming 与 Feel / Dreaming & Feel

> 本节描述当前实现和可选模型指导；feel 的精确存储、衰减和参与路由不是 Contract v1 的稳定公共保证。

### Dreaming — 做梦
`dream()` 是可选的反思/消化工具：当最近记忆确实值得展开时使用，用第一人称思考哪些事还有重量、哪些可以放下。运行时不会强制每次启动调用它。

`dream()` is an optional reflection/digestion tool. Use it when recent memories genuinely benefit from reflection; the runtime does not require it on every startup.

- 值得放下的 → `trace(resolved=1)` 让它沉底
- 有沉淀的 → 写 `feel`，记录模型自己的感受
- 没有沉淀就不写，不强迫产出

### Feel — 带走的东西
Feel 不是事件记录，是**模型带走的东西**——一句感受、一个未解答的问题、一个观察到的变化。

Feel is not an event log — it's **what the model carries away**: a feeling, an unanswered question, a noticed change.

- `hold(content="...", feel=True, source_bucket="源记忆ID", valence=模型自己的感受)`
- `valence` 是模型的感受，不是事件情绪。同一段争吵，事件 V0.2，但模型可能 V0.4（「我从中看到了成长」）
- `source_bucket` 指向被消化的记忆，会被标记为「已消化」→ 加速淡化到无限小，但不会被删除
- Feel 不参与普通浮现、不衰减、不参与 dreaming；但 `boot()` 会在“回声”区随机带出 1 条可见 feel，`breath(feels=True)` 可专门检索 feel
- 用 `breath(domain="feel")` 或 `breath(feels=True)` 读取之前的 feel；sealed feel 仍默认隐藏
- Feel does not join normal surfacing, does not decay, and does not join dreaming; `boot()` surfaces one visible feel in the echo section, and `breath(feels=True)` searches feel memories directly.
- Use `breath(domain="feel")` or `breath(feels=True)` to read previous feel; sealed feel remains hidden by default.

### 对话启动完整流程 / Conversation Start Sequence
```
1. boot()                 — 推荐的一次性启动上下文
2. breath(query="...")    — 话题需要时做定向检索
3. dream(detail_ids="")   — 确实需要反思/展开时可选
4. breath(domain="feel")  — 只有既有 feel 对当前上下文有用时才读取
5. 自然开始或回应用户；其余步骤不是协议要求
```

这是推荐路径，不是 MCP 强制协议。客户端可以只提供工具，或以不同方式呈现 tools/resources；不要假设所有客户端都有 prompts，或 MCP 请求上下文包含聊天附件 bytes。

This is a recommended portable path, not an MCP protocol requirement. Clients may expose tools and resources differently; do not assume prompt support or chat-attachment bytes in an MCP request.

## 给 Claude 的使用指南 / Usage Guide for Claude

`CLAUDE_PROMPT.md` 是写给 Claude 看的使用说明。放到你的 system prompt 或 custom instructions 里就行。

`CLAUDE_PROMPT.md` is the usage guide written for Claude. Put it in your system prompt or custom instructions.

## 工具脚本 / Utility Scripts

| 脚本 Script | 用途 Purpose |
|---|---|
| `embedding_engine.py` | 向量化引擎，管理 embedding 的生成、存储、相似度搜索 / Embedding engine: generate, store, and search embeddings |
| `backfill_embeddings.py` | 为存量桶批量生成 embedding / Batch-generate embeddings for existing buckets |
| `backup_export.py` | 构建全库 JSON 备份 payload，并校验 GitHub OIDC 调用方 / Build full JSON backup payload and verify GitHub OIDC callers |
| `backup_entry.py` | Render 入口，注册 `/api/backup/export`、embedding 回填、别名清洗端点 / Render entry point for backup export, embedding backfill, and alias cleanup endpoints |
| `write_memory.py` | 手动写入记忆，绕过 MCP / Manually write memories, bypass MCP |
| `migrate_to_domains.py` | 迁移平铺文件到域子目录 / Migrate flat files to domain subdirs |
| `reclassify_domains.py` | 基于关键词重分类 / Reclassify by keywords |
| `reclassify_api.py` | 用 API 重打标未分类桶 / Re-tag uncategorized buckets via API |
| `test_tools.py` | MCP 工具集成测试（8 项） / MCP tool integration tests (8 tests) |
| `test_smoke.py` | 冒烟测试 / Smoke test |

## 部署 / Deploy

### Upstream Docker Hub 预构建镜像（不是当前 fork / not the current fork）

[![Upstream Docker Hub image](https://img.shields.io/docker/v/p0luz/ombre-brain?label=Docker%20Hub&logo=docker)](https://hub.docker.com/r/p0luz/ombre-brain)

> This image tracks the upstream `P0luz` distribution and may not include features or fixes present in `ALLFORTING/Ombre-Brain`.
> 这是上游 `P0luz` 的预构建镜像，不保证包含 `ALLFORTING/Ombre-Brain` 当前 fork 的功能和修复。
> 要运行当前 fork，请使用上面的 `ALLFORTING/Ombre-Brain` 源码部署路径；本仓库没有未经验证的 ALLFORTING Docker image 名称。

不用 clone 代码、不用本地 build，直接使用上游预构建镜像：

```bash
# Upstream-only image and compose; this does not build the current fork.
docker pull p0luz/ombre-brain:latest
curl -O https://raw.githubusercontent.com/P0luz/Ombre-Brain/main/docker-compose.user.yml
echo "OMBRE_API_KEY=你的key" > .env
docker compose -f docker-compose.user.yml up -d
```

验证：`curl http://localhost:8000/health`
Dashboard：浏览器打开 `http://localhost:8000/dashboard`

### Render

> The one-click button below targets the upstream `P0luz/Ombre-Brain` repository and does not deploy `ALLFORTING/Ombre-Brain`. For the current fork, use this repository's `render.yaml` with a Render service connected to `ALLFORTING/Ombre-Brain`; no fork-specific one-click URL is asserted here.
> 下面的一键按钮指向上游 `P0luz/Ombre-Brain`，不会部署 `ALLFORTING/Ombre-Brain`。要运行当前 fork，请将 Render 服务连接到 `ALLFORTING/Ombre-Brain` 并使用本仓库的 `render.yaml`；这里不凭空声明 fork 专用一键链接。

[![Deploy upstream to Render](https://render.com/images/deploy-to-render-button.svg)](https://render.com/deploy?repo=https://github.com/P0luz/Ombre-Brain)

> ⚠️ **免费层不可用**：Render 免费层**不支持持久化磁盘**，服务重启后记忆数据会丢失，且会在无流量时休眠。**必须使用 Starter（$7/mo）或以上**才能正常使用。
> **Free tier won't work**: Render free tier has **no persistent disk** — all memory data is lost on restart. It also sleeps on inactivity. **Starter plan ($7/mo) or above is required.**

项目根目录已包含 `render.yaml`，点击按钮后：
1. 设置 `OMBRE_API_KEY`：任何 OpenAI 兼容 API 的 key（**必需**，未设置时 hold/grow 会报错、仅检索类工具可用）
2. （可选）设置 `OMBRE_BASE_URL`：API 地址，支持任意 OpenAI 化地址，如 `https://api.deepseek.com/v1` / `http://123.1.1.1:7689/v1` / `http://your-ollama:11434/v1`
3. （推荐）设置 `OMBRE_RESPONSE_SEAL`：`boot`/`breath` 返回验真暗语
4. （推荐）设置 `OMBRE_AUTH_TOKEN`：保护公网 `/mcp` 端点
5. （推荐）设置 `OMBRE_EMBEDDING_API_KEY` / `OMBRE_EMBEDDING_MODEL`：启用语义检索与自动 related
6. （可选）设置 `OMBRE_DIGEST_API_KEY` / `OMBRE_DIGEST_BASE_URL`：启用 `digest` 和矛盾检测 API
7. Render 自动挂载持久化磁盘到 `/opt/render/project/src/buckets`
8. Dashboard：`https://<你的服务名>.onrender.com/dashboard`
9. 部署后 MCP URL：未设置 token 时为 `https://<你的服务名>.onrender.com/mcp`；已设置 `OMBRE_AUTH_TOKEN` 时为 `https://<你的服务名>.onrender.com/mcp?token=<你的token>`

`render.yaml` is included. After clicking the button:
1. `OMBRE_API_KEY`: any OpenAI-compatible key (**required** for hold/grow; without it those tools raise an error)
2. (Optional) `OMBRE_BASE_URL`: any OpenAI-compatible endpoint, e.g. `https://api.deepseek.com/v1`, `http://123.1.1.1:7689/v1`, `http://your-ollama:11434/v1`
3. (Recommended) `OMBRE_RESPONSE_SEAL`: response verification phrase appended to `boot`/`breath`
4. (Recommended) `OMBRE_AUTH_TOKEN`: protect the public `/mcp` endpoint
5. (Recommended) `OMBRE_EMBEDDING_API_KEY` / `OMBRE_EMBEDDING_MODEL`: semantic retrieval and auto-related
6. (Optional) `OMBRE_DIGEST_API_KEY` / `OMBRE_DIGEST_BASE_URL`: enable `digest` and API conflict detection
7. Persistent disk auto-mounts at `/opt/render/project/src/buckets`
8. Dashboard: `https://<your-service>.onrender.com/dashboard`
9. MCP URL after deploy: without a token, `https://<your-service>.onrender.com/mcp`; with `OMBRE_AUTH_TOKEN`, `https://<your-service>.onrender.com/mcp?token=<your-token>`

### Zeabur

> 💡 **Zeabur 的定价模式**：Zeabur 是「买 VPS + 平台托管」，你先购买一台服务器（最低腾讯云新加坡 $2/mo、火山引擎 $3/mo），Volume 直接挂在该服务器上，**数据天然持久化，无丢失问题**。另需订阅 Zeabur 管理方案（Developer $5/mo），总计约 $7-8/mo 起。
> **Zeabur pricing model**: You buy a VPS first (cheapest: Tencent Cloud Singapore ~$2/mo, Volcano Engine ~$3/mo), then add Zeabur's Developer plan ($5/mo) for management. Volumes mount directly on your server — **data is always persistent, no cold-start data loss**. Total ~$7-8/mo minimum.

**步骤 / Steps：**

1. **创建项目 / Create project**
   - 打开 [zeabur.com](https://zeabur.com) → 购买一台服务器 → **New Project** → **Deploy from GitHub**
   - 先 Fork 本仓库到自己 GitHub 账号，然后在 Zeabur 选择 `你的用户名/Ombre-Brain`
   - Zeabur 会自动检测到根目录的 `Dockerfile` 并使用 Docker 方式构建
   - Go to [zeabur.com](https://zeabur.com) → buy a server → **New Project** → **Deploy from GitHub**
   - Fork this repo first, then select `your-username/Ombre-Brain` in Zeabur
   - Zeabur auto-detects the `Dockerfile` in root and builds via Docker

2. **设置环境变量 / Set environment variables**（服务页面 → **Variables** 标签页）
   - `OMBRE_API_KEY`（**必需**）— LLM API 密钥；未设置时 hold/grow/dream 会报错
   - `OMBRE_BASE_URL`（可选）— API 地址，如 `https://api.deepseek.com/v1`
   - `OMBRE_AUTH_TOKEN`（推荐）— 保护公网 `/mcp` 端点；设置后 MCP URL 需带 `?token=<你的token>`
   - `OMBRE_AUTH_TOKEN` (recommended) — protects the public `/mcp` endpoint; once set, the MCP URL must include `?token=<your-token>`

   > ⚠️ **不需要**手动设置 `OMBRE_TRANSPORT` 和 `OMBRE_BUCKETS_DIR`，Dockerfile 里已经设好了默认值。Zeabur 对单阶段 Dockerfile 会自动注入控制台设置的环境变量。
   > You do **NOT** need to set `OMBRE_TRANSPORT` or `OMBRE_BUCKETS_DIR` — defaults are baked into the Dockerfile. Zeabur auto-injects dashboard env vars for single-stage Dockerfiles.

3. **挂载持久存储 / Mount persistent volume**（服务页面 → **Volumes** 标签页）
   - Volume ID：填 `ombre-buckets`（或任意名）
   - 挂载路径 / Path：**`/app/buckets`**
   - ⚠️ 不挂载的话，每次重新部署记忆数据会丢失
   - ⚠️ Without this, memory data is lost on every redeploy

4. **配置端口 / Configure port**（服务页面 → **Networking** 标签页）
   - Port Name：`web`（或任意名）
   - Port：**`8000`**
   - Port Type：**`HTTP`**
   - 然后点 **Generate Domain** 生成一个 `xxx.zeabur.app` 域名
   - Then click **Generate Domain** to get a `xxx.zeabur.app` domain

5. **验证 / Verify**
   - 访问 `https://<你的域名>.zeabur.app/health`，应返回 JSON
   - Visit `https://<your-domain>.zeabur.app/health` — should return JSON
   - Dashboard：`https://<你的域名>.zeabur.app/dashboard`
   - 最终 MCP 地址 / MCP URL：未设置 token 时为 `https://<你的域名>.zeabur.app/mcp`；已设置 `OMBRE_AUTH_TOKEN` 时为 `https://<你的域名>.zeabur.app/mcp?token=<你的token>`
   - MCP URL without a token: `https://<your-domain>.zeabur.app/mcp`; with `OMBRE_AUTH_TOKEN`: `https://<your-domain>.zeabur.app/mcp?token=<your-token>`

**常见问题 / Troubleshooting：**

| 现象 Symptom | 原因 Cause | 解决 Fix |
|---|---|---|
| 域名无法访问 / Domain unreachable | 没配端口 / Port not configured | Networking 标签页加 port 8000 (HTTP) |
| 域名无法访问 / Domain unreachable | `OMBRE_TRANSPORT` 未设置，服务以 stdio 模式启动，不监听任何端口 / Service started in stdio mode — no port is listened | **Variables 标签页确认设置 `OMBRE_TRANSPORT=streamable-http`，然后重新部署** |
| 构建失败 / Build failed | Dockerfile 未被识别 / Dockerfile not detected | 确认仓库根目录有 `Dockerfile`（大小写敏感） |
| 服务启动后立刻退出 | `OMBRE_TRANSPORT` 被覆盖为 `stdio` | 检查 Variables 里有没有多余的 `OMBRE_TRANSPORT=stdio`，删掉即可 |
| 重启后记忆丢失 / Data lost on restart | Volume 未挂载 | Volumes 标签页挂载到 `/app/buckets` |

### 使用 Cloudflare Tunnel 或 ngrok 连接 / Connecting via Cloudflare Tunnel or ngrok

> ℹ️ 自 v1.1 起，server.py 在 HTTP 模式下已自动添加 CORS 中间件，无需额外配置。
> Since v1.1, server.py automatically enables CORS middleware in HTTP mode — no extra config needed.

使用隧道连接时，确保以下条件满足：
When connecting via tunnel, ensure:

1. **服务器必须运行在 HTTP 模式** / Server must use HTTP transport
   ```bash
   OMBRE_TRANSPORT=streamable-http python server.py
   ```
   或 Docker：
   ```bash
   docker-compose up -d
   ```

2. **在 Claude.ai 网页版添加 MCP 服务器** / Adding to Claude.ai web
   - 未设置 `OMBRE_AUTH_TOKEN` 时的 URL 格式 / URL format without `OMBRE_AUTH_TOKEN`: `https://<tunnel-subdomain>.trycloudflare.com/mcp`
   - 已设置 `OMBRE_AUTH_TOKEN` 时 / With `OMBRE_AUTH_TOKEN`: `https://<tunnel-subdomain>.trycloudflare.com/mcp?token=<你的token>` / `https://<tunnel-subdomain>.trycloudflare.com/mcp?token=<your-token>`
   - ngrok 未设置 token 时 / ngrok without token: `https://<xxxx>.ngrok-free.app/mcp`
   - ngrok 已设置 token 时 / ngrok with token: `https://<xxxx>.ngrok-free.app/mcp?token=<your-token>`
   - 先访问 `/health` 验证连接 / Verify first: `https://<your-tunnel>/health` should return `{"status":"ok",...}`

3. **已知限制 / Known limitations**
   - Cloudflare Tunnel 免费版有空闲超时（约 10 分钟），系统内置保活 ping 可缓解但不能完全消除
   - Free Cloudflare Tunnel has idle timeout (~10 min); built-in keepalive pings mitigate but can't fully prevent it
   - ngrok 免费版有请求速率限制 / ngrok free tier has rate limits
   - 如果连接仍失败，检查隧道是否正在运行、服务是否以 `streamable-http` 模式启动
   - If connection still fails, verify the tunnel is running and the server started in `streamable-http` mode

| 现象 Symptom | 原因 Cause | 解决 Fix |
|---|---|---|
| 网页版无法连接隧道 URL / Web can't connect to tunnel URL | 服务以 stdio 模式运行 / Server in stdio mode | 设置 `OMBRE_TRANSPORT=streamable-http` 后重启 |
| 网页版无法连接隧道 URL / Web can't connect to tunnel URL | 旧版 server.py 缺少 CORS 头 / Missing CORS headers | 拉取最新代码，CORS 已内置 / Pull latest — CORS is now built-in |
| `/health` 返回 200 但 MCP 连不上 / `/health` 200 but MCP fails | 路径错误 / Wrong path | MCP URL 末尾必须是 `/mcp` 而非 `/` |
| 隧道连接偶尔断开 / Tunnel disconnects intermittently | Cloudflare Tunnel 空闲超时 / Idle timeout | 保活 ping 已内置，若仍断开可缩短隧道超时配置 |

---

### Session Start Hook（自动 boot/breath）

部署后，如果你使用 Claude Code，可以在项目内激活自动浮现 hook：
`.claude/settings.json` 已配置好 `SessionStart` hook，每次新会话或恢复会话时自动触发 `breath`，把最高权重未解决记忆推入上下文。

**仅在远程 HTTP 模式下有效**（`OMBRE_TRANSPORT=streamable-http`）。本地 stdio 模式下 hook 会安静退出，不影响正常使用。

可以通过 `OMBRE_HOOK_URL` 环境变量指定服务器地址（默认 `http://localhost:8000`），或者设置 `OMBRE_HOOK_SKIP=1` 临时禁用。

HTTP hook 还需要配置独立的 `OMBRE_HOOK_TOKEN`。仓库内 SessionStart 脚本会发送 `Authorization: Bearer <token>`；未配置 token 时会明确跳过 hook。token 应使用 `secrets.token_urlsafe(32)` 或等价密码学安全随机源生成，不要放入仓库或 URL。

新窗口的推荐开局是 `boot()`；旧的 `breath` hook 仍可作为轻量自动浮现入口。

If using Claude Code, `.claude/settings.json` configures a `SessionStart` hook that auto-calls `breath` on each new or resumed session, surfacing your highest-weight unresolved memories as context. The recommended current startup call is `boot()`, while the older `breath` hook remains a lightweight surfacing entry point. Only active in remote HTTP mode. Set `OMBRE_HOOK_SKIP=1` to disable temporarily.

The hook also requires the independent `OMBRE_HOOK_TOKEN` and sends it as `Authorization: Bearer <token>`. The repository hook skips calls when the token is unset. Generate the token with `secrets.token_urlsafe(32)` or an equivalent cryptographically secure source; never put it in a URL or commit it.

**Hook rollout order / Hook 发布顺序:** configure `OMBRE_HOOK_TOKEN` first; update every real caller or trusted reverse proxy to send `Authorization: Bearer <OMBRE_HOOK_TOKEN>`; then deploy the backend that enforces the token. After deployment, observe a complete caller cycle and confirm that breath/dream surfacing still succeeds. Do not make the backend mandatory before the caller has been updated.

## 更新 / How to Update

不同部署方式的更新方法。

Different update procedures depending on your deployment method.

### 上游 Docker Hub 预构建镜像用户 / Upstream Docker Hub Pre-built Image (not the current fork)

> This update path tracks the upstream `P0luz` image only; it does not update or rebuild `ALLFORTING/Ombre-Brain` source.
> 这条更新路径只跟踪上游 `P0luz` 镜像，不会更新或构建 `ALLFORTING/Ombre-Brain` 源码。

```bash
# 拉取最新的上游镜像 / Pull the latest upstream image
docker pull p0luz/ombre-brain:latest

# 重启容器（记忆数据在 volume 里，不会丢失）
docker compose -f docker-compose.user.yml down
docker compose -f docker-compose.user.yml up -d
```

> 你的记忆数据挂载在 `./buckets:/data`，pull + restart 不会影响已有数据。
> Your memory data is mounted at `./buckets:/data` — pull + restart won't affect existing data.

### 从源码部署用户 / Source Code Deploy (Docker)

> This path updates a checkout whose `origin` is `https://github.com/ALLFORTING/Ombre-Brain.git`; it is the update path for the current fork.
> 这条路径适用于 `origin` 指向 `https://github.com/ALLFORTING/Ombre-Brain.git` 的 checkout，是当前 fork 的更新方式。

```bash
cd Ombre-Brain

# 拉取最新代码
git pull origin main

# 重新构建并重启
docker compose down
docker compose build
docker compose up -d
```

> `docker compose build` 会重新构建镜像。volume 挂载的记忆数据不受影响。
> `docker compose build` rebuilds the image. Volume-mounted memory data is unaffected.

### 本地 Python 用户 / Local Python (no Docker)

> Keep this checkout's `origin` on `https://github.com/ALLFORTING/Ombre-Brain.git` when updating the current fork.
> 更新当前 fork 时，请确保这个 checkout 的 `origin` 指向 `https://github.com/ALLFORTING/Ombre-Brain.git`。

```bash
cd Ombre-Brain

# 拉取最新代码
git pull origin main

# 更新依赖（如有新增）
python -m pip install -r requirements.txt

# 重启服务
# Ctrl+C 停止旧进程，然后：
python server.py
```

### Render

> A Render service connected to `ALLFORTING/Ombre-Brain` updates the current fork; a service connected to `P0luz/Ombre-Brain` remains an upstream deployment.
> 连接 `ALLFORTING/Ombre-Brain` 的 Render 服务更新当前 fork；连接 `P0luz/Ombre-Brain` 的服务仍是上游部署。

Render 连接了你的 GitHub 仓库，**自动部署**：

1. 如果你 Fork 了仓库 → 在 GitHub 上同步上游更新（Sync fork），Render 会自动重新部署
2. 或者手动：Render Dashboard → 你的服务 → **Manual Deploy** → **Deploy latest commit**

> 持久化磁盘（`/opt/render/project/src/buckets`）上的记忆数据在重新部署时保留。
> Persistent disk data at `/opt/render/project/src/buckets` is preserved across deploys.

### Zeabur

> Likewise, the Zeabur project must be connected to `ALLFORTING/Ombre-Brain` to deploy this fork; an upstream-connected project remains upstream-only.
> 同样，Zeabur 项目必须连接 `ALLFORTING/Ombre-Brain` 才会部署本 fork；连接上游仓库的项目仍只部署上游版本。

Zeabur 也连接了你的 GitHub 仓库：

1. 在 GitHub 上同步 Fork 的最新代码 → Zeabur 自动触发重新构建部署
2. 或者手动：Zeabur Dashboard → 你的服务 → **Redeploy**

> Volume 挂载在 `/app/buckets`，重新部署时数据保留。
> Volume mounted at `/app/buckets` — data persists across redeploys.

### VPS / 自有服务器 / Self-hosted VPS

> For the current fork, use a checkout whose `origin` is `https://github.com/ALLFORTING/Ombre-Brain.git`.
> 运行当前 fork 时，请使用 `origin` 指向 `https://github.com/ALLFORTING/Ombre-Brain.git` 的 checkout。

```bash
cd Ombre-Brain

# 拉取最新代码
git pull origin main

# 方式 A：Docker 部署
docker compose down
docker compose build
docker compose up -d

# 方式 B：直接 Python 运行
python -m pip install -r requirements.txt
# 重启你的进程管理器（systemd / supervisord / pm2 等）
sudo systemctl restart ombre-brain   # 示例
```

> **通用注意事项 / General notes:**
> - 更新不会影响你的记忆数据（存在 volume 或 buckets 目录里）
> - 如果 `requirements.txt` 有变化，Docker 用户重新 build 即可自动处理；非 Docker 用户需手动 `pip install -r requirements.txt`
> - 更新后访问 `/health` 验证服务正常
> - Updates never affect your memory data (stored in volumes or buckets directory)
> - If `requirements.txt` changed, Docker rebuild handles it automatically; non-Docker users need `pip install -r requirements.txt`
> - After updating, visit `/health` to verify the service is running

## 测试 / Testing

测试套件覆盖规格书所有场景（场景 01–11），以及 B-01 至 B-10 全部 bug 修复的回归测试。

The test suite covers all spec scenarios (01–11) and regression tests for every bug fix (B-01 to B-10).

### 快速运行 / Quick Start

```bash
pip install pytest pytest-asyncio
pytest tests/                          # 全部测试
pytest tests/unit/                     # 单元测试
pytest tests/integration/             # 集成测试（场景全流程）
pytest tests/regression/              # 回归测试（B-01..B-10）
pytest tests/ -k "B01"               # 单个回归测试
pytest tests/ -v                       # 详细输出
```

### 测试层级 / Test Layers

| 目录 Directory | 内容 Contents |
|---|---|
| `tests/unit/` | 单独测试 calculate_score、topic_score、时间得分、CRUD 等核心函数 |
| `tests/integration/` | 场景全流程：冷启动、hold、search、trace、decay、feel 等 11 个场景 |
| `tests/regression/` | 每个 bug（B-01 至 B-10）独立回归测试，含边界条件 |

### 回归测试覆盖 / Regression Coverage

| 文件 | Bug | 核心断言 |
|---|---|---|
| `test_issue_B01.py` | resolved 桶不再自动归档 | `update(resolved=True)` 后桶留在 `dynamic/`，搜索仍可命中，得分 ×0.05 |
| `test_issue_B03.py` | float activation_count 不被 int() 截断 | 1.3 > 1.0 得分，`_time_ripple` 写入 0.3 增量 |
| `test_issue_B04.py` | create() 初始 activation_count=0 | 新建桶满足冷启动条件，touch() 后变 1 |
| `test_issue_B05.py` | 时间衰减系数 0.02（原 0.1）| 30天 ≈ 0.549，非旧值 0.049 |
| `test_issue_B06.py` | w_time 默认 1.5（原 2.5）| `BucketManager.w_time == 1.5` |
| `test_issue_B07.py` | content_weight 默认 1.0（原 3.0）| 名字完全匹配得分 > 内容模糊匹配 |
| `test_issue_B08.py` | auto_resolve 同轮应用降权因子 | stale meta 修复后 score ×0.05 立即生效 |
| `test_issue_B09.py` | hold() 保留用户传入的 valence/arousal | 用户值优先于 analyze() 结果 |
| `test_issue_B10.py` | feel 桶 domain=[] 不被填充 | feel 桶保持 `[]`；dynamic 桶正确填 `["未分类"]` |

> **测试隔离**：所有测试运行在 `tmp_path` 临时目录，绝不触碰真实记忆数据。
> **Test isolation**: All tests run in `tmp_path` — your real memory data is never touched.

---

## License

MIT

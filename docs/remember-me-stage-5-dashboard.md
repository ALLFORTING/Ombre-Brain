# Remember-Me Stage 5A: Dashboard 信息架构与只读图片资产页

## 原 Dashboard 问题

Stage 5A 之前，Dashboard 是一个 `dashboard.html` 单页应用，使用内联 CSS 和
Vanilla JavaScript，通过客户端 tab 切换页面，没有前端构建步骤或路由框架。

原“记忆桶”接口调用 `BucketManager.list_all(include_archive=True)`，把普通记忆桶
和归档桶放在同一个数组中。前端还使用 `type === "archived" || score < 0.3`
判断“归档”，导致低分普通记忆也可能被误归类，用户无法稳定地区分记忆与对话归档。

Dashboard 认证使用现有的 `ombre_session` HttpOnly Cookie。登录密码来自
`OMBRE_DASHBOARD_PASSWORD` 或持久数据目录中的 Dashboard 密码哈希文件。

## 新信息架构

OB Dashboard 现在提供三个明确且互相独立的入口：

- **记忆桶**：仅展示 active permanent、dynamic 和 feel 数据。
- **归档对话**：仅展示 `type=archived` 且 `domain` 包含 `session` 的会话归档。
- **图片资产**：仅展示 Remember-Me 已清理、持久保存且 `kind=image` 的资产。

归档分类不再依赖衰减分数。当前数据仍由现有 BucketManager 管理，但展示层使用
`type + domain` 的稳定标记分流。后续如果归档量显著增长，可为会话归档增加独立的
索引或查询层，不需要再改变页面分类语义。

## Stage 5A 范围

图片资产页当前支持：

- 有界分页列表；
- 标题、描述、标签和原文件名的关键词搜索；
- 单标签筛选；
- 服务端生成的受保护缩略图；
- 标题、描述摘要、标签、MIME、尺寸、存储大小、创建和更新时间；
- 点击进入较大图片与完整安全元数据详情；
- 加载、空图片库、搜索无结果、请求错误和图片加载失败状态。

本阶段不支持：

- Dashboard 上传；
- 标题、描述或标签编辑；
- 删除和批量操作；
- Dashboard 向量搜索；
- 自动 reindex；
- 独立 Remember-Me Dashboard。

## 可复用资产架构

图片能力不依赖记忆桶或归档对话：

- `asset_dashboard.py` 是只读 Web 适配层，封装分页校验、安全字段投影、图片验证和
  缩略图生成。
- `AssetStore.search/get/resolve_file` 仍是唯一资产数据与文件解析核心。
- `/api/assets`、`/api/assets/{asset_id}`、缩略图和图片接口组成稳定的资产 Web API。
- `dashboard_assets.js` 与 `dashboard_assets.css` 是独立的 Vanilla JavaScript
  图片列表/详情组件，只依赖该 API 契约和可注入的认证 fetch 函数。
- OB Dashboard 只是为组件提供一个入口和现有 Cookie 认证适配。

未来独立 Remember-Me Dashboard 可以复用同一服务层、API 和组件，只增加轻量外壳。
独立版导航计划为：

- 图片库
- 上传
- 设置

OB 集成版和独立版不应复制两套资产查询、缩略图或列表/详情实现。

## 只读 API

所有资产数据接口均要求现有 Dashboard 认证：

- `GET /api/assets?q=&tag=&limit=&offset=`
- `GET /api/assets/{asset_id}`
- `GET /api/assets/{asset_id}/thumbnail`
- `GET|HEAD /api/assets/{asset_id}/image`

列表限制为每页 1 到 50 项。关键词与标签过滤复用 Stage 2 的
`AssetStore.search`，并固定 `kind=image`。

列表和详情响应不包含：

- `stored_relpath` 或服务器绝对路径；
- source/stored SHA-256；
- 图片字节或 base64；
- 上传或下载 token；
- EXIF、GPS 或原始上传元数据。

## 鉴权与读取边界

- 资产 JSON、缩略图和大图接口均调用现有 `_require_auth`。
- 图片只通过数据库登记的 `asset_id` 查询。
- `AssetStore.resolve_file` 验证解析后的路径仍位于持久资产目录中。
- 仅允许 PNG 和 JPEG，且读取前重新校验实际格式、文件大小、像素数和数据库尺寸。
- 缩略图仅从隐私清理后的持久副本生成，不访问上传临时文件。
- 图片响应设置 `X-Content-Type-Options: nosniff` 和 `Cache-Control: private, no-store`。
- 错误响应使用安全错误码，不返回服务器路径或底层异常正文。

组件脚本和样式是无用户数据的静态资源。认证抽象通过传入 `fetcher` 实现：OB 使用
现有 `authFetch`，独立 Remember-Me 外壳以后可替换为自己的等价认证适配器。

## 后续建议

### Stage 5B

- 元数据编辑；
- 明确确认后的单资产删除；
- Dashboard 上传并复用现有清理、去重和持久化流程；
- 必要的审计记录与并发保护；
- 保持 MCP 工具和 Dashboard 共用 AssetStore 业务规则。

### Stage 5C

- 建立独立 Remember-Me Dashboard 轻量外壳；
- 直接复用 Stage 5A 的 API、认证抽象和图片组件；
- 导航仅保留“图片库 / 上传 / 设置”；
- OB Dashboard 继续作为集成入口，不复制资产管理代码。

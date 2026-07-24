# Remember-Me Stage 5: Dashboard 资产管理

## 原 Dashboard 问题

Ombre Brain Dashboard 是一个无前端构建步骤的单页应用。Stage 5A 之前，普通记忆桶与归档对话共用列表，图片资产也没有独立管理入口。Stage 5A 将导航稳定拆为“记忆桶 / 归档对话 / 图片资产”，归档判定使用 `type=archived` 且 `domain` 包含 `session`，不再依赖衰减分数。

## 信息架构

- **记忆桶**：active permanent、dynamic 与 feel 数据。
- **归档对话**：会话归档摘要。
- **图片资产**：Remember-Me 清理并持久化的 `kind=image` 资产。
- **导入**：仍只处理历史对话和文本记忆，不承担图片上传。

## Stage 5A

只读图片库提供分页、标题/描述/标签/文件名关键词搜索、标签筛选、受保护缩略图、完整元数据详情和较大图片查看。列表 JSON 不携带图片 base64，列表也不会一次加载全部原图。

## Stage 5B

图片资产页新增：

- PNG/JPEG 拖拽或文件选择上传、预览、文件信息和可选标题/描述/标签；
- `multipart/form-data` 流式上传，服务端硬限制 10 MiB，最大 20,000,000 像素；
- 内容寻址、隐私清理、重编码、去重、持久化与资产向量刷新；
- 详情内标题、描述和标签编辑；
- 明确二次确认后的单张图片永久删除；
- 上传、编辑、删除的加载状态、中文错误与 toast 反馈。

图片不会被静默压缩、缩放、转换或替换。Stage-0 诊断探针仍保持 2 MiB 限制。

## Web API

所有接口均位于现有 Dashboard 认证边界：

- `GET /api/assets?q=&tag=&limit=&offset=`
- `POST /api/assets`
- `GET /api/assets/{asset_id}`
- `PATCH /api/assets/{asset_id}`
- `DELETE /api/assets/{asset_id}`
- `GET /api/assets/{asset_id}/thumbnail`
- `GET|HEAD /api/assets/{asset_id}/image`

写请求要求有效的 HttpOnly Dashboard 会话、会话绑定的 `X-Ombre-CSRF`，以及与当前 Dashboard 精确匹配的 `Origin`。上传请求体限制为文件上限加独立 multipart 开销，文件流入 `AssetStore` 控制的临时目录。

## 安全边界

- 仅接受 PNG 和 JPEG，并校验声明 MIME、实际格式、文件大小和像素数一致。
- 仅读取数据库登记且解析后仍位于 assets 目录内的清理后文件。
- 响应不包含路径、SHA-256、EXIF、GPS、Token、Cookie、签名 URL、图片 base64 或内部异常。
- 元数据编辑只允许标题、描述和标签，并复用 `AssetStore` 的长度、类型和标签数量限制。
- 删除先把合法持久文件原子移动到隔离路径，再事务删除资产主记录；标签与向量通过外键级联删除。数据库失败时恢复原文件。提交后清理隔离文件失败会返回 `cleanup_pending`，资产仍保持逻辑删除且不可访问。

## 可复用架构

- `AssetStore`：持久化、清理、去重、元数据事务与一致性删除。
- `AssetDashboardService`：分页、安全字段投影、流式 multipart、图片验证、缩略图、编辑与删除适配。
- `dashboard_assets.js` / `dashboard_assets.css`：不依赖记忆桶、归档对话、Breath 或 OB 页面状态的图片库组件。
- OB Dashboard 只提供导航外壳、Cookie 会话和 `fetcher` 认证适配。

未来独立 Remember-Me Dashboard 可直接复用同一服务、API 和组件，只增加“图片库 / 上传 / 设置”轻量外壳，不复制资产管理代码。

## 尚未完成

- 批量编辑和批量删除；
- 恢复站或延迟删除；
- Dashboard 向量搜索；
- 独立 Remember-Me Dashboard 外壳；
- 面向多用户的独立权限模型与审计日志。

Stage 5C 建议先抽取 Dashboard 外壳与认证适配接口，再实现独立版导航，保持现有 OB 路由和 MCP 工具向后兼容。
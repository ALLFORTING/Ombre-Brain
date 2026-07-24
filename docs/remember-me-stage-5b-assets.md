# Remember-Me Stage 5B: 图片写操作

## 上传流程

Dashboard 的“图片资产”页面通过 `POST /api/assets` 发送 `multipart/form-data`。服务端流式写入 `AssetStore.temp_dir`，计算 SHA-256，仅允许 PNG/JPEG，并复用现有图片解码、隐私清理、重编码、内容寻址和去重流程。成功后写入可选标题、描述和标签，并刷新资产向量。向量服务不可用不会回滚已经安全持久化的资产。

正式 RM 图片上限为 10 MiB 原始字节和 20,000,000 解码像素。multipart 开销另设有限余量。Stage-0 诊断探针仍为 2 MiB。

## 元数据编辑

`PATCH /api/assets/{asset_id}` 只接受 `title`、`description`、`tags`。输入使用既有规范化、长度和标签数量规则，不能修改文件名、MIME、尺寸、路径、图片字节或创建时间。成功后刷新语义向量。

## 永久删除

`DELETE /api/assets/{asset_id}` 需要详情页中的明确二次确认。删除覆盖资产主记录、标签、向量和清理后持久文件。

一致性策略：

1. 从数据库读取登记路径并验证其位于 assets 根目录。
2. 原子移动到受控隔离路径。
3. 在事务中删除资产记录，标签和向量通过外键级联清理。
4. 数据库失败时回滚并恢复原文件。
5. 提交后删除隔离文件；若最终文件清理失败，返回 `cleanup_pending`，但逻辑资产已删除且所有读取路由返回 404。

## 写操作保护

写接口同时要求：

- 有效 Dashboard HttpOnly 会话 Cookie；
- 会话内随机 CSRF Token，通过 `X-Ombre-CSRF` 提交；
- `Origin` 与当前 Dashboard 精确同源。

错误只返回稳定错误码，不包含绝对路径、内部堆栈、图片字节、哈希、Token 或私人元数据。

## 共享边界

Stage 5B 继续扩展 `AssetStore`、`AssetDashboardService` 和共享 Vanilla JavaScript 组件。它们不依赖记忆桶、归档对话或 Breath。未来独立 Remember-Me 外壳可复用列表、搜索、标签、上传、编辑、删除、详情、缩略图和可注入的认证 fetcher。

## 后续

批量操作、恢复站、独立 Remember-Me 外壳和多用户权限不属于 Stage 5B。
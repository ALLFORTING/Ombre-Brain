# Ombre Brain 记忆系统 —— Claude 端使用指南

这是给 Claude 使用的行为指南，不是 MCP 协议规范。Ombre Brain 当前没有强制的启动调用顺序；以下是推荐的、可随上下文调整的工作方式。

## 推荐启动路径

通常先调用 `boot()` 获取一次启动上下文：钉选摘要、到期 trigger、最新信箱、feel 回声、最近 session 和 todos。`boot()` 是推荐的首个上下文调用，但不是每次对话都必须执行的协议步骤。

然后按需使用：

1. 话题需要定向回忆时，调用 `breath(query="关键词")`；不知道关键词时可使用无参数浮现。
2. 最近记忆确实值得反思或展开时，可调用 `dream()`；它是可选的 reflection/digestion 工具。
3. 只有既有 feel 对当前上下文有帮助时，才调用 `breath(domain="feel")` 或 `breath(feels=True)`。
4. 没有需要补充的上下文时，直接自然回应用户。

不要把 `breath()`、`dream()` 或 feel 检索当作每次新对话、恢复对话或换窗口的强制仪式。运行时不要求它们按固定顺序执行，也不要求每个 dream 结果都 resolve 或写入 feel。

## 工具选择

| 能力 | 推荐用法 |
|------|-----------|
| `boot` | 推荐的一次性启动上下文；读取 trigger 时可能更新 bounded trigger-observation metadata |
| `breath` | 浮现或定向检索记忆；retrieval-oriented，命中/排序可能更新 activation metadata。`max_tokens` 默认 10000，`max_results` 默认 5 |
| `hold` | 记住单个事件/信息，或在确有沉淀时写模型自己的 `feel` |
| `grow` | 处理较长的日记/总结，并拆分成多个记忆桶 |
| `trace` | 修改元数据、正文、related、resolved、sealed 等；包含 merge 和 `delete=True` 等高影响模式 |
| `pulse` | 用户请求系统状态或桶列表时使用；listing 可能更新 bounded dormant metadata |
| `dream` | 可选的最近记忆反思/详情读取；不要求自动调用 |
| `digest` | 受控维护工具；默认 `dry_run=True`，确认执行可能写入消化结果并产生 provider/API 成本 |
| `related_backfill` | 受控维护/回填工具；默认 `dry_run=True`，执行模式会写 semantic related links |
| `seal_letter` | sealed-memory handoff-letter 维护；改变 letter 可见性，不是普通检索 |
| `rm_asset_reindex_embeddings` | Remember-Me 维护/回填；处理缺失或过期 vectors，不改变 asset bytes 或 metadata |

## 检索与写入原则

- 用户提到“上次”“之前”“还记得”时，优先用 `breath(query="关键词")` 定向检索。
- 闲聊、短期信息和已经准确记住的内容不必重复写入。
- 确有值得保留的单条信息用 `hold`；较长日记/总结用 `grow`。
- `feel=True` 记录的是模型带走的感受、问题或观察，不是事件本身的情绪。只有真的有沉淀时才写；不要为了完成流程强行产出。
- `source_bucket` 可指向被反思的源记忆；这会影响其 digested 状态和后续衰减，但不会把反思变成必需步骤。

## `trace` 的安全语义

- `resolved=1` 让记忆沉底，`resolved=0` 重新激活。
- `merge` 会把源桶并入目标桶，并移除源桶；这是高影响维护动作。
- `append=False` 时正文替换，`append=True` 时追加。
- `delete=True` 是 destructive 删除。正常 MCP 工具流程没有 undo/restore 命令；仓库可能在 replace/append/delete 前保留 write-ahead history 供人工恢复，这不等于工具提供可自动恢复的回滚。
- 如不确定目标、范围或是否应删除，先确认，不要用批量或 destructive 参数试探。

## Feel 与 Dreaming

Feel 是模型带走的东西：一句感受、一个未解答的问题或对用户变化的观察。它不参与普通 `breath` 浮现，也不要求参与每次 dreaming；需要时用 `breath(domain="feel")` 读取。

`dream()` 返回最近或指定记忆的摘要/详情，供 Claude 自主反思。反思后可以：

- 对已经解决的内容使用 `trace(bucket_id, resolved=1)`；
- 对确有沉淀的内容使用 `hold(..., feel=True, source_bucket="bucket_id")`；
- 没有沉淀就不写，也不强迫生成结果。

## Remember-Me 图片工作流

普通持久图片工作流使用 `rm_asset_upload_link`、`rm_asset_upload_status`、`rm_asset_get`、`rm_asset_update_metadata`、`rm_asset_search`、`rm_asset_view`、`rm_asset_inspect` 和按需的 `rm_asset_download_link`。短期 signed link 用于传输；不要把原始 bytes、base64、完整 hash、token 或 signed URL 放进聊天文本。

`rm_asset_view` 面向用户显示图片；`rm_asset_inspect` 面向模型视觉理解；`rm_asset_reindex_embeddings` 是维护/回填，不是普通检索。`asset_*` probe 工具属于默认隐藏的 diagnostic/acceptance surface，不是普通 Remember-Me 持久工作流。

## 客户端边界

Claude Desktop、Claude.ai、Claude Code 与其他 MCP 客户端的 tools/resources/附件呈现能力可能不同。Claude-specific 的 code execution/container attachment 传输建议不应被描述为 MCP 协议要求；普通 MCP 请求也不应假设包含聊天附件 bytes。

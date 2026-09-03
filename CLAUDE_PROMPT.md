# Ombre Brain 记忆系统 —— Claude 端使用指南

这是给 Claude 使用的行为指南，不是 MCP 协议规范。Ombre Brain 当前没有强制的启动调用顺序；以下是推荐的、可随上下文调整的工作方式。

## 推荐启动路径

通常先调用 `boot()` 获取一次启动上下文：钉选摘要、到期 trigger、最新信箱、feel 回声、最近 session 和 todos。`boot()` 是推荐的首个上下文调用，但不是每次对话都必须执行的协议步骤。

然后按需使用：

1. 话题需要定向回忆时，调用 `breath(query="关键词")`；不知道关键词时可使用无参数浮现。
2. 已知 handoff letter 的 `letter_id` 时，使用 `get_letter(letter_id)` 精确读取；不要为了找一封已知信件枚举整个 mailbox。
3. 最近记忆确实值得反思或展开时，可调用 `dream()`；它是可选的 reflection/digestion 工具。
4. 只有既有 feel 对当前上下文有帮助时，才调用 `breath(domain="feel")` 或 `breath(feels=True)`。
5. 没有需要补充的上下文时，直接自然回应用户。

不要把 `breath()`、`dream()` 或 feel 检索当作每次新对话、恢复对话或换窗口的强制仪式。运行时不要求它们按固定顺序执行，也不要求每个 dream 结果都 resolve 或写入 feel。

## 工具选择

| 能力 | 推荐用法 |
|------|-----------|
| `boot` | 推荐的一次性启动上下文；读取 trigger 时可能更新 bounded trigger-observation metadata |
| `breath` | 浮现或定向检索记忆；retrieval-oriented，命中/排序可能更新 activation metadata。`mailbox=True` 只适合读取最近 N 封信 |
| `get_letter` | 按 `letter_id` 精确读取单封 handoff letter；默认不返回 sealed letter，只有明确需要时才传 `include_sealed=True` |
| `hold` | 记住单个事件/信息，或在确有沉淀时写模型自己的 `feel` |
| `grow` | 处理较长的日记/总结，并拆分成多个记忆桶 |
| `trace` | 修改元数据、正文、related、resolved、sealed 等；包含 merge 和 `delete=True` 等高影响模式 |
| `pulse` | 用户请求系统状态或桶列表时使用；`show_all=True` 默认每页最多 50 个，可用 `limit`/`offset` 继续枚举；listing 可能更新 bounded dormant metadata |
| `dream` | 可选的最近记忆反思/详情读取；不要求自动调用 |
| `digest` | 受控维护工具；默认 `dry_run=True`，确认执行可能写入消化结果并产生 provider/API 成本 |
| `related_backfill` | 受控维护/回填工具；默认 `dry_run=True`，执行模式会写 semantic related links |
| `seal_letter` | sealed-memory handoff-letter 维护；改变 letter 可见性，不是普通检索 |
| `rm_asset_reindex_embeddings` | Remember-Me 维护/回填；处理缺失或过期 vectors，不改变 asset bytes 或 metadata |

## 检索与写入原则

- 用户提到“上次”“之前”“还记得”时，优先用 `breath(query="关键词")` 定向检索。
- 已知 `letter_id` 时，优先 `get_letter(letter_id)`；默认 `include_sealed=False`，只有显式 `include_sealed=True` 时才能读取 sealed letter。
- sealed letter 与真实不存在的 `letter_id` 都返回 not found；这是刻意的存在性隐藏，不应据此断言“这封信不存在”。
- 对用户应表述为：“当前无法读取该 letter；它可能不存在，也可能处于 sealed 状态。”
- 闲聊、短期信息和已经准确记住的内容不必重复写入。
- 确有值得保留的单条信息用 `hold`；较长日记/总结用 `grow`。
- `feel=True` 记录的是模型带走的感受、问题或观察，不是事件本身的情绪。只有真的有沉淀时才写；不要为了完成流程强行产出。
- `source_bucket` 只在 `hold(..., feel=True)` 时生效，用来指向被反思的源记忆；普通 `hold` 不要依赖这个字段产生关联。

## `pulse` 的有界列表

- `pulse(show_all=False)` 保持现有行为：返回所有 pinned/protected 桶，以及非 dormant 动态桶的 Top15；不要用 `limit`/`offset` 期待改变这个 Top15 行为。
- `pulse(show_all=True, limit=50, offset=0)` 按稳定顺序返回可见桶的一个 bounded page。`limit` 最大为 50，`offset` 从 0 开始；根据返回中的总数、当前显示数量和 `还有更多` 判断是否继续下一页。
- `include_archive` 和 `include_sealed` 仍分别控制归档桶和 sealed 桶可见性；分页不会改变 pinned/protected/dormant/sealed 的原有语义。

## `trace` 的安全语义

- `resolved=1` 表示这件事已经处理/可以沉底：降低后续浮现优先级；`resolved=0` 重新激活。它不是 dormant，也不是删除。
- `dormant=1` 表示自动或手动沉底的休眠状态，主要影响列表/浮现；`trace` 修改通常会唤醒它，除非明确传 `dormant=1`。它不是“已解决”。
- `merge` 会把源桶并入目标桶，并移除源桶；这是高影响维护动作。
- `append=False` 时正文替换，`append=True` 时追加。
- 归档后的 session bucket 仍可通过 `trace` 修改：未 sealed 时可以修改或追加正文；sealed 时正文修改受保护。
- `mode` 只有 `summary` 和 `full` 两种值；不要发明其他模式。

### 批量 trace

`bucket_id="id1,id2,id3"` 支持逐桶执行。批量 trace 是非原子的：中途失败不会回滚之前已经成功的桶；返回会按 `[bucket_id]` 分项显示每个结果，必须逐项辨识，不要把整批看成一个成功/失败状态。

### 删除

- `delete=True` 是 destructive 操作。不存在的桶返回“未找到”；pinned/protected/sealed 桶返回受到保护；已经找到且删除执行未完成时返回明确的删除失败。
- 删除前会先把正文写入 `bucket_history.sqlite3` 的 history snapshot；history capture 失败会 fail-closed，桶不会被删除。
- 只有 delete 返回明确成功时，才可以认为删除前 history snapshot 已成功写入。
- 当前没有 MCP undo/restore，也没有 MCP history 读取工具；history 只用于人工恢复。

### importance 与保护

- `pinned` 和 `protected` 是两个不同字段。`protected` 表示内部/system protection 语义；不要把它当成 pinned 的同义词。
- pinned/protected 桶不参与 decay compression、auto-resolve 或 archive；两者的 importance 都锁定为 10。
- 对 pinned/protected 桶传入 importance 时，返回会明确说明 importance 没有被修改，原因是 protection；不要把这个结果描述成 importance 修改成功，也不要尝试解除保护。

### related

- `related` 使用逗号分隔的 bucket IDs。
- 传入的 relation 是追加，不是整体替换；已有 ID 会去重。
- 对存在的目标桶会写入反向 relation；当前只能增加，不能 remove/clear。
- 因此只有在关系明确时才使用 `related`，不要把它当作临时标签或试探性搜索。

### supersedes

`hold(..., supersedes_id="...")` 是显式的原地事实演化。目标不存在、无效、受保护或更新失败时会明确报错，不会静默降级成新建。

## 参数约定

- `resonance` 格式严格为 `"valence,arousal"`，两个值都必须在 0–1 范围内。
- `source_bucket` 只配合 `hold(feel=True)` 使用，见上面的写入原则。
- `dormant` 与 `resolved` 是不同用途：前者是休眠/可见性与衰减路径状态，后者表示事项已处理并降低普通浮现优先级；不要互相替代。
- 如不确定目标、范围或是否应删除，先确认，不要用批量或 destructive 参数试探。

## Topic 命名约定

- 优先复用已经存在的 topic；topic 只是命名约定，不建立 topic registry、topic API 或数据库枚举，也不是 schema 硬限制。
- 使用稳定的层级式格式，例如：`项目/OB`、`项目/HAY`、`项目/TG`、`学习/生化`、`关系/沟通`、`日常/作息`。
- 不要随意制造同义词或临时缩写；同一项目不要同时出现 `项目/OB`、`OB项目`、`项目/线B` 这类重复标签。
- 归档时保持 topic 数量适度，不要为了覆盖所有细节制造大量标签。

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

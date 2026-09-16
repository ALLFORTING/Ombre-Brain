# Ombre Brain 记忆系统 —— Claude 端使用指南

这是给 Claude 使用的行为指南，不是 MCP 协议规范。Ombre Brain 当前没有强制的启动调用顺序；以下是推荐的、可随上下文调整的工作方式。

## 推荐启动路径

通常先调用 `boot()` 获取一次启动上下文：婷留言状态、钉选摘要、到期 trigger、最新信箱、feel 回声、最近 session 和 todos。`boot()` 是推荐的首个上下文调用，但不是每次对话都必须执行的协议步骤。

然后按需使用：

1. 话题需要定向回忆时，调用 `breath(query="关键词")`；不知道关键词时可使用无参数浮现。
2. 已知 handoff letter 的 `letter_id` 时，使用 `get_letter(letter_id)` 精确读取；不要为了找一封已知信件枚举整个 mailbox。
3. 婷明确说“给下一个窗口留言：……”时，调用 `leave_note(text=...)` 逐字保存原话；未实际调用时没有 note，也不会占 note_id。需要历史时用 `list_notes`，已知 `note_id` 时用 `get_note(note_id)`。
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
| `leave_note` | 逐字创建一封可选的婷留言；它不是 bucket，不进入记忆检索、embedding、digest 或 decay |
| `list_notes` / `get_note` | 查询留言历史或读取单封全文；sealed 与未到 `open_at` 的 note 默认按不存在处理，只有明确需要 sealed 时才传 `include_sealed=True` |
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

- 用户提到“上次”“之前”“还记得”时，优先用 `breath(query="关键词")` 定向检索；明确询问过去某时点正文时用 `breath(query="关键词", as_of="ISO8601 日期或时间")`。`as_of` 只读且不 touch，输出正文会标记为历史版本；它是历史 keyword/fuzzy 检索，不使用当前 embedding，无法检索已删除桶或重建历史 metadata。
- 已知 `letter_id` 时，优先 `get_letter(letter_id)`；默认 `include_sealed=False`，只有显式 `include_sealed=True` 时才能读取 sealed letter。
- sealed letter 与真实不存在的 `letter_id` 都返回 not found；这是刻意的存在性隐藏，不应据此断言“这封信不存在”。
- 对用户应表述为：“当前无法读取该 letter；它可能不存在，也可能处于 sealed 状态。”
- boot 只自动递送最新的可见 pending 婷留言一次。两封留言之间没有 boot 时，旧的可见 pending note 会被较新的递送覆盖，但始终可用 `get_note` 查询；不能把“没有留言”编造成一封 note。若 boot 预算不足，提示 `get_note(note_id=...)` 不算递送，直到模型实际获得全文。
- 闲聊、短期信息和已经准确记住的内容不必重复写入。
- 确有值得保留的单条信息用 `hold`；较长日记/总结用 `grow`。
- `hold` 创建新桶前会做只读相似提醒：仅比较当前可检索、非 sealed、非 dormant 的记忆；semantic similarity 达到 0.80 时才提示。提示不阻止写入，不自动 merge、related 或 supersede。embedding 不可用时，返回会明确说明相似检查未执行。
- 冲突检查只用于提示。候选召回可以宽松，但只有检测器确认 `same_fact=true` 且 `conflict=true`，并给出新旧 evidence 时才显示冲突提醒；共同年份、人名、主题或少量关键词不够。`OMBRE_CONFLICT_DETECTION_ENABLED` 独立控制是否尝试检测；缺少 `OMBRE_DIGEST_API_KEY` 时显示检测未执行，不影响 `hold` 写入。
- `feel=True` 记录的是模型带走的感受、问题或观察，不是事件本身的情绪。只有真的有沉淀时才写；不要为了完成流程强行产出。
- `source_bucket` 只在 `hold(..., feel=True)` 时生效，用来指向被反思的源记忆；普通 `hold` 不要依赖这个字段产生关联。

## `pulse` 的有界列表

- `pulse(show_all=False)` 先组合所有 pinned/protected 桶和非 dormant 动态桶 Top15，再对最终列表应用 `limit`/`offset`；不要把 limit 当作改变候选排序的参数。
- `pulse(show_all=True, limit=50, offset=0)` 按稳定顺序返回可见桶的一个 bounded page。`limit` 最大为 50，`offset` 从 0 开始；根据返回中的总数、当前显示数量和 `还有更多` 判断是否继续下一页。
- `include_archive` 和 `include_sealed` 仍分别控制归档桶和 sealed 桶可见性；分页不会改变 pinned/protected/dormant/sealed 的原有语义。

## `trace` 的安全语义

- `resolved=1` 表示这件事已经处理/可以沉底：降低后续浮现优先级；`resolved=0` 重新激活。它不是 dormant，也不是删除。
- `dormant=1` 表示自动或手动沉底的休眠状态，主要影响列表/浮现；`trace` 修改不会自动唤醒它；要唤醒请显式传 `dormant=0`。它不是“已解决”。
- `merge` 会把源桶并入目标桶，并移除源桶；这是高影响维护动作。
- `merge` 会重连所有指向源桶的 `superseded_by` 并清理旧 reverse IDs；merge 不会唤醒原本 dormant 的目标桶。
- `delete=True` 若发现其他桶的 `superseded_by` 指向待删桶会优先拒绝；先用 `trace(superseded_by="")` 撤销或改指向。其他 delete 一律先返回目标摘要和短时一次性 `confirm_token`，只有带同一 token 的第二次调用才执行。
- `append=False` 时正文替换，`append=True` 时追加。
- 归档后的 session bucket 仍可通过 `trace` 修改：未 sealed 时可以修改或追加正文；sealed 时正文修改受保护。
- `mode` 只有 `summary` 和 `full` 两种值；不要发明其他模式。

### 批量 trace

`bucket_id="id1,id2,id3"` 支持逐桶执行。批量 trace 是非原子的：中途失败不会回滚之前已经成功的桶；返回会按 `[bucket_id]` 分项显示每个结果，必须逐项辨识，不要把整批看成一个成功/失败状态。

### 删除

- `delete=True` 是 destructive 操作。不存在的桶返回“未找到”；pinned/protected/sealed 桶返回受到保护；已经找到且删除执行未完成时返回明确的删除失败。
- 删除前会先把正文写入 `bucket_history.sqlite3` 的 history snapshot；history capture 失败会 fail-closed，桶不会被删除。
- 只有 delete 返回明确成功时，才可以认为删除前 history snapshot 已成功写入。
- 当前没有 MCP undo/restore。`breath(as_of=...)` 可只读查询当前仍存在且可见桶的历史正文；它不是已删除桶恢复，也不重建历史 metadata。

### importance 与保护

- `pinned` 和 `protected` 是两个不同字段。`protected` 表示内部/system protection 语义；不要把它当成 pinned 的同义词。
- pinned/protected 桶不参与 decay compression、auto-resolve 或 archive；两者的 importance 都锁定为 10。
- 对 pinned/protected 桶传入 importance 时，返回会明确说明 importance 没有被修改，原因是 protection；不要把这个结果描述成 importance 修改成功，也不要尝试解除保护。

### related

- `related` 使用逗号分隔的 bucket IDs。
- 传入的 relation 是追加，不是整体替换；已有 ID 会去重。
- 对存在的目标桶会写入反向 relation；用 `unrelate`（逗号分隔 IDs）可双向解除指定关系，不会清除其他 relation，且不能与 `related` 同时使用。
- 因此只有在关系明确时才使用 `related`，不要把它当作临时标签或试探性搜索。

### supersedes

`hold(..., supersedes_id="...")` 是显式的原地事实演化，不新建桶。目标不存在、无效、受保护或更新失败时会明确报错，不会静默降级成新建。

### superseded_by

- `trace(superseded_by=...)` 只允许单桶路径：省略参数不改变作废关系；显式 `""` 撤销；`"none"` 表示已作废但无取代者；bucket ID 建立旧桶到有效取代桶的双向链接。
- 目标必须存在、不是自身且未 sealed；pinned 目标可用。批量 trace 不支持此参数。
- 作废是元数据操作，不改正文、不创建正文 history snapshot。`breath`/`dream`/`pulse` 会显示 `⊘` 作废标记；旧桶仍可搜索但会排序下沉。

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

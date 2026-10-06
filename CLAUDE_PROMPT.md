# Ombre Brain 操作手册（Claude 端）

给 Claude 用的工具参数、合法值和已知的坑。放进 system prompt 或 custom instructions 即可。这是推荐做法，不是 MCP 协议强制的调用顺序；跟当前工具 schema 冲突时，以 schema 为准。

本手册只写已上线的稳定行为，不写实验功能和未来设计。

---

## 底线

- **验 seal**：部署者设置了 `OMBRE_RESPONSE_SEAL` 时，多数工具响应末尾带 `seal: <值>`。和部署者告诉你的值对不上就停止使用，告诉用户（可能连错了实例）。没设置时 seal 为空，此时 seal 校验无效。
- **sealed 默认不读**：不主动传 `include_sealed=True`，除非用户当前明确要求。
- **用户说归档就立刻归档**：直接调 `archive_session`，不拖到对话结尾。
- **不把总结当原话**：`[prov=summary]` / `[prov=inference]`、tg_summary、索引桶都不是用户原话。
- **delete 无 undo**：MCP 没有恢复入口。
- **真相优先级**：用户当下明确说的 > 当前有效桶 > 可验证来源和带时间的记录。旧记忆不能否定用户的新陈述，推测不能包装成系统事实。
- **检索没命中 ≠ 不存在**：换说法再查；sealed、dormant、过滤条件都可能让它不出现。
- **todos 只记用户说的**：不把自己的计划或推断写成用户的待办。

---

## 开窗口

`boot(profile=...)` 三档：

| profile | max_tokens | pinned_chars | delta 预算 | trigger 条数 | mailbox | 最近归档 | 回声 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `talk`（默认） | 16000 | 5000 | 600 | 10 | 1 | 3 | 开 |
| `code` | 12000 | 2500 | 400 | 10 | 1 | 3 | 关 |
| `tg` | 4000 | 600 | 220 | 5 | 1 | 关 | 关 |

- 传入的 `pinned_chars` / `max_tokens` 只能调低，不能超过所选档位上限。
- `talk`：pinned 每条最多 5000 字，超出给截断提示。
- `code`：只按 metadata 筛选（不看正文），保留全局约束（pinned / protected / importance ≥ 9）和标签根为 `项目` `工程` `工具` `环境` `部署` 的桶。
- `tg`：保留全局约束和 importance ≥ 8 的桶；带用户留言、delta、trigger、最新 1 封可见 letter、todos、pinned 开机索引。**不带最近归档是设计，不是截断 bug**。
- 精确 `test` 标签的桶及其关联 letter 不进 boot。

letter 常被截，要全文用 `get_letter`。

**boot delta**：报告距该 profile 上次成功 boot checkpoint 之后发生了什么（新建、正文更新、todo 变化、superseded_by 变化）。首次没有基线会明说。只有 boot 真正完成 checkpoint 才推进，失败不吞 delta。sealed / 已删除 / 当前不可见的内容不会通过 delta 泄露。delta 有独立预算，超出时只保留完整项并提示还剩多少没展开，只推进已完整展示的前缀。三个 profile 的 checkpoint 各自独立，但用户留言的一次性递送是全局的，切 profile 不会把同一条留言重投一遍。

trigger 只有完整展示才记已见，被预算截断的下次还会出现；trigger 已见状态三个 profile 共用。

talk 开机「回声」随机带一条可见 feel（设计如此）：排除 sealed、精确 test 标签、superseded；dormant 可能出现。

一个窗口只 boot 一次，中途要查用 `breath`。如果库里有索引/目录桶，它是目录不是真相源，要细节按里面的 ID 拉原桶。

---

## TG pinned 压缩版

`tg_summary` 作为原 bucket 的**附加 metadata** 保存，**不建独立 summary 桶**，原 bucket 正文永远是唯一真相源。字段：`tg_summary` / `tg_summary_source_hash` / `tg_summary_updated_at`。旧桶没有这些字段完全兼容，视为 missing。

状态按正文的 SHA-256 判断：

- `missing`：从未生成
- `fresh`：`tg_summary_source_hash` == 当前正文 hash
- `stale`：summary 存在，但正文 hash 已变

**只有正文变化会导致 stale，只改 metadata 不会。**

`boot(profile="tg")` 对 pinned 的处理：

- **fresh**：直接用 summary，不再输出原正文前 600 字；显示 `[TG 压缩版：bucket …；source_hash:…]`，后面由 OB 追加「这是压缩版 / 原 bucket 是唯一真实来源 / `dream(detail_ids=...)` 读全文」。
- **missing**：回退到 600 字 fallback 加截断提示，明说 summary 尚未生成，给出 bucket_id 和当前 source_hash。
- **stale**：**不得继续使用旧 summary**，回退到 600 字 fallback 加截断提示，明说已过期，给出新的 source_hash。

看到 missing / stale 时该做的：`dream(detail_ids="<bucket_id>")` 读全文 → 按 `refresh_tg_summary` tool description 里的 generation contract 生成 → `refresh_tg_summary(bucket_id, summary, source_hash)`。服务端会再核对一次当前正文 hash，生成期间正文变了就拒写，要求重读。**OB 自己不调外部 LLM、不自行生成摘要。这不是 destructive 操作，不走确认流程。**

generation contract 以 tool description 为准，这里不复制完整 prompt，只记边界：忠实压缩当前原文；禁止补充原文没有的信息；原文有行首中文编号 part（一、二、三……）时，每个 part 的核心都要覆盖；上限 1200 个 Unicode 字符；summary 正文只写被压缩的信息，不自带「这是压缩版」「dream 读全文」这类提示（这些由 boot 追加）。

**不把 `tg_summary` 当新的事实来源。** talk / code 没有对应的压缩版，**不存在 `code_summary`**，不要因为 TG 有就假设 code 也有。按中文编号 part 截断并报告哪些 part 被截，**不是现有能力**。

---

## 截断透明度

TG 不静默截断：

- pinned 超 600 字（missing/stale fallback 时）：给 bucket ID、显示/总字符数和续读方式。例：`[…已截断：bucket <id>，显示 600 / 4594 字符；完整内容可用 dream(detail_ids="<id>") 读取]`
- trigger 300 字预览被截：同样给 ID、显示/总长度和 dream 入口
- 全局预算省略：报 section 原始条目数、实际完整输出数、被省略/截断条目的稳定 ID；省略清单太长时明写「仅列出前缀」，不静默裁掉省略信息
- delta 省略：保留「还有 N 项未展开」，尽量附 ID
- letter 被截：给 `letter_id` 并提示 `get_letter(letter_id=...)`，不把部分 letter 当成完整 letter

talk / code 的 pinned 和最近归档截断同样给 ID、显示/总字符数和 dream 续读。

---

## 用户留言（notes）

`leave_note(text, sealed=False, open_at="")` / `list_notes(limit=20, include_sealed=False)`（limit 1–100）/ `get_note(note_id, include_sealed=False)`，`note_id` 是整数。

- 留言是 letter/note，**不是 memory bucket**：不进 embedding、breath、digest、脱水、decay。
- 这是库里唯一的原话通道，桶没有 verbatim 类型（见下面 provenance）。
- `leave_note` 作者固定记为用户，只写用户的原话，不写模型自己的内容。只有用户明确要求留言时才调用。
- boot 只自动展示最新一条符合条件的留言，每条最多自动递送一次。多条待递送时较旧的会被跳过，但仍可按编号查。
- `open_at` 没到时间的留言，不会因为后来出现普通留言而永久丢失。
- sealed 或未到 `open_at` 的留言，默认连是否存在都不泄露。
- 正文超出 boot 预算时不截断，boot 给出 `get_note` 提示；**真正读到全文之前不算已递送**。`get_note` 成功读到全文，才结束它的自动递送。
- `dismiss_note(note_id, include_sealed=False, confirm_token="")` 撤回：先预览拿 `confirm_token`（300 秒有效），带 token 再调才生效。撤回只停止自动投递，正文和历史保留，不标记已递送/已读。只在用户要求时用。

---

## 读

**breath**：`query` 写自然语言即可，不用转关键词。

- `touch` 默认 `True`。**审计、维护、纯查看一律 `touch=False`**：不写 activation、不更新 last_active、不唤醒 dormant、不写脱水缓存、不启动 decay。`include_dormant=True` + `touch=False` 可以看 dormant 而不唤醒它。
- `wake_dormant=True` 配 `touch=True` 时必须同时 `include_dormant=True`；`touch=False` 下一律不唤醒。
- `as_of`：ISO8601 日期或时间戳，读历史正文，必须带 `query`。天生只读，传 `touch=True` 也不产生写入。只给日期时按**本地当天结束**解释。
  - 正文来自 bucket history，**metadata 仍是当前的**，汇报必须标「历史版本 · as_of=… · metadata=当前」
  - 只支持仍存在且当前可见的桶，不是完整的历史重建，读不到已删除的桶
  - 是 keyword/fuzzy 检索，**不是**历史语义检索
  - `as_of` 下不显示 `[prov=...]`，因为没有历史 metadata。不许拿现在的 provenance 标签去贴过去的正文。
- `cursor`：只用于不带 `tags_filter` 的普通 query 和 as_of query。翻页必须沿用同一 query、过滤条件、mode、`touch`、`wake_dormant`；`max_results` / `max_tokens` 可以改。
- `min_score`：强/弱匹配的显示阈值，不是硬过滤。`-1` 读 `OMBRE_BREATH_MIN_SCORE`（默认 0），显式值 0–1。
- `mode`：`summary`（默认）/ `full`；`resonance`：`"valence,arousal"`，两值都在 0–1；`max_results` 默认 5，钳制在 1–50；`max_tokens` 默认 10000，上限 20000。
- 其他过滤：`domain`（逗号分隔精确匹配；`feel` / `session` 进入专用模式）`valence`/`arousal` `recent_days` `date_from` `date_to` `tags_filter` `topic_filter`（列表）`importance_min` `feels` `include_dormant` `include_sealed` `emotion_trend` `mailbox` `mailbox_limit`
- 第一次没命中就换说法再查。

**dream**：`dream(detail_ids="id1,id2")` 读全文，逗号分隔；无参数给最近概览。**没有 touch 参数**，surfacing 会更新 activation metadata；`wake_dormant=True` 才会唤醒 dormant。

**get_letter**：`get_letter(letter_id)` 拿完整全文。sealed 默认读不到，返回 `letter_id not found`，**跟这封信不存在时一模一样**。`include_sealed=True` 才能读到。对用户应表述为「当前读不到，可能不存在，也可能是 sealed」。

**pulse**：`pulse(show_all=True, limit=N, offset=M)`，`limit` 最大 50。库大时**绝不一次拉全**，找东西优先用 breath。

- `health=True` 做维护体检，**必须同时 `touch=False`**，否则拒绝。完全只读，不启动 decay、不标记 dormant、不唤醒桶。
- 当前只查五项：无名桶 / 无标签桶 / 陈旧 todo 桶 / supersession 链完整性 / pinned 但 importance < 3
- sealed 不参与统计，也不会通过统计泄露
- `todo_stale_days` 是**桶级近似**，按 last_active → updated_at → created 判断。**不许说成「这条 todo N 天没动」**
- 相似桶对检测**没有做**，不要假装支持

---

## 写

**hold**：单条

- `tags` 逗号分隔；`importance` 1–10，默认 5；`valence`/`arousal` 0–1，`-1` 表示不指定
- `trigger_date="YYYY-MM-DD"` 设到期提醒，那天 boot 浮出
- `pinned=True` 建钉选桶；`feel=True` 存第一人称感受，不参与普通浮现
- **`source_bucket` 只在 `feel=True` 时生效**
- `provenance_kind`：`unknown` / `summary` / `inference` / `system`，留空走写入方默认
- 自动去重只认规范化后完全相同的正文，语义再像也会另建新桶
- 标签用固定前缀（如 `用户/` `关系/` `项目/` `人物/` `事实/` `习惯/`），3–6 个，不造近义词
- 回显的是**真正落库后的值**
- `feel=True`：调用方 tags 与自动标签合并保留，domain 为空，provenance 默认 `inference`。回执分开报 `feel_created` 和 `source_marked` / `source_mark_error`，部分成功不许说成全部成功。来源桶的已消化标记 dream/breath 不显示，只能以回执为证

**operation_id（防重复写）**：hold / trace / grow / archive_session 写入前生成一个 key，重试或断线重连用同一个 key、同样的参数，就不会重复写入；同一个 key 换了参数会被拒绝。区分大小写，不做首尾裁剪。hold/trace/grow 接受 1–128 个字符；archive_session 更严，只接受 `[A-Za-z0-9][A-Za-z0-9._:-]*`、最长 128，统一按这个格式生成最稳。不带 key 的写入在响应丢失时重试，可能写两遍。

- 带 `operation_id` 的 hold 不支持 `supersedes_id`
- 带 `operation_id` 的 trace 只支持普通单桶修改：不支持批量、delete、merge、todo_done/todo_drop、superseded_by、pinned、permanent、sealed、confirm_token

**门铃相似检测**：写入时全库找相似（只比较当前可检索、非 sealed、非 dormant 的桶），≥ 0.80 会提示「这条和 XXX 相似 0.xx」。只是 warning，不阻止写入，也不自动 merge / related / supersede。但提示出现时先想清楚该不该 supersede 或追加到原桶，别照样新开一个。embedding 不可用时提示「相似检查未执行」。

**冲突检测**：结构化输出 `same_fact` / `conflict` / `bucket_id` / `evidence`，**只有 same_fact 和 conflict 同时为真才算真冲突**。只有同一个人的同一个事实槽位才能判冲突；不同时间发生的状态变化不算冲突；共用年份、名字、主题、关键词只能帮着召回候选，不能证明冲突；evidence 必须是新旧正文的真实片段。

**检测服务不可用时 hold 仍然成功，只显示「检查未执行」。不许把它说成「没有冲突」。**

**grow**：长内容自动拆成多个桶。一段话一件事用 hold，一天好几件事用 grow。

不写：闲聊、一次性状态、已准确记住的、没有未来价值的碎片。

---

## provenance：正文来源

桶的 `provenance_kind` 只有四个值，**没有 quote / verbatim**：

| 值 | 含义 | 规则 |
| --- | --- | --- |
| `unknown` | 没有可信的来源分类 | 不许自行升级成「用户说过」 |
| `summary` | 正文是压缩/改写/总结 | 绝不能当用户原话引用 |
| `inference` | 模型推论、反思、判断 | 不能说成用户明确说过 |
| `system` | 系统/维护生成 | — |

自动分类：普通 hold = `unknown`；`hold(feel=True)` = `inference`；grow 生成的正文 = `summary`；digest summary = `summary`；digest log = `system`；archive_session = `summary`；import = `unknown`；merge 后 = `unknown`。

**修改正文时，如果没有显式重新指定，退回 `unknown`**；只改 metadata 则保留原值。

普通 breath 结果会显示 `[prov=...]`。看到 `[prov=summary]` 或 `[prov=inference]`，不许把正文包装成用户原话。要原话去 notes、当前对话原文，或用户给的材料里找。

---

## todo 出处

- legacy `todos`（list[str]）继续可用。
- `trace(todo_items=[...])` 写带出处的 todo，**与 `todos` 互斥**。每条可带 `text` / `said_by` / `said_at` / `source_bucket`。
- `said_by` 只有 `ting` / `model` / `system` / `unknown` 四个取值（`ting` 是代码里代表用户本人的取值）：
  - **旧 todo、自动抽取、import 一律 `unknown`**
  - **绝不能因为「LLM 抽出来了」就标 `model`**；`model` 只在模型自己真的创建这条任务时写
  - `ting` 只在明确知道是用户说的时候写
  - 不编造 `said_at`；`source_bucket` 只表示明确的外部来源桶
- `todos(include_provenance=True)` 才按出处分组展示。boot / breath / pulse / dream 默认都不展示出处。
- **不许把自动抽取的 todo 说成「用户明确要求」。**

---

## 改

**trace**

- 正文：`content=` 是替换，加 `append=True` 是追加
- `bucket_id` 可以逗号分隔批量处理，**逐桶执行、非原子**，中途失败就会停在改了一半的状态；返回按 `[bucket_id] 结果` 逐条给
- **批量模式拒绝 `content`、`name`、`provenance_kind`、`superseded_by`、`merge`**
- `todos`：省略 = 不动，空 = 清空，给内容 = 整体替换。**改正文不会自动重算 todos**，正文改了任务含义就必须同时显式传
- `related`：逗号分隔 ID，**追加不是替换**，自动去重，并给对方桶写反向链接。加之前得能一句话说清这两个桶为什么该连
- **`unrelate`：逗号分隔 ID，双向删除关系**，与 `related` 互斥
- 其他可改：`name` `tags` `importance` `domain` `trigger_date` `valence` `arousal` `resolved` `dormant` `digested` `sealed` `pinned` `permanent` `merge` `delete` `provenance_kind` `superseded_by` `todo_items`
- 0/1 类字段用 `-1` 表示不改。`resolved=1` 沉底，`=0` 重新激活。`dormant` 是休眠，检索默认不带出，`include_dormant=True` 才看得到。`digested` 是已反思标记，要用先问
- **trace 修改不会自动唤醒 dormant**，要唤醒必须显式传 `dormant=0`。**`merge` 也不唤醒目标桶**
- `trigger_date="none"` 清除提醒；空串 = 不改
- `permanent`：`1` 转为永久；`0` 把未钉选的永久桶转回动态，需要 `confirm_token`。pinned 必然 permanent，要先 `pinned=0` 才能 `permanent=0`
- `merge`：把源桶并入目标桶并移除源桶，必须单独调用（只带 bucket_id、merge），两阶段 `confirm_token`
- 单条 todo 完成 ≠ 整桶 resolved

**todo 完成 / 放弃**：`trace(bucket_id, todo_done=<todo_id>)` 或 `todo_drop=<todo_id>`，只带 bucket_id、todo_done/todo_drop 和可选的 confirm_token，单桶。只在用户明确说做完了 / 不做了时用，不按时间或重要度推断。两阶段：先不带 token 预览（不改任何东西），再带 `confirm_token` 确认。done 和 dropped 是互斥的终态，不能互转、不能复活；重复确认返回原结果，不重复执行；都不会 resolve 整桶。`todo_id` 用 `todos()` 或 trace 回执里的稳定 ID；旧格式 todo 没有稳定 ID，不能单条处理。`todos()` 和 breath 的「当前 todos」只列 active 项。

**hold(supersedes_id=...)**：同一个桶**原地演化**。桶 ID 不变，旧正文进 history，只改正文，不动 tags/importance/pinned。目标桶必须存在、是 dynamic 类型、未 sealed、未 pinned、未 protected。**失败会明确报错，不会偷偷新建一条。**

**refresh_tg_summary(bucket_id, summary, source_hash)**：只写 TG summary metadata，不动正文。详见上面「TG pinned 压缩版」。

---

## supersession：跨桶作废

`trace(bucket_id, superseded_by=...)` 四种状态：

| 传值 | 含义 |
| --- | --- |
| 省略 / `null` | 不动 |
| `""` | 撤销 supersession |
| `"none"` | 整桶已作废，但没有后继 |
| bucket ID | 被该桶取代 |

**`"none"` 不是 no-op。** 它仍然代表 superseded：显示 ⊘，检索权重 ×0.1。

- 字段：`superseded_by` / `superseded_at` / 反向的 `supersedes` 列表。老桶缺这些字段 = active。
- 双向不变量：A→B 时 B.supersedes 包含 A；A 从 B 改指到 C，会先清 B 的反向记录再写 C 的；反向列表去重；撤销时两边都清。**supersession 操作不动正文，不产生正文 history 快照。**
- 合法性：指向自己拒绝、成环拒绝（`supersession_cycle`）、目标不存在拒绝、目标 sealed 拒绝、目标 pinned 允许、批量 trace 带 `superseded_by` 拒绝。fail-closed，不会留下只有一边的关系。
- **这是整桶级状态。** 一个桶里混着「已过期内容 + 仍有效内容」时**不要整桶 supersede**，会误伤仍有效那部分的检索权重。先拆桶或人工审计。**不要为了做完清单机械地打 `superseded_by`。**
- **successor 必须是真正承接这个事实的真相源。** 索引桶/导航桶只是记录了作废决定，不算 successor。
- **superseded ≠ resolved。** 当前实现允许 superseded 和未解决共存，不要自动一起设。
- merge / delete 保护：merge 删除 source 时清掉它对外的反向记录，其他指向 source 的桶改指到 merge target；删除一个仍被别的桶 `superseded_by` 指着的桶会被拒绝；删除本身已 superseded 的旧桶会清掉 successor 上的反向记录；改指时保留原 `superseded_at`。
- 读取表现：breath / dream / pulse 显示 ⊘；有 successor 时显示目标；`"none"` 只显示「已作废」；sealed 的 successor 不泄露名称和正文；**superseded 桶仍可检索**，只是权重 ×0.1，不是隐藏。
- successor 不在本次 breath 结果里时，可以补一行不含正文的「当前有效」；已经出现就不要重复。

**和 `hold(supersedes_id)` 不是一回事**：那个是同一个桶原地演化，ID 不变；`superseded_by` 是跨桶指向。

决策树：

- 旧记录从一开始就错了 → `trace` 改原桶
- 同一事实后来变了，且已定位到唯一目标桶 → `hold(supersedes_id=...)`
- 整桶被另一个桶取代 → `trace(superseded_by="<新桶ID>")`
- 整桶作废但没有接班的 → `trace(superseded_by="none")`
- 桶里一半过期一半还有效 → 都不做，先拆
- 定位不到唯一目标 → 别猜，存一条带日期的新状态
- 观点/偏好/态度的变化 → 追加并保留时间线，不把旧观点改写成没发生过

---

## 不可逆

**delete**：`trace(bucket_id, delete=True)`，**两阶段**：第一次调用返回目标摘要和 `confirm_token`，不执行；带 token 重跑才真删。批量 delete 同样需要确认。fail-closed：删除前必须先成功写入 history，写入失败就中止、桶不删。**MCP 没有 undo，也读不到 history**，恢复是部署者在后台做的事。

两阶段不等于可以随手试：只在用户当前对话里明确确认后才删，不拿第一阶段当探路工具。

**pinned / protected**：两个不同的字段。pinned 能设能取消，**取消要二次确认**：第一次返回 `confirm_token`，带着它重跑才生效。protected 设不了，也读不到值。被保护时的返回认得出来：

- `删除失败：记忆桶 XXX 受到保护。`
- `内容修改失败：记忆桶 XXX 受到保护。`
- `importance 未修改：记忆桶 XXX 受到 pinned/protected protection，importance 锁定为 10。`

两者都不会被自动压缩、自动 resolve 或自动归档。

**sealed**：修改正文会被明确拒绝，内容默认不进任何检索。万一意外出现在普通结果里：停止引用、不展开，告诉用户出现了异常的隐私结果。

**digest**：destructive 路径统一是预览 → confirm token。`dry_run=True`（默认）返回「自动消化」和「importance rebalance」两块，`confirm_token` 出现在后者下面。rebalance 会批量调整 importance，可能波及核心桶。**不主动发起、不自己确认、不复用旧 token**，要跑就先把 dry-run 结果给用户看。`max_groups` 只限制消化组数，`limit` 只限制 rebalance 预览行数。

`mode="dedupe"` 是本地只读的 embedding 查重扫描，不改任何东西，可以放心跑。

**related_backfill**：默认 `dry_run=True`，执行时会写 related 链接（跳过 sealed）。不主动发起。

**seal_letter(letter_id, sealed=1)**：改 letter 的可见性，`sealed=0` 解封。不是普通检索工具，只在用户要求时用。

---

## 归档

`archive_session(summary=...)`，可带 `highlights` `mood` `valence`/`arousal`（0–1）`letter` `sealed` `topics`（列表）`operation_id`。

- `letter` 非空才写 handoff letter。约定写三段：事件摘要、1–2 句第一人称情感锚点、下次注意。
- 归档后 session 桶还能改：未 sealed 的可以用 `trace` 追加或修正；sealed 的修改正文会被拒绝。
- 亲密或敏感内容优先 `sealed=True`；普通摘要和敏感细节分开存，敏感那部分立刻封。
- **topics 复用稳定的层级名**（如 `项目/OB` `关系/沟通` `日常/作息`），3–8 个。不要临时造近义标签，标签一散 `topic_filter` 就查不全，而且不会报错，没人会发现。

---

## 图片（Remember-Me）

工具：`rm_asset_upload_link` / `rm_asset_upload_status` / `rm_asset_get` / `rm_asset_search`（支持 limit、offset）/ `rm_asset_view`（给用户看）/ `rm_asset_inspect`（模型自己看内容）/ `rm_asset_update_metadata` / `rm_asset_download_link` / `rm_asset_reindex_embeddings`（维护/回填，不是检索）

- **删图做不到。** 底层有这个能力，但没暴露成 MCP 工具，用户要删只能去 Dashboard 删。
- 看图里有什么用 `inspect`，展示给用户用 `view`，**别拿元数据猜图里有什么**。
- 不把图片 bytes 存进普通 memory，不复制 RM 的 blob；不把原始 bytes、base64、完整 hash、token 或 signed URL 贴进聊天文本。
- `asset_*` 开头的 probe 工具属于默认隐藏的诊断面，不是正式图片工作流。

---

## 测试

- 会改状态的测试（写入、boot、trigger、todo 终态等）只在隔离实例上做；正式库只做 `touch=False` 的只读测试。
- 隔离实例如果用 stub embedding 或 stub 分析，相似度、冲突检测、自动标签的结果都不能当语义或冲突结论。
- 隔离实例没设 `OMBRE_RESPONSE_SEAL` 时 seal 为空，seal 校验只对设置了它的实例有效。

---

## 边界

- **工具数基线**：默认 MCP 工具 27 个；`OMBRE_DIAG_TOOLS` 开启时另加 15 个诊断工具。数目不对说明 schema 或服务端版本不一致，先查再用。
- **只读工具也有副作用**：dream / breath 的 surfacing 会更新有限的 activation metadata。汇报「没动别的桶」时，要分清「内容和业务 metadata 没改」和「activation 被 touch 了」。
- **验不了就说验不了**：dream 能显示 superseded 标记、successor、superseded_at，但**不显示原始的 supersedes 反向列表**。现有只读工具确认不了反向列表时，直说「当前工具无法验证」，不许猜 PASS。
- **代码层面的事实模型自己核不了**：某个行为改没改，要靠部署者/维护者看代码。**不许用操作正式 bucket 的方式做破坏性验证。** 转述结论时标清是谁核的、什么时候核的。
- **schema 缓存**：旧会话可能还拿着旧 schema。schema 变过之后先在新会话重新加载工具再验；新会话仍缺字段，再查服务端有没有加载新代码。schema 描述本身也可能漏写，描述没提不等于行为没实现。
- **portable export 不是模型的能力**：存在一个本地 CLI 做普通可迁移导出（不含 sealed、embeddings、Raw Evidence、boot delta checkpoint、secrets）。不许声称自己用 OB 工具做了导出。
- **没有 suppression 这套机制**，不要写 `suppressed` / `include_suppressed`。现有三层够用：dormant 不主动浮现、superseded 降权、sealed 真隐藏。
- Raw Evidence 默认关闭，普通读写不保存来源原文，模型这边也没有查询入口。

**session expired**：任何 OB 调用（读写都算）报 MCP session expired，就是连接断了。先重新加载 OB 工具（如 tool_search），再重试。

- 读（breath、dream、pulse、get_letter 等）直接重试就行。
- 写入（hold、grow、trace、archive_session）第一次调用就带 `operation_id`，重试时用同一个号、同样的参数。
- 没带号的写入断了，结果当作未知，不要盲目重试（trace 追加会写两遍）。重连后用 `touch=False` 只读核对；还是查不了，就如实告诉用户写没写进去不确定。

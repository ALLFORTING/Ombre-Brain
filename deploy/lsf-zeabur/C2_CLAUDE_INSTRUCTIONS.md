# L-SF C2 分批真实 Claude 验收

仅启用小白鼠测试 connector。C1 已有连接、写入重放、跨重启读回结果保留；
本批不重做 C1、不换卷/token。服务更新重启后重新连接，新 cursor 从第一页开始。
下列结果仍须真实 Claude 实测，不能以本地测试代替。

管理员从 `/data/lsf/.c2-v1.json` 的 `today` 取得 D；PREV 为 D−1。
manifest 的日期是首次初始化日期，后续重启不改变。若现在的上海日期不再是 D，
recent_days=0/1 用例标“日期已变化，待单独补验”，不要改样例时间或传 time-travel。
历史创建=2026-10-01T00:00:00+08:00、替换=2026-10-02T00:00:00+08:00。
公开清单不向 Claude 提供 sealed 样例的名字/ID，以真实返回核对隐私。

## 每批共同指令

只调用真实 breath；除 mailbox 外，每次显式 touch=false。
mailbox 不传 touch=false，因为该 selector 拒绝非默认 bucket 参数。
不调用 boot/dream/hold/grow/trace/archive_session 或写入/修复/确认工具。
记录实际工具参数、bucket/letter ID、显示类型、total/displayed/omitted/remaining、时间。
cursor 只用于本聊天的真实下一次调用，不抄入报告，不编造工具返回。
预期冲突必须显示实际 mode、参数、原因及允许参数，不能删参数重试后冒充原例成功。
每批完成后停止，供婷审阅；真实失败停止依赖用例，不自动修业务或配置。

## 批1：ordinary_query、标签/日期/importance/domain 交集

统一 touch=false，max_results=50，max_tokens=10000：

1. query="C2-NAME-EXACT", domain="c2-lex", mode="full"。
   完整名称 c20000000001 优先于近似名称02；其他 fuzzy/semantic 命中另记。
2. query="C2-TAG-EXACT", domain="c2-lex", mode="full"：03/05为tag命中。
3. query="C2-BODY-NEEDLE", domain="c2-lex", mode="full"：04正文命中。
4. 第3项加 importance_min=7：04排除，但其他真实语义匹配不要求为零。
5. tags_filter=["C2-TAG-EXACT"], domain="c2-lex"：精确集合03/05。
6. 第5项加 importance_min=7,date_from="D",date_to="D"：只03。
7. 第5项加 recent_days=0：服务当天仍为D时只03。
8. query="C2-NAME-EXACT",domain="c2-missing"：空，不回全库。

冲突：query="C2-NAME-EXACT",domain="c2-lex",arousal=0；
同query/domain加 mode="full",valence=0。均touch=false。
min_score是strong/weak展示阈值，弱项不因此从total消失；不是硬过滤。
不同单位向量只用于synthetic路径，不代表真实provider相关性质量。

## 批2：六个当前桶 selector 的正常/冲突

全部 touch=false；未指定的参数保持默认。

| selector | 成功调用参数 | 预期冲突参数 |
|---|---|---|
| session | domain="session",topic_filter=["C2-TOPIC"],tags_filter=["C2-SESSION"]；再加query="C2SESSION",mode="full" | domain="session",mode="full"且无query；domain="session,c2-lex" |
| feel | feels=true,tags_filter=["C2-FEEL"]；再加query="C2FEEL",mode="full" | feels=true,query="C2FEEL",mode="full"但无tags_filter；feels=true,include_dormant=true |
| resonance | domain="c2-lex",tags_filter=["C2-ZERO"],resonance="0,0"，顺序03→05 | 同参数加mode="full" |
| tags_only | domain="c2-lex",tags_filter=["C2-TAG-EXACT"],valence=0 | 同参数加arousal=0 |
| importance_only | domain="c2-lex",importance_min=7，importance降序，同值不预定顺序 | 同参数加min_score=0 |
| default_emergence | domain="c2-emerge"；再用mode="full" | domain="c2-emerge",min_score=0 |

default emergence的full保留既有dehydration语义，不要求canonical原文。
固定列表remaining非零不代表支持cursor。

## 批3：可见性、compressed、superseded

query="C2STATE",domain="c2-state",mode="full",max_results=50,
max_tokens=10000,touch=false：

1. 默认：可见集合09/10/11；不得泄漏sealed名字、ID、正文或计数。
2. 加include_dormant=true：增加休眠项。
3. 加include_sealed=true：增加sealed项，由管理员另核对。
4. 加include_dormant=true,wake_dormant=true：touch=false不唤醒，状态由管理员前后投影证明。

09的compressed是tag，正文保留。10的superseded_by指向真实11；10可能作为弱匹配列名，
不能当成已删除。不调用dream读取旧桶。

## 批4：分页、摘要及长文

A. query="C2PAGE",domain="c2-page",mode="summary",touch=false,
max_results=2,max_tokens=180。用真实cursor逐页翻完；目标集合16～21，恰好6项，无重漏。
预算无进展时停止循环，仅提高max_tokens、保留其他绑定参数后继续。

B. 新开第一页，mode="full",max_results=3,max_tokens=80；其余如A。
核对截断标识、全部核算字段、页集合。cursor是桶级分页，不是长文分段续读；
截断显示的桶可以已消费，不能要求下一页继续该正文。

C. 用一个remaining>0的有效cursor，分别改变query、mode、touch，应拒绝。
然后用原cursor和原条件继续。max_results/max_tokens允许变化。
tags_filter非空的ordinary query不支持cursor。

D. query="C2-PAGE-LONG",domain="c2-page",mode="full",touch=false,
max_results=50,max_tokens=20000：长文含C2-LONG-HEAD与C2-LONG-TAIL。
再用max_tokens=20：有原文已截断标识。独立大预算证明完整性。

E. 同query/domain改mode="summary"，分别max_tokens=10000和80。
应走真实SDK→HTTP→JSON parser→render，长文显示短synthetic summary；
不要求尾部sentinel出现在摘要。短正文可能沿现有“无需脱水，显示原文”路径。
touch=false允许读缓存，不写缓存；冷runtime零写不在保证内。

## 批5：historical_query与历史分页

query="C2HISTORY",domain="c2-history",touch=false：

1. as_of="2026-10-01T18:00:00+08:00",mode="summary"：OLD-V1、历史原文。
2. 同as_of，mode="full"：仍OLD-V1。
3. as_of="2026-10-02T00:00:00+08:00",mode="full"：等号边界选NEW-V2。
4. as_of="2026-10-02T12:00:00+08:00",mode="full"：NEW-V2。

metadata明确为当前值，不声称历史metadata已恢复。
冲突：历史query加importance_min=7；加tags_filter=["c2-v1"]；as_of有值但query为空。

历史分页：query="C2PAGE",domain="c2-page",as_of="D",mode="summary",
touch=false,max_results=2,max_tokens=10000，真实cursor翻完同6ID集合。
as_of日期D代表本地日终，所有该组桶创建时间为D零点，不能被未来创建时间误排除。
拿有效cursor改mode="full"应拒绝，即使两mode都显示历史原文。

## 批6：mailbox

1. breath(mailbox=true,mailbox_limit=2)：可见C2LETTER PUBLIC，不泄漏HIDDEN；
   可能同时显示既有公开信，保留并记录。
2. breath(mailbox=true,mailbox_limit=2,include_sealed=true)：两封新增C2信。
3. 冲突分别调用mailbox=true加touch=false、mode="full"、query="C2LETTER"。

不传其他非默认bucket参数；letter ID由真实返回记录，不预猜编号。
mailbox是独立信件模型，不要求bucket分页/预算。

## 服务端补证与停止点

管理员只读核对初始化manifest及C2前后activation_count/last_active/dormant/cache状态。
Claude口述不能证明全部存储零写、完整协议header或provider调用数。
Docker镜像、Zeabur卷权限/部署、真实Claude均须后续授权实测。
本地通过只关闭限定本地用例，不宣布C2客户端或S5整体通过。

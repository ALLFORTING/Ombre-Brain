# C2 fixture 预期清单

批次c2-v1，全部新增，不复用c100 seed ID或obweb-ls operation_id。
D为首次初始化上海日期，PREV=D−1，二者随后冻结。每桶均有c2-v1 tag，没有exact test tag。
actual文件路径、metadata、body/file SHA256与SQLite自增ID保存在卷内`.c2-v1.json`。

| ID尾号（前缀c200000000） | 名称 | domain/type、附加条件 | 预期 |
|---|---|---|---|
| 01 | C2-NAME-EXACT | c2-lex/dynamic, importance8 | 完整名称优先 |
| 02 | C2-NAME-EXACT-NEAR | c2-lex/dynamic, importance8 | 同名近似对照 |
| 03 | C2-TAG-TODAY | c2-lex/dynamic, importance8, C2-TAG-EXACT/C2-ZERO, V0/A0, D | 标签、日期、零坐标 |
| 04 | C2-BODY-LOW | c2-lex/dynamic, importance2, body含C2-BODY-NEEDLE | 正文命中，importance>=7排除 |
| 05 | C2-TAG-YESTERDAY | c2-lex/dynamic, importance9, 同03标签, V1/A1, PREV | 旧日期、远坐标、差异向量 |
| 06 | C2-OTHER-DOMAIN | c2-other/dynamic, importance9, C2-TAG-EXACT | 域外排除 |
| 07 | C2-DORMANT | c2-state/dynamic, dormant, C2STATE | 默认不可见、显式可读 |
| 08 | C2-SEALED | c2-state/dynamic, sealed, C2STATE | 默认不泄漏、不建向量 |
| 09 | C2-COMPRESSED | c2-state/dynamic, compressed tag, C2STATE | 正文保留、关键词可达 |
| 10 | C2-OLD | c2-state/dynamic, superseded_by=11, C2STATE | 旧项标注真实successor，允许弱展示 |
| 11 | C2-SUCCESSOR | c2-state/dynamic, supersedes=[10], C2STATE | successor确实存在 |
| 12 | C2-FEEL | feel, 空domain, C2-FEEL tag, C2FEEL | feel selector；full需query+tag |
| 13 | C2-SESSION | session/archive, C2-SESSION tag, C2-TOPIC topic, C2SESSION | session/topic读；两信源 |
| 14 | C2-PIN | c2-emerge/permanent, pinned, importance10, C2EMERGE | 默认浮现、full仍沿dehydration |
| 15 | C2-HISTORY | c2-history/dynamic, 固定历史时间, C2HISTORY | OLD-V1→NEW-V2，边界等号选新正文 |
| 16 | C2-PAGE-16 | c2-page/dynamic, C2PAGE short16 | 分页集合成员 |
| 17 | C2-PAGE-17 | c2-page/dynamic, C2PAGE short17 | 分页集合成员 |
| 18 | C2-PAGE-18 | c2-page/dynamic, C2PAGE short18 | 分页集合成员 |
| 19 | C2-PAGE-19 | c2-page/dynamic, C2PAGE short19 | 分页集合成员 |
| 20 | C2-PAGE-20 | c2-page/dynamic, C2PAGE short20 | 分页集合成员 |
| 21 | C2-PAGE-LONG | c2-page/dynamic, >5000中文字符, HEAD/TAIL | full完整/截断、短摘要、分页成员 |

普通桶created为D零点；05为PREV零点。15在2026-10-01零点创建，
history changed_at=2026-10-02零点；as_of当时上海时间，当前metadata不回溯。
两封新增letter为C2LETTER PUBLIC/HIDDEN，sealed分别0/1，session_id=13；
SQLite自增letter ID不预猜，旧信件不覆盖。history只新增15的一条OLD-V1快照。

精确过滤集合：tag+C2-lex=03/05；再importance>=7、date=D=03；
resonance0,0+C2-ZERO+C2-lex排序03→05；c2-missing为空。
query C2PAGE+c2-page翻完的集合16～21，无重复，无遗漏；排名从真实首响应冻结。
一般query有真实semantic/fuzzy附加命中，不强行规定只有目标一项。
默认C2STATE可见09/10/11，include_dormant增加07，include_sealed增加08。

管理员清单含sealed身份，只在隔离运维审阅；不把本清单全文贴进默认隐私验收的Claude聊天。

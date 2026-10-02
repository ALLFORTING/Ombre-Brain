# C2 启动增量实现与更新步骤

起点提交 ab7a348b11e6c7d8725d8d0c4ad1f0a26e19cec6。
修改只在 deploy/lsf-zeabur/；业务源码、公开schema、production入口和默认值不改。
不新增必填变量，不换卷或token，不自动访问云端。旧seed身份基线885807…保留。

启动顺序：configure/tzset → 取得既有service.lock → 依赖/源码校验 → 原seed初始化或复用
→ import真实OB → 启动内部loopback stub → initialize_c2(ob, root, lock)
→ 新seed必要读回 → 构造SDK app/启动公开监听 → ready。
initialize_c2不调用lock_volume、不重新flock。公开OB socket在C2完成前尚未绑定。
首次冷初始化原seed也会增补C2；既有卷仅新增C2，不重做原seed。

`/data/lsf/.c2-v1.json` 是独立批次manifest，不替换`.synthetic-initialized.json`。
21桶为c20000000001～21；20个未sealed桶经真实EmbeddingEngine.generate_and_store，
SDK→内部HTTP→provider JSON解析→现有index存储；sealed08无向量。
只有新C2 ID可以被索引。初始化前拒绝ID/目录/history/letter/index冲突。
history快照及两封letter是管理员synthetic fixture，用现有表append-only插入，
不声称验证了写工具的业务写入；读取仍是production breath/history/mailbox实现。

完整批次核对身份、fixture bytes、SQL行、model/vector行后直接复用，不重新索引或插入。
这是C2只读验收批次；若有人改动C2正文/metadata/index，启动fail closed，
不自动覆盖。旧seed正文及既有obweb-ls对象/操作回执不按原seed内容重置。
初始化核对既有Markdown bytes、seed marker bytes及原history/letter/vector行保持原值。

跨文件/SQL/index不是一个事务。开始前持久化status=started；任何异常/进程中断保留现场，
下次拒绝partial，不续跑、不删除、不修复。不因部分文件存在就认为整批已完成。
只有全部fixture/index和保留断言完成后，fsync临时完整manifest并原子替换C2 marker。
服务锁在初始化和运行期间由外层serve一直持有，任何退出都释放。

所有普通C2桶created/last_active为首次上海日期D的零点，避免上午执行产生未来创建时间。
05为D−1；15固定10月1日零点创建、10月2日零点替换，as_of等号选NEW-V2。
manifest冻结D，重启不重算；后续日recent_days=0无法继续按原D断言。
compressed是tag；旧10指向真实11；长21有首尾sentinel，正文超过5000中文字符。
各样例metadata、正文hash和实际letter/history IDs以manifest为准。

短摘要仅对C2标识输入生效，仍是JSON parser输出；短正文沿原无需脱水分支。
差异向量保留synthetic-embedding-4-v1与4维，所有向量单位长度。
legacy为e0，C2PAGE为e1，C2STATE/HISTORY为e2，常规C2为e3；
LEX-05=(0,0,.6,.8)，LEX-06=e2，标签query=e3，用来验证1/.8/0差异。
这是deterministic synthetic scoring，不是真实provider相关性/质量验收。

来源校验：c2-source-hashes.json固定业务基线和当前测试runtime SHA256，启动检查。
hash采用LF归一化，区别Windows checkout行尾与Git/Docker内容；不更改Git配置。
artifact-hashes.json是本次目录工件清单；原reuse-provenance与evidence历史文件不改。

## 未来获授权后的最短更新步骤（本 Phase 不执行）

1. 审阅local commit；另行授权后将该提交push到原测试构建分支，选同一测试服务更新。
2. 保持原Dockerfile路径、原卷/data/lsf、原query token和全部已有必填变量。
   不粘贴额外初始化Command，不换卷，不追加必填变量。
3. 更新需要一次测试服务重启/短暂停机。旧进程停止释放锁；新进程先增补C2，再开放MCP。
   若平台滚动更新让新旧进程重叠，新进程会拒绝持锁冲突；须先停旧测试进程，不能绕过锁。
   若失败/不ready，保留卷，停止排查，不删除marker或重复制造fixture。
4. 只读确认C2 manifest完整、原数据保留；重新连接原L-SF connector。
5. 按C2_CLAUDE_INSTRUCTIONS.md分批验收，每批审阅后再继续。

Docker镜像构建、Zeabur实际卷/UID10001权限、更新、真实Claude均未在本Phase验证。
本地WSL不是Docker/Zeabur替代证据，生产OB及旧日雪不在更新范围。

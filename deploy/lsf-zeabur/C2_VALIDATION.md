# C2 本地限定验证 — 2026-10-03

结果：10 passed，440.70秒。JUnit：evidence/c2-junit.xml。
这是WSL Ubuntu限定本地验证；Docker、Zeabur和真实Claude未执行，保留BLOCKED。
未运行full suite；没有安装依赖、push、部署、操作云端、换卷/token或操作正式OB/旧日雪。

源码起点 ab7a348b11e6c7d8725d8d0c4ad1f0a26e19cec6，隔离分支codex/lsf-c2-20261003，
worktree D:/Codex/projects/OB-LSF-C2-20261003。所有修改均在deploy/lsf-zeabur/。
既有dirty checkout保持原状，main/origin配置不改。

## 环境与命令

通过交互式wsl -d Ubuntu执行；Python=/home/ting/.venvs/ombre/bin/python 3.12.14，
MCP1.29.1。该venv默认RM为0.1.0.dev7，未改venv；仅命令级PYTHONPATH只读复用
D:/Codex/projects/Ombre-Brain-S5-Remaining/.s5-rm010-official中的正式RM0.1.0。
真实runtime_sources核对metadata、direct_url官方release URL、SHA256
93d1514f940bde00a43b34b61681fe7f64da130313840f247869157d6e250485及现有contract。
这不是本轮重装/下载依赖或Docker安装验证。

```sh
PYTHONPATH=/mnt/d/Codex/projects/Ombre-Brain-S5-Remaining/.s5-rm010-official \
/home/ting/.venvs/ombre/bin/python -B -m pytest \
  deploy/lsf-zeabur/test_c2.py -q -x -p no:cacheprovider \
  --junitxml=deploy/lsf-zeabur/evidence/c2-junit.xml
```

pytest使用Linux默认/tmp，未设置TMP/TEMP或--basetemp。
服务子进程继续使用原harness的隔离TMPDIR，不影响pytest默认临时目录。
所有数据都是临时synthetic root，未读取云端卷或生产数据。
测试用内部HTTP监听18995、完整测试服务18993；控制器finally关闭子进程和监听。

## 通过的10个限定用例

| 用例 | 实际验证 |
|---|---|
| incremental_preservation_restart_real_paths | 先用真实keyed hold制造obweb-ls既有桶/回执，并保留原seed和一封旧信；首次新增21桶、1 history、2 letter、20 vector，第二进程复用；旧桶bytes、seed marker、原SQLite表行/向量保持；无重复初始化 |
| fail_closed[partial] | 真实HTTP embedding失败留下started marker；后续拒绝，不修复 |
| fail_closed[id] | 外来同ID冲突拒绝，无后续文件变化 |
| fail_closed[directory] | C2批次目录已存在拒绝 |
| fail_closed[identity] | manifest基线身份不符拒绝 |
| fail_closed[index] | provider HTTP400→真实SDK/index失败→started→重启拒绝 |
| fail_closed[fixture] | 完整C2正文被改，重启拒绝，不覆盖 |
| fail_closed[incomplete_manifest] | complete但索引清单缺失，拒绝，不误接受 |
| run_uses_one_lock_before_public_socket | AST核对仅一次lock_volume，C2初始化先于公开socket启动；不是云端时序观测 |
| real_service_initializes_before_requests_and_restarts | 真实serve()、正式依赖检查、query认证和HTTP MCP initialize/tools/call读到OLD-V1；同根第二服务进程不重复初始化，快照不变 |

read helper通过真实业务函数/API链路验证：长文短摘要确实触发dehydrate HTTP；
大预算full首尾齐全、小预算有截断；summary小预算cursor翻完16～21恰好6ID；
历史OLD/NEW及替换等号边界；mailbox公开/sealed读；tag/importance/date交集；
resonance零坐标；九selector正常/拒绝路径；sealed/dormant可见性。
actual索引为4维单位向量，tag query与03/05/06的cosine按1/.8/0区分；
非C2向量仍e0，非C2长文沿原stub摘要行为。
只读检索前后桶/SQLite快照及dehydration cache bytes相同。
没有直接插入向量数据库，没有mock业务检索/摘要/parser/index函数。
HTTP失败注入仅在测试provider表面返回400，真实SDK与存储错误路径保留。

静态C2 audit核对：指定基线、业务diff为空、无范围外untracked文件、Python语法、
业务/部署来源hash、工件hash和Docker上下文白名单。命令级core.autocrlf=true配合
LF归一化hash，避免把Windows checkout行尾误认为业务变化；未修改Git配置。
旧audit.py/reuse-provenance/evidence不改写，新的清单是C2工件清单。

## 保留的边界与BLOCKED

- Docker镜像构建、真实容器UID10001/卷权限、Zeabur停旧进程/滚动更新时序：BLOCKED，未执行。
- xiaobaishuob.zeabur.app的C2实际增量初始化与Claude分批验收：BLOCKED，未访问。
- 本地HTTP MCP不是Claude connector体验；既有C1通过事实由婷提供，未重做云端C1。
- 多文件、history、letter、index不构成整体ACID；真实SIGKILL全矩阵未执行。
  本次失败注入证明partial fail closed，不替代进程死亡各边界。
- 21桶普通query有真实fuzzy/semantic附加命中；只有封闭过滤/分页集合用精确集合断言。
- 严格完整批次校验适用于C2只读样例；改动C2内容/metadata/index会拒绝启动，需独立审阅。
- synthetic provider只证明确定性处理/评分路径，不证明真实provider质量、超时/费用或429矩阵。
- touch=false不承诺冷runtime零写；本次cache/activation只覆盖已初始化后的指定读用例。

最短后续更新步骤见C2_IMPLEMENTATION.md；真实Claude指令见C2_CLAUDE_INSTRUCTIONS.md。
本Phase到local commit停止。

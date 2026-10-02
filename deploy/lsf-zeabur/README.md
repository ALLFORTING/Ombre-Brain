# L-SF Zeabur 独立测试服务

本目录包含原部署适配及新增 C2 启动初始化。本 Phase 仅本地实现、限定验证和 local commit；没有 push、部署或操作云端。婷已确认小白鼠首组连接、写入重放和跨重启读回；这些既有结果保留，不据此宣布 C2 或 S-5 全部通过。

原部署来源基线：885807cf460bec47af09812a523677d9dbb33eba。
C2指定起点：ab7a348b11e6c7d8725d8d0c4ad1f0a26e19cec6。
C2隔离分支：codex/lsf-c2-20261003。
本次结果、样例和指令见 [C2验证](C2_VALIDATION.md)、[21桶清单](C2_FIXTURES.md)、[分批Claude指令](C2_CLAUDE_INSTRUCTIONS.md)。
原复用来源：D:/Codex/projects/OB-Claude-Synthetic-20261002-LSF-Local；历史来源 hash 保留在 reuse-provenance.json。observe.py、seed.py 继续原样；provider_stub.py 仅新增 C2 输入短摘要和差异4维单位向量，非C2行为保留。原 audit.py/evidence 是历史记录，C2 使用 c2_audit.py、test_c2.py 和 C2_VALIDATION.md。

## 婷需要填写的内容

以下只用于未来单独授权的全新测试服务，禁止填写到生产服务或共享变量。

| 平台变量 | 填写内容 | 含义 |
|---|---|---|
| ZBPACK_DOCKERFILE_PATH | deploy/lsf-zeabur/Dockerfile | 仓库根为构建 Root Directory；避免选择生产 Dockerfile |
| OB_LSF_TEST_SERVICE | true | 测试入口 opt-in，缺失时拒绝启动 |
| OMBRE_MCP_ALLOW_QUERY_TOKEN | true | 仅这个测试服务开启 query 认证 |
| OMBRE_MCP_QUERY_TOKEN | 婷私密生成的全新专用值 | 至少32随机字节的 base64url 无填充编码，43字符以上；只允许字母、数字、下划线、连字符 |
| PORT | 平台注入，默认8080 | OB监听0.0.0.0:$PORT；不得设为18995 |

token 不从此前 harness 或生产复用。由婷在私密密码工具中生成并直接填写平台变量；不要贴到聊天、Git、Docker build args、Dockerfile、报告、CLI参数或截图。部署工件没有任何真实 token。当前烟测 token 是全新临时内存值，经匿名管道传递；进程均已结束，不是未来平台 token。

不要填写真实 provider key、Bearer、代理、hook、RM runtime、dashboard/setup token、备份、Raw Evidence 或继承生产环境变量。入口导入业务前清空继承环境，仅重建测试固定配置与查询凭据；三类 provider 的 synthetic API key 均无外部权限，URL全部是内部 http://127.0.0.1:18995/v1。

## 构建与启动

未来获得单独授权、且该专用分支已可供构建之后，在仓库根执行：

```sh
docker build -f deploy/lsf-zeabur/Dockerfile -t ob-lsf-zeabur-test .
```

Dockerfile.dockerignore 是该 Dockerfile 专用上下文白名单；只包含受版本控制的根源码/运行资源/依赖及测试启动模块，不包含 .git、.env、config.yaml、原本地数据、烟测卷、证据、测试 suite、ngrok 或报告。不用生产 Dockerfile，不把 Root Directory 改成 deploy/lsf-zeabur（会找不到业务源码）。

启动命令已在 CMD 中固定：

```sh
python -B /app/deploy/lsf-zeabur/run.py
```

Python 基础镜像3.12.14；沿用原 requirements.txt 和 Linux constraints，MCP固定1.29.1。正式 RM 0.1.0 来源为 https://github.com/peanutsuee/Remember-Me/releases/download/v0.1.0/remember_me-0.1.0.tar.gz，SHA256为93d1514f940bde00a43b34b61681fe7f64da130313840f247869157d6e250485；pip安装使用原带hash URL，入口核对实际metadata、direct_url来源/hash及正式contract。容器实际依赖路径预期 /usr/local/lib/python3.12/site-packages，业务 /app/server.py，Linux路径不含WSL /mnt/d。启动校验源路径会报告到有限本地烟测的安全投影；云端不打印原始日志/凭据。

单进程、单OB worker、单SDK lifespan；不进入production main，不启动digest scheduler；沿用已验收 launcher 对lazy decay的进程内禁止启动绑定，hook固定skip。这些仅是测试部署适配，不是业务实现修改，也不代表scheduler行为通过。stub同进程另起内部loopback HTTP监听；没有独立公网provider服务或端口暴露。

仅公开 /mcp、/mcp/、/health；认证与access redaction仍调用真实production函数。外层测试surface拒绝dashboard、config和import HTTP路径，避免项目目录写入入口。其余业务写入由现有handlers及存储实现完成。

默认以容器root启动以初始化专用挂载点的所有权，只chown挂载点本身，不递归修改已有数据；随后降权为UID/GID10001运行OB/stub。镜像源码仍为root所有，运行进程不能写 /app。非root平台必须预先让UID10001有测试卷写权限；实际平台权限尚未验证。

## 持久卷

新建测试专用 Volume ID：ob-lsf-synthetic-20261002；Mount Directory：/data/lsf。只绑定此测试服务，不使用已有卷，不重新挂载或覆盖任何生产卷。入口要求此处确实为挂载点，缺卷拒绝启动，不会在镜像层偷偷seed。

| 测试卷路径 | 内容 |
|---|---|
| /data/lsf/buckets | Markdown桶、历史/operation SQLite、向量、脱水cache、letters、锁及state |
| /data/lsf/remember-me | RM预留根；runtime固定false |
| /data/lsf/raw-evidence | 独立预留路径，不启用capture |
| /data/lsf/tmp、home、cache | tempfile、jieba及其他运行缓存；不写项目目录 |
| /data/lsf/.service.lock | 单实例进程锁，退出释放 |
| /data/lsf/.synthetic-initialized.json | 固定synthetic根身份/seed标记，不含秘密 |

全新根只创建公开c10000000001和sealed c10000000002两桶，沿用原synthetic正文。首次seed写到同卷staging，再发布并写持久标记；已有正确标记仅复用，不覆盖正文、不再seed。无标记但已有buckets/staging、标记不匹配或symlink都会停止；不自动删除或repair。中断初始化的根须先保留证据、另行人工审阅，禁止自动重跑覆盖。

同服务保持单实例/副本，关闭自动扩容，不启动多个worker，不同时运行两服务指向同一实际目录。卷锁会拒绝第二个进程。Zeabur挂卷服务采用停止旧实例再启动的Recreate策略，重启会有短暂停机，不保证旧MCP transport session跨进程可用。[官方卷说明](https://zeabur.com/docs/en-US/operations/data/volumes)、[官方健康检查](https://zeabur.com/docs/en-US/operations/monitoring/health-checks)。

## 未来 C2 更新顺序（本轮不执行）

1. 另行授权push/更新后，将本次local commit用于现有小白鼠测试服务构建；本Phase不执行。
2. 保持现有Dockerfile、仓库根、卷/data/lsf、query token、域名和connector；不新增必填变量。
3. 停旧测试进程释放service.lock后启动新版本，需要短暂停机。若平台新旧重叠持锁，新进程拒绝；不能绕过锁。
4. 新进程自动追加C2，完整批次直接复用；部分批次或身份冲突停止。保留卷和失败现场，不删marker或重新seed。
5. ready后重新连接L-SF，用C2_CLAUDE_INSTRUCTIONS.md逐批验收；本地结果不替代真实Claude或平台协议/日志验收。
6. 既有首组连接、写入重放和跨重启读回结果保留；不占用/更换obweb-ls-*回执。
   详细步骤与边界见C2_IMPLEMENTATION.md。C2客户端及S-5仍未宣布完成。

## 原部署适配历史验证（ab7a348；C2结果另见C2_VALIDATION.md）

WSL Ubuntu /home/ting/.venvs/ombre/bin/python 实测3.12.14、MCP1.29.1；正式RM0.1.0实际import来自原.s5-rm010-official，direct_url/hash/contract通过。Windows对既有官方tar.gz重新计算SHA256与正式pin完全一致。

evidence/smoke-3.json为最终PASS：首次2seed、0.0.0.0 OB和loopback stub实际socket、health与限定surface、正确/缺失/错误query认证、stateful SSE/list27工具/trace schema、synthetic一次hold、同根立即重启不seed/正文hash不变、全新session读取、卷内operation receipt、监听关闭。只执行一个独立zeabur-preflight-hold-001，不占用首组obweb-ls-* keys；没有重跑full suite、旧矩阵或同key重放矩阵。

静态审计失败也保留：audit-1-failure.json记录Windows worktree指针在Linux需只读路径翻译；audit-2-failure.json记录Linux未继承Windows的core.autocrlf=true。审计命令临时对齐路径/CRLF转换，未改写.git、持久Git配置或checkout；Windows Git始终没有业务差异。最终静态结果见evidence/bounded-audit.json。

失败保留：smoke-1.json在首次读写后重启启动失败，早期harness只记录JSONDecodeError；smoke-2.json加入安全异常类型后记录子进程OSError。launcher加入SO_REUSEADDR后smoke-3通过；旧数据与失败没有改写或删除。未声称首次失败是OB业务失败。

真实production access过滤器保留；内存logging扫描没有当前随机token/sentinel泄漏，报告只有计数/布尔。云端也只使用这个安全handler，不归档原URL、query、headers或traceback。诊断受限：云端失败仅给通用错误，不输出任意异常payload。

Windows PATH及WSL均未发现Docker，未安装Docker。镜像构建、容器内UID/挂载点/依赖安装与路径、Zeabur启动/健康检查/持久卷、平台端口注入、外网SSE/TLS/日志、Claude connector与指令均未验证。Linux本地烟测不等同镜像或Zeabur验收；本地/.smoke-*根是模拟卷，main的真实挂载/降权分支未执行。

本轮只local commit，不push、部署或创建云资源。证据和synthetic烟测根保留，所有本轮OB/stub/controller进程关闭；S-5仍INCOMPLETE。完成后停止，请婷换窗口。

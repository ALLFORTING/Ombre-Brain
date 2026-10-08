# 全新合成备份验证入口（待独立审查）

实现基线：a8947eb3f11838a7c8054cc290d826ae06e9378b。
仅新增 run.py、Dockerfile、Dockerfile.dockerignore、限定测试及本文档。
原小白鼠分支 37139d1463b9fff010da8131e4d132d5019a8798、两个 main、
生产备份模块、OIDC 策略和 workflow 均不修改。

## 运行边界

入口不是旧 C2/C3 的移植，不连接小白鼠的旧数据。不读取、遍历、初始化、
chmod/chown、卸载或删除 /data/lsf。平台必须保持原卷原绑定。
默认 OB_BACKUP_SYNTHETIC_ENABLED=false：输出 synthetic_disabled 并退出，
不导入生产运行模块、不创建合成目录、不读取构建元数据、不联网。
只有显式 true 且 OB_BACKUP_SYNTHETIC_PLATFORM_ISOLATION_VERIFIED=true 才进入启动。
第二个开关只是婷确认已有独立系统隔离证据的声明，绝不是隔离证明。
缺少真实证据即不得填写、不得部署；本地测试不能证明平台隔离。

每次启动使用 Linux /tmp 下全新随机私有根，不借用已有数据或恢复 workspace。
source、workspace、restore 是相互隔离的同级目录。源码不可写；fixture 通过
真实 BucketManager.create/record_letter/record_note 创建普通与 sealed 桶、
普通与 sealed letters/notes，未来 notes 保留 open_at，使用实际 SQLite schema。
容器重建后 /tmp 会丢失；这不是持久状态验收。改变版本前须单独保留测试证据，
本入口不提供任何清理既有材料的命令，也不自动复用上一轮目录。

导入 server 前清除继承环境，配置仅固定新目录、禁用 RM/provider/hook/v2。
不读取源码 config.yaml，使用新根中不存在的配置路径取得默认值并显式关闭 embedding。
复用真实 server 运行组件、DEFAULT_WRITE_COORDINATOR、register_backup_auto_if_enabled，
将真实注册的自动备份路由组装为 ASGI app，并调用 install_backup_auto_lifespan。
正常停止等待真实采集 worker/finally，清理 task 结束；不启动 SDK/C2/C3、
生产主入口、provider、业务 scheduler、保活、守护、TG、GitHub 客户端或子进程。
默认没有任何 captures，没有网络监听；进入 lifespan 后仅等待 SIGTERM/SIGINT。
这是终端合成验收支架，不是公开的自动备份服务部署。

部署入口没有伪造 claims、签名、JWKS、认证旁路。真实生产 OIDC 的 repository/main/
workflow/audience/run/attempt 契约原样保留。正向认证仅在测试文件中生成内存 RSA/JWT，
注入合成 JWKS，仍通过真实验签及生产策略；测试文件不在镜像上下文内。
正向 API 请求只通过进程内 ASGI，不触达任何正式服务。

## 原卷与网络拒绝

Python 审计钩子拒绝 socket.connect/getaddrinfo；拒绝原路径的 open/SQLite connect。
标准文件操作 stat/listdir/scandir/chdir/mkdir/权限/删除/改名等先检查原路径；
相对 dir_fd 操作先核对 /proc/self/fd 所指路径。拒绝发生在相应文件操作之前，
测试没有打开或创建真实 /data/lsf。核心自己产生的临时目录 dir_fd 操作仍可完成。
这些是入口自身的防御，不是容器安全边界：原有描述符、路径别名、原生扩展等必须由
平台系统级隔离约束。不同 UID、只读原卷或一个环境开关均不能单独证明禁止读取。
需要独立证明测试进程不能通过原挂载或别名读写旧卷，做不到则不部署。

## 构建来源与白名单

从仓库根指定 deploy/backup-synthetic/Dockerfile。基础镜像固定 Python 3.12.14
slim-bookworm OCI digest 392307d22300de8b5986851a12d9176dfc0fc073e65bf6523ebd7dcbeb23564e。
沿用 requirements.txt/constraints-py312-linux.txt；运行 UID/GID 10002，
建议根文件系统只读、drop ALL/no-new-privileges，仅合成 /tmp 可写。
Dockerfile 专用忽略文件默认全部排除，逐项放行 52 个根 Python 依赖模块、显式 server
fragments、两份依赖文件、导入必需的 asset_viewer.html 和新入口，不包含 .git、.env、config.yaml、tests、
旧 L-SF deploy、任何卷数据或证据。COPY *.py 只能取得白名单内的根模块。

唯一构建参数 ZEABUR_GIT_COMMIT_SHA 是非秘密的 provider-issued 实际构建提交。
缺少或格式错误时构建失败；不把 a8947eb 基线写成部署 SHA。
构建元数据写入 /app/.backup-v2-build.json，运行时复用 resolve_runtime_commit，
可选 provider runtime SHA 必须一致。不允许 Render 变量代替 Zeabur 构建证据。
构建前必须隔离既有 query/provider/backup/共享 secret 注入引用，不查询真实值，
不放到 ARG/ENV/参数/镜像层或日志中。镜像构建是否确实取得准确 provider SHA待验收。

## 有限验证

仅 WSL Ubuntu /home/ting/.venvs/ombre/bin/python（Python 3.12.14），Linux 默认 /tmp：

```sh
PYTHONDONTWRITEBYTECODE=1 /home/ting/.venvs/ombre/bin/python -B -m pytest -q -p no:cacheprovider tests/test_backup_synthetic_launcher.py tests/test_stage8h_g1c_quiesced_capture.py::test_registered_production_write_coverage_is_complete --tb=short --color=no
```

不设置 TMP/TEMP/--basetemp，不运行 Windows Python 或全量套件。
每项使用新 Linux 合成子进程和新目录；网络拒绝、伪造继承配置清除、默认关闭/
隔离缺失、原路径操作拒绝、缺失/漂移 SHA、已有根拒绝、真实组件身份、
无自动采集、OIDC 错误 ref、成功下载/摘要/完整恢复对账、损坏不发布、失败释放、
停止等待 worker、真实 SIGTERM/SIGINT 均在限定范围。既有合成包保留，不调用 ack。
额外 image 检查只是把白名单 Python 文件复制到临时目录后实际运行同一合成验证；
使用 WSL 的依赖环境，不能替代 Docker 构建、层内容、UID或依赖安装验收。

首次限定测试出现 3 failed/22 passed：路径保护错误地拒绝核心临时目录 dir_fd 操作；
修正为先检查描述符路径后完成同一范围测试。此前一次启动探针发现默认配置无
embedding 字段，入口补齐关闭配置。没有改写生产代码或任何既有材料。
复制上下文检查另发现 server 的显式 fragments 与导入必需的 asset_viewer.html；白名单已补齐。一次复制测试的脚手架遗漏 requirements.txt，已改为复制全部放行资源；这些不是 Docker 运行记录。最终同一限定命令结果：30 passed in 30.46s。其中新增入口测试 29 项（28 个独立子进程场景和 1 个静态配置检查），既有生产写入覆盖回归 1 项。

Windows Get-Command docker 与 WSL command -v docker 均未找到 Docker。
镜像构建、层/历史扫描、实容器运行、UID/只读根/依赖、运行元数据与平台原卷隔离
均未执行或 unknown；没有最终镜像 digest，不声称云端通过。

## 婷之后的保存、停止与恢复步骤

实施前通过已认证终端保存原 service/project/environment 标识、实际部署 SHA/
不可变镜像或可恢复版本、仓库/分支/构建根/Dockerfile/启动命令、端口/健康检查/
副本/重启策略和原卷 ID及 /data/lsf 原绑定。仅保存非敏感配置和 secret引用，
不执行 env/printenv，不读取秘密。实际平台状态目前 unknown。
必须能正常停止原实例，排空完成、无重叠副本，准确保存并恢复全部设置，
且保留原卷绑定并在测试进程中强制禁止其访问；缺任一条件即不部署。
之后仅在单独授权下切换到精确审查后的验证版本；不要继承原启动覆盖或凭据。
测试完成停止验证进程、保留新根中的证据，再恢复原记录的部署版本及全部配置，
原卷绑定不变；确认原 L-SF ready、原 marker/C2验证通过、没有重新 seed。
禁止以覆盖原卷、删 marker、重跑初始化或重放旧操作 key 作为恢复。

本轮仅本地实施/有限验证/新增提交；没有推送、部署、供应或读取真实业务凭据、
访问原卷、正式采集、删除既有材料或发送 TG。停止待独立审查。

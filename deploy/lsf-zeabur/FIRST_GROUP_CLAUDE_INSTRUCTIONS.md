# 首组 L-SF：可直接发给 Claude 的指令

C1 既有指令保留；后续 C2 使用 [分批指令](C2_CLAUDE_INSTRUCTIONS.md)，不重做本页 smoke。

仅在管理员已完成隔离启动、fixture ID读回、端到端流式/query-token认证及日志门槛，且婷已自行在网页添加 OB-accept-L-SF 后发送。此文件不含任何认证凭据。本轮尚未执行。

下面内容可整段发给 Claude：

你正在验收专用 OB-accept-L-SF 测试 connector，legacy asset authority、stateful Streamable HTTP、全新 synthetic 根；仅使用此connector，不使用任何生产connector。只执行下列步骤，不读服务器代码、不跑测试、不安装软件、不修改部署。缺工具或失败时保留实际工具参数和报错，停止依赖步骤；不自行修业务，不换operation_id重试。不要索取、回显带凭据URL、query token或认证header/token、session原值或上传下载ticket。不要自动boot/dream/grow；仅按下面明确调用。把每次真正发出的调用和对象ID记录，不以“我认为成功”代替工具返回。

A. 连接与读：
1. 列出当前connector实际提供的工具及可见参数；若你无法主动tools/list，只说明可见工具，不能伪造tools/list网络调用。重点确认breath.max_results默认5、hold.importance默认5；dream仅detail_ids/wake_dormant、todos仅include_provenance。完整schema由服务端另行保存。
2. 调用 breath(query="OBWEB-LS-SEED", mode="full", max_results=5, touch=False)。应返回公开桶c10000000001和原文OBWEB-LS-SEED-PUBLIC-v1；不出现sealed桶c10000000002的名字、ID、正文或其计数。不要添加include_sealed。
3. 调用 dream(detail_ids="c10000000001")。应见同一公开原文；此调用允许既有touch记账。再调用 dream(detail_ids="c10000000002")，只记是否拒绝/无可见内容；不得读取隐藏正文，不传include_sealed或touch。
4. 调用 pulse(show_all=True, limit=50, offset=0, touch=False)。公开seed可见，sealed seed仍不可见；剩余统计按工具实际口径记录，不推断所有库计数。
5. 调用 breath(query="OBWEB-LS-SEED", domain="obweb-no-such-domain", mode="full", touch=False)。应为零匹配，不回退全库。
6. 调用 breath(mailbox=True, query="OBWEB-LS-SEED", touch=False)。这是预期拒绝的参数组合；记录明确不支持的参数/原因，不把预期拒绝算工具故障，也不擅自重试修改参数。

B. 同key写/重放（这些synthetic写已列入此指令）：
7. 精确调用 hold(content="OBWEB-LS-HOLD-v1：这里只记录隔离网页验收样例。", tags="obweb-ls", operation_id="obweb-ls-hold-001")，省略importance观察默认5。记返回bucket_id为H1。再精确重复同一调用，应同H1、无新增内容桶；用 dream(detail_ids=H1)读正文。不要仅凭返回计数宣布去重，等待服务器receipt/正文核对。
8. 同operation_id，仅把content改为"OBWEB-LS-HOLD-v2：这是同key冲突样例。"、tags不变，调用hold。应明确冲突且不覆盖H1；dream(detail_ids=H1)应仍是v1。
9. 调用 trace(bucket_id=H1, content="OBWEB-LS-APPEND-ONCE", append=True, operation_id="obweb-ls-trace-001")，再精确重复；dream(detail_ids=H1)应仅一个APPEND标识。
10. 调用 grow(content="OBWEB-LS-GROW-v1：第一条隔离事项是准备蓝色纸卡。第二条独立事项是核对绿色纸卡。", operation_id="obweb-ls-grow-001")。管理员stub约定返回两项，记所有bucket IDs为G1/G2。精确重复应保持原item/桶IDs；逐项dream(detail_ids=实际ID)核对两条正文，不把名称当ID，不自行补造ID。
11. 调用 archive_session(summary="OBWEB-LS-ARCHIVE-v1：本次仅为隔离网页验收。", letter="OBWEB-LS-LETTER-v1", topics=["obweb-ls"], operation_id="obweb-ls-archive-001")。记session bucket ID为S1；精确重复同参数应同S1。调用 breath(mailbox=True, mailbox_limit=10)核对该letter，记letter_id；服务器将核对没有第二封同操作letter。不要把HTTP session ID当业务S1。

C. 等待重连安排：
12. 到此停止并报告逐步结果、H1/G1/G2/S1/letter_id和实际operation_id。不要自己重启服务。婷/管理员通知“同组测试服务已重启，可以继续”后，在同一测试connector上先尝试读取H1，记录是自动重连成功还是需要婷手动重连；不得自行操作其他connector。
13. 重连后精确重复步骤7的v1 hold、步骤9 trace、步骤10 grow、步骤11 archive（所有参数与key不变），核对原IDs、APPEND仅一次、letter仅一封，再停止。不承诺旧confirmation/cursor/transport session跨进程可用。
14. 不做SIGKILL、断电、重跑自动化矩阵、生产repair、删除/merge/digest确认或资产上传。本轮只是L-SF连接与指定smoke；四组和C2～C8后续由单独指令分批完成，S-5仍未完成。

输出一张表：步骤、实际工具及参数、返回对象IDs、可观察预期是否满足、FAIL/BLOCKED原因。不要输出认证或confirmation凭据。预期拒绝与实际故障分开写。不要声称服务端receipt已验证——管理员要另外对照实际请求与synthetic状态。

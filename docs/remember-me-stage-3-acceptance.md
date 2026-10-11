# Remember-Me Stage 3 Acceptance

> P4 公开入口迁移注记（2026-10-11）：下文的阶段、audit/acceptance 结论、已执行调用、回执、数量、日期、固定 SHA 和 Python 源符号属于历史快照，保留原名，不代表当前推荐 MCP 调用。当前 OB 操作使用 [README 推荐入口与兼容窗口](../README.md#推荐-mcp-工具与兼容窗口--recommended-mcp-tools-and-compatibility-window)：note 用 leave/list/get/dismiss，letter 用 get/seal，revision 用 list/restore；RM 上传用 rm_asset_upload 的 link/status，metadata/检索用 rm_asset_find 的 get/search，显示/模型视觉/导出分别用 rm_asset_show 的 view/inspect/download_link。每次必须传 action 和该分支参数，原参数名不改，未知或无关字段严格拒绝。
>
> The historical recommendations below are superseded for current OB calls by the README action examples. This P4 version removes the 15 legacy public registrations: the default surface is 20 tools, plus 15 optional diagnostics (35 total). Original Python functions and private SDK implementations remain available. Historical names, schemas, receipts, and acceptance evidence below are retained. This branch documentation does not assert PR merge, live rollout, or elapsed compatibility time.
>
> Remember-Me 独立包的 MCP schema/EXPECTED_MCP_TOOLS、内部 Python 方法、测试名和源码链接是不同标识，保留真实名称；不要把它们改写为 server.note 等不存在的 Python 方法。生产工具 description 或原业务回执中的旧名也不由本次文档迁移改写。rm_asset_show(action="view") 仍为 UI/_meta 图像与 signed-link fallback，action="inspect" 仍为 ImageContent；确认 token 预览可省，执行须取得有效 token，300 秒/一次性/状态绑定不变。

Status: accepted on July 24, 2026.

Remember-Me is currently the asset-memory module inside Ombre Brain. It is also the project name for a possible future standalone product, but it is not a separate service today.

## Accepted Scope

Stage 3A.2 and the Stage 3B core loop have passed real Claude web-client acceptance testing.

The accepted end-to-end path is:

1. The user uploads a file through the signed browser upload page.
2. Ombre Brain decodes the image, applies orientation, removes private metadata, re-encodes it, and stores the cleaned copy persistently.
3. `rm_asset_inspect` (current OB call: `rm_asset_show(action="inspect")`) returns that cleaned stored copy as standard MCP `ImageContent` for model inspection.
4. The model reads the image and proposes a title, description, and tags.
5. `rm_asset_update_metadata` stores the approved metadata.
6. The asset semantic index is refreshed through the existing embedding path.
7. In a new conversation, a natural-language `rm_asset_search` (current OB call: `rm_asset_find(action="search")`) query finds the historical asset.
8. `rm_asset_view` (current OB call: `rm_asset_show(action="view")`) displays the original cleaned stored image inline.

The acceptance run confirmed that the model could use the actual image content to create metadata, retrieve the asset later from a differently worded natural-language query, and show the same stored image in a new conversation.

## Claude Web Compatibility Note

The Claude web client may not parse a newly returned MCP `ImageContent` block correctly in the same turn as the tool call. In the accepted test, the next turn could accurately read the image and its visible text without another tool call.

The current compatibility procedure is:

1. Call `rm_asset_show(action="inspect", asset_id=asset_id)` once.
2. If the first response does not accurately describe the image, do not call the tool again.
3. In the next turn, ask the model to inspect the image returned by the preceding tool result.
4. Only after the image is understood, use `rm_asset_update_metadata` and, when needed, the embedding reindex path.

This is a client-observed compatibility behavior, not a guarantee for every MCP host. Metadata alone must never be treated as evidence of image contents.

## Repository Safety

Acceptance records must remain synthetic and metadata-free. Do not commit:

- real user images or visible text transcribed from them;
- real asset IDs or database records;
- image bytes or base64;
- signed download links or deployment addresses;
- authentication tokens, credentials, or private configuration.

The repository records only the workflow, client behavior, and acceptance outcome.
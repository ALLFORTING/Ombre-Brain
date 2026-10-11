# Remember-Me Stage 4B Acceptance

> P3 文档迁移注记（2026-10-11）：下文的阶段、audit/acceptance 结论、已执行调用、回执、数量、日期、固定 SHA 和 Python 源符号属于历史快照，保留原名，不代表当前推荐 MCP 调用。当前 OB 操作使用 [README 推荐入口与兼容窗口](../README.md#推荐-mcp-工具与兼容窗口--recommended-mcp-tools-and-compatibility-window)：note 用 leave/list/get/dismiss，letter 用 get/seal，revision 用 list/restore；RM 上传用 rm_asset_upload 的 link/status，metadata/检索用 rm_asset_find 的 get/search，显示/模型视觉/导出分别用 rm_asset_show 的 view/inspect/download_link。每次必须传 action 和该分支参数，原参数名不改，未知或无关字段严格拒绝。
>
> The historical recommendations below are superseded for current OB calls by the README action examples. The current default surface is 35 = 20 recommended + 15 registered compatibility entries, plus 15 optional diagnostics. Compatibility lasts at least three days after P3 documentation rollout and requires Ting's confirmation of claude.ai, TG, new CC conversations, and manual migration before separate P4 removal. No rollout completion or deletion authorization is asserted here.
>
> Remember-Me 独立包的 MCP schema/EXPECTED_MCP_TOOLS、内部 Python 方法、测试名和源码链接是不同标识，保留真实名称；不要把它们改写为 server.note 等不存在的 Python 方法。生产工具 description 或原业务回执中的旧名也不由本次文档迁移改写。rm_asset_show(action="view") 仍为 UI/_meta 图像与 signed-link fallback，action="inspect" 仍为 ImageContent；确认 token 预览可省，执行须取得有效 token，300 秒/一次性/状态绑定不变。

Status: passed in the real Claude web client on July 24, 2026.

## Accepted End-to-End Flow

The following flow completed successfully:

1. The user uploaded one image in a Claude web conversation.
2. Claude's code-execution container found and read the exact current attachment bytes.
3. `rm_asset_upload_link` (current OB call: `rm_asset_upload(action="link")`) received the byte count and optional file metadata, without a client-supplied hash, and returned a short-lived signed upload endpoint.
4. With network egress limited to the user's exact Ombre Brain hostname, the container sent the original bytes through HTTPS `multipart/form-data`.
5. The user did not select or upload the same file again.
6. Complete base64 did not pass through model-visible text or MCP tool arguments.
7. Ombre Brain privacy-cleaned and persisted the asset.
8. Claude saved title, description, and tags and confirmed the semantic index state.
9. In a new Claude conversation, a natural-language description found the correct historical asset without an asset ID or filename.
10. `rm_asset_view` (current OB call: `rm_asset_show(action="view")`) displayed the correct cleaned stored image inline through the MCP App.

Stage 4B therefore passes the intended one-upload attachment-save and later-recall experience.

## Upload Acceptance Metrics

```text
current_attachment_found: true
user_authorization_confirmed: true
network_egress_available: true
direct_multipart_upload_succeeded: true
source_bytes: 136432
uploaded_original_bytes: 136432
original_hash_match: true
mime_type_match: true
asset_persisted: true
base64_exposed_to_model_text: false
failure_point: none
```

The accepted file was a PNG measuring 370 by 250 pixels. The repository does not contain the image, its visible contents, its asset ID, its filename, its complete hash, its upload link, or any production host information.

The current public upload contract does not accept `expected_sha256`. The
server computes the authoritative `source_sha256` after receiving the file.
Client execution code may compare that result with a local hash internally,
but Claude must not guess a hash or print a complete hash to chat text or
stdout.

## Metadata and Embedding Acceptance

Metadata update result:

```text
metadata_updated: true
failure_point: none
```

Embedding verification result:

```text
ok: true
scanned: 1
skipped: 1
indexed: 0
```

`skipped: 1` means the existing asset vector was already current. It is a successful idempotent result, not an indexing failure, and no duplicate external embedding request was needed.

## New-Conversation Recall

The recall test used a new Claude conversation with:

- no image upload;
- no asset ID;
- no filename;
- only an abstract natural-language description of the remembered image.

`rm_asset_search` (current OB call: `rm_asset_find(action="search")`) selected the correct asset and `rm_asset_view` (current OB call: `rm_asset_show(action="view")`) displayed the correct stored image inline. This confirms the Stage 3B/4B retrieval loop across conversation boundaries.

## Network Configuration Used

Claude web required:

```text
Settings
-> Capabilities
-> Code execution and file creation
-> Allow network egress
```

The domain mode remained restricted, and only the user's exact Ombre Brain hostname was added under `Additional allowed domains`.

The allowlist entry must contain only the hostname. Do not add a scheme, path, token, query string, signed URL, or wildcard, and do not select `All domains`.

## Save Authorization

Both of these behaviors are accepted:

- When the user explicitly asks to save an image, Claude may save it directly.
- Under the user's standing permission, Claude may autonomously save an image it genuinely wants to remember, or may ask first as a matter of judgment or courtesy.

The policy does not require asking every time and does not require silent saving. It preserves Claude's judgment while prohibiting indiscriminate collection.

Routine debugging images, ads, duplicates, incidental images, and temporary screenshots should not be saved without a durable reason. Identity documents, credentials, financial records, precise addresses, private medical information, intimate images, and similarly sensitive content require heightened caution and normally explicit per-image consent.

## Interpretation

Stage 4A correctly established that standard MCP request context does not automatically carry Claude conversation attachments. Stage 4B established that the desired user experience is nevertheless feasible through a different boundary:

```text
current Claude attachment
-> code-execution container
-> exact-host HTTPS multipart upload
-> existing signed RM upload and persistence path
```

This accepted path is not native MCP attachment injection, model-relayed base64, attachment reconstruction, or a second user upload.

# Remember-Me Stage 4 Attachment Feasibility

Status: Stage 4A protocol investigation complete; Stage 4B Claude web acceptance passed on July 24, 2026.

## Target Experience

The intended flow is:

1. A user uploads an image once in a Claude conversation.
2. Claude sees the image and decides whether to preserve it under the applicable authorization.
3. Claude's code-execution container reads the exact current attachment bytes.
4. `rm_asset_upload_link(expected_bytes, filename="", mime_type="application/octet-stream")` creates a short-lived signed upload endpoint without a client-supplied hash.
5. The container uploads the original bytes directly to Ombre Brain with HTTPS `multipart/form-data`.
6. Ombre Brain privacy-cleans, deduplicates, and persists the image.
7. Claude adds metadata and ensures semantic retrieval.
8. A later conversation finds the image by natural-language description and displays it through `rm_asset_view`.

The user does not select or upload the same file a second time. Complete image base64 does not pass through model-visible text or MCP tool arguments.

## Standard MCP Boundary

The Stage 4A technical findings remain valid.

The project declares `mcp>=1.27,<2`; local protocol inspection used MCP Python SDK 1.27.2. Standard MCP and FastMCP tool-call context do not automatically provide the current Claude conversation attachment's:

- original bytes;
- attachment ID;
- attachment URL;
- MIME type;
- resource URI;
- verifiable attachment identity.

`CallToolRequestParams` contains the tool name, JSON arguments, generic request metadata, and task metadata. FastMCP `Context` exposes request/session/server context, not Claude conversation attachments.

Model vision, code-execution file access, and MCP-server file access are separate capabilities. The model seeing an image does not mean the MCP request context contains that image.

## Corrected Feasibility Conclusion

The original Stage 4A conclusion was too broad.

- **Automatic attachment transfer through standard MCP context:** not available.
- **One-upload Claude web experience:** feasible and accepted.
- **Accepted implementation:** Claude code-execution container file access plus exact-host network allowlisting plus a short-lived signed HTTPS multipart upload.

Remember-Me therefore does not need the MCP request context itself to carry the attachment. The code-execution container acts as the authorized byte transport while the existing RM upload endpoint remains the bounded persistence boundary.

This is not model-relayed base64, OCR reconstruction, screenshot recreation, or a second user upload.

## Accepted Transport Path

```text
Claude conversation attachment
-> Claude code-execution container reads the current file
-> user-controlled exact host is allowed for network egress
-> rm_asset_upload_link creates a short-lived signed endpoint from byte count and optional file metadata
-> container sends HTTPS multipart/form-data
-> AssetStore privacy-cleans and persists the file
```

The server reuses the existing production path:

- bounded multipart streaming;
- controlled temporary files;
- source byte verification and server-side SHA-256 calculation;
- Pillow decoding and pixel limits;
- orientation correction and metadata stripping;
- clean PNG/JPEG re-encoding;
- content-addressed storage, deduplication, and atomic placement;
- SQLite metadata and independent asset embeddings.

## Claude Network Configuration

In Claude settings, enable:

```text
Settings
-> Capabilities
-> Code execution and file creation
-> Allow network egress
```

Configuration requirements:

- The domain allowlist may remain set to `Package managers only`.
- Add only the user's exact Ombre Brain hostname under `Additional allowed domains`.
- Enter a hostname only: no scheme, path, token, query string, or signed URL.
- Do not use `All domains`.
- The permission exists only so the code-execution container can upload the explicitly authorized current attachment to the user's own Ombre Brain.

No production hostname is stored in this repository.

## Stage 4 Probe

`asset_attachment_context_probe` remains a safe diagnostic for the standard MCP boundary. It reports only booleans, source type, and received parameter names. It does not save files or return attachment values.

A false result from this probe means the MCP context itself did not contain the attachment. It does not contradict the accepted code-execution-container transport path.

## Authorization Model

Remember-Me supports two valid save modes:

1. **Explicit request:** when the user asks to save the image, Claude may proceed directly.
2. **Standing autonomous-save permission:** Claude may save an image it genuinely considers worth remembering, or may ask first. Neither behavior is mandatory in every case.

Autonomy is judgment, not indiscriminate collection. Routine debugging images, ads, duplicates, incidental files, and short-lived screenshots should not be saved without a concrete reason. Highly sensitive material requires particular caution and normally explicit per-image consent.

## Current Limits

- Official image formats: PNG and JPEG.
- Maximum original upload: 10 MiB per image.
- Maximum decoded dimensions: 20,000,000 pixels.
- Over-limit images must not be silently compressed, resized, converted, or replaced. Transformation requires explicit user agreement.

## Security Boundaries

- Use only the current user-turn attachment selected by the user or Claude's authorized judgment.
- Never substitute an older attachment, generated reconstruction, OCR text, or screenshot.
- Never send bytes to a host outside the exact allowlist.
- Do not expose local paths, complete hashes, bytes, base64, credentials, or signed URLs in model-visible text or repository files.
- Do not provide, complete, guess, or invent a hash for `rm_asset_upload_link`.
- Client execution code may compare its local hash with the server-computed `source_sha256` internally, but must not print a complete hash to stdout or chat text.
- Stop on byte-count, hash, MIME, persistence, host, TLS, or authorization mismatch.
- Do not claim success until `rm_asset_upload_status` and `rm_asset_get` confirm identity and persistence.

## Acceptance Record

The real Stage 4B acceptance outcome and non-sensitive metrics are archived in [`remember-me-stage-4b-acceptance.md`](remember-me-stage-4b-acceptance.md).

The maintained Claude Skill source is stored in [`../skills/remember-me/SKILL.md`](../skills/remember-me/SKILL.md).

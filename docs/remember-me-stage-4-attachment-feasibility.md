# Remember-Me Stage 4 Attachment Feasibility

Status: protocol investigation complete; real Claude web-client probe pending deployment and user-run acceptance.

## Target Experience

The desired flow is:

1. A user uploads an image directly in a Claude conversation.
2. Claude can see the image.
3. After an explicit user request or auditable authorization, Claude asks Ombre Brain to save that exact attachment.
4. The original attachment enters the existing Remember-Me privacy-cleaning and persistent-storage path without a second upload page.
5. A later conversation can find it through `rm_asset_search` and display it with `rm_asset_view`.

Stage 4A investigates whether the attachment can cross the Claude-to-MCP boundary. It does not save attachments and does not claim that direct save is implemented.

## Current Claude Web Capability

Previous acceptance proves that Claude web can visually understand a user attachment and can receive MCP `ImageContent`. Visual access does not establish that the model has the original attachment bytes, an attachment identifier, or a retrievable attachment URL.

No repository or protocol evidence currently shows that Claude web automatically includes its conversation attachment in a third-party MCP tool call. The Stage 4 probe must be run in the real client to detect any undocumented host extension.

## Current MCP Capability

The project declares `mcp>=1.27,<2`; local protocol inspection used MCP Python SDK 1.27.2.

In that SDK:

- `CallToolRequestParams` contains the tool name, JSON arguments, generic request metadata, and task metadata.
- FastMCP `Context` exposes the request context, request ID, session, client ID, and server/lifespan facilities.
- The request context contains the request, generic metadata, session, lifespan context, and experimental server context.
- None of these standard fields defines a current chat attachment, attachment ID, attachment URL, MIME type, file resource, or original attachment bytes.
- Client roots describe filesystem roots exposed by a client; they do not automatically represent Claude conversation attachments.

Therefore model vision, model possession of original bytes, and MCP-server possession of original bytes are three distinct capabilities. Only the first is currently proven.

## Reusable Remember-Me Path

If a future client supplies trustworthy original bytes or a securely retrievable reference, the existing implementation can be reused:

- bounded streaming and temporary-file handling from the signed browser upload route;
- `AssetStore.create_temp_path` for controlled temporary files;
- `AssetStore.persist_upload` for source size/hash verification, Pillow decoding, pixel limits, orientation correction, metadata stripping, re-encoding, content-addressed storage, deduplication, atomic placement, and SQLite metadata;
- `rm_asset_update_metadata` and the existing embedding index for later descriptive metadata and semantic retrieval;
- `rm_asset_inspect` for model vision and `rm_asset_view` for user display of the cleaned stored copy.

The missing component is a trustworthy attachment-byte or attachment-reference input. Storage and retrieval are not the blocker.

## Stage 4 Probe

Tool:

```text
asset_attachment_context_probe(
    attachment_reference: str = "",
    attachment_mime_type: str = ""
)
```

FastMCP injects `Context`; it is not part of the public tool input schema.

The probe returns JSON containing only:

```json
{
  "ok": true,
  "attachment_reference_available": false,
  "attachment_bytes_available": false,
  "mime_type_available": false,
  "source_kind": "none",
  "received_parameter_names": [],
  "original_attachment_identity_verified": false
}
```

Possible `source_kind` values are:

- `none`
- `metadata_only`
- `explicit_reference_parameter`
- `request_context_reference`
- `request_context_bytes`

The probe checks only fixed attachment-like field names in host extension metadata. It does not return field values. Generic unscoped `data`, `url`, or MIME fields are ignored.

The tool description forbids the model from transcribing, redrawing, downloading, OCR-reconstructing, or base64-encoding the image for the probe. An explicit parameter proves only that a parameter was supplied; it does not prove that the value identifies the original attachment. `original_attachment_identity_verified` is always false in Stage 4A.

The probe does not:

- create temporary files;
- call `AssetStore` persistence methods;
- write buckets or databases;
- generate hashes;
- return or log references, URLs, filenames, bytes, base64, or image contents.

## Real Claude Test Procedure

Use a new, non-sensitive synthetic image created only for acceptance testing.

1. Upload the image through the normal Claude conversation attachment control.
2. Ask Claude to call `asset_attachment_context_probe` for the current attachment.
3. Do not provide a URL, attachment ID, filename, hash, base64, or manually copied reference in the prompt.
4. Do not ask Claude to OCR, redraw, screenshot, or reconstruct the image.
5. Record only the returned booleans, `source_kind`, and `received_parameter_names`.
6. Do not paste any hidden reference or attachment value into an issue, repository file, or chat report.
7. Run a second independent conversation to check whether the result is stable rather than a one-off host artifact.

If the probe reports a reference or bytes, stop before saving anything. A separate controlled follow-up must prove that the reference is authorized, short-lived, retrievable by the server, bound to the intended user/session, and byte-identical to the attachment.

## Decision Criteria

### A. Native feasible

A requires the real client to call the probe without explicit attachment parameters while request context reliably reports attachment bytes or a stable reference. A later controlled test must prove safe retrieval and attachment identity. A single true boolean is not sufficient by itself.

### B. Parameter transfer feasible

B requires the client to supply a machine-generated native content object or stable reference without model transcription, base64 relay, OCR, screenshotting, or user re-upload. The server must be able to authenticate and retrieve it reproducibly. A model-authored string is not sufficient.

### C. Currently native-infeasible

C applies when the probe reports `source_kind=none`, metadata only, or an unverified explicit parameter; when the model can only describe the picture; or when success requires base64 relay, reconstruction, or a second browser upload.

## Current Conclusion

**C: currently native-infeasible at the standard MCP layer.**

The standard MCP tool call and FastMCP context do not carry Claude conversation attachments. The real Claude web probe remains necessary to check for a host-specific extension, but no such extension is assumed. Until A or B is proven with stable original-byte identity, the signed browser upload remains the closest safe path and must not be described as conversation-attachment direct save.

Because the current conclusion is C, Stage 4A does not introduce `rm_asset_save_attachment` or a formal attachment-save API.

## Security Risks

Any later A/B implementation must address:

- explicit, auditable user authorization before persistence;
- reference ownership, session binding, expiration, and replay protection;
- server-side authentication for attachment retrieval;
- streaming byte and pixel limits before decoding;
- MIME distrust and full image decoding;
- URL allowlisting and SSRF prevention if references are network locations;
- temporary-file cleanup on every failure path;
- no attachment bytes, URLs, identifiers, hashes, or base64 in logs or model-visible text;
- no automatic metadata claims based only on filenames or model guesses.

Claude liking or recognizing an image is not authorization to save it.

## Next Step

Deploy the diagnostic tool through the normal release process and run the two real Claude web tests above. If both return no native reference or bytes, keep conclusion C and continue using explicit browser upload. If a stable host-native capability appears, document its exact protocol shape without recording live values, then design a separate authenticated ingestion stage that feeds the existing `AssetStore.persist_upload` path.
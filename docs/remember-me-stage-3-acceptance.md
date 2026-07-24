# Remember-Me Stage 3 Acceptance

Status: accepted on July 24, 2026.

Remember-Me is currently the asset-memory module inside Ombre Brain. It is also the project name for a possible future standalone product, but it is not a separate service today.

## Accepted Scope

Stage 3A.2 and the Stage 3B core loop have passed real Claude web-client acceptance testing.

The accepted end-to-end path is:

1. The user uploads a file through the signed browser upload page.
2. Ombre Brain decodes the image, applies orientation, removes private metadata, re-encodes it, and stores the cleaned copy persistently.
3. `rm_asset_inspect` returns that cleaned stored copy as standard MCP `ImageContent` for model inspection.
4. The model reads the image and proposes a title, description, and tags.
5. `rm_asset_update_metadata` stores the approved metadata.
6. The asset semantic index is refreshed through the existing embedding path.
7. In a new conversation, a natural-language `rm_asset_search` query finds the historical asset.
8. `rm_asset_view` displays the original cleaned stored image inline.

The acceptance run confirmed that the model could use the actual image content to create metadata, retrieve the asset later from a differently worded natural-language query, and show the same stored image in a new conversation.

## Claude Web Compatibility Note

The Claude web client may not parse a newly returned MCP `ImageContent` block correctly in the same turn as the tool call. In the accepted test, the next turn could accurately read the image and its visible text without another tool call.

The current compatibility procedure is:

1. Call `rm_asset_inspect(asset_id)` once.
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
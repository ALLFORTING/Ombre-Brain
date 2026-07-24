---
name: remember-me
description: Save, organize, search, and redisplay images in the user's Ombre Brain image memory. Use this skill whenever the user uploads an image and asks to save, remember, keep, collect, store, find, search, recall, or show it; when the user asks for a previously saved image by natural-language description; or when an uploaded image has genuine personal, emotional, relational, creative, or future recall value and Claude is considering preserving it under the user's standing permission for autonomous image saving.
---

# Remember-Me

Use Ombre Brain as a long-term image memory.

The intended experience is:

1. The user uploads an image once in the current Claude conversation.
2. Claude may save it because the user explicitly asks, or because Claude genuinely wants to remember it under the user's standing permission.
3. The exact current attachment is uploaded from Claude's code-execution container to Ombre Brain through a short-lived signed upload endpoint.
4. Ombre Brain privacy-cleans and persists the image.
5. Claude adds accurate metadata and ensures semantic searchability.
6. A later conversation can find the image from a natural-language description and display the original image inline.

## Standing permission for autonomous saving

The user has granted continuing permission to save an uploaded image without asking again when Claude genuinely considers it worth remembering.

This permits judgment, not indiscriminate collection.

Autonomous saving is appropriate when there is a concrete reason, such as:

- emotional or relational significance;
- a shared moment, running joke, creative idea, or meaningful preference;
- likely value in a future conversation;
- a specific reason Claude wants to revisit it;
- losing it would reduce continuity between conversations.

Do not automatically save:

- routine error screenshots, temporary debugging screenshots, receipts, ads, or incidental images;
- duplicates or near-duplicates;
- identity documents, financial records, exact addresses, credentials, private medical records, intimate images, or other highly sensitive material;
- an image whose privacy status or the user's intent is unclear.

For sensitive or ambiguous material, ask first.

After an autonomous save, tell the user naturally that it was saved and briefly explain why Claude wanted to keep it.

## Save the current attachment

### 1. Identify the intended image

- Work only with image attachments from the current user turn.
- If there is exactly one current image, use it.
- If there are multiple current images and the intended one is unclear, ask which one.
- Never substitute an older attachment, generated reconstruction, screenshot, OCR text, or visual description for the original file.
- Do not read or upload unrelated container files.

### 2. Read and fingerprint locally

Use code execution to locate the current attachment and read its exact bytes.

Internally calculate:

- source byte count;
- SHA-256;
- detected MIME type;
- dimensions when available.

Never expose the complete local path, complete hash, raw bytes, base64, cookies, environment variables, authentication tokens, or complete signed upload URL in chat text or stdout.

### 3. Obtain a signed upload endpoint

Call `rm_asset_upload_link`.

Use the returned short-lived upload endpoint only for the intended image.

### 4. Upload directly from code execution

Send the exact original attachment bytes to the signed endpoint using HTTPS `multipart/form-data`.

Requirements:

- use only the exact Ombre Brain host in the user's network allowlist;
- never switch network access to all domains;
- do not relay the file through model-visible base64 or long tool arguments;
- do not upload any other container file;
- follow redirects only on the same allowed host;
- stop on unexpected hosts, certificate errors, authorization errors, or non-success responses.

If network access is unavailable, tell the user to enable:

`Settings → Capabilities → Code execution and file creation → Allow network egress`

and add only the exact Ombre Brain hostname under `Additional allowed domains`.

Do not ask the user to paste tokens, signed URLs, or base64.

### 5. Verify identity and persistence

Call `rm_asset_upload_status`.

Verify:

- uploaded original byte count equals the local source byte count;
- uploaded original SHA-256 equals the local SHA-256;
- MIME type is consistent;
- upload state is completed.

On mismatch, stop and report the real failure. Do not continue to metadata or indexing.

Call `rm_asset_get` and confirm that the asset is persistent.

### 6. Add metadata

Create from the actual image:

- a concise title;
- a useful description covering visible content, important text, style, context, and recall cues;
- 5–8 specific tags.

Call `rm_asset_update_metadata`.

Never place base64, paths, signed URLs, hashes, tokens, or internal implementation details in metadata.

### 7. Ensure semantic searchability

Call `rm_asset_reindex_embeddings`.

Both outcomes are successful:

- a new embedding is indexed;
- the asset is scanned and skipped because its existing embedding is already current.

Do not force repeated reindexing merely to increase `indexed`.

### 8. Report naturally

For an explicit save request, confirm that the image is saved and searchable.

For an autonomous save, say that Claude chose to keep it and give the genuine reason in one or two natural sentences.

Do not dump internal IDs, hashes, signed URLs, or logs unless the user explicitly asks for diagnostics.

## Find and display a saved image

1. Use `rm_asset_search` with the user's natural-language description.
2. Prefer semantic and metadata evidence over asking for an asset ID.
3. When one result is clearly correct, call `rm_asset_view` and display it inline.
4. When several results are plausible, distinguish them briefly and ask the user to choose.
5. Use `rm_asset_inspect` only when Claude itself needs to understand the stored image's real visual content.

`rm_asset_view` shows the image to the user.

`rm_asset_inspect` gives the model the stored image as MCP ImageContent.

Do not infer contents from filename or metadata when actual visual inspection is needed.

## Claude web ImageContent compatibility

Claude web may occasionally fail to parse MCP ImageContent in the same turn that `rm_asset_inspect` is called.

When that happens:

- do not repeatedly call `rm_asset_inspect`;
- in the next turn, re-examine the ImageContent already present in context;
- never pretend to have seen details that were not visible.

## Security rules

- User authorization or standing autonomous-save permission is required.
- Never replace the original with a model-generated recreation.
- Never send image data to any domain except the exact user-controlled Ombre Brain host.
- Keep network access limited to the exact allowlisted host.
- Never expose raw bytes or complete base64 in model-visible text.
- Never write image bytes into Markdown memory buckets.
- Do not store highly sensitive images without explicit per-image consent.
- Do not claim success until persistence and identity checks pass.

## Trigger examples

- “把这张图片存进 OB。”
- “记住这张。”
- “这张以后别弄丢。”
- “帮我找以前那张几只小猫挤在篮子里的图片。”
- The user uploads an emotionally meaningful image without asking to save it, and Claude has a genuine reason to preserve it under the standing permission.

Do not autonomously save merely because the user uploaded a routine image for analysis.

## Current image limits

- Officially supported image formats are PNG and JPEG.
- The current maximum original upload size is 2 MiB per image.
- The current maximum decoded image size is 20,000,000 pixels.
- If an image exceeds either limit, do not silently compress, resize, convert, or substitute it. Only transform the original after the user explicitly agrees.
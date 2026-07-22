import base64
import hashlib
import importlib
import json
import sys
from pathlib import Path

import pytest


def _load_server(tmp_path, monkeypatch):
    monkeypatch.setenv("OMBRE_BUCKETS_DIR", str(tmp_path / "buckets"))
    monkeypatch.delenv("OMBRE_API_KEY", raising=False)
    sys.modules.pop("server", None)
    return importlib.import_module("server")


@pytest.mark.asyncio
async def test_asset_ingest_probe_accepts_base64_and_hashes_without_files(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    payload = b"phase-0 binary payload"
    encoded = base64.b64encode(payload).decode("ascii")
    expected = hashlib.sha256(payload).hexdigest()
    before = sorted(str(path.relative_to(tmp_path)) for path in tmp_path.rglob("*"))

    result = json.loads(await server.asset_ingest_probe(encoded, expected, "application/octet-stream"))
    after = sorted(str(path.relative_to(tmp_path)) for path in tmp_path.rglob("*"))

    assert result["ok"] is True
    assert result["base64_chars"] == len(encoded)
    assert result["decoded_bytes"] == len(payload)
    assert result["sha256"] == expected
    assert result["expected_sha256"] == expected
    assert result["hash_match"] is True
    assert result["mime_type"] == "application/octet-stream"
    assert after == before


@pytest.mark.asyncio
async def test_asset_ingest_probe_rejects_invalid_base64(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)

    result = json.loads(await server.asset_ingest_probe("not valid ***"))

    assert result["ok"] is False
    assert result["error"] == "invalid_base64"


@pytest.mark.asyncio
async def test_asset_ingest_probe_rejects_oversized_input(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)

    result = json.loads(await server.asset_ingest_probe("A" * (server.ASSET_PROBE_MAX_BASE64_CHARS + 1)))

    assert result["ok"] is False
    assert result["error"] == "base64_too_large"
    assert result["max_base64_chars"] == server.ASSET_PROBE_MAX_BASE64_CHARS


@pytest.mark.asyncio
async def test_asset_render_probe_returns_png_image_block(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)

    result = await server.asset_render_probe()
    assert isinstance(result, server.CallToolResult)
    image_blocks = [block for block in result.content if getattr(block, "type", None) == "image"]
    text_blocks = [block for block in result.content if getattr(block, "type", None) == "text"]

    assert len(image_blocks) == 1
    image = image_blocks[0]
    assert image.mimeType == "image/png"
    decoded = base64.b64decode(image.data, validate=True)
    assert decoded.startswith(b"\x89PNG\r\n\x1a\n")
    assert Path(server.ASSET_PROBE_PATH).name == "probe.png"
    assert all(image.data not in getattr(block, "text", "") for block in text_blocks)
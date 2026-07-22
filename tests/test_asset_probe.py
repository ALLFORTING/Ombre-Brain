import base64
import hashlib
import importlib
import json
import struct
import sys
import zlib
from pathlib import Path

import pytest


PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
PNG_METADATA_CHUNKS = {b"eXIf", b"tEXt", b"zTXt", b"iTXt"}


def _parse_png(data):
    assert data.startswith(PNG_SIGNATURE)
    offset = len(PNG_SIGNATURE)
    chunks = []
    while offset < len(data):
        assert offset + 12 <= len(data)
        length = struct.unpack(">I", data[offset:offset + 4])[0]
        chunk_type = data[offset + 4:offset + 8]
        chunk_data_start = offset + 8
        chunk_data_end = chunk_data_start + length
        crc_start = chunk_data_end
        crc_end = crc_start + 4
        assert crc_end <= len(data)
        chunk_data = data[chunk_data_start:chunk_data_end]
        expected_crc = struct.unpack(">I", data[crc_start:crc_end])[0]
        actual_crc = zlib.crc32(chunk_type + chunk_data) & 0xFFFFFFFF
        assert actual_crc == expected_crc
        chunks.append((chunk_type, chunk_data))
        offset = crc_end
        if chunk_type == b"IEND":
            break
    assert offset == len(data)
    return chunks


def _assert_valid_probe_png(data):
    chunks = _parse_png(data)
    chunk_types = [chunk_type for chunk_type, _ in chunks]
    assert b"IHDR" in chunk_types
    assert b"IDAT" in chunk_types
    assert b"IEND" in chunk_types
    assert not PNG_METADATA_CHUNKS.intersection(chunk_types)

    ihdr = next(chunk_data for chunk_type, chunk_data in chunks if chunk_type == b"IHDR")
    width, height, bit_depth, color_type, compression, filter_method, interlace = struct.unpack(
        ">IIBBBBB", ihdr
    )
    assert (width, height) == (128, 128)
    assert bit_depth == 8
    assert color_type in (2, 6)
    assert compression == 0
    assert filter_method == 0
    assert interlace == 0

    idat = b"".join(chunk_data for chunk_type, chunk_data in chunks if chunk_type == b"IDAT")
    raw = zlib.decompress(idat)
    channels = 3 if color_type == 2 else 4
    assert len(raw) == height * (1 + width * channels)
    assert chunks[-1][0] == b"IEND"
    return {"width": width, "height": height, "color_type": color_type, "chunks": chunk_types}


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
async def test_asset_render_probe_returns_valid_png_image_block(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)

    result = await server.asset_render_probe()
    assert isinstance(result, server.CallToolResult)
    image_blocks = [block for block in result.content if getattr(block, "type", None) == "image"]
    text_blocks = [block for block in result.content if getattr(block, "type", None) == "text"]

    assert len(image_blocks) == 1
    image = image_blocks[0]
    assert image.mimeType == "image/png"
    decoded = base64.b64decode(image.data, validate=True)
    disk_bytes = Path(server.ASSET_PROBE_PATH).read_bytes()
    assert decoded == disk_bytes
    info = _assert_valid_probe_png(decoded)
    assert info["width"] == 128
    assert info["height"] == 128
    assert Path(server.ASSET_PROBE_PATH).name == "probe.png"
    assert all(image.data not in getattr(block, "text", "") for block in text_blocks)
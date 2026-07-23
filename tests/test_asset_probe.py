import asyncio
import base64
import hashlib
import importlib
import json
import re
import struct
import sys
import zlib
from pathlib import Path

import pytest


PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
PNG_METADATA_CHUNKS = {b"eXIf", b"tEXt", b"zTXt", b"iTXt", b"iCCP"}


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


def _assert_valid_probe_png(data, expected_size=(128, 128), expected_color_type=None):
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
    assert (width, height) == expected_size
    assert bit_depth == 8
    if expected_color_type is None:
        assert color_type in (2, 6)
    else:
        assert color_type == expected_color_type
    assert compression == 0
    assert filter_method == 0
    assert interlace == 0

    idat = b"".join(chunk_data for chunk_type, chunk_data in chunks if chunk_type == b"IDAT")
    raw = zlib.decompress(idat)
    channels = 3 if color_type == 2 else 4
    assert len(raw) == height * (1 + width * channels)
    assert chunks[-1][0] == b"IEND"
    return {"width": width, "height": height, "color_type": color_type, "chunks": chunk_types, "raw": raw}


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

@pytest.mark.asyncio
async def test_asset_export_probe_returns_user_visible_base64_payload(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)

    result_text = await server.asset_export_probe()
    payload = json.loads(result_text)

    assert payload["ok"] is True
    assert payload["filename"] == "remember-me-probe.png"
    assert payload["mime_type"] == "image/png"
    decoded = base64.b64decode(payload["data_base64"], validate=True)
    disk_bytes = Path(server.ASSET_PROBE_PATH).read_bytes()
    assert decoded == disk_bytes
    assert payload["decoded_bytes"] == len(disk_bytes)
    assert payload["sha256"] == hashlib.sha256(disk_bytes).hexdigest()
    assert _assert_valid_probe_png(decoded)["chunks"] == [b"IHDR", b"IDAT", b"IEND"]

    render_result = await server.asset_render_probe()
    render_image = next(block for block in render_result.content if getattr(block, "type", None) == "image")
    assert base64.b64decode(render_image.data, validate=True) == decoded
    assert str(Path.cwd()) not in result_text
    assert str(Path.home()) not in result_text
    assert not re.search(r"[A-Za-z]:[\\/]", result_text)
    assert "/".join(["", "app", "assets"]) not in result_text
    assert "data:image/" not in payload["data_base64"]

@pytest.mark.asyncio
async def test_asset_vision_challenge_returns_blind_text_and_png(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)

    result = await server.asset_vision_challenge()
    assert isinstance(result, server.CallToolResult)
    image_blocks = [block for block in result.content if getattr(block, "type", None) == "image"]
    text_blocks = [block for block in result.content if getattr(block, "type", None) == "text"]
    assert len(image_blocks) == 1
    assert len(text_blocks) == 1

    prompt = json.loads(text_blocks[0].text)
    assert set(prompt) == {
        "allowed_colors",
        "allowed_symbol_positions",
        "allowed_symbols",
        "answer_format",
        "decoded_bytes",
        "sha256",
        "submit_to",
        "trial_id",
    }
    assert len(prompt["trial_id"]) == 32
    assert prompt["answer_format"] == {
        "top_left": "<color>",
        "top_right": "<color>",
        "bottom_left": "<color>",
        "bottom_right": "<color>",
        "symbol": "<symbol>",
        "symbol_position": "<position>",
    }
    assert prompt["submit_to"] == "asset_vision_verify"
    assert set(prompt["allowed_colors"]) == set(server.ASSET_VISION_COLORS)
    assert set(prompt["allowed_symbols"]) == set(server.ASSET_VISION_SYMBOLS)
    assert set(prompt["allowed_symbol_positions"]) == set(server.ASSET_VISION_POSITIONS)
    assert prompt["trial_id"] in server._asset_vision_trials

    trial_answer = server._asset_vision_trials[prompt["trial_id"]]["answer"]
    prompt_text = text_blocks[0].text
    for position in server.ASSET_VISION_POSITIONS:
        assert f'"{position}": "{trial_answer[position]}"' not in prompt_text
    assert f'"symbol": "{trial_answer["symbol"]}"' not in prompt_text
    assert f'"symbol_position": "{trial_answer["symbol_position"]}"' not in prompt_text

    image = image_blocks[0]
    assert image.mimeType == "image/png"
    decoded = base64.b64decode(image.data, validate=True)
    info = _assert_valid_probe_png(decoded, expected_size=(256, 256), expected_color_type=2)
    assert info["chunks"] == [b"IHDR", b"IDAT", b"IEND"]
    assert prompt["decoded_bytes"] == len(decoded)
    assert prompt["sha256"] == hashlib.sha256(decoded).hexdigest()
    assert server._asset_vision_trials[prompt["trial_id"]]["sha256"] == prompt["sha256"]
    assert server._asset_vision_trials[prompt["trial_id"]]["exported"] is False


@pytest.mark.asyncio
async def test_asset_vision_export_matches_challenge_png_and_keeps_trial_verifiable(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)

    challenge = await server.asset_vision_challenge()
    prompt = json.loads(next(block.text for block in challenge.content if getattr(block, "type", None) == "text"))
    image = next(block for block in challenge.content if getattr(block, "type", None) == "image")
    challenge_png = base64.b64decode(image.data, validate=True)

    export_text = await server.asset_vision_export(prompt["trial_id"])
    exported = json.loads(export_text)
    exported_png = base64.b64decode(exported["data_base64"], validate=True)
    answer = dict(server._asset_vision_trials[prompt["trial_id"]]["answer"])
    verify = json.loads(await server.asset_vision_verify(prompt["trial_id"], json.dumps(answer)))

    assert exported["ok"] is True
    assert exported["trial_id"] == prompt["trial_id"]
    assert exported["filename"] == f"remember-me-vision-{prompt['trial_id']}.png"
    assert re.fullmatch(r"remember-me-vision-[0-9a-f]{32}\.png", exported["filename"])
    assert exported["mime_type"] == "image/png"
    assert exported["decoded_bytes"] == len(challenge_png) == prompt["decoded_bytes"]
    assert exported["sha256"] == hashlib.sha256(challenge_png).hexdigest() == prompt["sha256"]
    assert exported_png == challenge_png
    assert _assert_valid_probe_png(exported_png, expected_size=(256, 256), expected_color_type=2)["chunks"] == [b"IHDR", b"IDAT", b"IEND"]
    assert "answer" not in exported
    assert "field_results" not in exported
    assert "top_left" not in export_text
    assert verify["ok"] is True
    assert verify["score"] == 6


@pytest.mark.asyncio
async def test_asset_vision_export_rejects_second_verified_expired_missing_and_invalid_trials(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    challenge = await server.asset_vision_challenge()
    trial_id = json.loads(next(block.text for block in challenge.content if getattr(block, "type", None) == "text"))["trial_id"]
    answer = dict(server._asset_vision_trials[trial_id]["answer"])

    first_export = json.loads(await server.asset_vision_export(trial_id))
    second_export = json.loads(await server.asset_vision_export(trial_id))
    verify = json.loads(await server.asset_vision_verify(trial_id, json.dumps(answer)))
    verified_export = json.loads(await server.asset_vision_export(trial_id))

    expired = server._asset_new_vision_trial(now=100.0)
    server._asset_store_vision_trial(expired, now=100.0)
    expired_export = json.loads(server._asset_export_vision_trial(expired["trial_id"], now=100.0 + server.ASSET_VISION_TTL_SECONDS + 1))
    missing_export = json.loads(await server.asset_vision_export("0" * 32))
    invalid_export = json.loads(await server.asset_vision_export("../" + trial_id))

    assert first_export["ok"] is True
    assert second_export == {"ok": False, "trial_id": trial_id, "error": "already_exported"}
    assert verify["ok"] is True
    assert verified_export == {"ok": False, "trial_id": trial_id, "error": "trial_unavailable"}
    assert expired_export == {"ok": False, "trial_id": expired["trial_id"], "error": "trial_unavailable"}
    assert missing_export == {"ok": False, "trial_id": "0" * 32, "error": "trial_unavailable"}
    assert invalid_export["ok"] is False
    assert invalid_export["error"] == "invalid_trial_id"
    assert "filename" not in invalid_export


@pytest.mark.asyncio
async def test_asset_vision_export_concurrent_single_success_and_safe_filename(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    challenge = await server.asset_vision_challenge()
    trial_id = json.loads(next(block.text for block in challenge.content if getattr(block, "type", None) == "text"))["trial_id"]

    results = await asyncio.gather(*(server.asset_vision_export(trial_id) for _ in range(8)))
    parsed = [json.loads(result) for result in results]
    successes = [result for result in parsed if result.get("ok") is True]
    failures = [result for result in parsed if result.get("ok") is False]

    assert len(successes) == 1
    assert len(failures) == 7
    assert all(result["error"] == "already_exported" for result in failures)
    assert successes[0]["filename"] == f"remember-me-vision-{trial_id}.png"
    assert "/" not in successes[0]["filename"]
    assert "\\" not in successes[0]["filename"]
    assert ".." not in successes[0]["filename"]


@pytest.mark.asyncio
async def test_asset_vision_verify_scores_correct_and_consumes_trial(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    trial = server._asset_new_vision_trial()
    ok, error = server._asset_store_vision_trial(trial)
    assert ok, error

    result = json.loads(await server.asset_vision_verify(trial["trial_id"], json.dumps(trial["answer"])))

    assert result == {
        "ok": True,
        "trial_id": trial["trial_id"],
        "score": 6,
        "max_score": 6,
        "all_correct": True,
        "field_results": {
            "top_left": True,
            "top_right": True,
            "bottom_left": True,
            "bottom_right": True,
            "symbol": True,
            "symbol_position": True,
        },
    }
    assert trial["trial_id"] not in server._asset_vision_trials
    assert not set(result).intersection(set(server.ASSET_VISION_COLORS))


@pytest.mark.asyncio
async def test_asset_vision_verify_scores_partial_answer(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    trial = server._asset_new_vision_trial()
    server._asset_store_vision_trial(trial)
    answer = dict(trial["answer"])
    answer["top_left"] = next(color for color in server.ASSET_VISION_COLORS if color != answer["top_left"])

    result = json.loads(await server.asset_vision_verify(trial["trial_id"], json.dumps(answer)))

    assert result["ok"] is True
    assert result["score"] == 5
    assert result["max_score"] == 6
    assert result["all_correct"] is False
    assert result["field_results"]["top_left"] is False
    assert all(result["field_results"][key] for key in result["field_results"] if key != "top_left")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad_answer,expected_error",
    [
        ("not json", "invalid_json"),
        (json.dumps({}), "invalid_fields"),
        (json.dumps({"top_left": "red", "top_right": "green", "bottom_left": "blue", "bottom_right": "orange", "symbol": "circle"}), "invalid_fields"),
        (json.dumps({"top_left": "red", "top_right": "green", "bottom_left": "blue", "bottom_right": "orange", "symbol": "circle", "symbol_position": "top_left", "extra": "no"}), "invalid_fields"),
        (json.dumps({"top_left": 1, "top_right": "green", "bottom_left": "blue", "bottom_right": "orange", "symbol": "circle", "symbol_position": "top_left"}), "invalid_field_type"),
        (json.dumps({"top_left": "cyan", "top_right": "green", "bottom_left": "blue", "bottom_right": "orange", "symbol": "circle", "symbol_position": "top_left"}), "invalid_enum"),
    ],
)
async def test_asset_vision_verify_rejects_invalid_answers_and_consumes_trial(tmp_path, monkeypatch, bad_answer, expected_error):
    server = _load_server(tmp_path, monkeypatch)
    trial = server._asset_new_vision_trial()
    server._asset_store_vision_trial(trial)

    result = json.loads(await server.asset_vision_verify(trial["trial_id"], bad_answer))
    second = json.loads(await server.asset_vision_verify(trial["trial_id"], json.dumps(trial["answer"])))

    assert result["ok"] is False
    assert result["error"] == expected_error
    assert "field_results" not in result
    assert second["ok"] is False
    assert second["error"] == "trial_unavailable"


@pytest.mark.asyncio
async def test_asset_vision_verify_second_submit_expired_and_missing_fail(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    trial = server._asset_new_vision_trial()
    server._asset_store_vision_trial(trial)

    first = json.loads(await server.asset_vision_verify(trial["trial_id"], json.dumps(trial["answer"])))
    second = json.loads(await server.asset_vision_verify(trial["trial_id"], json.dumps(trial["answer"])))
    expired = server._asset_new_vision_trial(now=100.0)
    server._asset_store_vision_trial(expired, now=100.0)
    expired_result = json.loads(server._asset_score_vision_answer(expired["trial_id"], json.dumps(expired["answer"]), now=100.0 + server.ASSET_VISION_TTL_SECONDS + 1))
    missing = json.loads(await server.asset_vision_verify("missing-trial", json.dumps(trial["answer"])))

    assert first["ok"] is True
    assert second["ok"] is False
    assert second["error"] == "trial_unavailable"
    assert expired_result["ok"] is False
    assert expired_result["error"] == "trial_unavailable"
    assert missing["ok"] is False
    assert missing["error"] == "trial_unavailable"


@pytest.mark.asyncio
async def test_asset_vision_concurrent_create_and_verify_are_safe(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)

    challenges = await asyncio.gather(*(server.asset_vision_challenge() for _ in range(20)))
    trial_ids = [json.loads(next(block.text for block in result.content if getattr(block, "type", None) == "text"))["trial_id"] for result in challenges]
    assert len(set(trial_ids)) == 20

    trial = server._asset_new_vision_trial()
    server._asset_store_vision_trial(trial)
    results = await asyncio.gather(*(server.asset_vision_verify(trial["trial_id"], json.dumps(trial["answer"])) for _ in range(8)))
    parsed = [json.loads(result) for result in results]
    assert sum(1 for result in parsed if result.get("ok") is True) == 1
    assert sum(1 for result in parsed if result.get("error") == "trial_unavailable") == 7


def test_asset_vision_trial_limit_and_expired_cleanup(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    monkeypatch.setattr(server, "ASSET_VISION_MAX_TRIALS", 2)
    server._asset_vision_trials.clear()

    first = server._asset_new_vision_trial(now=100.0)
    second = server._asset_new_vision_trial(now=100.0)
    third = server._asset_new_vision_trial(now=100.0)
    assert server._asset_store_vision_trial(first, now=100.0) == (True, "")
    assert server._asset_store_vision_trial(second, now=100.0) == (True, "")
    assert server._asset_store_vision_trial(third, now=100.0) == (False, "trial_store_full")

    server._asset_vision_trials.clear()
    expired = server._asset_new_vision_trial(now=100.0)
    fresh = server._asset_new_vision_trial(now=100.0 + server.ASSET_VISION_TTL_SECONDS + 1)
    assert server._asset_store_vision_trial(expired, now=100.0) == (True, "")
    assert server._asset_store_vision_trial(fresh, now=100.0 + server.ASSET_VISION_TTL_SECONDS + 1) == (True, "")
    assert list(server._asset_vision_trials) == [fresh["trial_id"]]

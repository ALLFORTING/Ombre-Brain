import concurrent.futures
import hashlib
import importlib
import io
import json
import sqlite3
import sys
from pathlib import Path

import pytest
from PIL import ExifTags, Image, PngImagePlugin
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

import asset_store as asset_store_module
from asset_store import AssetStore, InvalidAssetImage


def _temp_source(store, data, suffix=".upload"):
    path = store.create_temp_path(suffix)
    path.write_bytes(data)
    return path


def _persist(store, data, filename="asset.bin", mime_type="application/octet-stream"):
    digest = hashlib.sha256(data).hexdigest()
    return store.persist_upload(
        _temp_source(store, data),
        digest,
        len(data),
        filename,
        mime_type,
    )


def _stored_path(store, asset):
    return store.data_root / asset["stored_relpath"]


def _png_with_text():
    image = Image.new("RGBA", (32, 20), (12, 34, 56, 200))
    metadata = PngImagePlugin.PngInfo()
    metadata.add_text("private-note", "remove me")
    output = io.BytesIO()
    image.save(output, format="PNG", pnginfo=metadata)
    image.close()
    return output.getvalue()


def _jpeg_with_exif_gps_orientation():
    image = Image.new("RGB", (20, 10), "red")
    exif = Image.Exif()
    exif[ExifTags.Base.Orientation] = 6
    gps = exif.get_ifd(ExifTags.IFD.GPSInfo)
    gps[1] = "N"
    gps[2] = (1.0, 2.0, 3.0)
    output = io.BytesIO()
    image.save(output, format="JPEG", quality=95, exif=exif)
    image.close()
    return output.getvalue()


def _load_server(tmp_path, monkeypatch):
    monkeypatch.setenv("OMBRE_BUCKETS_DIR", str(tmp_path / "buckets"))
    monkeypatch.delenv("OMBRE_API_KEY", raising=False)
    sys.modules.pop("server", None)
    return importlib.import_module("server")


def _asset_client(server):
    app = Starlette(routes=[
        Route(
            "/rm/asset-upload/{token}",
            server.rm_asset_upload_route,
            methods=["GET", "POST"],
        ),
        Route(
            "/rm/asset-download/{token}",
            server.rm_asset_download_route,
            methods=["GET", "HEAD"],
        ),
    ])
    return TestClient(app)


def test_binary_asset_persists_across_store_restart(tmp_path):
    root = tmp_path / "data"
    store = AssetStore(root)
    payload = b"persistent Remember-Me binary"
    asset = _persist(store, payload, "../../unsafe\\file.bin")

    assert asset["kind"] == "file"
    assert asset["mime_type"] == "application/octet-stream"
    assert asset["decoded_bytes"] == len(payload)
    assert asset["stored_bytes"] == len(payload)
    assert asset["source_sha256"] == hashlib.sha256(payload).hexdigest()
    assert asset["stored_sha256"] == asset["source_sha256"]
    assert "/" not in asset["original_filename"]
    assert "\\" not in asset["original_filename"]
    path = _stored_path(store, asset)
    assert path.read_bytes() == payload
    assert path.relative_to(root).as_posix() == (
        f"assets/{asset['stored_sha256'][:2]}/{asset['stored_sha256']}.bin"
    )

    restarted = AssetStore(root)
    assert restarted.get(asset["asset_id"]) == store.get(asset["asset_id"])
    assert restarted.resolve_file(asset["asset_id"])[1].read_bytes() == payload
    with sqlite3.connect(restarted.db_path) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(assets)")}
    assert {
        "asset_id", "source_sha256", "stored_sha256", "stored_relpath",
        "original_filename", "mime_type", "kind", "decoded_bytes",
        "stored_bytes", "width", "height", "created_at",
    }.issubset(columns)


def test_png_is_reencoded_without_text_metadata(tmp_path):
    store = AssetStore(tmp_path / "data")
    source = _png_with_text()
    asset = _persist(store, source, "private.png", "image/png")
    stored = _stored_path(store, asset).read_bytes()

    assert asset["kind"] == "image"
    assert asset["mime_type"] == "image/png"
    assert (asset["width"], asset["height"]) == (32, 20)
    assert asset["source_sha256"] == hashlib.sha256(source).hexdigest()
    assert asset["stored_sha256"] == hashlib.sha256(stored).hexdigest()
    with Image.open(io.BytesIO(stored)) as cleaned:
        cleaned.load()
        assert cleaned.format == "PNG"
        assert "private-note" not in cleaned.info
        assert "exif" not in cleaned.info
        assert "icc_profile" not in cleaned.info


def test_jpeg_applies_orientation_and_removes_exif_gps(tmp_path):
    store = AssetStore(tmp_path / "data")
    source = _jpeg_with_exif_gps_orientation()
    with Image.open(io.BytesIO(source)) as original:
        assert original.getexif().get(ExifTags.Base.Orientation) == 6
        assert original.getexif().get_ifd(ExifTags.IFD.GPSInfo)

    asset = _persist(store, source, "camera.jpg", "image/jpeg")
    stored = _stored_path(store, asset).read_bytes()
    assert asset["mime_type"] == "image/jpeg"
    assert (asset["width"], asset["height"]) == (10, 20)
    assert asset["stored_sha256"] == hashlib.sha256(stored).hexdigest()
    with Image.open(io.BytesIO(stored)) as cleaned:
        cleaned.load()
        assert cleaned.size == (10, 20)
        assert not cleaned.getexif()
        assert "exif" not in cleaned.info
        assert "icc_profile" not in cleaned.info


def test_invalid_image_and_pixel_limit_leave_no_files(tmp_path, monkeypatch):
    store = AssetStore(tmp_path / "data")
    invalid = b"not a real png"
    with pytest.raises(InvalidAssetImage):
        _persist(store, invalid, "fake.png", "image/png")
    assert not list(store.temp_dir.iterdir())
    assert not [path for path in store.assets_dir.rglob("*") if path.is_file()]

    monkeypatch.setattr(asset_store_module, "MAX_IMAGE_PIXELS", 100)
    image = Image.new("RGB", (11, 10), "blue")
    output = io.BytesIO()
    image.save(output, format="PNG")
    image.close()
    with pytest.raises(InvalidAssetImage):
        _persist(store, output.getvalue(), "large.png", "image/png")
    assert not list(store.temp_dir.iterdir())
    assert not [path for path in store.assets_dir.rglob("*") if path.is_file()]


def test_stored_sha_dedup_and_concurrent_upload(tmp_path):
    store = AssetStore(tmp_path / "data")
    payload = b"same cleaned content" * 100
    first = _persist(store, payload)
    second = _persist(store, payload)
    assert first["asset_id"] == second["asset_id"]
    assert first["deduplicated"] is False
    assert second["deduplicated"] is True

    sources = [_temp_source(store, payload) for _ in range(8)]
    digest = hashlib.sha256(payload).hexdigest()

    def persist_one(path):
        return store.persist_upload(path, digest, len(payload), "same.bin", "application/octet-stream")

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(persist_one, sources))
    assert {item["asset_id"] for item in results} == {first["asset_id"]}
    assert all(item["deduplicated"] is True for item in results)
    files = [
        path for path in store.assets_dir.rglob("*")
        if path.is_file() and store.temp_dir not in path.parents
    ]
    assert len(files) == 1
    with sqlite3.connect(store.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM assets").fetchone()[0] == 1
    assert not list(store.temp_dir.iterdir())


@pytest.mark.asyncio
async def test_rm_asset_http_upload_get_and_download(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    payload = b"browser-to-persistent-store" * 1000
    source_sha = hashlib.sha256(payload).hexdigest()
    link_text = await server.rm_asset_upload_link(
        len(payload), source_sha, "../../upload\\control.bin", "application/octet-stream"
    )
    link = json.loads(link_text)
    assert link["ok"] is True
    assert "data_base64" not in link_text

    with _asset_client(server) as client:
        page = client.get(link["upload_path"])
        uploaded = client.post(
            link["upload_path"],
            files={"file": ("ignored.bin", payload, "application/octet-stream")},
        )
    assert page.status_code == 200
    assert page.headers["cache-control"] == "no-store"
    assert uploaded.status_code == 200

    status_text = await server.rm_asset_upload_status(link["upload_id"])
    status = json.loads(status_text)
    assert status["state"] == "completed"
    assert status["asset_id"]
    assert status["source_sha256"] == source_sha
    assert status["stored_sha256"] == source_sha
    assert status["decoded_bytes"] == len(payload)
    assert status["stored_bytes"] == len(payload)
    assert status["kind"] == "file"
    assert status["deduplicated"] is False
    assert "/" not in status["filename"] and "\\" not in status["filename"]
    assert "stored_relpath" not in status_text
    assert "data_base64" not in status_text

    get_text = await server.rm_asset_get(status["asset_id"])
    metadata = json.loads(get_text)
    assert metadata["ok"] is True
    assert metadata["stored_sha256"] == source_sha
    assert "stored_relpath" not in get_text
    assert payload.decode("ascii") not in get_text

    download = json.loads(await server.rm_asset_download_link(status["asset_id"]))
    assert "stored_relpath" not in json.dumps(download)
    with _asset_client(server) as client:
        head = client.head(download["download_path"])
        gets = [client.get(download["download_path"]) for _ in range(4)]
    assert head.status_code == 200 and head.content == b""
    assert head.headers["content-length"] == str(len(payload))
    assert gets[0].content == payload
    assert hashlib.sha256(gets[0].content).hexdigest() == status["stored_sha256"]
    assert [response.status_code for response in gets] == [200, 200, 200, 404]
    assert gets[0].headers["content-type"].startswith("application/octet-stream")
    assert gets[0].headers["cache-control"] == "no-store"
    assert gets[0].headers["x-content-type-options"] == "nosniff"
    assert str(tmp_path) not in gets[0].headers.get("content-disposition", "")


@pytest.mark.asyncio
async def test_rm_asset_upload_rejects_bad_image_and_hash_mismatch(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    bad = b"not an image"
    digest = hashlib.sha256(bad).hexdigest()
    image_link = json.loads(await server.rm_asset_upload_link(
        len(bad), digest, "fake.png", "image/png"
    ))
    wrong_link = json.loads(await server.rm_asset_upload_link(
        len(bad), "0" * 64, "bad.bin", "application/octet-stream"
    ))

    with _asset_client(server) as client:
        image_response = client.post(
            image_link["upload_path"], files={"file": ("fake.png", bad, "image/png")}
        )
        wrong_response = client.post(
            wrong_link["upload_path"], files={"file": ("bad.bin", bad)}
        )
    assert image_response.status_code == 422
    assert wrong_response.status_code == 422
    assert json.loads(await server.rm_asset_upload_status(image_link["upload_id"]))["state"] == "pending"
    assert json.loads(await server.rm_asset_upload_status(wrong_link["upload_id"]))["state"] == "pending"
    assert server.asset_store.get("0" * 32) is None
    assert not list(server.asset_store.temp_dir.iterdir())
    with sqlite3.connect(server.asset_store.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM assets").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_rm_asset_download_rejects_forged_and_expired_tokens(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    payload = b"download token test"
    asset = _persist(server.asset_store, payload)
    link = json.loads(await server.rm_asset_download_link(asset["asset_id"]))
    token = link["download_path"].rsplit("/", 1)[1]
    server._rm_asset_download_tokens[token]["expires_at"] = 0

    with _asset_client(server) as client:
        assert client.get(link["download_path"]).status_code == 404
        assert client.get("/rm/asset-download/" + "A" * 43).status_code == 404

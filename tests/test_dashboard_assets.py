import hashlib
import importlib
import io
import sqlite3
import sys
from pathlib import Path

from PIL import Image
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient


def _png(color=(20, 40, 60), size=(48, 32)):
    output = io.BytesIO()
    with Image.new("RGB", size, color) as image:
        image.save(output, format="PNG")
    return output.getvalue()


def _persist(store, data, filename="image.png", mime_type="image/png"):
    source = store.create_temp_path()
    source.write_bytes(data)
    return store.persist_upload(
        source,
        hashlib.sha256(data).hexdigest(),
        len(data),
        filename,
        mime_type,
    )


def _load_server(tmp_path, monkeypatch):
    monkeypatch.setenv("OMBRE_BUCKETS_DIR", str(tmp_path / "buckets"))
    monkeypatch.setenv("OMBRE_DASHBOARD_PASSWORD", "test-password")
    monkeypatch.delenv("OMBRE_API_KEY", raising=False)
    sys.modules.pop("server", None)
    return importlib.import_module("server")


def _client(server, authenticated=True):
    app = Starlette(routes=[
        Route("/api/assets", server.api_assets, methods=["GET"]),
        Route("/api/assets/{asset_id}", server.api_asset_detail, methods=["GET"]),
        Route(
            "/api/assets/{asset_id}/thumbnail",
            server.api_asset_thumbnail,
            methods=["GET"],
        ),
        Route(
            "/api/assets/{asset_id}/image",
            server.api_asset_image,
            methods=["GET", "HEAD"],
        ),
        Route("/api/buckets", server.api_buckets, methods=["GET"]),
        Route("/api/archives", server.api_archives, methods=["GET"]),
        Route("/dashboard", server.dashboard, methods=["GET"]),
        Route(
            "/dashboard-assets.js",
            server.dashboard_assets_script,
            methods=["GET"],
        ),
        Route(
            "/dashboard-assets.css",
            server.dashboard_assets_styles,
            methods=["GET"],
        ),
    ])
    client = TestClient(app)
    if authenticated:
        token = server._create_session()
        client.cookies.set("ombre_session", token)
    return client


def test_asset_routes_require_dashboard_auth(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    assert _client(server, authenticated=False).get("/api/assets").status_code == 401


def test_asset_list_search_pagination_and_safe_shape(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    first = _persist(server.asset_store, _png(), "first-secret-name.png")
    second = _persist(
        server.asset_store,
        _png((80, 30, 20)),
        "second.png",
    )
    server.asset_store.update_metadata(
        first["asset_id"],
        title="山间记录",
        description="一段中文描述",
        tags=["旅行", "收藏"],
    )
    server.asset_store.update_metadata(
        second["asset_id"],
        title="Other",
        description="Unrelated",
        tags=["archive"],
    )
    client = _client(server)

    page = client.get("/api/assets?limit=1&offset=0")
    assert page.status_code == 200
    payload = page.json()
    assert payload["total"] == 2
    assert len(payload["results"]) == 1

    for query in ("山间", "中文描述", "first-secret-name"):
        result = client.get("/api/assets", params={"q": query}).json()
        assert [item["asset_id"] for item in result["results"]] == [first["asset_id"]]
    tagged = client.get("/api/assets", params={"tag": "旅行"}).json()
    assert [item["asset_id"] for item in tagged["results"]] == [first["asset_id"]]

    safe = tagged["results"][0]
    assert safe["thumbnail_url"].endswith("/thumbnail")
    assert safe["image_url"].endswith("/image")
    forbidden = {
        "stored_relpath",
        "stored_sha256",
        "source_sha256",
        "decoded_bytes",
        "base64",
        "token",
    }
    assert not forbidden.intersection(safe)
    assert str(server.asset_store.data_root) not in page.text


def test_asset_routes_validate_pagination_and_missing_asset(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    client = _client(server)

    for query in ("limit=0", "limit=51", "offset=-1", "limit=nope"):
        response = client.get("/api/assets?" + query)
        assert response.status_code == 400
        assert response.json() == {"error": "invalid_pagination"}
    missing = "0" * 32
    assert client.get(f"/api/assets/{missing}").status_code == 404
    assert client.get(f"/api/assets/{missing}/image").status_code == 404


def test_asset_image_and_thumbnail_use_cleaned_registered_file(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    asset = _persist(server.asset_store, _png(size=(600, 400)), "large.png")
    resolved = server.asset_store.resolve_file(asset["asset_id"])
    stored_bytes = resolved[1].read_bytes()
    client = _client(server)

    image = client.get(f"/api/assets/{asset['asset_id']}/image")
    assert image.status_code == 200
    assert image.headers["content-type"].startswith("image/png")
    assert image.headers["x-content-type-options"] == "nosniff"
    assert image.content == stored_bytes

    thumbnail = client.get(f"/api/assets/{asset['asset_id']}/thumbnail")
    assert thumbnail.status_code == 200
    assert thumbnail.headers["content-type"].startswith("image/png")
    with Image.open(io.BytesIO(thumbnail.content)) as opened:
        assert opened.width <= 360
        assert opened.height <= 240


def test_asset_image_rejects_tampered_path_without_leaking_it(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    asset = _persist(server.asset_store, _png(), "safe.png")
    with sqlite3.connect(server.asset_store.db_path) as conn:
        conn.execute(
            "UPDATE assets SET stored_relpath = ? WHERE asset_id = ?",
            ("../private.png", asset["asset_id"]),
        )
    response = _client(server).get(f"/api/assets/{asset['asset_id']}/image")

    assert response.status_code == 404
    assert response.json() == {"error": "asset_unavailable"}
    assert ".." not in response.text
    assert str(tmp_path) not in response.text


def test_empty_asset_library(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    assert _client(server).get("/api/assets").json() == {
        "total": 0,
        "offset": 0,
        "limit": 20,
        "results": [],
    }


def test_archives_are_separate_from_active_buckets(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)

    active_id = "active-bucket"
    archive_id = "session-archive"
    active = {
        "id": active_id,
        "content": "active content",
        "metadata": {
            "name": "active",
            "type": "dynamic",
            "domain": ["daily"],
            "created": "2026-07-20T10:00:00+00:00",
        },
    }
    archive = {
        "id": archive_id,
        "content": "archived conversation",
        "metadata": {
            "name": "session archive",
            "type": "archived",
            "domain": ["session"],
            "tags": ["session", "archive"],
            "created": "2026-07-21T10:00:00+00:00",
        },
    }

    async def fake_list(include_archive=False):
        return [active, archive] if include_archive else [active]

    monkeypatch.setattr(server.bucket_mgr, "list_all", fake_list)
    client = _client(server)
    buckets = client.get("/api/buckets").json()
    archives = client.get("/api/archives").json()

    assert [item["id"] for item in buckets] == [active_id]
    assert [item["id"] for item in archives["results"]] == [archive_id]


def test_dashboard_static_contract_has_three_sections_and_safe_asset_ui(
    tmp_path,
    monkeypatch,
):
    server = _load_server(tmp_path, monkeypatch)
    client = _client(server)
    html = client.get("/dashboard").text
    script = client.get("/dashboard-assets.js").text
    css = client.get("/dashboard-assets.css")

    assert 'data-tab="list">记忆桶<' in html
    assert 'data-tab="archives">归档对话<' in html
    assert 'data-tab="assets">图片资产<' in html
    assert 'id="archives-view"' in html
    assert 'id="assets-view"' in html
    assert "没有符合搜索条件的归档对话" in html
    assert "图片库还是空的" in script
    assert "没有符合当前搜索条件的图片" in script
    assert "缩略图无法加载" in script
    assert "rm-asset-detail" in script
    assert "innerHTML" not in script
    assert "stored_relpath" not in script
    assert "base64" not in script.lower()
    assert css.status_code == 200
    assert css.headers["content-type"].startswith("text/css")

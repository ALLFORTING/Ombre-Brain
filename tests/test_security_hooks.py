import asyncio
import importlib
import sys
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient


pytestmark = pytest.mark.security

HOOK_TOKEN = "H" * 32


def _load_server(tmp_path, monkeypatch, *, hook_token=None, auth_token=None):
    monkeypatch.setenv("OMBRE_BUCKETS_DIR", str(tmp_path / "buckets"))
    monkeypatch.delenv("OMBRE_DASHBOARD_PASSWORD", raising=False)
    monkeypatch.delenv("OMBRE_HOOK_TOKEN", raising=False)
    monkeypatch.delenv("OMBRE_AUTH_TOKEN", raising=False)
    if hook_token is not None:
        monkeypatch.setenv("OMBRE_HOOK_TOKEN", hook_token)
    if auth_token is not None:
        monkeypatch.setenv("OMBRE_AUTH_TOKEN", auth_token)
    sys.modules.pop("server", None)
    return importlib.import_module("server")


def _hook_client(server):
    app = Starlette(routes=[
        Route("/breath-hook", server.breath_hook, methods=["GET"]),
        Route("/dream-hook", server.dream_hook, methods=["GET"]),
    ])
    return TestClient(app)


def _install_hook_fakes(server, buckets, *, touch=None, dehydrate=None):
    server.bucket_mgr = SimpleNamespace(
        list_all=AsyncMock(return_value=buckets),
        touch=AsyncMock(side_effect=touch),
    )
    server.decay_engine = SimpleNamespace(calculate_score=lambda metadata: 1.0)
    server.dehydrator = SimpleNamespace(
        dehydrate=AsyncMock(side_effect=dehydrate or (lambda content, meta: content)),
    )
    server._fire_webhook = AsyncMock()


def _bucket(bucket_id, content, **metadata):
    return {"id": bucket_id, "content": content, "metadata": metadata}


def test_hooks_return_503_before_reading_memory_when_unconfigured(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch, auth_token="A" * 32)
    list_all = AsyncMock(side_effect=AssertionError("memory must not be read"))
    server.bucket_mgr = SimpleNamespace(list_all=list_all)
    client = _hook_client(server)

    breath = client.get("/breath-hook", headers={"Authorization": "Bearer anything"})
    dream = client.get("/dream-hook", params={"token": "anything"})

    assert breath.status_code == 503
    assert breath.json() == {"error": "hook_not_configured"}
    assert dream.status_code == 503
    assert dream.json() == {"error": "hook_not_configured"}
    list_all.assert_not_awaited()


@pytest.mark.parametrize(
    "headers,params",
    [
        ({}, {}),
        ({"Authorization": "Basic abc"}, {}),
        ({"Authorization": "Bearer"}, {}),
        ({"Authorization": "Bearer wrong"}, {}),
        ({"Authorization": b"Bearer \xe4\xf6\xfc"}, {}),
        ({}, {"token": HOOK_TOKEN}),
    ],
)
def test_hook_authentication_failures_have_one_fixed_401_response(
    tmp_path, monkeypatch, headers, params
):
    server = _load_server(tmp_path, monkeypatch, hook_token=HOOK_TOKEN)
    server.bucket_mgr = SimpleNamespace(
        list_all=AsyncMock(side_effect=AssertionError("memory must not be read")),
    )
    client = _hook_client(server)

    responses = [
        client.get("/breath-hook", headers=headers, params=params),
        client.get("/dream-hook", headers=headers, params=params),
    ]

    assert [response.status_code for response in responses] == [401, 401]
    assert responses[0].content == responses[1].content
    assert responses[0].json() == {"error": "hook_unauthorized"}
    assert HOOK_TOKEN.encode() not in responses[0].content


def test_hook_bearer_success_is_independent_from_mcp_token(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch, hook_token=HOOK_TOKEN)
    buckets = [_bucket("open", "visible", created="2026-01-01T00:00:00")]
    _install_hook_fakes(server, buckets)
    client = _hook_client(server)

    response = client.get(
        "/dream-hook",
        headers={"Authorization": f"Bearer {HOOK_TOKEN}"},
    )

    assert response.status_code == 200
    assert "visible" in response.text


@pytest.mark.parametrize(
    "value",
    ["x" * 31, "x" * 31 + " ", "x" * 31 + "\u00e4", "changeme"],
)
def test_hook_token_rejects_invalid_startup_values(tmp_path, monkeypatch, value):
    server = _load_server(tmp_path, monkeypatch)
    monkeypatch.setenv("OMBRE_HOOK_TOKEN", value)

    with pytest.raises(RuntimeError, match="^OMBRE_HOOK_TOKEN invalid$"):
        server._validate_hook_token()


def test_hook_token_denylist_is_exact(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    monkeypatch.setenv("OMBRE_HOOK_TOKEN", "changeme" + "X" * 24)

    server._validate_hook_token()


@pytest.mark.asyncio
async def test_breath_hook_filters_sealed_and_enters_one_touch_scope_after_dehydration(
    tmp_path, monkeypatch
):
    server = _load_server(tmp_path, monkeypatch, hook_token=HOOK_TOKEN)
    buckets = [
        _bucket("sealed-pinned", "SEALED_PINNED", pinned=True, sealed=1),
        _bucket("pinned", "PINNED", pinned=True),
        _bucket("sealed-open", "SEALED_OPEN", sealed=1),
        _bucket("open", "OPEN", created="2026-01-01T00:00:00"),
        _bucket("legacy", "LEGACY", created="2026-01-02T00:00:00"),
    ]
    touched = []
    dehydration_active = False
    scope_entries = 0

    async def touch(bucket_id):
        touched.append(bucket_id)
        assert not dehydration_active

    async def dehydrate(content, _meta):
        assert not dehydration_active
        await asyncio.sleep(0)
        return f"SUMMARY:{content}"

    @asynccontextmanager
    async def scope(_operation):
        nonlocal scope_entries
        scope_entries += 1
        yield True

    _install_hook_fakes(server, buckets, touch=touch, dehydrate=dehydrate)
    monkeypatch.setattr(server, "optional_async_writer_scope", scope)
    client = _hook_client(server)

    response = client.get(
        "/breath-hook",
        headers={"Authorization": f"Bearer {HOOK_TOKEN}"},
    )

    assert response.status_code == 200
    assert "SEALED_PINNED" not in response.text
    assert "SEALED_OPEN" not in response.text
    assert "PINNED" in response.text
    assert "OPEN" in response.text
    assert "LEGACY" in response.text
    assert touched == ["open", "legacy"]
    assert scope_entries == 1


@pytest.mark.asyncio
async def test_hooks_return_content_without_touch_during_drain_or_freeze(
    tmp_path, monkeypatch
):
    server = _load_server(tmp_path, monkeypatch, hook_token=HOOK_TOKEN)
    buckets = [_bucket("open", "OPEN", created="2026-01-01T00:00:00")]
    touched = []
    blocked = True
    scope_entries = 0

    async def touch(bucket_id):
        touched.append(bucket_id)

    @asynccontextmanager
    async def scope(_operation):
        nonlocal scope_entries
        scope_entries += 1
        yield not blocked

    _install_hook_fakes(server, buckets, touch=touch)
    monkeypatch.setattr(server, "optional_async_writer_scope", scope)
    client = _hook_client(server)

    response = client.get(
        "/dream-hook",
        headers={"Authorization": f"Bearer {HOOK_TOKEN}"},
    )

    assert response.status_code == 200
    assert "OPEN" in response.text
    assert touched == []
    assert scope_entries == 1


def test_breath_hook_freeze_starting_during_build_skips_touch_without_losing_content(
    tmp_path, monkeypatch
):
    server = _load_server(tmp_path, monkeypatch, hook_token=HOOK_TOKEN)
    buckets = [_bucket("open", "OPEN", created="2026-01-01T00:00:00")]
    touched = []
    blocked = False
    scope_entries = 0

    async def dehydrate(content, _meta):
        nonlocal blocked
        blocked = True
        return f"SUMMARY:{content}"

    @asynccontextmanager
    async def scope(_operation):
        nonlocal scope_entries
        scope_entries += 1
        yield not blocked

    _install_hook_fakes(server, buckets, touch=touched.append, dehydrate=dehydrate)
    monkeypatch.setattr(server, "optional_async_writer_scope", scope)
    client = _hook_client(server)

    response = client.get(
        "/breath-hook",
        headers={"Authorization": f"Bearer {HOOK_TOKEN}"},
    )

    assert response.status_code == 200
    assert "SUMMARY:OPEN" in response.text
    assert touched == []
    assert scope_entries == 1


def test_session_hook_script_sends_bearer_and_skips_without_token():
    source = open(".claude/hooks/session_breath.py", encoding="utf-8").read()
    assert '"Authorization": f"Bearer {hook_token}"' in source
    assert 'if not hook_token:' in source

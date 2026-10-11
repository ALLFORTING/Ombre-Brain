import base64
import importlib
import inspect
import sys
from copy import deepcopy
from unittest.mock import AsyncMock

import pytest
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import CallToolResult, TextContent, ImageContent
from mcp.shared.memory import create_connected_server_and_client_session
from mcp_action_tools import ACTION_TOOLS


CASES = [(name, action, old) for name, actions in ACTION_TOOLS.items() for action, old in actions.items()]
ARGS = {
    "leave_note": {"text": "exact body"}, "list_notes": {}, "get_note": {"note_id": 1},
    "dismiss_note": {"note_id": 1}, "get_letter": {"letter_id": 1}, "seal_letter": {"letter_id": 1},
    "list_revisions": {"bucket_id": "b"}, "restore_revision": {"bucket_id": "b", "revision": "r"},
    "rm_asset_upload_link": {"expected_bytes": 12}, "rm_asset_upload_status": {"upload_id": "u"},
    "rm_asset_get": {"asset_id": "a"}, "rm_asset_search": {}, "rm_asset_view": {"asset_id": "a"},
    "rm_asset_inspect": {"asset_id": "a"}, "rm_asset_download_link": {"asset_id": "a"},
}


def _reference_server(module):
    """Independent test-only SDK surface for retained private implementations."""
    reference = FastMCP("private-sdk-reference")
    for name, action, old in CASES:
        reference._tool_manager._tools[old] = module.mcp._tool_manager.get_tool(name).targets[action]
    return reference


@pytest.fixture
def server(tmp_path, monkeypatch):
    monkeypatch.setenv("OMBRE_BUCKETS_DIR", str(tmp_path / "buckets"))
    monkeypatch.delenv("OMBRE_API_KEY", raising=False)
    sys.modules.pop("server", None)
    return importlib.import_module("server")


@pytest.mark.asyncio
@pytest.mark.parametrize("name,action,old", CASES)
async def test_each_action_keeps_wire_and_exact_sdk_arguments(server, monkeypatch, name, action, old):
    target = server.mcp._tool_manager.get_tool(name).targets[action]
    if old in {"rm_asset_view", "rm_asset_inspect"}:
        content = [TextContent(type="text", text='"exact receipt"')]
        if old == "rm_asset_inspect":
            content.append(ImageContent(type="image", data=base64.b64encode(b"image").decode(), mimeType="image/png"))
        result = CallToolResult(content=content, structuredContent={"asset_id": "a"},
                                _meta={"rememberMe": {"imageBase64": "exact-image", "downloadUrl": "exact-url"}})
    else:
        result = '"exact receipt"'
    calls = []
    async def business(**kwargs):
        calls.append(deepcopy(kwargs))
        return result
    monkeypatch.setattr(target, "fn", business)
    async with create_connected_server_and_client_session(server.mcp) as client:
        async with create_connected_server_and_client_session(_reference_server(server)) as reference:
            legacy = await reference.call_tool(old, ARGS[old])
        absent = await client.call_tool(old, ARGS[old])
        assert absent.isError and f"Unknown tool: {old}" in absent.content[0].text
        assert not calls[1:]
        assert server.mcp._tool_manager.get_tool(old) is None
        merged = await client.call_tool(name, {"action": action, **ARGS[old]})
        assert merged.model_dump(by_alias=True) == legacy.model_dump(by_alias=True)
        assert not merged.isError
        assert calls[0] == calls[1]
        assert len(calls) == 2
        for extra in [{"unknown": 0}, *[{key: prop.get("default", None)} for key, prop in server.mcp._tool_manager.get_tool(name).parameters["properties"].items() if key not in target.parameters["properties"] and key != "action"]]:
            rejected = await client.call_tool(name, {"action": action, **ARGS[old], **extra})
            assert rejected.isError
            assert len(calls) == 2
        for key, prop in target.parameters["properties"].items():
            if prop.get("type") == "integer":
                assert (await client.call_tool(name, {"action": action, **ARGS[old], key: "not-an-integer"})).isError
                assert len(calls) == 2
        if old == "list_notes":
            assert (await client.call_tool(name, {"action": action, "limit": 0})).isError
            assert len(calls) == 2
        for invalid in [{}, {"action": "invalid"}, {"action": None}, {"action": []}]:
            assert (await client.call_tool(name, invalid)).isError
            assert len(calls) == 2
        for required in target.parameters.get("required", []):
            missing = {k: v for k, v in ARGS[old].items() if k != required}
            rejected = await client.call_tool(name, {"action": action, **missing})
            assert rejected.isError
            assert rejected.content[0].text == f"{name}(action={action}): missing required parameters: {required}"
            assert len(calls) == 2


@pytest.mark.asyncio
async def test_metadata_descriptions_and_schema(server):
    async with create_connected_server_and_client_session(server.mcp) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
        assert len(tools) == 20
        assert tools["rm_asset_show"].meta == server.mcp._tool_manager.get_tool("rm_asset_show").targets["view"].meta
        assert tools["rm_asset_show"].outputSchema is None
        assert len((await client.list_resources()).resources) == 1
    for name, action, old in CASES:
        target = server.mcp._tool_manager.get_tool(name).targets[action]
        assert target.description == target.fn.__doc__
        assert server.mcp._tool_manager.get_tool(old) is None
        schema = tools[name].inputSchema
        assert schema["required"] == ["action"]
        assert "default" not in schema["properties"]["action"]
        branch = next(b for b in schema["oneOf"] if b["properties"]["action"]["const"] == action)
        assert branch["additionalProperties"] is False
        assert {k: v for k, v in branch["properties"].items() if k != "action"} == target.parameters["properties"]
        assert branch["required"] == ["action", *target.parameters.get("required", [])]


def _route_module(module, monkeypatch):
    """Run existing behavioral assertions through the new SDK entry point."""
    for name, action, old in CASES:
        original = getattr(module, old)
        signature = inspect.signature(original)
        tool = module.mcp._tool_manager.get_tool(name)
        async def routed(*args, _signature=signature, _tool=tool, _action=action, **kwargs):
            explicit = _signature.bind(*args, **kwargs).arguments
            return await _tool.run({"action": _action, **explicit})
        monkeypatch.setattr(module, old, routed)
    return module


from tests import test_ting_note_delivery as notes
from tests import test_boot_mailbox_seal as letters
from tests import test_bucket_revisions_restore as revisions
from tests import test_remember_me_stage8f_c_asset_get_wiring as rm_get
from tests import test_remember_me_stage8f_d_download_link_wiring as rm_download
from tests import test_remember_me_stage8f_e_view_wiring as rm_view
from tests import test_remember_me_stage8f_f_inspect_wiring as rm_inspect
from tests import test_remember_me_stage8f_h_search_wiring as rm_search
from tests import test_remember_me_stage8f_i_upload_wiring as rm_upload

# Reuse meaningful storage/confirmation regressions, with all calls routed
# through the new tool. Original suites are also run independently.
REGRESSIONS = [(module, name) for module in (notes, letters, revisions, rm_get, rm_download, rm_view, rm_inspect, rm_search, rm_upload)
               for name, fn in vars(module).items() if name.startswith("test_")
               and inspect.iscoroutinefunction(fn)
               and set(inspect.signature(fn).parameters) <= {"tmp_path", "monkeypatch", "server"}
               and not any(getattr(mark, "name", "") == "parametrize" for mark in getattr(fn, "pytestmark", []))]


@pytest.mark.asyncio
@pytest.mark.parametrize("module,test_name", REGRESSIONS, ids=[name for _, name in REGRESSIONS])
async def test_existing_storage_rules_through_actions(module, test_name, tmp_path, monkeypatch):
    if test_name in {"test_enabled_rm_asset_view_uses_presenter_and_rm_ticket_only", "test_enabled_rm_asset_view_fallback_redeems_rm_core_bytes", "test_enabled_rm_asset_inspect_uses_presenter_only_without_tickets", "test_enabled_rm_asset_inspect_supports_jpeg", "test_rm_default_threshold_filters_before_public_total_and_pagination"}:
        from remember_me_adapter import validate_remember_me_contract, RememberMeAdapterError
        try:
            validate_remember_me_contract()
        except RememberMeAdapterError as exc:
            pytest.skip(f"Real RM dependency unavailable: {exc}")
    if hasattr(module, "_load_server"):
        loader = module._load_server
        def load(*args, **kwargs):
            return _route_module(loader(*args, **kwargs), monkeypatch)
        monkeypatch.setattr(module, "_load_server", load)
    kwargs = {"tmp_path": tmp_path, "monkeypatch": monkeypatch}
    fn = getattr(module, test_name)
    if "server" in inspect.signature(fn).parameters:
        kwargs["server"] = _route_module(revisions.server.__wrapped__(tmp_path, monkeypatch), monkeypatch)
    await fn(**{k: v for k, v in kwargs.items() if k in inspect.signature(fn).parameters})

@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["dismiss", "restore"])
async def test_confirmation_ttl_cross_entry_and_replay(server, monkeypatch, operation):
    server.decay_engine.ensure_started = AsyncMock(return_value=None)
    manager = server.mcp._tool_manager
    if operation == "dismiss":
        await server.leave_note("retained body")
        tool = manager.get_tool("note")
        args = {"action": "dismiss", "note_id": 1}
        legacy = server.dismiss_note
        legacy_args = {"note_id": 1}
        def extract(text):
            return text.split("confirm_token=", 1)[1].split()[0].rstrip("；")
    else:
        bucket = await server.bucket_mgr.create(content="before")
        await server.trace(bucket, content="after")
        ref = revisions.ref_of(server, bucket, "replace")
        tool = manager.get_tool("revision")
        args = {"action": "restore", "bucket_id": bucket, "revision": ref}
        legacy = server.restore_revision
        legacy_args = {"bucket_id": bucket, "revision": ref}
        extract = revisions.token
    preview = await tool.run(args)
    token = extract(preview)
    server._mutation_confirm_tokens[token]["expires_at"] = server.time.monotonic() - 1
    rejected = await tool.run({**args, "confirm_token": token})
    assert ("确认无效" if operation == "dismiss" else "confirmation invalid") in rejected
    assert token not in server._mutation_confirm_tokens
    # Old-issued token accepted by new entry, and new-issued token by old entry.
    token = extract(await legacy(**legacy_args))
    await tool.run({**args, "confirm_token": token})
    assert token not in server._mutation_confirm_tokens
    count = len(server.bucket_mgr.restoration_rows(bucket)) if operation == "restore" else notes._note_rows(server)[0]["dismissed_at"]
    await tool.run({**args, "confirm_token": token})
    assert (len(server.bucket_mgr.restoration_rows(bucket)) if operation == "restore" else notes._note_rows(server)[0]["dismissed_at"]) == count
    if operation == "dismiss":
        await server.leave_note("second")
        args["note_id"] = legacy_args["note_id"] = 2
    else:
        await server.trace(bucket, content="later")
    token = extract(await tool.run(args))
    await legacy(**legacy_args, confirm_token=token)
    assert token not in server._mutation_confirm_tokens

@pytest.mark.asyncio
@pytest.mark.parametrize("action,old", ACTION_TOOLS["rm_asset_show"].items())
async def test_real_legacy_image_wire_and_download_effects(tmp_path, monkeypatch, action, old):
    server = rm_view._load_server(tmp_path, monkeypatch)
    data = rm_view._png_bytes()
    asset = rm_view._persist_legacy(server, data, filename="exact.png", mime_type="image/png")
    monkeypatch.setattr(server.secrets, "token_urlsafe", lambda size: "A" * 43)
    monkeypatch.setattr(server.time, "time", lambda: 2000000000.0)
    async with create_connected_server_and_client_session(server.mcp) as client:
        async with create_connected_server_and_client_session(_reference_server(server)) as reference:
            legacy = await reference.call_tool(old, {"asset_id": asset["asset_id"]})
        before = (deepcopy(server._rm_asset_download_tokens), deepcopy(server._rm_asset_download_sources))
        server._rm_asset_download_tokens.clear()
        server._rm_asset_download_sources.clear()
        merged = await client.call_tool("rm_asset_show", {"action": action, "asset_id": asset["asset_id"]})
        assert not merged.isError
        assert merged.model_dump(by_alias=True) == legacy.model_dump(by_alias=True)
        assert (server._rm_asset_download_tokens, server._rm_asset_download_sources) == before
        if action == "inspect":
            assert merged.content[1].type == "image"
        elif action == "view":
            assert all(item.type != "image" for item in merged.content)
            assert base64.b64decode(merged.meta["rememberMe"]["imageBase64"]) == data
        else:
            assert merged.structuredContent == {"result": merged.content[0].text}


@pytest.mark.asyncio
@pytest.mark.parametrize("name,action,old", CASES)
async def test_required_raw_keys_order_and_explicit_null(server, monkeypatch, name, action, old):
    tool = server.mcp._tool_manager.get_tool(name)
    target = tool.targets[action]
    required = target.parameters.get("required", [])
    business = AsyncMock(return_value="receipt")
    monkeypatch.setattr(target, "fn", business)
    if required:
        with pytest.raises(ToolError) as exc:
            await tool.run({"action": action})
        assert str(exc.value) == f"{name}(action={action}): missing required parameters: {', '.join(required)}"
        business.assert_not_awaited()
        for key in required:
            with pytest.raises(Exception) as exc:
                await tool.run({"action": action, **ARGS[old], key: None})
            assert "missing required parameters" not in str(exc.value)
            business.assert_not_awaited()

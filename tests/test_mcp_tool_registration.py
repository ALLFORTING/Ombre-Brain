import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import server
from asset_viewer import ASSET_VIEWER_URI


DIAGNOSTIC_TOOLS = {
    "asset_attachment_context_probe",
    "asset_ingest_probe",
    "asset_ingest_begin",
    "asset_ingest_chunk",
    "asset_ingest_finish",
    "asset_ingest_abort",
    "asset_browser_upload_link",
    "asset_browser_upload_status",
    "asset_render_probe",
    "asset_export_probe",
    "asset_vision_challenge",
    "asset_vision_verify",
    "asset_vision_export",
    "asset_vision_download_link",
    "asset_vision_upload_challenge",
}

FORMAL_TOOLS = {
    "archive_session",
    "boot",
    "breath",
    "digest",
    "dream",
    "grow",
    "hold",
    "pulse",
    "related_backfill",
    "rm_asset_download_link",
    "rm_asset_get",
    "rm_asset_inspect",
    "rm_asset_reindex_embeddings",
    "rm_asset_search",
    "rm_asset_update_metadata",
    "rm_asset_upload_link",
    "rm_asset_upload_status",
    "rm_asset_view",
    "seal_letter",
    "todos",
    "trace",
}

FORMAL_TOOL_SIGNATURES = {
    "archive_session": (
        ["summary", "highlights", "mood", "valence", "arousal", "letter", "sealed"],
        ["summary"],
    ),
    "boot": (["pinned_chars", "max_tokens"], []),
    "breath": (
        [
            "query", "max_tokens", "domain", "valence", "arousal",
            "max_results", "importance_min", "mode", "recent_days",
            "emotion_trend", "include_dormant", "include_sealed",
            "date_from", "date_to", "resonance", "mailbox",
            "mailbox_limit", "feels",
        ],
        [],
    ),
    "digest": (["dry_run", "max_groups", "confirm_token"], []),
    "dream": (["detail_ids"], []),
    "grow": (["content"], ["content"]),
    "hold": (
        [
            "content", "tags", "importance", "pinned", "feel",
            "source_bucket", "valence", "arousal", "trigger_date",
        ],
        ["content"],
    ),
    "pulse": (["include_archive", "show_all", "include_sealed"], []),
    "related_backfill": (["dry_run", "limit", "threshold"], []),
    "rm_asset_download_link": (["asset_id"], ["asset_id"]),
    "rm_asset_get": (["asset_id"], ["asset_id"]),
    "rm_asset_inspect": (["asset_id"], ["asset_id"]),
    "rm_asset_reindex_embeddings": (["asset_id", "limit"], []),
    "rm_asset_search": (
        [
            "query", "tags", "kind", "mime_type", "created_from",
            "created_to", "limit", "offset",
        ],
        [],
    ),
    "rm_asset_update_metadata": (
        ["asset_id", "title", "description", "tags"],
        ["asset_id"],
    ),
    "rm_asset_upload_link": (
        ["expected_bytes", "filename", "mime_type"],
        ["expected_bytes"],
    ),
    "rm_asset_upload_status": (["upload_id"], ["upload_id"]),
    "rm_asset_view": (["asset_id"], ["asset_id"]),
    "seal_letter": (["letter_id", "sealed"], ["letter_id"]),
    "todos": ([], []),
    "trace": (
        [
            "bucket_id", "name", "domain", "valence", "arousal",
            "importance", "tags", "resolved", "pinned", "digested",
            "dormant", "sealed", "content", "related", "merge", "append",
            "trigger_date", "delete",
        ],
        ["bucket_id"],
    ),
}

PROBE_SCRIPT = r"""
import asyncio
import json

import server
from mcp.shared.memory import create_connected_server_and_client_session


async def main():
    async with create_connected_server_and_client_session(server.mcp) as client:
        tools = (await client.list_tools()).tools
        resources = (await client.list_resources()).resources
    print(json.dumps({
        "tools": [
            {"name": tool.name, "input_schema": tool.inputSchema}
            for tool in tools
        ],
        "resources": [str(resource.uri) for resource in resources],
    }))


asyncio.run(main())
"""


def _registered_surface(tmp_path: Path, value=None):
    env = os.environ.copy()
    env["OMBRE_BUCKETS_DIR"] = str(tmp_path / "buckets")
    env.pop("OMBRE_API_KEY", None)
    if value is None:
        env.pop("OMBRE_DIAG_TOOLS", None)
    else:
        env["OMBRE_DIAG_TOOLS"] = value
    completed = subprocess.run(
        [sys.executable, "-c", PROBE_SCRIPT],
        cwd=Path(__file__).resolve().parent.parent,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(completed.stdout)


def _tool_names(surface):
    return [tool["name"] for tool in surface["tools"]]


def test_default_tool_surface_contains_only_formal_tools(tmp_path):
    surface = _registered_surface(tmp_path)
    names = _tool_names(surface)

    assert len(names) == 21
    assert len(names) == len(set(names))
    assert set(names) == FORMAL_TOOLS
    assert DIAGNOSTIC_TOOLS.isdisjoint(names)
    assert ASSET_VIEWER_URI in surface["resources"]


def test_formal_tool_signatures_match_stable_baseline(tmp_path):
    surface = _registered_surface(tmp_path)
    actual = {
        tool["name"]: (
            list(tool["input_schema"].get("properties", {})),
            tool["input_schema"].get("required", []),
        )
        for tool in surface["tools"]
    }
    assert actual == FORMAL_TOOL_SIGNATURES


@pytest.mark.parametrize("value", ["1", "true", "yes", "on"])
def test_supported_values_enable_all_diagnostic_tools(tmp_path, value):
    surface = _registered_surface(tmp_path, value)
    names = _tool_names(surface)

    assert len(names) == 36
    assert len(names) == len(set(names))
    assert set(names) == FORMAL_TOOLS | DIAGNOSTIC_TOOLS


@pytest.mark.parametrize("value", ["", "0", "false", "off", "invalid"])
def test_empty_and_invalid_values_keep_diagnostic_tools_disabled(tmp_path, value):
    assert len(_tool_names(_registered_surface(tmp_path, value))) == 21


@pytest.mark.parametrize("value", [" TRUE ", "\tYeS\n", " On ", " 1 "])
def test_flag_parsing_ignores_case_and_surrounding_whitespace(value):
    assert server._env_flag_enabled(value) is True


def test_diagnostic_inventory_matches_central_registration_list():
    assert server.DIAGNOSTIC_TOOL_NAMES == DIAGNOSTIC_TOOLS

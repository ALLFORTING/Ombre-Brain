import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import server


MANIFEST_PATH = Path(__file__).resolve().parent.parent / "docs" / "mcp-public-contract.json"
_SCHEMA_KEYS = {
    "additionalItems",
    "additionalProperties",
    "const",
    "default",
    "dependentRequired",
    "exclusiveMaximum",
    "exclusiveMinimum",
    "items",
    "maxItems",
    "maxLength",
    "maximum",
    "minItems",
    "minLength",
    "minimum",
    "multipleOf",
    "not",
    "oneOf",
    "pattern",
    "prefixItems",
    "properties",
    "propertyNames",
    "required",
    "type",
    "uniqueItems",
    "anyOf",
    "allOf",
    "enum",
    "nullable",
}
_PRESENTATION_KEYS = {"title", "description", "$comment", "$schema"}


def _load_manifest():
    with MANIFEST_PATH.open(encoding="utf-8") as handle:
        return json.load(handle)


MANIFEST = _load_manifest()
TOOL_ENTRIES = {entry["name"]: entry for entry in MANIFEST["tools"]}
DEFAULT_TOOLS = {
    name for name, entry in TOOL_ENTRIES.items() if entry["exposure"] == "default"
}
DIAGNOSTIC_TOOLS = {
    name for name, entry in TOOL_ENTRIES.items() if entry["exposure"] == "diagnostic"
}
TOOL_SCHEMA_CONTRACTS = {
    name: entry["input_schema_contract"] for name, entry in TOOL_ENTRIES.items()
}
RESOURCE_URIS = {entry["uri"] for entry in MANIFEST["resources"]}
PROMPT_NAMES = {entry["name"] for entry in MANIFEST["prompts"]}
RESOURCE_TEMPLATE_URIS = {
    entry["uri_template"] for entry in MANIFEST["resource_templates"]
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
        prompts = (await client.list_prompts()).prompts
        resource_templates = (await client.list_resource_templates()).resourceTemplates
    print(json.dumps({
        "tools": [
            {"name": tool.name, "input_schema": tool.inputSchema}
            for tool in tools
        ],
        "resources": [str(resource.uri) for resource in resources],
        "prompts": [prompt.name for prompt in prompts],
        "resource_templates": [template.uriTemplate for template in resource_templates],
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


def _normalize_schema(schema):
    """Keep validation semantics, ignoring presentation and generated titles."""
    if isinstance(schema, dict):
        normalized = {}
        for key, value in schema.items():
            if key in _PRESENTATION_KEYS or key not in _SCHEMA_KEYS:
                continue
            if key == "properties":
                normalized[key] = {
                    property_name: _normalize_schema(property_schema)
                    for property_name, property_schema in sorted(value.items())
                }
            elif key == "required":
                normalized[key] = sorted(value)
            elif key in {"anyOf", "oneOf", "allOf"}:
                branches = [_normalize_schema(branch) for branch in value]
                normalized[key] = sorted(
                    branches,
                    key=lambda branch: json.dumps(
                        branch, sort_keys=True, separators=(",", ":")
                    ),
                )
            else:
                normalized[key] = _normalize_schema(value)
        return normalized
    if isinstance(schema, list):
        return [_normalize_schema(item) for item in schema]
    return schema


def _validate_schema_contract(schema, *, path="schema"):
    assert isinstance(schema, dict), f"{path} must be an object"
    assert schema.get("type") == "object", f"{path} must describe an object"
    properties = schema.get("properties")
    assert isinstance(properties, dict), f"{path}.properties must be an object"
    required = schema.get("required", [])
    assert isinstance(required, list), f"{path}.required must be a list"
    assert all(isinstance(name, str) for name in required)
    assert set(required) <= set(properties), f"{path}.required has unknown fields"

    def visit(node, node_path):
        assert isinstance(node, dict), f"{node_path} must be an object"
        assert set(node) <= _SCHEMA_KEYS, f"{node_path} has unsupported contract keys"
        if "anyOf" in node or "oneOf" in node or "allOf" in node:
            for index, branch in enumerate(
                node.get("anyOf", [])
                + node.get("oneOf", [])
                + node.get("allOf", [])
            ):
                visit(branch, f"{node_path}.branch[{index}]")
        if "items" in node:
            visit(node["items"], f"{node_path}.items")
        if "properties" in node:
            assert isinstance(node["properties"], dict)
            for name, child in node["properties"].items():
                assert isinstance(name, str)
                visit(child, f"{node_path}.properties.{name}")

    visit(schema, path)


def _validate_manifest():
    assert isinstance(MANIFEST["manifest_version"], int)
    assert MANIFEST["diagnostic_flag"]["name"] == "OMBRE_DIAG_TOOLS"
    assert MANIFEST["diagnostic_flag"]["enabled_values"] == [
        "1",
        "true",
        "yes",
        "on",
    ]
    assert isinstance(MANIFEST["tools"], list)
    assert len(TOOL_ENTRIES) == len(MANIFEST["tools"]), "duplicate tool names"
    valid_exposure = set(MANIFEST["vocabularies"]["exposure"])
    valid_mutability = set(MANIFEST["vocabularies"]["mutability"])
    valid_compatibility = set(MANIFEST["vocabularies"]["compatibility_status"])
    for name, entry in TOOL_ENTRIES.items():
        assert entry["primitive"] == "tool"
        assert entry["exposure"] in valid_exposure
        assert entry["mutability"] in valid_mutability
        assert entry["compatibility_status"] in valid_compatibility
        assert entry["diagnostic_only"] is (entry["exposure"] == "diagnostic")
        if entry["diagnostic_only"]:
            assert entry["feature_flag"] == "OMBRE_DIAG_TOOLS", name
        else:
            assert entry["feature_flag"] is None, name
        _validate_schema_contract(entry["input_schema_contract"], path=name)
    assert len(RESOURCE_URIS) == len(MANIFEST["resources"]), "duplicate resource URIs"
    for entry in MANIFEST["resources"]:
        assert entry["primitive"] == "resource"
        assert entry["exposure"] in valid_exposure
        assert entry["compatibility_status"] in valid_compatibility
    assert isinstance(MANIFEST["prompts"], list)
    assert isinstance(MANIFEST["resource_templates"], list)


def test_manifest_is_valid_and_current_counts_are_derived():
    _validate_manifest()
    assert len(DEFAULT_TOOLS) == 21
    assert len(DIAGNOSTIC_TOOLS) == 15
    assert len(TOOL_ENTRIES) == len(DEFAULT_TOOLS) + len(DIAGNOSTIC_TOOLS)
    assert DEFAULT_TOOLS.isdisjoint(DIAGNOSTIC_TOOLS)


def test_default_surface_matches_manifest_exactly(tmp_path):
    surface = _registered_surface(tmp_path)
    names = _tool_names(surface)

    assert len(names) == len(DEFAULT_TOOLS)
    assert len(names) == len(set(names))
    assert set(names) == DEFAULT_TOOLS
    assert DIAGNOSTIC_TOOLS.isdisjoint(names)
    assert set(surface["resources"]) == RESOURCE_URIS
    assert set(surface["prompts"]) == PROMPT_NAMES
    assert set(surface["resource_templates"]) == RESOURCE_TEMPLATE_URIS


def test_default_input_schemas_match_manifest_contract(tmp_path):
    surface = _registered_surface(tmp_path)
    actual = {
        tool["name"]: _normalize_schema(tool["input_schema"])
        for tool in surface["tools"]
    }
    expected = {
        name: _normalize_schema(schema)
        for name, schema in TOOL_SCHEMA_CONTRACTS.items()
        if name in DEFAULT_TOOLS
    }
    assert actual == expected


@pytest.mark.parametrize("value", ["1", "true", "yes", "on"])
def test_supported_values_enable_all_diagnostic_tools(tmp_path, value):
    surface = _registered_surface(tmp_path, value)
    names = _tool_names(surface)

    assert len(names) == len(DEFAULT_TOOLS | DIAGNOSTIC_TOOLS)
    assert len(names) == len(set(names))
    assert set(names) == DEFAULT_TOOLS | DIAGNOSTIC_TOOLS
    assert set(surface["resources"]) == RESOURCE_URIS
    assert set(surface["prompts"]) == PROMPT_NAMES
    assert set(surface["resource_templates"]) == RESOURCE_TEMPLATE_URIS


def test_diagnostic_input_schemas_match_manifest_contract(tmp_path):
    surface = _registered_surface(tmp_path, "true")
    actual = {
        tool["name"]: _normalize_schema(tool["input_schema"])
        for tool in surface["tools"]
    }
    expected = {
        name: _normalize_schema(schema)
        for name, schema in TOOL_SCHEMA_CONTRACTS.items()
    }
    assert actual == expected


@pytest.mark.parametrize("value", ["", "0", "false", "off", "invalid"])
def test_empty_and_invalid_values_keep_diagnostic_tools_disabled(tmp_path, value):
    surface = _registered_surface(tmp_path, value)
    assert set(_tool_names(surface)) == DEFAULT_TOOLS


@pytest.mark.parametrize("value", [" TRUE ", "\tYeS\n", " On ", " 1 "])
def test_flag_parsing_ignores_case_and_surrounding_whitespace(value):
    assert server._env_flag_enabled(value) is True


def test_diagnostic_inventory_matches_manifest_and_central_registration_list():
    assert server.DIAGNOSTIC_TOOL_NAMES == DIAGNOSTIC_TOOLS

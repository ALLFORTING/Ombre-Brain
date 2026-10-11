"""Public action tools: validate raw keys, then use the original SDK tool."""
from copy import deepcopy
from functools import cached_property
import re

from mcp.server.fastmcp.exceptions import ToolError
from mcp.server.fastmcp.tools.base import Tool


ACTION_TOOLS = {
    "note": {"leave": "leave_note", "list": "list_notes", "get": "get_note", "dismiss": "dismiss_note"},
    "letter": {"get": "get_letter", "seal": "seal_letter"},
    "revision": {"list": "list_revisions", "restore": "restore_revision"},
    "rm_asset_upload": {"link": "rm_asset_upload_link", "status": "rm_asset_upload_status"},
    "rm_asset_find": {"get": "rm_asset_get", "search": "rm_asset_search"},
    "rm_asset_show": {"view": "rm_asset_view", "inspect": "rm_asset_inspect", "download_link": "rm_asset_download_link"},
}


def action_schema(schemas):
    """Retain each legacy parameter schema and defaults in its own branch."""
    properties = {"action": {"type": "string", "enum": list(schemas)}}
    branches = []
    for action, schema in schemas.items():
        fields = deepcopy(schema["properties"])
        # The root only advertises fields; defaults belong to the action branch.
        for key, value in fields.items():
            properties.setdefault(key, {k: v for k, v in value.items() if k != "default"})
        fields["action"] = {"type": "string", "const": action}
        branches.append({"type": "object", "properties": fields,
                         "required": ["action", *schema.get("required", [])],
                         "additionalProperties": False})
    return {"type": "object", "properties": properties, "required": ["action"],
            "oneOf": branches}


class ActionTool(Tool):
    targets: dict[str, Tool]

    @cached_property
    def output_schema(self):
        # show mixes text and CallToolResult; each selected legacy tool keeps
        # its own result conversion, including text structuredContent.
        return None if self.name == "rm_asset_show" else next(iter(self.targets.values())).output_schema

    async def run(self, arguments, context=None, convert_result=False):
        action = arguments.get("action")
        if not isinstance(action, str) or action not in self.targets:
            raise ToolError(f"{self.name}: action must be one of {', '.join(self.targets)}")
        target = self.targets[action]
        extra = set(arguments) - {"action", *target.parameters["properties"]}
        if extra:
            raise ToolError(f"{self.name}(action={action}): unsupported parameters: {', '.join(sorted(extra))}")
        missing = [key for key in target.parameters.get("required", []) if key not in arguments]
        if missing:
            raise ToolError(f"{self.name}(action={action}): missing required parameters: {', '.join(missing)}")
        # SDK required/type/range validation and original result conversion
        # run exactly once, after raw-key validation and before business code.
        return await target.run({k: v for k, v in arguments.items() if k != "action"},
                                context=context, convert_result=convert_result)


def register_action_tools(mcp):
    manager = mcp._tool_manager
    public_names = {old: f'{name}(action="{action}")'
                    for name, actions in ACTION_TOOLS.items() for action, old in actions.items()}

    def public_description(description):
        # Rewrite public guidance only; retained targets keep exact descriptions.
        return re.sub(r"\b(" + "|".join(public_names) + r")\b",
                      lambda match: public_names[match.group()], description)
    for name, actions in ACTION_TOOLS.items():
        targets = {action: manager.get_tool(old) for action, old in actions.items()}
        first = next(iter(targets.values()))
        fields = {field: getattr(first, field) for field in Tool.model_fields}
        fields.update(name=name, title=None, annotations=None, icons=None,
                      description="Dispatch to the original tools by required action. Parameters and receipts retain their original behavior. " +
                                  " ".join(f"{action}: {public_description(target.description)}" for action, target in targets.items()),
                      parameters=action_schema({action: target.parameters for action, target in targets.items()}),
                      meta=deepcopy(targets["view"].meta) if name == "rm_asset_show" else None)
        # ToolManager has no public API for a Tool subclass. Keep this single
        # SDK seam covered by registration and actual MCP wire tests.
        if manager.get_tool(name) is not None:
            raise ValueError(f"Tool already exists: {name}")
        manager._tools[name] = ActionTool(**fields, targets=targets)
    # Capture every target before removal; the private SDK objects remain usable.
    for actions in ACTION_TOOLS.values():
        for old in actions.values():
            manager.remove_tool(old)

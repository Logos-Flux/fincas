"""Translate erp_assist MCP tool metadata into Anthropic tool schemas.

The erp_assist.mcp_adapter module registers every tool via
FastMCP's @mcp.tool decorator. FastMCP builds a JSON Schema for each
tool's parameters from the function signature; we just need to repack
that into Anthropic's `{name, description, input_schema}` shape.

This module is evaluated once per process (the chat view caches the
result). Adding a tool in erp_assist automatically picks it up
here — no duplication, no sync required.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any

from erp_assist import mcp_adapter


def _normalise_input_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Clean up the FastMCP-generated JSON Schema for Anthropic.

    FastMCP adds `title` fields and an `...Arguments` wrapper title
    that Anthropic doesn't need. We strip them so the schema the model
    sees is the minimum useful subset.
    """
    if not isinstance(schema, dict):
        return {"type": "object", "properties": {}}

    cleaned = {k: v for k, v in schema.items() if k != "title"}
    # Recurse into properties to drop their titles too — keeps the
    # schema compact in the model's context.
    props = cleaned.get("properties")
    if isinstance(props, dict):
        cleaned["properties"] = {
            name: ({k: v for k, v in prop.items() if k != "title"}
                   if isinstance(prop, dict) else prop)
            for name, prop in props.items()
        }
    # Anthropic requires `type: object` at the top level.
    cleaned.setdefault("type", "object")
    cleaned.setdefault("properties", {})
    return cleaned


@lru_cache(maxsize=1)
def anthropic_tool_schemas() -> list[dict[str, Any]]:
    """Return the full list of erp_assist tools as Anthropic schemas.

    Cached for the life of the process. If the tool surface changes
    (only possible at import time, since @mcp.tool is a module-level
    decorator), restart the Django process to pick up new tools.
    """
    tool_manager = mcp_adapter.mcp._tool_manager
    schemas: list[dict[str, Any]] = []
    for tool in tool_manager.list_tools():
        schemas.append(
            {
                "name": tool.name,
                "description": tool.description or "",
                "input_schema": _normalise_input_schema(tool.parameters or {}),
            }
        )
    return schemas


def tool_names() -> set[str]:
    """Set of all tool names currently registered. Used by the executor to
    validate tool-call requests before dispatching."""
    return {schema["name"] for schema in anthropic_tool_schemas()}

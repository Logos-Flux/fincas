"""Tool execution bridge from chat view to erp_assist.

Every @mcp.tool function on erp_assist.mcp_adapter is an async
function that returns a JSON-serialisable dict (or a structured error
envelope). We dispatch by name — no separate tool registry, no
duplication — so the chat tool surface stays in lockstep with the MCP
server's.
"""

from __future__ import annotations

from typing import Any

from erp_assist import mcp_adapter

from .llm.tool_schema import tool_names


class UnknownTool(Exception):
    """Raised when the model emits a tool_use for a name we don't expose."""


async def execute_tool_call(name: str, arguments: dict[str, Any]) -> Any:
    """Invoke an erp_assist MCP tool by name.

    Returns whatever the tool returns — typically a dict (detail call)
    or a list (list call), or an `{error, message}` envelope on failure.
    All known failure modes (validation errors, 404s, DRF errors, tool
    lookup failures) come back as envelopes the LLM can reason about,
    not exceptions that crash the stream.

    The `arguments` dict is passed straight through. erp_assist
    already validates writable-field names and guards confirm-required
    deletes, so this layer is intentionally thin.
    """
    if name not in tool_names():
        raise UnknownTool(f"Unknown tool: {name}")

    tool_fn = getattr(mcp_adapter, name)
    try:
        return await tool_fn(**arguments)
    except TypeError as exc:
        # Bad arguments (wrong shape, missing required, etc.). Return
        # as an error envelope so the model can self-correct without
        # the whole stream aborting.
        return {
            "error": "BadArguments",
            "message": f"Tool {name} rejected arguments: {exc}",
        }

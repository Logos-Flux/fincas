"""Tests for the tool bridge — schema translation and dispatch."""

from __future__ import annotations

import asyncio

import pytest

from chat.llm.tool_schema import anthropic_tool_schemas, tool_names
from chat.tools import UnknownTool, execute_tool_call


class TestToolSchemas:
    def test_at_least_70_tools_exposed(self) -> None:
        schemas = anthropic_tool_schemas()
        assert len(schemas) >= 70, f"expected ≥70 tools, got {len(schemas)}"

    def test_every_schema_has_required_keys(self) -> None:
        schemas = anthropic_tool_schemas()
        for s in schemas:
            assert "name" in s and s["name"], f"missing name: {s}"
            assert "description" in s, f"missing description on {s['name']}"
            assert "input_schema" in s, f"missing input_schema on {s['name']}"
            assert s["input_schema"].get("type") == "object", s["name"]
            assert "properties" in s["input_schema"], s["name"]

    def test_no_title_fields_leak_into_properties(self) -> None:
        """FastMCP adds title fields that Anthropic doesn't need.

        The cleaner should strip them to keep schemas compact.
        """
        for s in anthropic_tool_schemas():
            for prop_name, prop in s["input_schema"].get("properties", {}).items():
                if isinstance(prop, dict):
                    assert "title" not in prop, (
                        f"{s['name']}.{prop_name} has a stray title field"
                    )
            assert "title" not in s["input_schema"], (
                f"{s['name']} input_schema has a stray title field"
            )

    def test_required_flagship_tools_present(self) -> None:
        names = tool_names()
        for expected in [
            "list_products",
            "find_variant_by_sku",
            "update_variant_price",
            "bulk_update_variant_prices",
            "list_customers",
            "list_orders",
            "set_stock_quantity",
            "delete_quote",
        ]:
            assert expected in names, f"missing required tool: {expected}"


class TestExecuteToolCall:
    def test_unknown_tool_raises(self) -> None:
        with pytest.raises(UnknownTool):
            asyncio.run(execute_tool_call("not_a_real_tool", {}))

    def test_bad_arguments_returns_error_envelope(self) -> None:
        # list_brands takes keyword args; passing a positional-only-looking
        # call with an unknown kwarg will TypeError on the underlying fn.
        result = asyncio.run(execute_tool_call("list_brands", {"nonsense_kwarg": 42}))
        assert isinstance(result, dict)
        assert result.get("error") == "BadArguments"
        assert "list_brands" in result["message"]

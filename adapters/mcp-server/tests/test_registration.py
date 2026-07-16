"""Tool registration and JSON-schema (enum) correctness.

These verify the MCP server advertises exactly the expected ``engram_*`` tools
and that each tool's input schema surfaces the *current* API/SDK enum values.
Schema drift here is how an API change silently breaks MCP clients, so the
enums are pinned to the live SDK contract.
"""

from __future__ import annotations

from typing import Any

import pytest

from engram_mcp.server import build_server

EXPECTED_TOOLS = {
    "engram_remember",
    "engram_recall",
    "engram_search",
    "engram_classify",
    "engram_kg_query",
    "engram_kg_add",
    "engram_diary_write",
}


@pytest.fixture
def tools(engram_env) -> list[Any]:
    """All tools advertised by a freshly built server."""
    import asyncio

    return asyncio.run(build_server().list_tools())


def test_all_expected_tools_registered(tools) -> None:
    names = {t.name for t in tools}
    assert names >= EXPECTED_TOOLS


def test_no_unexpected_tools_registered(tools) -> None:
    names = {t.name for t in tools}
    extras = names - EXPECTED_TOOLS
    assert not extras, f"unexpected engram tools registered: {extras}"


def test_all_tool_names_prefixed_engram(tools) -> None:
    for tool in tools:
        assert tool.name.startswith("engram_"), tool.name


def test_tools_carry_descriptions(tools) -> None:
    """Each tool must document itself for MCP clients."""
    for tool in tools:
        assert tool.description, f"{tool.name} has no description"


def _enum_of(tool: Any, field: str) -> list[str]:
    """Return the ``enum`` values for ``field``, whether required or optional.

    An optional ``Literal[...] | None`` field (e.g. ``visibility``,
    ENG-SCOPE-001) surfaces as ``anyOf: [{enum: [...]}, {type: "null"}]``
    rather than a flat top-level ``enum`` key — check both shapes.
    """
    schema: dict[str, Any] = tool.inputSchema
    prop = schema["properties"][field]
    if "enum" in prop:
        return list(prop["enum"])
    for option in prop.get("anyOf", []):
        if "enum" in option:
            return list(option["enum"])
    raise AssertionError(f"no enum found for field {field!r} in schema {prop!r}")


def _tool(tools: list[Any], name: str) -> Any:
    return next(t for t in tools if t.name == name)


def test_remember_sensitivity_enum_matches_contract(tools) -> None:
    """sensitivity must expose normal/sensitive/restricted — never 'confidential'."""
    enum = _enum_of(_tool(tools, "engram_remember"), "sensitivity")
    assert enum == ["normal", "sensitive", "restricted"]
    assert "confidential" not in enum


def test_remember_visibility_enum(tools) -> None:
    assert _enum_of(_tool(tools, "engram_remember"), "visibility") == [
        "private",
        "workspace",
        "tenant",
    ]


def test_remember_source_type_enum(tools) -> None:
    assert _enum_of(_tool(tools, "engram_remember"), "source_type") == [
        "manual",
        "import",
        "migration",
        "extraction",
        "sync_turn",
        "pre_compress",
    ]


def test_recall_mode_enum_includes_semantic(tools) -> None:
    """BL-004: recall must advertise 'semantic' alongside 'startup'."""
    enum = _enum_of(_tool(tools, "engram_recall"), "mode")
    assert enum == ["startup", "semantic"]


def test_search_mode_enum(tools) -> None:
    assert _enum_of(_tool(tools, "engram_search"), "mode") == [
        "keyword",
        "semantic",
        "hybrid",
    ]


def test_kg_query_direction_enum(tools) -> None:
    assert _enum_of(_tool(tools, "engram_kg_query"), "direction") == [
        "outgoing",
        "incoming",
        "both",
    ]


def test_remember_required_content_only(tools) -> None:
    """Only ``content`` is required; every other field has a default."""
    schema: dict[str, Any] = _tool(tools, "engram_remember").inputSchema
    assert schema["required"] == ["content"]

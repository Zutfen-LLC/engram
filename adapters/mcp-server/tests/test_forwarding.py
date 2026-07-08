"""Unit-level tool-forwarding tests.

Each test drives an MCP tool through the in-memory client/session harness with a
**mocked SDK client** and asserts the MCP tool invoked the corresponding SDK
method with the expected request shape (arguments + defaults). This catches
adapter/SDK contract drift in CI without a database or network.
"""

from __future__ import annotations

from typing import Any

import engram_client
from mcp.shared.memory import create_connected_server_and_client_session


def _text(result: Any) -> str:
    return "".join(getattr(c, "text", "") for c in result.content)


async def _call(mcp_server, name: str, args: dict[str, Any]) -> Any:
    """Open an in-memory session and call ``name``; assert it did not error."""
    async with create_connected_server_and_client_session(mcp_server) as session:
        result = await session.call_tool(name, args)
    assert not result.isError, f"{name} unexpectedly errored: {_text(result)}"
    return result


# ---- engram_remember ----


async def test_remember_forwards_full_shape(mcp_server, mock_client) -> None:
    """All explicit + default fields are forwarded to the SDK."""
    result = await _call(
        mcp_server,
        "engram_remember",
        {
            "content": "always use lowercase table names",
            "kind": "invariant",
            "importance": 0.9,
            "sensitivity": "restricted",
        },
    )

    assert mock_client.remember.await_args.args == ("always use lowercase table names",)
    assert mock_client.remember.await_args.kwargs == {
        "kind": "invariant",
        "wing": None,
        "room": None,
        "workspace": None,
        "visibility": "workspace",
        "source_type": "manual",
        "importance": 0.9,
        "sensitivity": "restricted",
        "subject_type": None,
        "subject_id": None,
        "subject_name": None,
        "external_id": None,
        "external_source": None,
    }
    assert result.structuredContent["status"] == "created"
    assert result.structuredContent["memory_confidence"] == 0.9


async def test_remember_applies_trust_defaults(mcp_server, mock_client) -> None:
    """Omitted fields get the documented defaults (manual/workspace/normal)."""
    await _call(mcp_server, "engram_remember", {"content": "a bare fact"})

    kwargs = mock_client.remember.await_args.kwargs
    assert kwargs["source_type"] == "manual"
    assert kwargs["visibility"] == "workspace"
    assert kwargs["sensitivity"] == "normal"
    assert kwargs["importance"] == 0.5


# ---- engram_recall ----


async def test_recall_forwards_semantic_shape(mcp_server, mock_client) -> None:
    result = await _call(
        mcp_server,
        "engram_recall",
        {"mode": "semantic", "query": "table names", "token_budget": 512},
    )

    assert mock_client.recall.await_args.args == ()
    assert mock_client.recall.await_args.kwargs == {
        "mode": "semantic",
        "query": "table names",
        "workspace": None,
        "token_budget": 512,
    }
    assert result.structuredContent["item_count"] == 1


async def test_recall_startup_defaults(mcp_server, mock_client) -> None:
    await _call(mcp_server, "engram_recall", {})

    kwargs = mock_client.recall.await_args.kwargs
    assert kwargs["mode"] == "startup"
    assert kwargs["query"] is None


# ---- engram_search ----


async def test_search_forwards_shape(mcp_server, mock_client) -> None:
    result = await _call(
        mcp_server,
        "engram_search",
        {"query": "table names", "mode": "keyword", "limit": 5},
    )

    assert mock_client.search.await_args.args == ("table names",)
    assert mock_client.search.await_args.kwargs == {
        "mode": "keyword",
        "limit": 5,
        "wing": None,
        "room": None,
        "kind": None,
    }
    assert result.structuredContent["total"] == 1


async def test_search_hybrid_default(mcp_server, mock_client) -> None:
    await _call(mcp_server, "engram_search", {"query": "x"})

    assert mock_client.search.await_args.kwargs["mode"] == "hybrid"
    assert mock_client.search.await_args.kwargs["limit"] == 10


# ---- engram_classify ----


async def test_classify_forwards_shape(mcp_server, mock_client) -> None:
    result = await _call(
        mcp_server,
        "engram_classify",
        {"content": "always lowercase", "context": "chat", "workspace": "eng"},
    )

    assert mock_client.classify.await_args.args == ("always lowercase",)
    assert mock_client.classify.await_args.kwargs == {"context": "chat", "workspace": "eng"}
    assert result.structuredContent["suggested_kind"] == "invariant"


# ---- engram_kg_add ----


async def test_kg_add_forwards_shape(mcp_server, mock_client) -> None:
    result = await _call(
        mcp_server,
        "engram_kg_add",
        {
            "subject": "users",
            "predicate": "located_in",
            "object": "us-east-1",
            "confidence": 0.7,
        },
    )

    assert mock_client.kg_add.await_args.args == ("users", "located_in", "us-east-1")
    assert mock_client.kg_add.await_args.kwargs == {
        "workspace": None,
        "source_item_id": None,
        "confidence": 0.7,
    }
    assert result.structuredContent["triple"]["predicate"] == "located_in"


# ---- engram_kg_query ----


async def test_kg_query_forwards_shape(mcp_server, mock_client) -> None:
    result = await _call(
        mcp_server,
        "engram_kg_query",
        {"entity": "users", "direction": "outgoing", "predicate": "located_in"},
    )

    assert mock_client.kg_query.await_args.args == ("users",)
    assert mock_client.kg_query.await_args.kwargs == {
        "direction": "outgoing",
        "predicate": "located_in",
        "as_of": None,
    }
    # list-returning tools are surfaced under structuredContent["result"]
    triples = result.structuredContent["result"]
    assert triples[0]["object"] == "us-east-1"


# ---- engram_diary_write ----


async def test_diary_write_forwards_shape(mcp_server, mock_client) -> None:
    result = await _call(
        mcp_server,
        "engram_diary_write",
        {"entry": "explored the search path", "principal": "hermes", "topic": "debug"},
    )

    assert mock_client.diary_write.await_args.args == ("explored the search path", "hermes")
    assert mock_client.diary_write.await_args.kwargs == {"topic": "debug"}
    assert result.structuredContent["status"] == "created"


# ---- error propagation through the MCP layer ----


async def test_sdk_error_surfaces_as_mcp_error(mcp_server, mock_client) -> None:
    """An SDK exception must become a useful MCP error result, not be swallowed."""
    mock_client.recall.side_effect = engram_client.EngramServerError(
        503, "upstream is down", {"detail": "upstream is down"}
    )

    async with create_connected_server_and_client_session(mcp_server) as session:
        result = await session.call_tool("engram_recall", {})

    assert result.isError
    message = _text(result)
    assert "engram_recall" in message
    assert "503" in message
    assert "upstream is down" in message

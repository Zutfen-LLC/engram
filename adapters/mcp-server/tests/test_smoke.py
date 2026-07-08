from __future__ import annotations

import pytest

from engram_mcp.server import build_server


def test_build_server_requires_base_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ENGRAM_BASE_URL", raising=False)

    with pytest.raises(RuntimeError, match="ENGRAM_BASE_URL is required"):
        build_server()


def test_build_server_smoke(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENGRAM_BASE_URL", "http://engram.test")
    monkeypatch.delenv("ENGRAM_API_KEY", raising=False)
    monkeypatch.delenv("ENGRAM_TIMEOUT", raising=False)

    server = build_server()

    assert server is not None


async def test_engram_remember_schema_exposes_restricted_not_confidential(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The engram_remember tool schema must advertise the real sensitivity
    vocabulary (normal/sensitive/restricted). 'confidential' is a value the
    database has never accepted and must not appear in the tool schema."""
    monkeypatch.setenv("ENGRAM_BASE_URL", "http://engram.test")
    monkeypatch.delenv("ENGRAM_API_KEY", raising=False)
    monkeypatch.delenv("ENGRAM_TIMEOUT", raising=False)

    server = build_server()
    tools = await server.list_tools()
    remember_tool = next(t for t in tools if t.name == "engram_remember")

    schema_text = str(remember_tool.inputSchema)
    assert "restricted" in schema_text
    assert "confidential" not in schema_text


async def test_engram_recall_schema_advertises_semantic_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """BL-004: the engram_recall tool must surface the 'semantic' recall mode
    in its schema — the API now implements it, so the MCP enum must not hide
    it from clients."""
    monkeypatch.setenv("ENGRAM_BASE_URL", "http://engram.test")
    monkeypatch.delenv("ENGRAM_API_KEY", raising=False)
    monkeypatch.delenv("ENGRAM_TIMEOUT", raising=False)

    server = build_server()
    tools = await server.list_tools()
    recall_tool = next(t for t in tools if t.name == "engram_recall")

    schema_text = str(recall_tool.inputSchema)
    assert "semantic" in schema_text
    assert "startup" in schema_text

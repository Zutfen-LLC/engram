"""Integration round trips against a running Engram service.

A real uvicorn Engram server is started in-process against a live PostgreSQL
and driven end-to-end through the in-memory MCP client harness:

    MCP client -> MCP server (engram_mcp) -> EngramClient (httpx)
               -> HTTP -> uvicorn -> FastAPI -> PostgreSQL

These skip when no database is reachable; CI runs them against the Compose
postgres and treats an unexpected skip as a failure (``ENGRAM_FAIL_ON_DB_SKIP``).

Auth must be disabled (``ENGRAM_AUTH_ENABLED=false``), matching the CI Compose
and the local ``docker compose up`` defaults.
"""

from __future__ import annotations

import socket
import threading
import time
from collections.abc import AsyncIterator, Iterator
from typing import Any

import pytest
from mcp.shared.memory import create_connected_server_and_client_session
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from engram_mcp.server import build_server

# Tables mutated by the round trips, in FK-safe delete order. The ``engram`` DB
# role owns these tables, so RLS is bypassed and the cleanup sees every row.
_CLEAN_TABLES = (
    "feedback_events",
    "item_events",
    "memory_embeddings",
    "kg_triples",
    "recall_logs",
    "deletion_events",
    "memory_items",
)


def _text(result: Any) -> str:
    return "".join(getattr(c, "text", "") for c in result.content)


def _closed_port() -> int:
    """Reserve and release an ephemeral port so nothing is listening on it."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


@pytest.fixture(scope="module")
def engram_url(db_ok: bool) -> Iterator[str]:
    """Start a real Engram uvicorn service on an ephemeral port; yield its URL.

    Skips the whole module when no DB is reachable.
    """
    if not db_ok:
        pytest.skip("integration test requires a live PostgreSQL (run docker compose up)")

    import uvicorn
    from engram.api.app import create_app

    app = create_app()
    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    # Wait for the server to bind a socket.
    deadline = time.monotonic() + 15.0
    while not server.started:
        if time.monotonic() > deadline:
            server.should_exit = True
            pytest.fail("uvicorn Engram service did not start within 15s")
        time.sleep(0.02)

    bound = server.servers[0].sockets[0].getsockname()
    port = bound[1]
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=10)


@pytest.fixture
def mcp_live_server(engram_url: str, monkeypatch: pytest.MonkeyPatch):
    """Build an MCP server whose SDK client targets the live Engram service.

    Tests open the in-memory client session inline (not via a yielding fixture)
    to keep anyio's task group within a single task.
    """
    monkeypatch.setenv("ENGRAM_BASE_URL", engram_url)
    monkeypatch.delenv("ENGRAM_API_KEY", raising=False)
    return build_server()


@pytest.fixture(autouse=True)
async def _clean_db() -> AsyncIterator[None]:
    """Wipe round-trip data before each test for isolation (no-op without a DB)."""
    try:
        from engram.config import settings
    except Exception:
        yield
        return
    engine = create_async_engine(settings.database_url, poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            for table in _CLEAN_TABLES:
                await conn.execute(text(f"DELETE FROM {table}"))
    except Exception:
        pass
    finally:
        await engine.dispose()
    yield


# ---- happy-path round trips ----


async def test_remember_recall_search_round_trip(mcp_live_server) -> None:
    """engram_remember -> engram_recall -> engram_search against a live service.

    Uses kind='fact' (requires_review=False in the builtin memory_kinds
    registry) so this generic plumbing round trip isn't entangled with
    review-gating semantics — 'invariant' has requires_review=True (ENG-AUD-010),
    so a manual write of that kind starts 'proposed' and is correctly excluded
    from startup recall until reviewed, which is not what this test exercises.
    """
    content = "Always use lowercase table names for portability across databases"

    async with create_connected_server_and_client_session(mcp_live_server) as session:
        created = await session.call_tool(
            "engram_remember",
            {"content": content, "kind": "fact", "importance": 0.95},
        )
        assert not created.isError, _text(created)
        item_id = created.structuredContent["id"]
        assert created.structuredContent["status"] == "created"

        # recall: the item must surface in the startup working set.
        recalled = await session.call_tool("engram_recall", {"mode": "startup"})
        assert not recalled.isError, _text(recalled)
        recall_ids = {item.get("id") for item in recalled.structuredContent["items"]}
        assert item_id in recall_ids, "remembered item missing from startup recall"

        # search: keyword search must find it.
        searched = await session.call_tool(
            "engram_search",
            {"query": "lowercase table names", "mode": "keyword", "limit": 20},
        )
        assert not searched.isError, _text(searched)
        search_ids = {r.get("id") for r in searched.structuredContent["results"]}
        assert item_id in search_ids, "remembered item missing from keyword search"


async def test_kg_add_query_round_trip(mcp_live_server) -> None:
    """engram_kg_add -> engram_kg_query against a live service."""
    async with create_connected_server_and_client_session(mcp_live_server) as session:
        added = await session.call_tool(
            "engram_kg_add",
            {
                "subject": "payments-service",
                "predicate": "deployed_in",
                "object": "us-east-1",
                "confidence": 0.85,
            },
        )
        assert not added.isError, _text(added)
        triple_id = added.structuredContent["id"]

        queried = await session.call_tool(
            "engram_kg_query",
            {"entity": "payments-service", "direction": "both"},
        )
        assert not queried.isError, _text(queried)
        # list-returning tools surface under structuredContent["result"]
        triples = queried.structuredContent["result"]
        query_ids = {t.get("id") for t in triples}
        assert triple_id in query_ids, "added triple missing from kg query"
        matched = next(t for t in triples if t.get("id") == triple_id)
        assert matched["object"] == "us-east-1"


# ---- failure paths ----


async def test_validation_error_propagates(mcp_live_server) -> None:
    """Content matching the secret denylist must surface a useful 422 error."""
    secret = "the deploy token is ghp_" + "a" * 36
    async with create_connected_server_and_client_session(mcp_live_server) as session:
        result = await session.call_tool("engram_remember", {"content": secret})

    assert result.isError
    message = _text(result)
    # The SDK raises EngramValidationError(422, ...); the status reaches the client.
    assert "422" in message


async def test_unreachable_service_returns_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A service that cannot be reached must error promptly, not hang or swallow."""
    monkeypatch.setenv("ENGRAM_BASE_URL", f"http://127.0.0.1:{_closed_port()}")
    monkeypatch.setenv("ENGRAM_TIMEOUT", "2")
    server = build_server()

    async with create_connected_server_and_client_session(server) as session:
        result = await session.call_tool("engram_recall", {})

    assert result.isError
    message = _text(result)
    assert "engram_recall" in message

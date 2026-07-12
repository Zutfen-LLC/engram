"""Shared fixtures and skip handling for the MCP adapter tests.

The suite is layered:

* **Unit tests** (registration, config, forwarding) build an MCP server with an
  injected mock SDK client and drive it through an in-memory
  :class:`~mcp.client.session.ClientSession`. No network, no database, no
  Engram server process required.
* **Integration tests** (``test_integration.py``) stand up a real uvicorn
  Engram service against a live PostgreSQL and exercise full
  MCP -> SDK -> HTTP -> FastAPI -> DB round trips. They skip automatically when
  no database is reachable, but CI fails the run if they skip unexpectedly
  (``ENGRAM_FAIL_ON_DB_SKIP=1``).
"""

from __future__ import annotations

import logging
import os
from typing import cast
from unittest.mock import AsyncMock
from uuid import UUID

import engram_client
import pytest

from engram_mcp.server import build_server

# mcp logs every request at INFO; quiet it so test output stays readable.
logging.getLogger("mcp").setLevel(logging.WARNING)

# Tests that need a live PostgreSQL skip with a reason containing this marker;
# CI (ENGRAM_FAIL_ON_DB_SKIP=1) treats an unexpected skip as a failure.
DB_SKIP_MARKER = "requires a live PostgreSQL"

ITEM_ID = UUID("11111111-1111-1111-1111-111111111111")
OTHER_ID = UUID("22222222-2222-2222-2222-222222222222")


def build_mock_client() -> AsyncMock:
    """Return an :class:`AsyncMock` typed as the SDK client.

    Each method is wired to return a valid SDK model instance so the MCP tools
    can call ``model_dump`` exactly as they do in production. Individual tests
    override specific return values or ``side_effect`` as needed.
    """
    client = AsyncMock(spec=engram_client.EngramClient)

    client.remember.return_value = engram_client.RememberResponse(
        id=ITEM_ID,
        status="created",
        review_status="active",
        memory_confidence=0.9,
    )
    client.recall.return_value = engram_client.RecallResponse(
        working_set="- [invariant] always use lowercase table names",
        item_count=1,
        byte_count=42,
        omitted_count=0,
        items=[{"id": str(ITEM_ID), "kind": "invariant"}],
        recall_log_id=str(OTHER_ID),
    )
    client.search.return_value = engram_client.SearchResponse(
        results=[{"id": str(ITEM_ID), "content": "hit", "score": 0.9, "mode": "hybrid"}],
        total=1,
    )
    client.classify.return_value = engram_client.ClassifyResponse(
        suggested_kind="invariant",
        suggested_wing="engineering",
        suggested_room="conventions",
        confidence=0.82,
        reason="rule: 'always'",
        rules_matched=["always_keyword"],
    )
    client.kg_add.return_value = engram_client.KgAddResponse(
        id=OTHER_ID,
        triple={"subject": "users", "predicate": "located_in", "object": "us-east-1"},
        source_item_id=ITEM_ID,
    )
    client.kg_query.return_value = [
        engram_client.KgTripleOut(
            id=OTHER_ID,
            subject="users",
            predicate="located_in",
            object="us-east-1",
            confidence=0.7,
            review_status="active",
            created_at="2026-07-07T12:00:00+00:00",
        )
    ]
    client.diary_write.return_value = engram_client.DiaryWriteResponse(
        id=ITEM_ID,
        status="created",
        review_status="proposed",
        principal_id=OTHER_ID,
        actor_principal_id=OTHER_ID,
        represented=False,
        authority=10,
        authority_label="inferred",
    )
    return client


@pytest.fixture
def mock_client() -> AsyncMock:
    """A fresh mocked SDK client for each test."""
    return build_mock_client()


@pytest.fixture
def mcp_server(mock_client: AsyncMock):
    """An MCP server wired to the mock SDK client (no env config needed).

    Tests open an in-memory client session inline via
    :func:`create_connected_server_and_client_session` (rather than through a
    yielding fixture) to keep anyio's task group within a single task.
    """
    return build_server(client=cast(engram_client.EngramClient, mock_client))


@pytest.fixture
def engram_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set a minimal ``ENGRAM_*`` env so ``build_server`` config succeeds."""
    monkeypatch.setenv("ENGRAM_BASE_URL", "http://engram.test")
    monkeypatch.delenv("ENGRAM_API_KEY", raising=False)
    monkeypatch.delenv("ENGRAM_TIMEOUT", raising=False)


@pytest.fixture(scope="module")
def db_ok() -> bool:
    """Module-scoped DB reachability flag for the integration module."""
    return _db_available()


def _db_available() -> bool:
    """Best-effort check that the Engram DB is connectable.

    Imports the server package lazily so unit tests never require it.
    """
    try:
        from engram.config import settings
        from sqlalchemy import text
        from sqlalchemy.ext.asyncio import create_async_engine
    except Exception:
        return False
    import asyncio

    async def _check() -> bool:
        engine = create_async_engine(settings.database_url)
        try:
            async with engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
            return True
        except Exception:
            return False
        finally:
            await engine.dispose()

    try:
        return asyncio.run(_check())
    except Exception:
        return False


# ---- CI: fail if DB-required integration tests are skipped unexpectedly ----

_skipped_db_tests: list[str] = []


def pytest_runtest_logreport(report: pytest.TestReport) -> None:
    if os.environ.get("ENGRAM_FAIL_ON_DB_SKIP") != "1":
        return
    # Skips can land in either phase: a skip raised in the test body reports as
    # "call", while a skip raised in a fixture (e.g. the module-scoped
    # ``engram_url`` fixture) reports as "setup". Catch both.
    if report.when not in ("setup", "call") or not report.skipped:
        return
    if DB_SKIP_MARKER in str(report.longrepr):
        _skipped_db_tests.append(report.nodeid)


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    if os.environ.get("ENGRAM_FAIL_ON_DB_SKIP") != "1":
        return
    if not _skipped_db_tests:
        return
    terminal = session.config.pluginmanager.get_plugin("terminalreporter")
    if terminal is not None:
        joined = ", ".join(_skipped_db_tests)
        terminal.write_line(
            f"DB-required MCP integration tests skipped unexpectedly: {joined}",
            red=True,
        )
    session.exitstatus = pytest.ExitCode.TESTS_FAILED

"""MCP server exposing Engram memory tools.

Each ``engram_*`` tool is a thin async wrapper over the corresponding
:class:`engram_client.EngramClient` method. The server speaks stdio by default,
which is the transport Hermes uses to launch MCP servers as subprocesses.

Configuration is read from the environment once at startup:

- ``ENGRAM_BASE_URL`` (required) — Engram REST API base URL.
- ``ENGRAM_API_KEY``    (optional) — bearer token; omitted for local no-auth setups.
- ``ENGRAM_TIMEOUT``    (optional) — per-request timeout in seconds, default 30.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, Literal

import engram_client
from mcp.server.fastmcp import Context, FastMCP
from mcp.server.session import ServerSession
from pydantic import BaseModel
from starlette.requests import Request

# Named Literal aliases so tool signatures surface as enums in MCP client schemas
# rather than bare free-form strings.
SourceType = Literal["manual", "import", "migration", "extraction", "sync_turn", "pre_compress"]
Sensitivity = Literal["normal", "sensitive", "restricted"]
Visibility = Literal["private", "workspace", "tenant", "public"]
SearchMode = Literal["keyword", "semantic", "hybrid"]
RecallMode = Literal["startup", "semantic"]
Direction = Literal["outgoing", "incoming", "both"]

# Context is generic over (session, lifespan-state, request). Pin the lifespan
# state to EngramState so tools get a typed client and mypy --strict is happy.
ToolCtx = Context[ServerSession, "EngramState", Request]

_DEFAULT_TIMEOUT = 30.0


def _config() -> tuple[str, str | None, float]:
    """Read Engram connection config from the environment.

    Returns ``(base_url, api_key, timeout)``. Raises ``RuntimeError`` if
    ``ENGRAM_BASE_URL`` is unset — failing fast at startup beats failing on the
    first tool call.
    """
    base_url = os.environ.get("ENGRAM_BASE_URL")
    if not base_url:
        msg = (
            "ENGRAM_BASE_URL is required. Set it to the Engram REST API base URL "
            "(e.g. http://localhost:8000)."
        )
        raise RuntimeError(msg)
    api_key = os.environ.get("ENGRAM_API_KEY") or None
    timeout_raw = os.environ.get("ENGRAM_TIMEOUT")
    try:
        timeout = float(timeout_raw) if timeout_raw else _DEFAULT_TIMEOUT
    except ValueError:
        timeout = _DEFAULT_TIMEOUT
    return base_url, api_key, timeout


class EngramState(BaseModel):
    """Lifespan-shared state: the Engram client held open for the server lifetime."""

    model_config = {"arbitrary_types_allowed": True}

    client: engram_client.EngramClient


def _client(ctx: ToolCtx) -> engram_client.EngramClient:
    """Resolve the shared Engram client from the request's lifespan state."""
    # The object yielded by the lifespan below becomes request_context's
    # lifespan_context for every tool call served within that lifespan.
    state: EngramState = ctx.request_context.lifespan_context
    return state.client


def build_server(
    client: engram_client.EngramClient | None = None,
) -> FastMCP[EngramState]:
    """Construct the FastMCP server with all Engram tools registered.

    A single :class:`~engram_client.EngramClient` is created in the lifespan
    context so tool calls reuse one HTTP connection pool for the server's
    lifetime rather than opening one per call.

    Pass ``client`` to inject a pre-built SDK client (used by tests). When
    injected the lifespan neither constructs nor closes a client — the caller
    owns it — and ``ENGRAM_*`` environment config is not read, so no
    ``ENGRAM_BASE_URL`` is required.
    """
    injected = client is not None
    base_url: str = ""
    api_key: str | None = None
    timeout = _DEFAULT_TIMEOUT
    if not injected:
        base_url, api_key, timeout = _config()

    @asynccontextmanager
    async def lifespan(_server: FastMCP) -> AsyncIterator[EngramState]:
        if client is not None:
            # Injected (test) path: caller owns the client, so don't close it.
            yield EngramState(client=client)
        else:
            async with engram_client.EngramClient(base_url, api_key, timeout=timeout) as created:
                yield EngramState(client=created)

    mcp = FastMCP[EngramState](
        name="engram",
        instructions=(
            "Engram institutional memory tools. Use engram_remember to persist "
            "facts, engram_recall to fetch the startup working set, "
            "engram_search for keyword/semantic lookup."
        ),
        lifespan=lifespan,
    )

    register_tools(mcp)
    return mcp


def register_tools(mcp: FastMCP[EngramState]) -> None:
    """Register all ``engram_*`` tools on ``mcp``."""

    @mcp.tool(name="engram_remember")
    async def remember(
        ctx: ToolCtx,
        content: str,
        kind: str | None = None,
        wing: str | None = None,
        room: str | None = None,
        workspace: str | None = None,
        visibility: Visibility | None = None,
        source_type: SourceType = "manual",
        importance: float = 0.5,
        sensitivity: Sensitivity = "normal",
        subject_type: str | None = None,
        subject_id: str | None = None,
        subject_name: str | None = None,
        external_id: str | None = None,
        external_source: str | None = None,
    ) -> dict[str, Any]:
        """Persist a memory item with dedup, trust defaults, and supersession.

        ``visibility`` is optional (ENG-SCOPE-001): omitted, it derives a safe
        default from ``workspace`` — private when no workspace is given,
        workspace-shared when one is. An explicit ``visibility="workspace"``
        still requires ``workspace`` to be set (the server rejects it with a
        422 otherwise).

        Returns the new item id, status (created/deduped/superseded),
        review_status, and memory_confidence.
        """
        resp = await _client(ctx).remember(
            content,
            kind=kind,
            wing=wing,
            room=room,
            workspace=workspace,
            visibility=visibility,
            source_type=source_type,
            importance=importance,
            sensitivity=sensitivity,
            subject_type=subject_type,
            subject_id=subject_id,
            subject_name=subject_name,
            external_id=external_id,
            external_source=external_source,
        )
        return resp.model_dump(mode="json")

    @mcp.tool(name="engram_recall")
    async def recall(
        ctx: ToolCtx,
        mode: RecallMode = "startup",
        query: str | None = None,
        workspace: str | None = None,
        token_budget: int | None = None,
    ) -> dict[str, Any]:
        """Fetch a bounded working set of active memories (startup or semantic)."""
        resp = await _client(ctx).recall(
            mode=mode,
            query=query,
            workspace=workspace,
            token_budget=token_budget,
        )
        return resp.model_dump(mode="json")

    @mcp.tool(name="engram_search")
    async def search(
        ctx: ToolCtx,
        query: str,
        mode: SearchMode = "hybrid",
        limit: int = 10,
        wing: str | None = None,
        room: str | None = None,
        kind: str | None = None,
    ) -> dict[str, Any]:
        """Keyword (FTS), semantic (vector), or hybrid search over active memories."""
        resp = await _client(ctx).search(
            query,
            mode=mode,
            limit=limit,
            wing=wing,
            room=room,
            kind=kind,
        )
        return resp.model_dump(mode="json")

    @mcp.tool(name="engram_classify")
    async def classify(
        ctx: ToolCtx,
        content: str,
        context: str | None = None,
        workspace: str | None = None,
    ) -> dict[str, Any]:
        """Suggest kind, wing, room, and visibility for raw text."""
        resp = await _client(ctx).classify(content, context=context, workspace=workspace)
        return resp.model_dump(mode="json")

    @mcp.tool(name="engram_kg_query")
    async def kg_query(
        ctx: ToolCtx,
        entity: str,
        direction: Direction = "both",
        predicate: str | None = None,
        as_of: str | None = None,
    ) -> list[dict[str, Any]]:
        """Query knowledge-graph triples for ``entity`` (as subject or object)."""
        triples = await _client(ctx).kg_query(
            entity,
            direction=direction,
            predicate=predicate,
            as_of=as_of,
        )
        return [t.model_dump(mode="json") for t in triples]

    @mcp.tool(name="engram_kg_add")
    async def kg_add(
        ctx: ToolCtx,
        subject: str,
        predicate: str,
        object: str,
        workspace: str | None = None,
        visibility: Visibility | None = None,
        source_item_id: str | None = None,
        confidence: float = 0.5,
    ) -> dict[str, Any]:
        """Add a knowledge-graph triple, backed by a memory item."""
        resp = await _client(ctx).kg_add(
            subject,
            predicate,
            object,
            workspace=workspace,
            visibility=visibility,
            source_item_id=source_item_id,
            confidence=confidence,
        )
        return resp.model_dump(mode="json")

    @mcp.tool(name="engram_diary_write")
    async def diary_write(
        ctx: ToolCtx,
        entry: str,
        principal: str | None = None,
        topic: str | None = None,
        on_behalf_of_principal_id: str | None = None,
        reason: str | None = None,
    ) -> dict[str, Any]:
        """Write the caller's diary; admin representation is explicit by UUID."""
        resp = await _client(ctx).diary_write(
            entry,
            principal,
            topic=topic,
            on_behalf_of_principal_id=on_behalf_of_principal_id,
            reason=reason,
        )
        return resp.model_dump(mode="json")


def main() -> None:
    """Entry point: build the server and run it over stdio."""
    build_server().run()


if __name__ == "__main__":
    main()

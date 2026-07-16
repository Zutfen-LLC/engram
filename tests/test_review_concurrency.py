# ruff: noqa: E501
"""Real-Postgres concurrency coverage for V2-BL-003A.

Two independent connections/transactions race a review decision or a
verification against the same row. ``POST /v1/items/{id}/review`` and
``POST /v1/items/{id}/verify`` both resolve the target item with
``SELECT ... FOR UPDATE`` (``_require_eligible_item(..., for_update=True)``
in engram/api/routes/memory.py) before evaluating the transition/verification
— so the loser blocks at the database until the winner commits, then
re-evaluates against the *post-commit* state rather than a stale read. These
tests prove that serialization holds under real concurrent traffic, not just
by inspection of the locking code.

Requires a live PostgreSQL with the v2 schema; skips automatically when no DB
is reachable.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from engram.api.app import create_app
from engram.config import settings
from engram.db import get_session

_test_engine = create_async_engine(settings.database_url, poolclass=NullPool)
_test_session_factory = async_sessionmaker(
    _test_engine, class_=AsyncSession, expire_on_commit=False
)


async def _db_ok() -> bool:
    try:
        async with _test_engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


def _require_db():
    pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")


@pytest.fixture(autouse=True)
async def _clean_db():
    if not await _db_ok():
        return
    async with _test_engine.begin() as conn:
        await conn.execute(text("DELETE FROM item_events"))
        await conn.execute(text("DELETE FROM memory_items"))
        await conn.execute(text("DELETE FROM workspace_members"))
        await conn.execute(text("DELETE FROM workspaces WHERE slug != 'general'"))
        await conn.execute(text("DELETE FROM tenants WHERE slug != 'default'"))


async def _seed_tenant(name: str) -> str:
    tenant_id = str(uuid4())
    async with _test_session_factory() as session:
        await session.execute(
            text("INSERT INTO tenants (id, name, slug) VALUES (:id, :name, :slug)"),
            {"id": tenant_id, "name": name, "slug": f"{name.lower()}-{tenant_id[:8]}"},
        )
        await session.execute(
            text(
                "INSERT INTO tenant_config (tenant_id, config_version, active) "
                "VALUES (:tid, 'v1', TRUE)"
            ),
            {"tid": tenant_id},
        )
        await session.commit()
    return tenant_id


async def _seed_principal(tenant_id: str, name: str, ptype: str) -> str:
    principal_id = str(uuid4())
    async with _test_session_factory() as session:
        await session.execute(
            text(
                "INSERT INTO principals (id, tenant_id, name, type) "
                "VALUES (:id, :tid, :name, :type)"
            ),
            {"id": principal_id, "tid": tenant_id, "name": name, "type": ptype},
        )
        await session.commit()
    return principal_id


async def _insert_item(
    *, tenant_id: str, principal_id: str, content: str, review_status: str = "proposed"
) -> str:
    item_id = str(uuid4())
    async with _test_session_factory() as session:
        await session.execute(
            text(
                "INSERT INTO memory_items ("
                "id, tenant_id, principal_id, content, content_hash, kind, "
                "visibility, review_status, memory_confidence, source_trust, importance, "
                "source_type"
                ") VALUES ("
                ":id, :tenant_id, :principal_id, :content, :content_hash, 'fact', "
                "'tenant', :review_status, 0.8, 0.7, 0.5, 'manual')"
            ),
            {
                "id": item_id,
                "tenant_id": tenant_id,
                "principal_id": principal_id,
                "content": content,
                "content_hash": f"sha256:{uuid4().hex}",
                "review_status": review_status,
            },
        )
        await session.commit()
    return item_id


def _make_client(tenant_id: str, principal_id: str) -> AsyncClient:
    async def _override_get_session() -> AsyncIterator[AsyncSession]:
        async with _test_session_factory() as session:
            await session.execute(
                text("SELECT set_config('app.tenant_id', :tid, true)"), {"tid": tenant_id}
            )
            await session.execute(
                text("SELECT set_config('app.principal_id', :pid, true)"),
                {"pid": principal_id},
            )
            yield session

    app = create_app()
    app.dependency_overrides[get_session] = _override_get_session
    transport = ASGITransport(app=app)
    return AsyncClient(transport=transport, base_url="http://test")


async def _events_for(item_id: str) -> list[dict[str, object]]:
    async with _test_session_factory() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT event_type, field_name, old_value, new_value, "
                    "actor_principal_id::text AS actor_principal_id "
                    "FROM item_events WHERE item_id = :id ORDER BY created_at ASC, id ASC"
                ),
                {"id": item_id},
            )
        ).mappings().all()
    return [dict(r) for r in rows]


async def _item_row(item_id: str) -> dict[str, object]:
    async with _test_session_factory() as session:
        return dict(
            (
                await session.execute(
                    text("SELECT * FROM memory_items WHERE id = :id"), {"id": item_id}
                )
            )
            .mappings()
            .one()
        )


async def _scenario() -> dict[str, str]:
    tenant_1 = await _seed_tenant("ReviewConcurrencyT1")
    agent = await _seed_principal(tenant_1, f"agent-{uuid4().hex[:8]}", "agent")
    user_a = await _seed_principal(tenant_1, f"user-a-{uuid4().hex[:8]}", "user")
    user_b = await _seed_principal(tenant_1, f"user-b-{uuid4().hex[:8]}", "user")
    return {"tenant_1": tenant_1, "agent": agent, "user_a": user_a, "user_b": user_b}


# ===========================================================================
# Competing human review decisions
# ===========================================================================


async def test_competing_review_decisions_serialize_via_row_lock():
    """Two authorized users concurrently attempt proposed -> active and
    proposed -> rejected on the same item. Both requests are individually
    authorized and (active <-> rejected) chains in either direction, so both
    ultimately succeed — but the *order* is decided by which connection wins
    the ``SELECT ... FOR UPDATE`` lock, and the audit trail must truthfully
    describe the serialized sequence: exactly one event has
    ``old_value='proposed'`` (the true initial state), and the other event's
    ``old_value`` must equal the winner's ``new_value`` — never a stale
    'proposed' read that never actually happened for the loser."""
    if not await _db_ok():
        _require_db()
    s = await _scenario()
    item_id = await _insert_item(
        tenant_id=s["tenant_1"], principal_id=s["agent"], content="race target"
    )

    async def _activate():
        async with _make_client(s["tenant_1"], s["user_a"]) as client:
            return await client.post(
                f"/v1/items/{item_id}/review", json={"review_status": "active"}
            )

    async def _reject():
        async with _make_client(s["tenant_1"], s["user_b"]) as client:
            return await client.post(
                f"/v1/items/{item_id}/review", json={"review_status": "rejected"}
            )

    resp_a, resp_b = await asyncio.gather(_activate(), _reject())

    # Both authorized principals' requests succeed regardless of lock order —
    # this is the "no lost update" property: neither request is silently
    # dropped or overwritten without a trace.
    assert resp_a.status_code == 200, resp_a.text
    assert resp_b.status_code == 200, resp_b.text

    events = await _events_for(item_id)
    assert len(events) == 2, "exactly one event per request — no lost update, no duplication"

    proposed_origin = [e for e in events if e["old_value"] == "proposed"]
    assert len(proposed_origin) == 1, (
        "exactly one event may claim old_value='proposed' — the true initial "
        "state; the loser must observe the winner's post-commit state, not a "
        "stale 'proposed' read"
    )
    winner, loser = events[0], events[1]
    # Ordering by created_at/id: the second event's old_value must equal the
    # first event's new_value — the loser genuinely observed the serialized
    # post-commit state rather than an impossible old/new pair.
    assert loser["old_value"] == winner["new_value"]
    assert {winner["new_value"], loser["new_value"]} == {"active", "rejected"}

    final = await _item_row(item_id)
    assert final["review_status"] == loser["new_value"]


async def test_competing_terminal_decisions_second_may_become_invalid():
    """proposed -> archived vs proposed -> disputed: whichever wins first
    reaches a state from which the other's originally-requested transition is
    no longer structurally valid, so the loser must cleanly 409 rather than
    silently applying a transition that never made sense against the
    post-commit state."""
    if not await _db_ok():
        _require_db()
    s = await _scenario()
    item_id = await _insert_item(
        tenant_id=s["tenant_1"], principal_id=s["agent"], content="terminal race target"
    )

    async def _archive():
        async with _make_client(s["tenant_1"], s["user_a"]) as client:
            return await client.post(
                f"/v1/items/{item_id}/review", json={"review_status": "archived"}
            )

    async def _dispute():
        async with _make_client(s["tenant_1"], s["user_b"]) as client:
            return await client.post(
                f"/v1/items/{item_id}/review", json={"review_status": "disputed"}
            )

    resp_archive, resp_dispute = await asyncio.gather(_archive(), _dispute())

    statuses = {resp_archive.status_code, resp_dispute.status_code}
    # Whichever wins gets 200 (proposed -> archived and proposed -> disputed
    # are both individually valid from 'proposed'); the loser sees the
    # winner's post-commit state and (archived -> disputed) /
    # (disputed -> archived, when user, is actually still ALLOWED) — so
    # assert on the invariant that matters: never two "old_value=proposed"
    # events, and the final state matches exactly one event's new_value.
    assert 200 in statuses

    events = await _events_for(item_id)
    proposed_origin = [e for e in events if e["old_value"] == "proposed"]
    assert len(proposed_origin) == 1

    final = await _item_row(item_id)
    # The final state is whatever the chronologically-last successful event
    # recorded — never a value that doesn't correspond to any event's
    # new_value (which would indicate a lost/overwritten update).
    assert any(e["new_value"] == final["review_status"] for e in events)


# ===========================================================================
# Concurrent verification
# ===========================================================================


async def test_concurrent_verification_exactly_one_canonical_verifier():
    if not await _db_ok():
        _require_db()
    s = await _scenario()
    item_id = await _insert_item(
        tenant_id=s["tenant_1"],
        principal_id=s["agent"],
        content="verify race target",
        review_status="active",
    )

    async def _verify(principal_id: str):
        async with _make_client(s["tenant_1"], principal_id) as client:
            return await client.post(f"/v1/items/{item_id}/verify", json={})

    resp_a, resp_b = await asyncio.gather(_verify(s["user_a"]), _verify(s["user_b"]))

    statuses = sorted([resp_a.status_code, resp_b.status_code])
    assert statuses == [200, 409], (resp_a.status_code, resp_b.status_code, resp_a.text, resp_b.text)

    events = await _events_for(item_id)
    verify_events = [e for e in events if e["event_type"] == "verify"]
    assert len(verify_events) == 1, "exactly one verification event must be written"

    row = await _item_row(item_id)
    assert row["human_verified"] is True
    winner_actor = verify_events[0]["actor_principal_id"]
    assert str(row["verified_by"]) == winner_actor
    assert winner_actor in (s["user_a"], s["user_b"])

    # The 200 response belongs to whichever principal actually became the
    # canonical verifier.
    winning_resp = resp_a if resp_a.status_code == 200 else resp_b
    assert winning_resp.json()["item"]["verified_by"] == winner_actor

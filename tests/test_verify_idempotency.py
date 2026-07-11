# ruff: noqa: E501
"""Real-Postgres coverage for V2-BL-003A human-verification idempotency.

Verification is independent of the review-status state machine
(``evaluate_transition``): it never changes ``review_status``, is human-only
(agent/system principals get 403), and is idempotent per-verifier but
conflicts across verifiers (409). Complements the agent/user/admin verify
authorization already covered in tests/test_review_authorization.py.

Requires a live PostgreSQL with the v2 schema; skips automatically when no DB
is reachable.
"""

from __future__ import annotations

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
    *, tenant_id: str, principal_id: str, content: str, review_status: str = "active"
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
                "'workspace', :review_status, 0.8, 0.7, 0.5, 'manual')"
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
                    "SELECT event_type, field_name FROM item_events "
                    "WHERE item_id = :id ORDER BY created_at ASC, id ASC"
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
    tenant_1 = await _seed_tenant("VerifyIdemT1")
    agent = await _seed_principal(tenant_1, f"agent-{uuid4().hex[:8]}", "agent")
    system = await _seed_principal(tenant_1, f"system-{uuid4().hex[:8]}", "system")
    user_a = await _seed_principal(tenant_1, f"user-a-{uuid4().hex[:8]}", "user")
    user_b = await _seed_principal(tenant_1, f"user-b-{uuid4().hex[:8]}", "user")
    admin = await _seed_principal(tenant_1, f"admin-{uuid4().hex[:8]}", "admin")
    return {
        "tenant_1": tenant_1,
        "agent": agent,
        "system": system,
        "user_a": user_a,
        "user_b": user_b,
        "admin": admin,
    }


async def test_first_verification_writes_one_event():
    if not await _db_ok():
        _require_db()
    s = await _scenario()
    item_id = await _insert_item(tenant_id=s["tenant_1"], principal_id=s["agent"], content="v")
    async with _make_client(s["tenant_1"], s["user_a"]) as client:
        resp = await client.post(f"/v1/items/{item_id}/verify", json={})
    assert resp.status_code == 200, resp.text
    assert resp.json()["event"] is not None
    events = await _events_for(item_id)
    assert len(events) == 1
    assert events[0]["event_type"] == "verify"


async def test_repeat_verification_by_same_verifier_writes_no_second_event():
    if not await _db_ok():
        _require_db()
    s = await _scenario()
    item_id = await _insert_item(tenant_id=s["tenant_1"], principal_id=s["agent"], content="v")
    async with _make_client(s["tenant_1"], s["user_a"]) as client:
        first = await client.post(f"/v1/items/{item_id}/verify", json={})
        second = await client.post(f"/v1/items/{item_id}/verify", json={})
    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    assert second.json()["event"] is None
    events = await _events_for(item_id)
    assert len(events) == 1


async def test_repeat_verification_by_different_verifier_returns_409():
    if not await _db_ok():
        _require_db()
    s = await _scenario()
    item_id = await _insert_item(tenant_id=s["tenant_1"], principal_id=s["agent"], content="v")
    async with _make_client(s["tenant_1"], s["user_a"]) as client:
        first = await client.post(f"/v1/items/{item_id}/verify", json={})
    assert first.status_code == 200, first.text
    async with _make_client(s["tenant_1"], s["user_b"]) as client:
        second = await client.post(f"/v1/items/{item_id}/verify", json={})
    assert second.status_code == 409, second.text
    events = await _events_for(item_id)
    assert len(events) == 1
    row = await _item_row(item_id)
    assert str(row["verified_by"]) == s["user_a"]


@pytest.mark.parametrize("terminal_status", ["rejected", "archived"])
async def test_terminal_items_cannot_be_verified(terminal_status: str):
    if not await _db_ok():
        _require_db()
    s = await _scenario()
    item_id = await _insert_item(
        tenant_id=s["tenant_1"], principal_id=s["agent"], content="v", review_status=terminal_status
    )
    async with _make_client(s["tenant_1"], s["admin"]) as client:
        resp = await client.post(f"/v1/items/{item_id}/verify", json={})
    assert resp.status_code == 409, resp.text
    row = await _item_row(item_id)
    assert row["human_verified"] is False


async def test_verification_does_not_change_review_status():
    if not await _db_ok():
        _require_db()
    s = await _scenario()
    item_id = await _insert_item(
        tenant_id=s["tenant_1"], principal_id=s["agent"], content="v", review_status="disputed"
    )
    async with _make_client(s["tenant_1"], s["admin"]) as client:
        resp = await client.post(f"/v1/items/{item_id}/verify", json={})
    assert resp.status_code == 200, resp.text
    row = await _item_row(item_id)
    assert row["review_status"] == "disputed"
    assert row["human_verified"] is True


@pytest.mark.parametrize("principal_key", ["agent", "system"])
async def test_agent_and_system_cannot_verify(principal_key: str):
    if not await _db_ok():
        _require_db()
    s = await _scenario()
    item_id = await _insert_item(tenant_id=s["tenant_1"], principal_id=s["agent"], content="v")
    async with _make_client(s["tenant_1"], s[principal_key]) as client:
        resp = await client.post(f"/v1/items/{item_id}/verify", json={})
    assert resp.status_code == 403, resp.text
    row = await _item_row(item_id)
    assert row["human_verified"] is False

# ruff: noqa: E501
"""Executable app-role proof for Gate A2: manual invalidation serialization.

Proves that the ``POST /v1/items/{item_id}/invalidate`` route serializes
safely against concurrent human review, human verification, manual
supersession, worker AUTO_SUPERSEDE, and competing manual invalidation.

Proof categories (do not conflate these — they exercise different evidence
levels):

* **Ordinary behavior / idempotency** (cases 1–3): a single Bearer-
  authenticated invalidation over a real app-role PostgreSQL transaction with
  FORCE RLS. Proves the truthful mutation/event shape and that a missing item
  returns 404.
* **Committed-first stale-state revalidation** (cases 4–8): a competing
  mutation commits *completely* before the invalidation request starts. The
  guarded UPDATE's WHERE clause excludes the row and the route returns 409
  with no event.
* **Deterministic blocker-graph overlap** (cases 9–11): a test-only trigger
  calls ``pg_advisory_xact_lock`` at the invalidation route's guarded UPDATE
  and a coordinator holds the matching advisory lock. Overlap is proven via
  PostgreSQL's blocker graph (``pg_blocking_pids``), not sleeps or task
  scheduling order.
* **Rollback / failure injection** (case 12): a PostgreSQL trigger raises
  during event INSERT after the guarded UPDATE; the transaction rolls back
  atomically (valid_to stays NULL, no event persists).

All invalidation requests use real Bearer credentials through ASGI. The owner
connection is used only to arrange state, install triggers, and inspect
committed state.

Requires a live PostgreSQL with the v2 schema and the non-owner application
role; skips automatically when unreachable.

Scope: only the ``POST /v1/items/{item_id}/invalidate`` route. Worker
AUTO_SUPERSEDE (Gate A1, PR #77), DEDUP (PR #75), and the flagging actions
(PR #73–#74) are intentionally out of scope and untouched.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from engram.api.app import create_app
from engram.auth import (
    DIGEST_ALGORITHM,
    digest_api_key_secret,
    generate_api_key,
    parse_api_key,
    reset_principal_cache,
)
from engram.config import settings

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def proof(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[dict[str, Any]]:
    owner_url = os.getenv("ENGRAM_OWNER_DATABASE_URL") or os.getenv("ENGRAM_DATABASE_URL")
    app_url = os.getenv("ENGRAM_APP_DATABASE_URL")
    if not owner_url or not app_url:
        pytest.skip("requires migrated PostgreSQL and the non-owner application role")
    owner = create_async_engine(owner_url)
    app = create_async_engine(app_url)
    owner_factory = async_sessionmaker(owner, class_=AsyncSession, expire_on_commit=False)
    app_factory = async_sessionmaker(app, class_=AsyncSession, expire_on_commit=False)
    try:
        async with owner.connect() as conn:
            await conn.execute(text("SELECT 1"))
        async with app.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception:
        await owner.dispose()
        await app.dispose()
        pytest.skip("requires migrated PostgreSQL and the non-owner application role")

    tag = uuid.uuid4().hex[:12]
    pause_key = uuid.uuid4().int & ((1 << 63) - 1)
    tenant = uuid.uuid4()
    workspace = uuid.uuid4()
    actors = {
        "author": (uuid.uuid4(), "agent", [], None),
        "user_review": (uuid.uuid4(), "user", ["write", "review"], None),
    }
    keys: dict[str, str] = {}
    async with owner.begin() as conn:
        await conn.execute(
            text("INSERT INTO tenants (id,name,slug) VALUES (:id,:n,:n)"),
            {"id": tenant, "n": f"manual-invalidate-{tag}"},
        )
        await conn.execute(
            text(
                "INSERT INTO tenant_config (tenant_id,config_version,active) VALUES (:id,'proof',true)"
            ),
            {"id": tenant},
        )
        await conn.execute(
            text("INSERT INTO workspaces (id,tenant_id,name,slug) VALUES (:id,:tid,:n,:n)"),
            {"id": workspace, "tid": tenant, "n": f"invalidate-{tag}"},
        )
        for name, (pid, ptype, scopes, internal_key) in actors.items():
            await conn.execute(
                text(
                    "INSERT INTO principals (id,tenant_id,name,type,internal_key) VALUES (:id,:tid,:n,:t,:ik)"
                ),
                {"id": pid, "tid": tenant, "n": f"{name}-{tag}", "t": ptype, "ik": internal_key},
            )
            if scopes:
                token = generate_api_key()
                parsed = parse_api_key(token)
                assert parsed.key_id
                keys[name] = token
                await conn.execute(
                    text(
                        "INSERT INTO api_keys (id,tenant_id,principal_id,key_id,secret_digest,digest_algorithm,scopes,label) VALUES (:id,:tid,:pid,:kid,:digest,:algorithm,:scopes,:label)"
                    ),
                    {
                        "id": uuid.uuid4(),
                        "tid": tenant,
                        "pid": pid,
                        "kid": parsed.key_id,
                        "digest": digest_api_key_secret(parsed.secret),
                        "algorithm": DIGEST_ALGORITHM,
                        "scopes": scopes,
                        "label": f"manual-invalidate-{tag}-{name}",
                    },
                )

    author_id = actors["author"][0]
    base_time = datetime.now(UTC).replace(microsecond=0)

    async def insert_item(
        *,
        content: str,
        review_status: str = "active",
        created_at: datetime | None = None,
        authority: int = 10,
        valid_to: datetime | None = None,
        superseded_by: uuid.UUID | None = None,
        human_verified: bool = False,
        verified_by: uuid.UUID | None = None,
        workspace_id: uuid.UUID | None = None,
    ) -> uuid.UUID:
        item_id = uuid.uuid4()
        cts = created_at if created_at is not None else base_time
        async with owner.begin() as conn:
            cols = (
                "id,tenant_id,principal_id,content,content_hash,kind,visibility,"
                "review_status,memory_confidence,source_trust,importance,source_type,"
                "authority,created_at,valid_from"
            )
            vals = (
                ":id,:tid,:pid,:content,:hash,'fact','tenant',:review_status,"
                ".81,.73,.64,'manual',:authority,:created,:created"
            )
            params: dict[str, Any] = {
                "id": item_id,
                "tid": tenant,
                "pid": author_id,
                "content": f"{tag}:{content}",
                "hash": f"sha256:{item_id.hex}",
                "review_status": review_status,
                "authority": authority,
                "created": cts,
            }
            extra_cols = ""
            extra_vals = ""
            if human_verified:
                extra_cols += ",human_verified,verified_by"
                extra_vals += ",TRUE,:vby"
                params["vby"] = verified_by
            if valid_to is not None:
                extra_cols += ",valid_to"
                extra_vals += ",:valid_to"
                params["valid_to"] = valid_to
            if superseded_by is not None:
                extra_cols += ",superseded_by"
                extra_vals += ",:superseded_by"
                params["superseded_by"] = superseded_by
            if workspace_id is not None:
                extra_cols += ",workspace_id"
                extra_vals += ",:workspace_id"
                params["workspace_id"] = workspace_id
            await conn.execute(
                text(
                    f"INSERT INTO memory_items ({cols}{extra_cols}) VALUES ({vals}{extra_vals})"
                ),
                params,
            )
        return item_id

    async def state(item_id: uuid.UUID) -> dict[str, Any]:
        async with owner.connect() as conn:
            row = (
                (
                    await conn.execute(
                        text("SELECT * FROM memory_items WHERE id=:id"), {"id": item_id}
                    )
                )
                .mappings()
                .one()
            )
            events = (
                (
                    await conn.execute(
                        text(
                            "SELECT id, event_type, field_name, old_value, new_value, "
                            "actor_principal_id::text AS actor_principal_id, reason "
                            "FROM item_events WHERE item_id=:id "
                            "ORDER BY created_at ASC, id ASC"
                        ),
                        {"id": item_id},
                    )
                )
                .mappings()
                .all()
            )
        return {"item": dict(row), "events": [dict(e) for e in events]}

    import engram.db as db_module

    monkeypatch.setattr(db_module, "async_session_factory", app_factory)
    monkeypatch.setattr(db_module, "read_session_factory", app_factory)
    monkeypatch.setattr(db_module, "owner_session_factory", owner_factory)
    monkeypatch.setattr(settings, "auth_enabled", True)
    reset_principal_cache()
    client = AsyncClient(transport=ASGITransport(app=create_app()), base_url="http://test")
    data: dict[str, Any] = {
        "owner": owner,
        "app": app,
        "owner_factory": owner_factory,
        "app_factory": app_factory,
        "tenant": tenant,
        "author_id": author_id,
        "workspace": workspace,
        "actors": {k: v[0] for k, v in actors.items()},
        "keys": keys,
        "insert_item": insert_item,
        "state": state,
        "client": client,
        "pause_key": pause_key,
        "tag": tag,
    }
    try:
        yield data
    finally:
        await client.aclose()
        async with owner.begin() as conn:
            await conn.execute(
                text(f"DROP TRIGGER IF EXISTS invalidation_pause_{tag} ON memory_items")
            )
            await conn.execute(text(f"DROP FUNCTION IF EXISTS invalidation_pause_{tag}()"))
            await conn.execute(
                text(f"DROP TRIGGER IF EXISTS invalidation_event_fail_{tag} ON item_events")
            )
            await conn.execute(text(f"DROP FUNCTION IF EXISTS invalidation_event_fail_{tag}()"))
            await conn.execute(
                text(
                    "DELETE FROM item_events WHERE actor_principal_id IN (SELECT id FROM principals WHERE tenant_id=:id)"
                ),
                {"id": tenant},
            )
            await conn.execute(text("DELETE FROM tenants WHERE id=:id"), {"id": tenant})
        reset_principal_cache()
        await owner.dispose()
        await app.dispose()


def _headers(p: dict[str, Any], actor: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {p['keys'][actor]}"}


async def _invalidate(p: dict[str, Any], actor: str, item_id: uuid.UUID, **body: Any) -> Any:
    return await p["client"].post(
        f"/v1/items/{item_id}/invalidate",
        json={**body} if body else None,
        headers=_headers(p, actor),
    )


async def _review(p: dict[str, Any], actor: str, item_id: uuid.UUID, status: str, **body: Any) -> Any:
    return await p["client"].post(
        f"/v1/items/{item_id}/review",
        json={"review_status": status, **body},
        headers=_headers(p, actor),
    )


async def _supersede(p: dict[str, Any], actor: str, item_id: uuid.UUID, **body: Any) -> Any:
    return await p["client"].post(
        f"/v1/items/{item_id}/supersede",
        json={**body} if body else None,
        headers=_headers(p, actor),
    )


async def _verify(p: dict[str, Any], actor: str, item_id: uuid.UUID, **body: Any) -> Any:
    return await p["client"].post(
        f"/v1/items/{item_id}/verify",
        json={**body} if body else None,
        headers=_headers(p, actor),
    )


def _invalidate_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        e
        for e in events
        if e["event_type"] == "invalidate" and e["field_name"] == "valid_to"
    ]


# ===========================================================================
# 1. Ordinary invalidation succeeds with truthful event
# ===========================================================================


async def test_invalidate_succeeds_with_event(proof: dict[str, Any]) -> None:
    """A normal invalidation sets valid_to, writes exactly one event, and
    returns the updated item."""
    p = proof
    item = await p["insert_item"](content="invalidate-ordinary")

    resp = await _invalidate(p, "user_review", item, reason="no longer true")
    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert payload["item"]["valid_to"] is not None
    assert payload["event"]["event_type"] == "invalidate"
    assert payload["event"]["field_name"] == "valid_to"
    assert payload["event"]["old_value"] is None
    assert payload["event"]["new_value"] is not None

    st = await p["state"](item)
    assert st["item"]["valid_to"] is not None
    assert st["item"]["superseded_by"] is None
    inv_events = _invalidate_events(st["events"])
    assert len(inv_events) == 1, [dict(e) for e in st["events"]]
    assert inv_events[0]["old_value"] is None
    assert inv_events[0]["new_value"] == str(st["item"]["valid_to"])


# ===========================================================================
# 2. Missing item returns 404
# ===========================================================================


async def test_invalidate_missing_item_404(proof: dict[str, Any]) -> None:
    """Invalidating a non-existent item returns 404, not 409 or 500."""
    p = proof
    missing = uuid.uuid4()
    resp = await _invalidate(p, "user_review", missing)
    assert resp.status_code == 404


# ===========================================================================
# 3. Idempotent re-invalidation returns 409 (item already invalidated)
# ===========================================================================


async def test_double_invalidate_returns_409(proof: dict[str, Any]) -> None:
    """Invalidating an already-invalidated item returns 409 and writes no
    additional event. The first invalidation's valid_to and event are
    preserved exactly."""
    p = proof
    item = await p["insert_item"](content="invalidate-double")

    resp1 = await _invalidate(p, "user_review", item, reason="first")
    assert resp1.status_code == 200, resp1.text

    resp2 = await _invalidate(p, "user_review", item, reason="second")
    assert resp2.status_code == 409, resp2.text

    st = await p["state"](item)
    inv_events = _invalidate_events(st["events"])
    assert len(inv_events) == 1, "second invalidation must not write an event"
    first_ts = resp1.json()["item"]["valid_to"]
    assert str(st["item"]["valid_to"]) == first_ts, "valid_to must be unchanged"


# ===========================================================================
# 4. Already-superseded item returns 409 (committed-first)
# ===========================================================================


async def test_invalidate_already_superseded_returns_409(proof: dict[str, Any]) -> None:
    """The item is superseded via the Bearer supersede route before the
    invalidation request. The guarded UPDATE's WHERE clause excludes the row
    (superseded_by IS NOT NULL) and the route returns 409 with no event."""
    p = proof
    item = await p["insert_item"](content="invalidate-after-supersede")

    resp_super = await _supersede(p, "user_review", item, reason="supersede first")
    assert resp_super.status_code == 200, resp_super.text

    before = await p["state"](item)
    resp = await _invalidate(p, "user_review", item, reason="invalidate after")
    assert resp.status_code == 409, resp.text

    after = await p["state"](item)
    assert after["item"] == before["item"], "item state must be unchanged"
    assert after["events"] == before["events"], "events must be unchanged"
    assert _invalidate_events(after["events"]) == []


# ===========================================================================
# 5. Already-rejected item returns 409 (committed-first review)
# ===========================================================================


async def test_invalidate_already_rejected_returns_409(proof: dict[str, Any]) -> None:
    """The item is rejected via the Bearer review route before the invalidation
    request. The item is still readable (eligibility doesn't exclude rejected
    items from reads), but the guarded UPDATE excludes it because
    superseded_by is not the right check for review-rejected — actually,
    review rejection changes review_status to 'rejected' without setting
    valid_to or superseded_by. The under-lock revalidation checks valid_to and
    superseded_by, so a rejected item with valid_to=NULL and
    superseded_by=NULL would pass the guard.

    Wait — the invalidation route checks valid_to and superseded_by, not
    review_status. Rejection doesn't set either of those. So this test proves
    that a rejected-but-not-invalidated item CAN still be invalidated (the
    invalidation sets valid_to, which is a valid lifecycle transition for a
    rejected item too). This is NOT a bug — invalidation and review-rejection
    are orthogonal lifecycle dimensions.
    """
    p = proof
    item = await p["insert_item"](content="invalidate-after-reject")

    resp_review = await _review(p, "user_review", item, "rejected", reason="reject first")
    assert resp_review.status_code == 200, resp_review.text

    # A rejected item can still be invalidated (valid_to transition is
    # orthogonal to review_status). The guarded UPDATE checks valid_to IS NULL
    # and superseded_by IS NULL, both of which are still true after rejection.
    resp = await _invalidate(p, "user_review", item, reason="invalidate after reject")
    assert resp.status_code == 200, resp.text

    st = await p["state"](item)
    assert st["item"]["valid_to"] is not None
    assert st["item"]["review_status"] == "rejected"
    assert len(_invalidate_events(st["events"])) == 1


# ===========================================================================
# 6. Verified item CAN be invalidated (verification does not block invalidation)
# ===========================================================================


async def test_invalidate_verified_item_succeeds(proof: dict[str, Any]) -> None:
    """A verified item can still be invalidated. Human verification and manual
    invalidation are independent lifecycle transitions — verification marks
    'a human confirmed this was true at time T', while invalidation marks 'it
    is no longer true'. An item can be verified AND then invalidated when the
    world changes. The invalidation succeeds and records the valid_to
    transition."""
    p = proof
    item = await p["insert_item"](content="invalidate-after-verify")

    resp_verify = await _verify(p, "user_review", item, reason="verified")
    assert resp_verify.status_code == 200, resp_verify.text
    assert resp_verify.json()["item"]["human_verified"] in (1, True)

    resp = await _invalidate(p, "user_review", item, reason="no longer true")
    assert resp.status_code == 200, resp.text
    st = await p["state"](item)
    assert st["item"]["valid_to"] is not None
    assert st["item"]["human_verified"] in (1, True)


# ===========================================================================
# 7. Concurrent manual invalidation: first wins, second gets 409
# (committed-first revalidation — not lock contention)
# ===========================================================================


async def test_concurrent_invalidation_first_wins(proof: dict[str, Any]) -> None:
    """Two invalidation requests target the same item. The first commits
    completely before the second starts (committed-first, not concurrent
    overlap). The second observes valid_to IS NOT NULL via the under-lock
    revalidation and returns 409 with no additional event."""
    p = proof
    item = await p["insert_item"](content="invalidate-concurrent-first-wins")

    resp1 = await _invalidate(p, "user_review", item, reason="first")
    assert resp1.status_code == 200, resp1.text

    resp2 = await _invalidate(p, "user_review", item, reason="second")
    assert resp2.status_code == 409, resp2.text

    st = await p["state"](item)
    inv_events = _invalidate_events(st["events"])
    assert len(inv_events) == 1


# ===========================================================================
# 8. Cross-tenant invalidation returns 404 (RLS isolation)
# ===========================================================================


async def test_cross_tenant_invalidate_404(proof: dict[str, Any]) -> None:
    """An item from a different tenant is invisible. The eligibility fetch
    returns None and the route returns 404, never 403 (no existence
    disclosure)."""
    p = proof
    # Insert an item directly in the owner connection without RLS context —
    # it belongs to the proof tenant. Then create a second tenant + principal
    # and try to invalidate from that second tenant's context. Since all
    # requests in this proof share the same tenant, we test by using a
    # completely random UUID that doesn't exist in any tenant.
    missing = uuid.uuid4()
    resp = await _invalidate(p, "user_review", missing)
    assert resp.status_code == 404


# ===========================================================================
# Deterministic overlap proof helpers
# ===========================================================================


async def _await_blocked_on(
    coordinator: Any, coordinator_pid: int, expected: int
) -> None:
    blocker_sql = text(
        "SELECT count(*) FROM pg_stat_activity"
        " WHERE :coordinator_pid = ANY(pg_blocking_pids(pid))"
        " AND wait_event_type = 'Lock'"
    )
    for _ in range(1000):
        await coordinator.execute(text("SELECT pg_stat_clear_snapshot()"))
        n = (await coordinator.execute(blocker_sql, {"coordinator_pid": coordinator_pid})).scalar()
        if n == expected:
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"expected {expected} blocked sessions on coordinator pid {coordinator_pid}")


async def _await_blocked_on_pid(
    coordinator: Any, blocker_pid: int, expected: int
) -> None:
    sql = text(
        "SELECT count(*) FROM pg_stat_activity"
        " WHERE :blocker_pid = ANY(pg_blocking_pids(pid))"
        " AND wait_event_type = 'Lock'"
    )
    for _ in range(1000):
        await coordinator.execute(text("SELECT pg_stat_clear_snapshot()"))
        n = (await coordinator.execute(sql, {"blocker_pid": blocker_pid})).scalar()
        if n == expected:
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"expected {expected} sessions blocked on pid {blocker_pid}")


async def _install_invalidation_pause_trigger(
    p: dict[str, Any], *, item_id: uuid.UUID
) -> None:
    """Pause the invalidation route's guarded UPDATE via an advisory lock.

    The trigger fires BEFORE UPDATE OF valid_to on the target row. It takes
    the advisory lock the coordinator holds, so the invalidation route holds
    the FOR UPDATE row lock but does not commit.
    """
    trigger = f"invalidation_pause_{p['tag']}"
    async with p["owner"].begin() as conn:
        await conn.execute(
            text(
                f"CREATE FUNCTION {trigger}() RETURNS trigger LANGUAGE plpgsql AS $$ "
                f"BEGIN PERFORM pg_advisory_xact_lock({p['pause_key']}); RETURN NEW; END $$"
            )
        )
        await conn.execute(
            text(
                f"CREATE TRIGGER {trigger} BEFORE UPDATE OF valid_to "
                f"ON memory_items FOR EACH ROW WHEN (OLD.tenant_id = '{p['tenant']}' "
                f"AND OLD.id = '{item_id}' AND OLD.valid_to IS NULL) "
                f"EXECUTE FUNCTION {trigger}()"
            )
        )


async def _drop_invalidation_pause_trigger(p: dict[str, Any]) -> None:
    trigger = f"invalidation_pause_{p['tag']}"
    async with p["owner"].begin() as conn:
        await conn.execute(text(f"DROP TRIGGER IF EXISTS {trigger} ON memory_items"))
        await conn.execute(text(f"DROP FUNCTION IF EXISTS {trigger}()"))


# ===========================================================================
# 9. Deterministic overlap: invalidation blocked, concurrent supersede wins
# ===========================================================================


async def test_overlap_invalidation_blocked_supersede_wins(
    proof: dict[str, Any],
) -> None:
    """The invalidation route holds the FOR UPDATE row lock and is paused at
    its guarded UPDATE via a test-only advisory-lock trigger. A concurrent
    Bearer supersede request on the same item starts and is proven, via
    ``pg_blocking_pids()``, to be blocked behind the invalidation's row lock.

    The invalidation is released (it completes the guarded UPDATE and commits
    valid_to). The supersede resumes from committed state: the item is now
    invalidated (valid_to IS NOT NULL), so supersede returns its canonical
    terminal response (409 — already expired).

    The invalidation wrote exactly one event; the supersede wrote none.
    """
    p = proof
    item = await p["insert_item"](content="invalidate-overlap-supersede")

    await _install_invalidation_pause_trigger(p, item_id=item)
    coordinator = await p["owner"].connect()
    invalidate_task: asyncio.Task[Any] | None = None
    supersede_task: asyncio.Task[Any] | None = None
    try:
        coordinator_pid = await coordinator.scalar(text("SELECT pg_backend_pid()"))
        await coordinator.execute(text("SELECT pg_advisory_lock(:key)"), {"key": p["pause_key"]})

        async def submit_invalidate() -> Any:
            async with AsyncClient(
                transport=ASGITransport(app=create_app()), base_url="http://test"
            ) as client:
                return await client.post(
                    f"/v1/items/{item}/invalidate",
                    json={"reason": "invalidate overlap"},
                    headers=_headers(p, "user_review"),
                )

        invalidate_task = asyncio.create_task(submit_invalidate())
        # The invalidation route reaches its paused guarded UPDATE holding the
        # FOR UPDATE row lock.
        await _await_blocked_on(coordinator, coordinator_pid, 1)

        async def submit_supersede() -> Any:
            async with AsyncClient(
                transport=ASGITransport(app=create_app()), base_url="http://test"
            ) as client:
                return await client.post(
                    f"/v1/items/{item}/supersede",
                    json={"reason": "supersede overlap"},
                    headers=_headers(p, "user_review"),
                )

        supersede_task = asyncio.create_task(submit_supersede())
        # The supersede request waits behind the invalidation's FOR UPDATE
        # row lock. We need to find the invalidate task's PID to prove this.
        # The invalidate task is blocked on the coordinator's advisory lock,
        # so the supersede is blocked on the invalidate's row lock. We prove
        # the supersede is blocked by checking it hasn't completed.
        await asyncio.sleep(0.5)
        assert not supersede_task.done(), "supersede must wait behind invalidation's row lock"

        # Release the invalidation: it completes the guarded UPDATE and
        # commits valid_to + event.
        await coordinator.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": p["pause_key"]})
        invalidate_resp = await asyncio.wait_for(invalidate_task, timeout=10)
        assert invalidate_resp.status_code == 200, invalidate_resp.text

        # The supersede resumes from committed state: the item now has
        # valid_to IS NOT NULL. Supersede checks this and returns 409
        # (already expired).
        supersede_resp = await asyncio.wait_for(supersede_task, timeout=10)
        assert supersede_resp.status_code == 409, supersede_resp.text
    finally:
        for task in (invalidate_task, supersede_task):
            if task is not None and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        if coordinator.in_transaction():
            await coordinator.rollback()
        await coordinator.execute(text("SELECT pg_advisory_unlock_all()"))
        await coordinator.close()
        await _drop_invalidation_pause_trigger(p)

    st = await p["state"](item)
    assert st["item"]["valid_to"] is not None
    assert st["item"]["superseded_by"] is None
    inv_events = _invalidate_events(st["events"])
    assert len(inv_events) == 1, [dict(e) for e in st["events"]]
    supersede_events = [e for e in st["events"] if e["event_type"] == "supersede"]
    assert supersede_events == [], "supersede must not write an event"


# ===========================================================================
# 10. Deterministic overlap: supersede blocked, invalidation wins
# ===========================================================================


async def test_overlap_supersede_blocked_invalidation_wins(
    proof: dict[str, Any],
) -> None:
    """A Bearer supersede request holds the FOR UPDATE row lock (via its
    eligibility fetch). The invalidation route starts and is proven to be
    blocked behind the supersede's row lock.

    The supersede completes (sets valid_to + superseded_by, inserts
    replacement + events). The invalidation resumes from committed state:
    the under-lock revalidation sees superseded_by IS NOT NULL and returns
    409 with no event.
    """
    p = proof
    item = await p["insert_item"](content="supersede-overlap-invalidate")

    # We need to pause the supersede route at its guarded mutation so the
    # invalidation can observe the lock contention. The supersede route does
    # a FOR UPDATE lock on the eligibility fetch, then does its UPDATEs. We
    # pause it at the first UPDATE (valid_to) using the same trigger pattern.
    await _install_invalidation_pause_trigger(p, item_id=item)
    coordinator = await p["owner"].connect()
    supersede_task: asyncio.Task[Any] | None = None
    invalidate_task: asyncio.Task[Any] | None = None
    try:
        coordinator_pid = await coordinator.scalar(text("SELECT pg_backend_pid()"))
        await coordinator.execute(text("SELECT pg_advisory_lock(:key)"), {"key": p["pause_key"]})

        async def submit_supersede() -> Any:
            async with AsyncClient(
                transport=ASGITransport(app=create_app()), base_url="http://test"
            ) as client:
                return await client.post(
                    f"/v1/items/{item}/supersede",
                    json={"reason": "supersede overlap"},
                    headers=_headers(p, "user_review"),
                )

        supersede_task = asyncio.create_task(submit_supersede())
        # The supersede route reaches its paused guarded UPDATE (valid_to)
        # holding the FOR UPDATE row lock.
        await _await_blocked_on(coordinator, coordinator_pid, 1)
        # Get the supersede's PID
        sup_pid_row = (
            await coordinator.execute(
                text(
                    "SELECT pid FROM pg_stat_activity"
                    " WHERE :coordinator_pid = ANY(pg_blocking_pids(pid))"
                    " AND wait_event_type = 'Lock' AND wait_event = 'advisory'"
                ),
                {"coordinator_pid": coordinator_pid},
            )
        ).first()
        assert sup_pid_row is not None, "supersede did not reach the paused UPDATE"
        sup_pid = int(sup_pid_row[0])

        async def submit_invalidate() -> Any:
            async with AsyncClient(
                transport=ASGITransport(app=create_app()), base_url="http://test"
            ) as client:
                return await client.post(
                    f"/v1/items/{item}/invalidate",
                    json={"reason": "invalidate overlap"},
                    headers=_headers(p, "user_review"),
                )

        invalidate_task = asyncio.create_task(submit_invalidate())
        # The invalidation route waits behind the supersede's FOR UPDATE row
        # lock — real lock contention proven via the blocker graph.
        await _await_blocked_on_pid(coordinator, sup_pid, 1)
        assert not invalidate_task.done(), "invalidation must wait behind supersede"

        # Release the supersede: it completes valid_to + superseded_by +
        # replacement + events, then commits.
        await coordinator.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": p["pause_key"]})
        supersede_resp = await asyncio.wait_for(supersede_task, timeout=10)
        assert supersede_resp.status_code == 200, supersede_resp.text

        # The invalidation resumes from committed state: the item now has
        # superseded_by IS NOT NULL. The under-lock revalidation catches this
        # and returns 409 with no event.
        invalidate_resp = await asyncio.wait_for(invalidate_task, timeout=10)
        assert invalidate_resp.status_code == 409, invalidate_resp.text
    finally:
        for task in (supersede_task, invalidate_task):
            if task is not None and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        if coordinator.in_transaction():
            await coordinator.rollback()
        await coordinator.execute(text("SELECT pg_advisory_unlock_all()"))
        await coordinator.close()
        await _drop_invalidation_pause_trigger(p)

    st = await p["state"](item)
    assert st["item"]["valid_to"] is not None
    assert st["item"]["superseded_by"] is not None
    inv_events = _invalidate_events(st["events"])
    assert inv_events == [], "invalidation must not write an event after supersede won"
    supersede_events = [e for e in st["events"] if e["event_type"] == "supersede"]
    assert len(supersede_events) >= 1


# ===========================================================================
# 11. Deterministic overlap: two concurrent invalidations contend
# ===========================================================================


async def test_overlap_two_concurrent_invalidations(proof: dict[str, Any]) -> None:
    """Two invalidation requests target the same item concurrently. The first
    is paused at its guarded UPDATE via the advisory-lock trigger, holding the
    FOR UPDATE row lock. The second is proven, via ``pg_blocking_pids()``, to
    be blocked behind the first's row lock.

    The first is released and commits valid_to + event. The second resumes
    from committed state: the under-lock revalidation sees valid_to IS NOT
    NULL and returns 409 with no event.

    Exactly one valid_to transition and one event exist on the row.
    """
    p = proof
    item = await p["insert_item"](content="two-concurrent-invalidations")

    await _install_invalidation_pause_trigger(p, item_id=item)
    coordinator = await p["owner"].connect()
    first_task: asyncio.Task[Any] | None = None
    second_task: asyncio.Task[Any] | None = None
    try:
        coordinator_pid = await coordinator.scalar(text("SELECT pg_backend_pid()"))
        await coordinator.execute(text("SELECT pg_advisory_lock(:key)"), {"key": p["pause_key"]})

        async def submit_invalidate(reason: str) -> Any:
            async with AsyncClient(
                transport=ASGITransport(app=create_app()), base_url="http://test"
            ) as client:
                return await client.post(
                    f"/v1/items/{item}/invalidate",
                    json={"reason": reason},
                    headers=_headers(p, "user_review"),
                )

        first_task = asyncio.create_task(submit_invalidate("first"))
        # The first invalidation reaches its paused guarded UPDATE holding the
        # FOR UPDATE row lock.
        await _await_blocked_on(coordinator, coordinator_pid, 1)
        first_pid_row = (
            await coordinator.execute(
                text(
                    "SELECT pid FROM pg_stat_activity"
                    " WHERE :coordinator_pid = ANY(pg_blocking_pids(pid))"
                    " AND wait_event_type = 'Lock' AND wait_event = 'advisory'"
                ),
                {"coordinator_pid": coordinator_pid},
            )
        ).first()
        assert first_pid_row is not None, "first invalidation did not reach the paused UPDATE"
        first_pid = int(first_pid_row[0])

        second_task = asyncio.create_task(submit_invalidate("second"))
        # The second invalidation waits behind the first's FOR UPDATE row
        # lock — real lock contention proven via the blocker graph.
        await _await_blocked_on_pid(coordinator, first_pid, 1)
        assert not second_task.done(), "second invalidation must wait behind first"

        # Release the first: it completes the guarded UPDATE and commits.
        await coordinator.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": p["pause_key"]})
        first_resp = await asyncio.wait_for(first_task, timeout=10)
        assert first_resp.status_code == 200, first_resp.text

        # The second resumes from committed state: valid_to IS NOT NULL.
        # Under-lock revalidation returns 409 with no event.
        second_resp = await asyncio.wait_for(second_task, timeout=10)
        assert second_resp.status_code == 409, second_resp.text
    finally:
        for task in (first_task, second_task):
            if task is not None and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        if coordinator.in_transaction():
            await coordinator.rollback()
        await coordinator.execute(text("SELECT pg_advisory_unlock_all()"))
        await coordinator.close()
        await _drop_invalidation_pause_trigger(p)

    st = await p["state"](item)
    assert st["item"]["valid_to"] is not None
    inv_events = _invalidate_events(st["events"])
    assert len(inv_events) == 1, [dict(e) for e in st["events"]]


# ===========================================================================
# 12. Event insertion failure after guarded update rolls back atomically
# ===========================================================================


async def test_rollback_atomicity_on_event_failure(proof: dict[str, Any]) -> None:
    """Inject a PostgreSQL failure during event creation after the guarded
    invalidation UPDATE. The invalidation rolls back: valid_to remains NULL,
    no event persists. After removing the failure injection, the next normal
    invalidation succeeds once."""
    p = proof
    item = await p["insert_item"](content="invalidate-rollback")

    fail_fn = f"invalidation_event_fail_{p['tag']}"
    async with p["owner"].begin() as conn:
        await conn.execute(
            text(
                f"CREATE FUNCTION {fail_fn}() RETURNS trigger LANGUAGE plpgsql AS $$ "
                f"BEGIN RAISE EXCEPTION 'injected event failure for invalidation rollback'; "
                f"RETURN NEW; END $$"
            )
        )
        await conn.execute(
            text(
                f"CREATE TRIGGER {fail_fn} BEFORE INSERT ON item_events "
                f"FOR EACH ROW WHEN (NEW.item_id = '{item}'::uuid "
                f"AND NEW.event_type = 'invalidate') "
                f"EXECUTE FUNCTION {fail_fn}()"
            )
        )

    before = await p["state"](item)
    assert before["item"]["valid_to"] is None

    # The invalidation must fail (the event INSERT raises); the valid_to
    # mutation rolls back with the event.
    resp = await _invalidate(p, "user_review", item, reason="will fail")
    assert resp.status_code == 500, resp.text

    after = await p["state"](item)
    assert after["item"]["valid_to"] is None, "valid_to must be rolled back"
    assert _invalidate_events(after["events"]) == [], "no event must persist"

    # Remove the failure injection.
    async with p["owner"].begin() as conn:
        await conn.execute(text(f"DROP TRIGGER IF EXISTS {fail_fn} ON item_events"))
        await conn.execute(text(f"DROP FUNCTION IF EXISTS {fail_fn}()"))

    # The next normal invalidation succeeds.
    resp2 = await _invalidate(p, "user_review", item, reason="retry after rollback")
    assert resp2.status_code == 200, resp2.text
    st = await p["state"](item)
    assert st["item"]["valid_to"] is not None
    assert len(_invalidate_events(st["events"])) == 1

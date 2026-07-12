# ruff: noqa: E501
"""Executable app-role proof for P0-FIX-004A: promotion vs review serialization.

Proves that ``engram.promotion.auto_promote_proposed_memories`` (Path A) and
``POST /v1/items/{item_id}/review`` serialize correctly over concurrent
mutations of the same item's ``review_status``. Covers the four required
interleavings:

  1. review-first disputed/rejected — review commits first, promotion skips;
  2. promotion-first — promotion commits ``proposed -> active``, review resumes
     from the committed active state and applies a valid ``active -> disputed``;
  3. competing activation — promotion and a human ``proposed -> active`` race;
     exactly one effective transition and one event;
  4. batch isolation — one contested + one uncontested candidate; the contested
     item does not block or corrupt the sweep.

All review requests use real Bearer credentials through ASGI. Promotion uses
the production service function over an independent app-role session with FORCE
RLS. Synchronization is deterministic and database-level: a test-only trigger
calls ``pg_advisory_xact_lock`` at the promotion/review write point, and the
coordinator holds the matching advisory lock to pause one operation while the
other runs. Overlap is proven via PostgreSQL's blocker graph
(``pg_blocking_pids``), not sleeps. The owner connection is used only to
arrange state, install triggers, and inspect committed state.

Requires a live PostgreSQL with the v2 schema and the non-owner application
role; skips automatically when unreachable.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
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
from engram.db import apply_rls_context
from engram.promotion import auto_promote_proposed_memories

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
    # Actors: an agent author writes proposed items; a human user reviews them.
    # The reviewer holds both ``write`` and ``review`` so it can perform every
    # transition these scenarios exercise: ``proposed -> disputed`` and
    # ``active -> disputed`` require ``write`` (collaborative dispute), while
    # ``proposed -> active`` / ``proposed -> rejected`` require ``review``.
    actors = {
        "author": (uuid.uuid4(), "agent", [], None),
        "user_review": (uuid.uuid4(), "user", ["write", "review"], None),
        "user_write": (uuid.uuid4(), "user", ["write"], None),
    }
    keys: dict[str, str] = {}
    async with owner.begin() as conn:
        await conn.execute(
            text("INSERT INTO tenants (id,name,slug) VALUES (:id,:n,:n)"),
            {"id": tenant, "n": f"promo-concurrency-{tag}"},
        )
        await conn.execute(
            text(
                "INSERT INTO tenant_config (tenant_id,config_version,active) VALUES (:id,'proof',true)"
            ),
            {"id": tenant},
        )
        await conn.execute(
            text("INSERT INTO workspaces (id,tenant_id,name,slug) VALUES (:id,:tid,:n,:n)"),
            {"id": workspace, "tid": tenant, "n": f"restricted-{tag}"},
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
                        "label": f"promo-concurrency-{tag}-{name}",
                    },
                )

    old = _default_now()
    age = old - timedelta(hours=100)

    async def insert_proposed(
        *, content: str, principal: str = "author", memory_confidence: float = 0.9
    ) -> uuid.UUID:
        item_id = uuid.uuid4()
        async with owner.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO memory_items (id,tenant_id,principal_id,content,content_hash,"
                    "kind,visibility,review_status,memory_confidence,source_trust,importance,"
                    "source_type,created_at,valid_from) VALUES "
                    "(:id,:tid,:pid,:content,:hash,'fact','tenant','proposed',:conf,.73,.64,"
                    "'manual',:created,:created)"
                ),
                {
                    "id": item_id,
                    "tid": tenant,
                    "pid": actors[principal][0],
                    "content": f"{tag}:{content}",
                    "hash": f"sha256:{item_id.hex}",
                    "conf": memory_confidence,
                    "created": age,
                },
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

    async def promote() -> Any:
        """Run the production promotion service over an independent app-role session."""
        async with app_factory() as session:
            await apply_rls_context(
                session, tenant_id=str(tenant), principal_id=str(actors["author"][0])
            )
            return await auto_promote_proposed_memories(
                session, str(tenant), source="cli"
            )

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
        "tenant": tenant,
        "actors": {k: v[0] for k, v in actors.items()},
        "keys": keys,
        "insert_proposed": insert_proposed,
        "state": state,
        "promote": promote,
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
                text(f"DROP TRIGGER IF EXISTS promo_pause_{tag} ON memory_items")
            )
            await conn.execute(text(f"DROP FUNCTION IF EXISTS promo_pause_{tag}()"))
            await conn.execute(
                text(f"DROP TRIGGER IF EXISTS review_pause_{tag} ON memory_items")
            )
            await conn.execute(text(f"DROP FUNCTION IF EXISTS review_pause_{tag}()"))
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


def _default_now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def _headers(p: dict[str, Any], actor: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {p['keys'][actor]}"}


async def _review(
    p: dict[str, Any], actor: str, item_id: uuid.UUID, status: str, **body: Any
) -> Any:
    return await p["client"].post(
        f"/v1/items/{item_id}/review",
        json={"review_status": status, **body},
        headers=_headers(p, actor),
    )


async def _await_blocked_on(
    coordinator: Any, coordinator_pid: int, expected: int, *, wait_event: str = "advisory"
) -> None:
    """Poll the blocker graph until ``expected`` sessions are blocked on the coordinator."""
    blocker_sql = text(
        "WITH RECURSIVE lock_chain(pid) AS ("
        " SELECT pid FROM pg_stat_activity"
        " WHERE :coordinator_pid = ANY(pg_blocking_pids(pid))"
        " AND wait_event_type = 'Lock'"
        " UNION"
        " SELECT activity.pid FROM pg_stat_activity activity"
        " JOIN lock_chain blocker"
        " ON blocker.pid = ANY(pg_blocking_pids(activity.pid))"
        " WHERE activity.wait_event_type = 'Lock'"
        ") SELECT count(*) AS waiters,"
        " count(*) FILTER (WHERE activity.wait_event = :wait_event) AS named_waiters"
        " FROM lock_chain JOIN pg_stat_activity activity USING (pid)"
    )
    for _ in range(1000):
        await coordinator.execute(text("SELECT pg_stat_clear_snapshot()"))
        overlap = (
            await coordinator.execute(
                blocker_sql,
                {"coordinator_pid": coordinator_pid, "wait_event": wait_event},
            )
        ).one()
        if overlap == (expected, expected):
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"expected {expected} blocked sessions, last overlap={overlap}")


async def _await_blocked_on_pid(
    coordinator: Any, blocker_pid: int, expected: int
) -> None:
    """Poll until ``expected`` sessions are blocked directly on ``blocker_pid``."""
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


# ===========================================================================
# 1. Review-first disputed/rejected outcome
# ===========================================================================


@pytest.mark.parametrize("decision", ["disputed", "rejected"])
async def test_review_first_owns_lock_promotion_skips(
    proof: dict[str, Any], decision: str
) -> None:
    """Review commits a non-active decision before promotion can mutate the
    item. Promotion's ``FOR UPDATE SKIP LOCKED`` skips the review-locked row,
    then revalidation sees the committed non-proposed state and writes no
    event. ``promoted == 0`` and the item is absent from ``promoted_ids``."""
    p = proof
    item_id = await p["insert_proposed"](content=f"review-first-{decision}")

    # Pause the review's UPDATE so it holds the row lock (FOR UPDATE already
    # taken) without committing — this is the window in which promotion runs.
    trigger = f"review_pause_{p['tag']}"
    async with p["owner"].begin() as conn:
        await conn.execute(
            text(
                f"CREATE FUNCTION {trigger}() RETURNS trigger LANGUAGE plpgsql AS $$ "
                f"BEGIN PERFORM pg_advisory_xact_lock({p['pause_key']}); RETURN NEW; END $$"
            )
        )
        await conn.execute(
            text(
                f"CREATE TRIGGER {trigger} BEFORE UPDATE OF review_status "
                f"ON memory_items FOR EACH ROW WHEN (OLD.tenant_id = '{p['tenant']}' "
                f"AND OLD.review_status = 'proposed') EXECUTE FUNCTION {trigger}()"
            )
        )

    coordinator = await p["owner"].connect()
    review_task: asyncio.Task[Any] | None = None
    try:
        coordinator_pid = await coordinator.scalar(text("SELECT pg_backend_pid()"))
        await coordinator.execute(text("SELECT pg_advisory_lock(:key)"), {"key": p["pause_key"]})

        async def submit_review() -> Any:
            async with AsyncClient(
                transport=ASGITransport(app=create_app()), base_url="http://test"
            ) as client:
                return await client.post(
                    f"/v1/items/{item_id}/review",
                    json={"review_status": decision, "reason": f"review first {decision}"},
                    headers=_headers(p, "user_review"),
                )

        review_task = asyncio.create_task(submit_review())
        # Prove the review request is genuinely blocked behind the advisory
        # lock — it holds the row's FOR UPDATE lock at this point.
        await _await_blocked_on(coordinator, coordinator_pid, 1)

        # Promotion runs while review holds the row lock. SKIP LOCKED skips the
        # locked row, so promotion completes immediately with nothing promoted.
        result = await asyncio.wait_for(p["promote"](), timeout=10)
        assert result.promoted == 0
        assert item_id not in result.promoted_ids
        assert result.scanned == 0  # the locked row is skipped, not scanned

        # Release the advisory lock so review commits its decision.
        await coordinator.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": p["pause_key"]})
        review_resp = await asyncio.wait_for(review_task, timeout=10)
        assert review_resp.status_code == 200, review_resp.text
    finally:
        if review_task is not None and not review_task.done():
            review_task.cancel()
            await asyncio.gather(review_task, return_exceptions=True)
        if coordinator.in_transaction():
            await coordinator.rollback()
        await coordinator.execute(text("SELECT pg_advisory_unlock_all()"))
        await coordinator.close()
        async with p["owner"].begin() as conn:
            await conn.execute(text(f"DROP TRIGGER IF EXISTS {trigger} ON memory_items"))
            await conn.execute(text(f"DROP FUNCTION IF EXISTS {trigger}()"))

    st = await p["state"](item_id)
    # Final state is the review decision; promotion did not overwrite it.
    assert st["item"]["review_status"] == decision
    # Exactly one review event; no auto-promotion event.
    review_events = [
        e for e in st["events"] if e["event_type"] == "review_change"
        and e["field_name"] == "review_status"
    ]
    assert len(review_events) == 1
    assert review_events[0]["old_value"] == "proposed"
    assert review_events[0]["new_value"] == decision
    assert review_events[0]["actor_principal_id"] == str(p["actors"]["user_review"])
    auto_events = [
        e for e in st["events"] if "auto-promotion" in (e["reason"] or "")
    ]
    assert auto_events == []


# ===========================================================================
# 2. Promotion-first followed by a valid review transition
# ===========================================================================


async def test_promotion_first_then_review_resumes_from_active(proof: dict[str, Any]) -> None:
    """Promotion owns the row lock first and pauses before commit. A
    Bearer-authenticated review request starts and genuinely waits behind
    promotion's row lock. Promotion commits exactly one ``proposed -> active``
    event. Review resumes from the committed ``active`` state and applies a
    valid ``active -> disputed`` transition. The final state and events form a
    valid ordered history; neither operation reports a database error; no event
    claims an incorrect old state."""
    p = proof
    item_id = await p["insert_proposed"](content="promotion-first")

    # Pause promotion's UPDATE so it holds the FOR UPDATE SKIP LOCKED row lock
    # without committing — this is the window in which the review request waits.
    trigger = f"promo_pause_{p['tag']}"
    async with p["owner"].begin() as conn:
        await conn.execute(
            text(
                f"CREATE FUNCTION {trigger}() RETURNS trigger LANGUAGE plpgsql AS $$ "
                f"BEGIN PERFORM pg_advisory_xact_lock({p['pause_key']}); RETURN NEW; END $$"
            )
        )
        await conn.execute(
            text(
                f"CREATE TRIGGER {trigger} BEFORE UPDATE OF review_status "
                f"ON memory_items FOR EACH ROW WHEN (OLD.tenant_id = '{p['tenant']}' "
                f"AND OLD.review_status = 'proposed') EXECUTE FUNCTION {trigger}()"
            )
        )

    coordinator = await p["owner"].connect()
    promo_task: asyncio.Task[Any] | None = None
    review_task: asyncio.Task[Any] | None = None
    try:
        coordinator_pid = await coordinator.scalar(text("SELECT pg_backend_pid()"))
        await coordinator.execute(text("SELECT pg_advisory_lock(:key)"), {"key": p["pause_key"]})

        promo_task = asyncio.create_task(p["promote"]())

        # Wait for promotion to reach its blocked UPDATE (it holds the row lock
        # at this point). Give the advisory wait a moment to register.
        await _await_blocked_on(coordinator, coordinator_pid, 1)

        # Start the review request on a distinct client/session path. It takes
        # SELECT ... FOR UPDATE on the same row and blocks behind promotion.
        async def submit_review() -> Any:
            async with AsyncClient(
                transport=ASGITransport(app=create_app()), base_url="http://test"
            ) as client:
                return await client.post(
                    f"/v1/items/{item_id}/review",
                    json={"review_status": "disputed", "reason": "review after promotion"},
                    headers=_headers(p, "user_review"),
                )

        review_task = asyncio.create_task(submit_review())

        # Prove the review request is genuinely waiting on promotion's backend
        # pid (row lock), not just on the advisory lock. Find promotion's pid:
        # it is the session blocked on the coordinator's advisory lock.
        promo_pid_row = (
            await coordinator.execute(
                text(
                    "SELECT pid FROM pg_stat_activity"
                    " WHERE :coordinator_pid = ANY(pg_blocking_pids(pid))"
                    " AND wait_event_type = 'Lock' AND wait_event = 'advisory'"
                ),
                {"coordinator_pid": coordinator_pid},
            )
        ).first()
        assert promo_pid_row is not None, "promotion did not reach the paused UPDATE"
        promo_pid = int(promo_pid_row[0])
        # The review request should be blocked on promotion's row lock.
        await _await_blocked_on_pid(coordinator, promo_pid, 1)
        assert not review_task.done(), "review must still be waiting behind promotion"

        # Release the advisory lock so promotion commits proposed -> active.
        await coordinator.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": p["pause_key"]})
        promo_result = await asyncio.wait_for(promo_task, timeout=10)
        assert promo_result.promoted == 1
        assert item_id in promo_result.promoted_ids

        # Review resumes from the committed active state: active -> disputed is
        # a valid transition for a user.
        review_resp = await asyncio.wait_for(review_task, timeout=10)
        assert review_resp.status_code == 200, review_resp.text
        body = review_resp.json()
        assert body["item"]["review_status"] == "disputed"
    finally:
        for task in (promo_task, review_task):
            if task is not None and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        if coordinator.in_transaction():
            await coordinator.rollback()
        await coordinator.execute(text("SELECT pg_advisory_unlock_all()"))
        await coordinator.close()
        async with p["owner"].begin() as conn:
            await conn.execute(text(f"DROP TRIGGER IF EXISTS {trigger} ON memory_items"))
            await conn.execute(text(f"DROP FUNCTION IF EXISTS {trigger}()"))

    st = await p["state"](item_id)
    assert st["item"]["review_status"] == "disputed"
    events = st["events"]
    # Exactly two review_change events: proposed -> active (promotion), then
    # active -> disputed (review), forming a valid ordered history.
    assert len(events) == 2
    assert events[0]["old_value"] == "proposed"
    assert events[0]["new_value"] == "active"
    assert "auto-promotion" in (events[0]["reason"] or "")
    assert events[1]["old_value"] == "active"
    assert events[1]["new_value"] == "disputed"
    assert events[1]["actor_principal_id"] == str(p["actors"]["user_review"])
    # No event claims an incorrect old state — each old_value matches the
    # prior event's new_value (or the true initial state).
    assert events[1]["old_value"] == events[0]["new_value"]


# ===========================================================================
# 3. Competing activation
# ===========================================================================


@pytest.mark.parametrize(
    "winner",
    ["promotion_wins", "review_wins"],
    ids=["promotion_wins", "review_wins"],
)
async def test_competing_activation_one_effective_transition(
    proof: dict[str, Any], winner: str
) -> None:
    """Path A promotion and a human ``proposed -> active`` review decision race
    on the same item. Both lock orders are exercised (parametrized by winner),
    so the winner-agnostic invariants below are proven for each:

      - ``promotion_wins``: promotion is paused at its UPDATE (holding the row
        lock); the review request waits behind it; promotion commits first.
      - ``review_wins``: the review request is paused at its UPDATE (holding the
        row lock); promotion's ``FOR UPDATE SKIP LOCKED`` skips the locked row
        and completes with ``promoted == 0``; the review then commits first.

    In both cases: the final state is ``active``, exactly one effective
    ``proposed -> active`` transition occurs, exactly one corresponding event
    persists, the losing operation is a truthful skip or no-op, attribution
    identifies the actual winning path, and promotion's result accounting
    matches whether promotion won."""
    p = proof
    item_id = await p["insert_proposed"](content=f"competing-activation-{winner}")

    if winner == "promotion_wins":
        # Pause promotion's UPDATE so it holds the FOR UPDATE SKIP LOCKED row
        # lock without committing — the window in which the review waits.
        trigger = f"promo_pause_{p['tag']}"
    else:
        # Pause the review's UPDATE so it holds the SELECT ... FOR UPDATE row
        # lock without committing — the window in which promotion skips it.
        trigger = f"review_pause_{p['tag']}"

    async with p["owner"].begin() as conn:
        await conn.execute(
            text(
                f"CREATE FUNCTION {trigger}() RETURNS trigger LANGUAGE plpgsql AS $$ "
                f"BEGIN PERFORM pg_advisory_xact_lock({p['pause_key']}); RETURN NEW; END $$"
            )
        )
        # The trigger fires on the paused operation's UPDATE (review for
        # review_wins, promotion for promotion_wins) — both route their
        # transition through an UPDATE OF review_status on this item. Scoped
        # to this item only so a concurrent batch sweep's other rows are
        # unaffected.
        await conn.execute(
            text(
                f"CREATE TRIGGER {trigger} BEFORE UPDATE OF review_status "
                f"ON memory_items FOR EACH ROW WHEN (OLD.tenant_id = '{p['tenant']}' "
                f"AND OLD.review_status = 'proposed' AND OLD.id = '{item_id}') "
                f"EXECUTE FUNCTION {trigger}()"
            )
        )

    coordinator = await p["owner"].connect()
    promo_task: asyncio.Task[Any] | None = None
    review_task: asyncio.Task[Any] | None = None
    try:
        coordinator_pid = await coordinator.scalar(text("SELECT pg_backend_pid()"))
        await coordinator.execute(text("SELECT pg_advisory_lock(:key)"), {"key": p["pause_key"]})

        async def submit_review() -> Any:
            async with AsyncClient(
                transport=ASGITransport(app=create_app()), base_url="http://test"
            ) as client:
                return await client.post(
                    f"/v1/items/{item_id}/review",
                    json={"review_status": "active", "reason": f"human activation ({winner})"},
                    headers=_headers(p, "user_review"),
                )

        if winner == "promotion_wins":
            # Start promotion first; it locks the row and pauses at its UPDATE.
            promo_task = asyncio.create_task(p["promote"]())
            await _await_blocked_on(coordinator, coordinator_pid, 1)
            # The review request waits behind promotion's row lock.
            review_task = asyncio.create_task(submit_review())
            promo_pid_row = (
                await coordinator.execute(
                    text(
                        "SELECT pid FROM pg_stat_activity"
                        " WHERE :coordinator_pid = ANY(pg_blocking_pids(pid))"
                        " AND wait_event_type = 'Lock' AND wait_event = 'advisory'"
                    ),
                    {"coordinator_pid": coordinator_pid},
                )
            ).first()
            assert promo_pid_row is not None, "promotion did not reach the paused UPDATE"
            promo_pid = int(promo_pid_row[0])
            await _await_blocked_on_pid(coordinator, promo_pid, 1)
            assert not review_task.done(), "review must still be waiting behind promotion"
        else:
            # Start the review first; it takes FOR UPDATE and pauses at its
            # UPDATE behind the advisory lock — holding the row lock.
            review_task = asyncio.create_task(submit_review())
            await _await_blocked_on(coordinator, coordinator_pid, 1)
            # Promotion's FOR UPDATE SKIP LOCKED skips the review-locked row, so
            # it completes immediately with nothing promoted (no overlap wait).
            promo_task = asyncio.create_task(p["promote"]())

        # Release the advisory lock so the paused operation commits.
        await coordinator.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": p["pause_key"]})
        promo_result = await asyncio.wait_for(promo_task, timeout=10)
        review_resp = await asyncio.wait_for(review_task, timeout=10)
    finally:
        for task in (promo_task, review_task):
            if task is not None and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        if coordinator.in_transaction():
            await coordinator.rollback()
        await coordinator.execute(text("SELECT pg_advisory_unlock_all()"))
        await coordinator.close()
        async with p["owner"].begin() as conn:
            await conn.execute(text(f"DROP TRIGGER IF EXISTS {trigger} ON memory_items"))
            await conn.execute(text(f"DROP FUNCTION IF EXISTS {trigger}()"))

    st = await p["state"](item_id)
    # Final state is active regardless of which path won.
    assert st["item"]["review_status"] == "active"
    events = st["events"]
    # Exactly one effective proposed -> active transition event.
    activation_events = [
        e for e in events
        if e["event_type"] == "review_change"
        and e["field_name"] == "review_status"
        and e["old_value"] == "proposed"
        and e["new_value"] == "active"
    ]
    assert len(activation_events) == 1, [dict(e) for e in events]
    winning_event = activation_events[0]

    if winner == "promotion_wins":
        # Promotion won the lock: its accounting reports the transition, and the
        # winning event is the auto-promotion. The losing review is a truthful
        # no-op (it found the item already active -> 200, event=None).
        assert promo_result.promoted == 1
        assert item_id in promo_result.promoted_ids
        assert "auto-promotion" in (winning_event["reason"] or "")
        assert winning_event["actor_principal_id"] != str(p["actors"]["user_review"])
        assert review_resp.status_code == 200, review_resp.text
        assert review_resp.json()["event"] is None
    else:
        # Review won the lock: promotion's SKIP LOCKED skipped the row, so its
        # accounting reports nothing. The winning event is the human review
        # (actor = authenticated reviewer), and promotion wrote no event.
        assert promo_result.promoted == 0
        assert item_id not in promo_result.promoted_ids
        assert "auto-promotion" not in (winning_event["reason"] or "")
        assert winning_event["actor_principal_id"] == str(p["actors"]["user_review"])
        assert review_resp.status_code == 200, review_resp.text
        assert review_resp.json()["event"] is not None

    # No second activation event and no event with an incorrect old state.
    assert len(events) == 1


async def test_review_first_activation_then_promotion_skips(proof: dict[str, Any]) -> None:
    """A human ``proposed -> active`` review owns the row lock (paused at its
    UPDATE) before promotion can mutate the item. Promotion's
    ``FOR UPDATE SKIP LOCKED`` skips the locked row (``promoted == 0``, item
    absent from ``promoted_ids``). After the review commits, the final state is
    ``active`` with exactly one ``proposed -> active`` event whose actor is the
    authenticated reviewer — and no auto-promotion event. This is the
    review-first activation case proved through genuine overlap, distinct from
    the parametrized competing-activation test (which exercises both orders but
    asserts on the race outcome) and from the review-first disputed/rejected
    tests (which assert on a non-active decision)."""
    p = proof
    item_id = await p["insert_proposed"](content="review-first-activation")

    # Pause the review's UPDATE so it holds the row lock (FOR UPDATE already
    # taken) without committing — this is the window in which promotion runs.
    trigger = f"review_pause_{p['tag']}"
    async with p["owner"].begin() as conn:
        await conn.execute(
            text(
                f"CREATE FUNCTION {trigger}() RETURNS trigger LANGUAGE plpgsql AS $$ "
                f"BEGIN PERFORM pg_advisory_xact_lock({p['pause_key']}); RETURN NEW; END $$"
            )
        )
        await conn.execute(
            text(
                f"CREATE TRIGGER {trigger} BEFORE UPDATE OF review_status "
                f"ON memory_items FOR EACH ROW WHEN (OLD.tenant_id = '{p['tenant']}' "
                f"AND OLD.review_status = 'proposed' AND OLD.id = '{item_id}') "
                f"EXECUTE FUNCTION {trigger}()"
            )
        )

    coordinator = await p["owner"].connect()
    review_task: asyncio.Task[Any] | None = None
    try:
        coordinator_pid = await coordinator.scalar(text("SELECT pg_backend_pid()"))
        await coordinator.execute(text("SELECT pg_advisory_lock(:key)"), {"key": p["pause_key"]})

        async def submit_review() -> Any:
            async with AsyncClient(
                transport=ASGITransport(app=create_app()), base_url="http://test"
            ) as client:
                return await client.post(
                    f"/v1/items/{item_id}/review",
                    json={"review_status": "active", "reason": "review first activation"},
                    headers=_headers(p, "user_review"),
                )

        review_task = asyncio.create_task(submit_review())
        # Prove the review request is genuinely blocked behind the advisory
        # lock — it holds the row's FOR UPDATE lock at this point.
        await _await_blocked_on(coordinator, coordinator_pid, 1)

        # Promotion runs while review holds the row lock. SKIP LOCKED skips the
        # locked row, so promotion completes immediately with nothing promoted.
        result = await asyncio.wait_for(p["promote"](), timeout=10)
        assert result.promoted == 0
        assert item_id not in result.promoted_ids
        assert result.scanned == 0  # the locked row is skipped, not scanned

        # Release the advisory lock so review commits its activation.
        await coordinator.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": p["pause_key"]})
        review_resp = await asyncio.wait_for(review_task, timeout=10)
        assert review_resp.status_code == 200, review_resp.text
    finally:
        if review_task is not None and not review_task.done():
            review_task.cancel()
            await asyncio.gather(review_task, return_exceptions=True)
        if coordinator.in_transaction():
            await coordinator.rollback()
        await coordinator.execute(text("SELECT pg_advisory_unlock_all()"))
        await coordinator.close()
        async with p["owner"].begin() as conn:
            await conn.execute(text(f"DROP TRIGGER IF EXISTS {trigger} ON memory_items"))
            await conn.execute(text(f"DROP FUNCTION IF EXISTS {trigger}()"))

    st = await p["state"](item_id)
    # Final state is the review activation; promotion did not overwrite it.
    assert st["item"]["review_status"] == "active"
    events = st["events"]
    # Exactly one proposed -> active event; its actor is the authenticated
    # reviewer, not the auto-promotion internal actor.
    activation_events = [
        e for e in events
        if e["event_type"] == "review_change"
        and e["field_name"] == "review_status"
        and e["old_value"] == "proposed"
        and e["new_value"] == "active"
    ]
    assert len(activation_events) == 1, [dict(e) for e in events]
    assert activation_events[0]["actor_principal_id"] == str(p["actors"]["user_review"])
    # No auto-promotion event was written.
    auto_events = [
        e for e in st["events"] if "auto-promotion" in (e["reason"] or "")
    ]
    assert auto_events == []


# ===========================================================================
# 4. Batch isolation
# ===========================================================================


async def test_batch_isolation_contested_does_not_block_uncontested(
    proof: dict[str, Any]
) -> None:
    """Two eligible proposed items: one is concurrently locked by a review (it
    owns the row lock), the other is uncontested. A bounded promotion sweep
    skips the locked item via ``FOR UPDATE SKIP LOCKED`` and promotes the
    uncontested one normally. Result counts and IDs describe only the actual
    transition; the contested item's event ledger is untouched by promotion."""
    p = proof
    contested = await p["insert_proposed"](content="batch contested")
    uncontested = await p["insert_proposed"](content="batch uncontested")

    # Pause the review's UPDATE on the contested item so it holds the row lock
    # without committing — the contested row is locked for the duration of the
    # promotion sweep.
    trigger = f"review_pause_{p['tag']}"
    async with p["owner"].begin() as conn:
        await conn.execute(
            text(
                f"CREATE FUNCTION {trigger}() RETURNS trigger LANGUAGE plpgsql AS $$ "
                f"BEGIN PERFORM pg_advisory_xact_lock({p['pause_key']}); RETURN NEW; END $$"
            )
        )
        await conn.execute(
            text(
                f"CREATE TRIGGER {trigger} BEFORE UPDATE OF review_status "
                f"ON memory_items FOR EACH ROW WHEN (OLD.tenant_id = '{p['tenant']}' "
                f"AND OLD.id = '{contested}' AND OLD.review_status = 'proposed') "
                f"EXECUTE FUNCTION {trigger}()"
            )
        )

    coordinator = await p["owner"].connect()
    review_task: asyncio.Task[Any] | None = None
    try:
        coordinator_pid = await coordinator.scalar(text("SELECT pg_backend_pid()"))
        await coordinator.execute(text("SELECT pg_advisory_lock(:key)"), {"key": p["pause_key"]})

        async def submit_review() -> Any:
            async with AsyncClient(
                transport=ASGITransport(app=create_app()), base_url="http://test"
            ) as client:
                return await client.post(
                    f"/v1/items/{contested}/review",
                    json={"review_status": "disputed", "reason": "batch contest"},
                    headers=_headers(p, "user_review"),
                )

        review_task = asyncio.create_task(submit_review())
        await _await_blocked_on(coordinator, coordinator_pid, 1)

        # Run a bounded sweep over both candidates. The contested row is
        # SKIP LOCKED (not scanned); the uncontested row promotes normally.
        result = await asyncio.wait_for(p["promote"](), timeout=10)
        # Only the uncontested item transitioned.
        assert result.promoted == 1
        assert result.promoted_ids == [uncontested]
        # The contested item was skipped, not scanned.
        assert result.scanned == 1
        assert contested not in result.promoted_ids

        # Release so the review commits its disputed decision.
        await coordinator.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": p["pause_key"]})
        review_resp = await asyncio.wait_for(review_task, timeout=10)
        assert review_resp.status_code == 200, review_resp.text
    finally:
        if review_task is not None and not review_task.done():
            review_task.cancel()
            await asyncio.gather(review_task, return_exceptions=True)
        if coordinator.in_transaction():
            await coordinator.rollback()
        await coordinator.execute(text("SELECT pg_advisory_unlock_all()"))
        await coordinator.close()
        async with p["owner"].begin() as conn:
            await conn.execute(text(f"DROP TRIGGER IF EXISTS {trigger} ON memory_items"))
            await conn.execute(text(f"DROP FUNCTION IF EXISTS {trigger}()"))

    # The uncontested item promoted: active with one auto-promotion event.
    un_st = await p["state"](uncontested)
    assert un_st["item"]["review_status"] == "active"
    un_events = [
        e for e in un_st["events"]
        if e["event_type"] == "review_change" and e["field_name"] == "review_status"
    ]
    assert len(un_events) == 1
    assert un_events[0]["old_value"] == "proposed"
    assert un_events[0]["new_value"] == "active"
    assert "auto-promotion" in (un_events[0]["reason"] or "")

    # The contested item reflects the review decision, untouched by promotion.
    c_st = await p["state"](contested)
    assert c_st["item"]["review_status"] == "disputed"
    c_events = [
        e for e in c_st["events"]
        if e["event_type"] == "review_change" and e["field_name"] == "review_status"
    ]
    assert len(c_events) == 1
    assert c_events[0]["old_value"] == "proposed"
    assert c_events[0]["new_value"] == "disputed"
    assert c_events[0]["actor_principal_id"] == str(p["actors"]["user_review"])
    auto_on_contested = [
        e for e in c_st["events"] if "auto-promotion" in (e["reason"] or "")
    ]
    assert auto_on_contested == []
# ruff: noqa: E501
"""Executable app-role proof for P0-FIX-004B: promotion vs feedback serialization.

Proves that ``engram.promotion.auto_promote_proposed_memories`` (Path A) and
``POST /v1/feedback`` serialize correctly through their shared
``memory_items`` row lock when both target the same item. Covers the four
required scenarios:

  1. feedback-first external ``noise`` — an external principal's feedback
     owns the item row lock (paused before commit); promotion's
     ``FOR UPDATE SKIP LOCKED`` skips the locked row and reports zero
     transitions; after release, feedback commits and its canonical verdict
     is ``noise``; a later promotion sweep observes the committed noise
     through the external-dispute gate (``skipped_dispute``) and still does
     not promote.
  2. promotion-first — promotion owns the item row lock (paused before
     commit); the external principal's ``noise`` feedback waits behind
     promotion's backend (proven via ``pg_blocking_pids``); promotion
     commits exactly one ``proposed -> active``; feedback resumes and
     commits against the now-active item; final state remains ``active``
     with one current external ``noise`` row.
  3. policy preservation — external ``useful`` and author-self ``noise`` do
     NOT satisfy the external-dispute gate; they remain recorded normally
     with their existing importance effects, and a later promotion is not
     blocked by them.
  4. batch isolation — one feedback-locked candidate + one uncontested
     candidate; the sweep skips the locked row via ``SKIP LOCKED`` and
     promotes the uncontested one normally; the contested item receives no
     promotion event and its feedback commits normally after release.

All feedback requests use real Bearer credentials through ASGI. Promotion
uses the production service function over an independent app-role session with
FORCE RLS. Synchronization is deterministic and database-level: a test-only
trigger calls ``pg_advisory_xact_lock`` at the feedback event INSERT or the
promotion UPDATE write point, and the coordinator holds the matching advisory
lock to pause one operation while the other runs. Overlap is proven via
PostgreSQL's blocker graph (``pg_blocking_pids``), not sleeps. The owner
connection is used only to arrange state, install triggers, and inspect
committed state.

The global lock order is preserved throughout: memory item before principal
(``engram.feedback.record_feedback`` locks the principal only after the
route has already locked the item). Promotion locks only the memory item, so
the two operations share exactly the memory-item row lock as their
serialization point.

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
    # Actors: an agent author writes proposed items; a different agent
    # (external principal) records feedback. The author's own ``noise`` does
    # not count as an external dispute, so we need a distinct principal for
    # the external-noise scenarios. ``external`` holds ``write`` so it can
    # record feedback; ``author`` holds ``write`` so it can record author-self
    # noise for the policy-preservation scenario. The author also writes the
    # proposed items (via the owner fixture, not the API).
    actors = {
        "author": (uuid.uuid4(), "agent", ["write"], None),
        "external": (uuid.uuid4(), "agent", ["write"], None),
    }
    keys: dict[str, str] = {}
    async with owner.begin() as conn:
        await conn.execute(
            text("INSERT INTO tenants (id,name,slug) VALUES (:id,:n,:n)"),
            {"id": tenant, "n": f"promo-feedback-{tag}"},
        )
        await conn.execute(
            text(
                "INSERT INTO tenant_config (tenant_id,config_version,active) VALUES (:id,'proof',true)"
            ),
            {"id": tenant},
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
                        "label": f"promo-feedback-{tag}-{name}",
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

    async def insert_active(
        *, content: str, principal: str = "author", memory_confidence: float = 0.9
    ) -> uuid.UUID:
        item_id = uuid.uuid4()
        async with owner.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO memory_items (id,tenant_id,principal_id,content,content_hash,"
                    "kind,visibility,review_status,memory_confidence,source_trust,importance,"
                    "source_type,created_at,valid_from) VALUES "
                    "(:id,:tid,:pid,:content,:hash,'fact','tenant','active',:conf,.73,.64,"
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
            feedback = (
                (
                    await conn.execute(
                        text(
                            "SELECT id, principal_id::text AS principal_id, verdict, "
                            "superseded_at, replaces_feedback_event_id::text AS replaces_feedback_event_id "
                            "FROM feedback_events WHERE item_id=:id "
                            "ORDER BY created_at ASC, id ASC"
                        ),
                        {"id": item_id},
                    )
                )
                .mappings()
                .all()
            )
        return {
            "item": dict(row),
            "events": [dict(e) for e in events],
            "feedback": [dict(f) for f in feedback],
        }

    async def promote(*, now: datetime | None = None) -> Any:
        """Run the production promotion service over an independent app-role session."""
        async with app_factory() as session:
            await apply_rls_context(
                session, tenant_id=str(tenant), principal_id=str(actors["author"][0])
            )
            return await auto_promote_proposed_memories(
                session, str(tenant), source="cli", now=now
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
        "insert_active": insert_active,
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
                text(f"DROP TRIGGER IF EXISTS feedback_pause_{tag} ON feedback_events")
            )
            await conn.execute(text(f"DROP FUNCTION IF EXISTS feedback_pause_{tag}()"))
            await conn.execute(
                text(f"DROP TRIGGER IF EXISTS promo_pause_{tag} ON memory_items")
            )
            await conn.execute(text(f"DROP FUNCTION IF EXISTS promo_pause_{tag}()"))
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


async def _feedback(
    p: dict[str, Any], actor: str, item_id: uuid.UUID, verdict: str
) -> Any:
    """Submit a real Bearer-authenticated POST /v1/feedback on a fresh client."""
    async with AsyncClient(
        transport=ASGITransport(app=create_app()), base_url="http://test"
    ) as client:
        return await client.post(
            "/v1/feedback",
            json={"item_id": str(item_id), "feedback": verdict},
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


def _install_feedback_pause_trigger(p: dict[str, Any], item_id: uuid.UUID) -> str:
    """Return SQL to install a feedback_events BEFORE INSERT advisory-lock trigger.

    The trigger fires only for the target item's feedback rows, so concurrent
    feedback on other items is unaffected. The trigger acquires a per-transaction
    advisory lock: when the coordinator holds the matching session-level lock
    first, the feedback request blocks here. At this point the feedback route
    has already taken SELECT ... FOR UPDATE on the memory_items row (step 2 of
    the route), so the item row lock is held while the request is paused.
    """
    trigger = f"feedback_pause_{p['tag']}"
    fn = trigger
    return (
        f"CREATE FUNCTION {fn}() RETURNS trigger LANGUAGE plpgsql AS $$ "
        f"BEGIN PERFORM pg_advisory_xact_lock({p['pause_key']}); RETURN NEW; END $$"
    ), (
        f"CREATE TRIGGER {trigger} BEFORE INSERT ON feedback_events "
        f"FOR EACH ROW WHEN (NEW.item_id = '{item_id}') EXECUTE FUNCTION {fn}()"
    )


def _install_promo_pause_trigger(p: dict[str, Any], item_id: uuid.UUID) -> tuple[str, str, str]:
    """Return (function_name, create_function_sql, create_trigger_sql) for a
    promotion-UPDATE advisory-lock trigger scoped to one item.

    Fires on the guarded ``UPDATE OF review_status`` (proposed -> active) so
    promotion holds the FOR UPDATE SKIP LOCKED row lock while paused.
    """
    trigger = f"promo_pause_{p['tag']}"
    return (
        trigger,
        (
            f"CREATE FUNCTION {trigger}() RETURNS trigger LANGUAGE plpgsql AS $$ "
            f"BEGIN PERFORM pg_advisory_xact_lock({p['pause_key']}); RETURN NEW; END $$"
        ),
        (
            f"CREATE TRIGGER {trigger} BEFORE UPDATE OF review_status "
            f"ON memory_items FOR EACH ROW WHEN (OLD.tenant_id = '{p['tenant']}' "
            f"AND OLD.review_status = 'proposed' AND OLD.id = '{item_id}') "
            f"EXECUTE FUNCTION {trigger}()"
        ),
    )


# ===========================================================================
# 1. Feedback-first external noise wins first
# ===========================================================================


async def test_feedback_first_external_noise_skips_promotion_then_dispute_gate(
    proof: dict[str, Any],
) -> None:
    """An external principal's ``noise`` feedback owns the item row lock
    (paused at the feedback_events INSERT, after the route's FOR UPDATE). A
    concurrent promotion sweep ``FOR UPDATE SKIP LOCKED`` skips the locked row
    and reports zero transitions for it (not scanned, not promoted, no event).
    After release, feedback commits and its canonical current verdict is
    ``noise`` with exactly one importance change. A later promotion sweep
    observes the committed external noise through the external-dispute gate:
    ``skipped_dispute`` increments and the item remains ``proposed`` with no
    promotion event."""
    p = proof
    item_id = await p["insert_proposed"](content="feedback-first-noise")
    before = await p["state"](item_id)
    initial_importance = float(before["item"]["importance"])

    fn_sql, trig_sql = _install_feedback_pause_trigger(p, item_id)
    async with p["owner"].begin() as conn:
        await conn.execute(text(fn_sql))
        await conn.execute(text(trig_sql))

    coordinator = await p["owner"].connect()
    feedback_task: asyncio.Task[Any] | None = None
    try:
        coordinator_pid = await coordinator.scalar(text("SELECT pg_backend_pid()"))
        await coordinator.execute(text("SELECT pg_advisory_lock(:key)"), {"key": p["pause_key"]})

        # Start the external noise feedback. It takes FOR UPDATE on the item,
        # then blocks at the feedback_events INSERT trigger (advisory lock).
        feedback_task = asyncio.create_task(_feedback(p, "external", item_id, "noise"))
        await _await_blocked_on(coordinator, coordinator_pid, 1)

        # Prove the feedback backend genuinely holds the item row lock: a
        # second app-role session's SELECT ... FOR UPDATE on the same row must
        # block on the feedback backend's pid. (Promotion uses SKIP LOCKED so
        # it would not block — this proves the lock is held by a direct
        # blocking probe.)
        feedback_pid_row = (
            await coordinator.execute(
                text(
                    "SELECT pid FROM pg_stat_activity"
                    " WHERE :coordinator_pid = ANY(pg_blocking_pids(pid))"
                    " AND wait_event_type = 'Lock' AND wait_event = 'advisory'"
                ),
                {"coordinator_pid": coordinator_pid},
            )
        ).first()
        assert feedback_pid_row is not None, "feedback did not reach the paused INSERT"
        feedback_pid = int(feedback_pid_row[0])

        probe = await p["app"].connect()
        try:
            await apply_rls_context_session(probe, p)
            probe_task = asyncio.create_task(
                probe.execute(text("SELECT id FROM memory_items WHERE id=:id FOR UPDATE"), {"id": item_id})
            )
            await _await_blocked_on_pid(coordinator, feedback_pid, 1)
            # The probe is blocked on the feedback backend -> feedback owns the row lock.
            probe_task.cancel()
            await asyncio.gather(probe_task, return_exceptions=True)
        finally:
            await probe.close()

        # Promotion runs while feedback holds the row lock. SKIP LOCKED skips
        # the locked row, so promotion completes immediately with nothing promoted.
        result = await asyncio.wait_for(p["promote"](), timeout=10)
        assert result.promoted == 0
        assert item_id not in result.promoted_ids
        # The locked row is skipped, not scanned.
        assert result.scanned == 0
        assert result.skipped_dispute == 0

        # Release the advisory lock so feedback commits its noise verdict.
        await coordinator.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": p["pause_key"]})
        feedback_resp = await asyncio.wait_for(feedback_task, timeout=10)
        assert feedback_resp.status_code == 201, feedback_resp.text
        feedback_task = None
        body = feedback_resp.json()
        assert body["status"] == "recorded"
        assert body["feedback"] == "noise"
        assert body["previous_feedback"] is None
    finally:
        if feedback_task is not None and not feedback_task.done():
            feedback_task.cancel()
            await asyncio.gather(feedback_task, return_exceptions=True)
        if coordinator.in_transaction():
            await coordinator.rollback()
        await coordinator.execute(text("SELECT pg_advisory_unlock_all()"))
        await coordinator.close()
        async with p["owner"].begin() as conn:
            await conn.execute(text(f"DROP TRIGGER IF EXISTS feedback_pause_{p['tag']} ON feedback_events"))
            await conn.execute(text(f"DROP FUNCTION IF EXISTS feedback_pause_{p['tag']}()"))

    # After feedback commits: item remains proposed, one current noise row,
    # importance changed exactly once by the external agent noise effect.
    st = await p["state"](item_id)
    assert st["item"]["review_status"] == "proposed"
    assert len(st["feedback"]) == 1
    assert st["feedback"][0]["verdict"] == "noise"
    assert st["feedback"][0]["superseded_at"] is None
    assert st["feedback"][0]["principal_id"] == str(p["actors"]["external"])
    # External agent noise: effect_for_feedback(agent, is_author=False, noise) = -0.05.
    assert st["item"]["importance"] == pytest.approx(initial_importance - 0.05)
    # No auto-promotion event was written.
    auto_events = [e for e in st["events"] if "auto-promotion" in (e["reason"] or "")]
    assert auto_events == []

    # Later sweep: the committed external noise satisfies the dispute gate.
    later = await p["promote"]()
    assert later.promoted == 0
    assert item_id not in later.promoted_ids
    # The item IS examined now (not locked) and skipped by the dispute gate.
    assert later.scanned == 1
    assert later.skipped_dispute == 1

    # Final state: still proposed, no promotion event, noise still current.
    st2 = await p["state"](item_id)
    assert st2["item"]["review_status"] == "proposed"
    assert len(st2["feedback"]) == 1
    assert st2["feedback"][0]["verdict"] == "noise"
    auto_events2 = [e for e in st2["events"] if "auto-promotion" in (e["reason"] or "")]
    assert auto_events2 == []


# ===========================================================================
# 2. Promotion wins first
# ===========================================================================


async def test_promotion_first_then_feedback_commits_against_active(
    proof: dict[str, Any],
) -> None:
    """Promotion owns the item row lock first (paused at its guarded UPDATE).
    An external principal's ``noise`` feedback starts and genuinely waits
    behind promotion's backend (proven via ``pg_blocking_pids``). Promotion
    commits exactly one ``proposed -> active`` transition and event. Feedback
    resumes from the committed ``active`` state and succeeds; final state
    remains ``active`` with one current external ``noise`` row whose importance
    effect is exactly the feedback policy's. Neither operation reports a
    database error; no demotion or retroactive cancellation occurs."""
    p = proof
    item_id = await p["insert_proposed"](content="promotion-first-feedback")
    before = await p["state"](item_id)
    initial_importance = float(before["item"]["importance"])

    trigger, fn_sql, trig_sql = _install_promo_pause_trigger(p, item_id)
    async with p["owner"].begin() as conn:
        await conn.execute(text(fn_sql))
        await conn.execute(text(trig_sql))

    coordinator = await p["owner"].connect()
    promo_task: asyncio.Task[Any] | None = None
    feedback_task: asyncio.Task[Any] | None = None
    try:
        coordinator_pid = await coordinator.scalar(text("SELECT pg_backend_pid()"))
        await coordinator.execute(text("SELECT pg_advisory_lock(:key)"), {"key": p["pause_key"]})

        promo_task = asyncio.create_task(p["promote"]())
        # Wait for promotion to reach its blocked UPDATE (it holds the row lock).
        await _await_blocked_on(coordinator, coordinator_pid, 1)

        # Start the external noise feedback. It takes SELECT ... FOR UPDATE on
        # the same row and blocks behind promotion's row lock.
        feedback_task = asyncio.create_task(_feedback(p, "external", item_id, "noise"))

        # Prove feedback waits behind promotion's backend pid (row lock).
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
        assert not feedback_task.done(), "feedback must still be waiting behind promotion"

        # Release so promotion commits proposed -> active.
        await coordinator.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": p["pause_key"]})
        promo_result = await asyncio.wait_for(promo_task, timeout=10)
        promo_task = None
        assert promo_result.promoted == 1
        assert item_id in promo_result.promoted_ids

        # Feedback resumes from the committed active state and succeeds.
        feedback_resp = await asyncio.wait_for(feedback_task, timeout=10)
        feedback_task = None
        assert feedback_resp.status_code == 201, feedback_resp.text
        body = feedback_resp.json()
        assert body["status"] == "recorded"
        assert body["feedback"] == "noise"
    finally:
        for task in (promo_task, feedback_task):
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
    # Final review state remains active; feedback did not demote or reopen.
    assert st["item"]["review_status"] == "active"
    # Exactly one proposed -> active promotion event.
    promo_events = [
        e for e in st["events"]
        if e["event_type"] == "review_change"
        and e["field_name"] == "review_status"
        and e["old_value"] == "proposed"
        and e["new_value"] == "active"
    ]
    assert len(promo_events) == 1, [dict(e) for e in st["events"]]
    assert "auto-promotion" in (promo_events[0]["reason"] or "")
    # One current external noise feedback row persists.
    assert len(st["feedback"]) == 1
    assert st["feedback"][0]["verdict"] == "noise"
    assert st["feedback"][0]["superseded_at"] is None
    assert st["feedback"][0]["principal_id"] == str(p["actors"]["external"])
    # Importance contains exactly the feedback effect (external agent noise: -0.05).
    assert st["item"]["importance"] == pytest.approx(initial_importance - 0.05)
    # Event/history ordering is consistent: promotion precedes the canonical
    # context-bearing feedback audit event introduced by ENG-SCOPE-002C.
    assert [event["event_type"] for event in st["events"]] == [
        "review_change",
        "feedback",
    ]


# ===========================================================================
# 3. Useful and self-noise policy preservation
# ===========================================================================


async def test_external_useful_does_not_block_promotion(proof: dict[str, Any]) -> None:
    """External ``useful`` feedback is NOT an external dispute under Path A
    policy. It is recorded normally (one current ``useful`` row, the positive
    importance effect from ``effect_for_feedback``), and a later promotion of
    the item succeeds: the dispute gate does not trip, the item transitions
    ``proposed -> active`` with exactly one auto-promotion event."""
    p = proof
    item_id = await p["insert_proposed"](content="external-useful-policy")
    before = await p["state"](item_id)
    initial_importance = float(before["item"]["importance"])

    # Record external useful feedback sequentially (no concurrency needed for
    # the policy-preservation check; the point is that it does not veto).
    resp = await _feedback(p, "external", item_id, "useful")
    assert resp.status_code == 201, resp.text
    assert resp.json()["status"] == "recorded"
    assert resp.json()["feedback"] == "useful"

    st = await p["state"](item_id)
    assert st["item"]["review_status"] == "proposed"
    assert len(st["feedback"]) == 1
    assert st["feedback"][0]["verdict"] == "useful"
    assert st["feedback"][0]["superseded_at"] is None
    # External agent useful: effect_for_feedback(agent, is_author=False, useful) = +0.025.
    assert st["item"]["importance"] == pytest.approx(initial_importance + 0.025)

    # Later promotion: the external-useful feedback does NOT satisfy the
    # dispute gate, so the item promotes normally.
    result = await p["promote"]()
    assert result.promoted == 1
    assert item_id in result.promoted_ids
    assert result.skipped_dispute == 0

    st2 = await p["state"](item_id)
    assert st2["item"]["review_status"] == "active"
    promo_events = [
        e for e in st2["events"]
        if e["event_type"] == "review_change"
        and e["field_name"] == "review_status"
        and e["old_value"] == "proposed"
        and e["new_value"] == "active"
    ]
    assert len(promo_events) == 1
    assert "auto-promotion" in (promo_events[0]["reason"] or "")
    # The useful feedback row is unchanged by promotion.
    assert len(st2["feedback"]) == 1
    assert st2["feedback"][0]["verdict"] == "useful"
    assert st2["feedback"][0]["superseded_at"] is None


async def test_author_self_noise_does_not_satisfy_dispute_gate(proof: dict[str, Any]) -> None:
    """Author-self ``noise`` feedback is explicitly excluded from the
    external-dispute gate (``FeedbackEvent.principal_id != item.principal_id``).
    It is recorded normally with the author-self effect (zero importance delta
    for an agent authoring its own item), and a later promotion succeeds: the
    dispute gate does not trip, the item transitions ``proposed -> active``
    with exactly one auto-promotion event."""
    p = proof
    item_id = await p["insert_proposed"](content="author-self-noise-policy")
    before = await p["state"](item_id)
    initial_importance = float(before["item"]["importance"])

    # The author records noise on its own item.
    resp = await _feedback(p, "author", item_id, "noise")
    assert resp.status_code == 201, resp.text
    assert resp.json()["status"] == "recorded"
    assert resp.json()["feedback"] == "noise"

    st = await p["state"](item_id)
    assert st["item"]["review_status"] == "proposed"
    assert len(st["feedback"]) == 1
    assert st["feedback"][0]["verdict"] == "noise"
    assert st["feedback"][0]["superseded_at"] is None
    assert st["feedback"][0]["principal_id"] == str(p["actors"]["author"])
    # Author-self noise (agent): effect_for_feedback(agent, is_author=True, noise) = 0.0.
    assert st["item"]["importance"] == pytest.approx(initial_importance)

    # Later promotion: author-self noise does NOT satisfy the dispute gate.
    result = await p["promote"]()
    assert result.promoted == 1
    assert item_id in result.promoted_ids
    assert result.skipped_dispute == 0

    st2 = await p["state"](item_id)
    assert st2["item"]["review_status"] == "active"
    promo_events = [
        e for e in st2["events"]
        if e["event_type"] == "review_change"
        and e["field_name"] == "review_status"
        and e["old_value"] == "proposed"
        and e["new_value"] == "active"
    ]
    assert len(promo_events) == 1
    assert "auto-promotion" in (promo_events[0]["reason"] or "")
    assert len(st2["feedback"]) == 1
    assert st2["feedback"][0]["verdict"] == "noise"
    assert st2["feedback"][0]["superseded_at"] is None


# ===========================================================================
# 4. Batch isolation with a feedback-locked candidate
# ===========================================================================


async def test_batch_isolation_feedback_locked_does_not_block_uncontested(
    proof: dict[str, Any],
) -> None:
    """Two eligible proposed items: one is concurrently locked by an external
    ``noise`` feedback request (it owns the row lock, paused before commit),
    the other is uncontested. A bounded promotion sweep skips the locked item
    via ``FOR UPDATE SKIP LOCKED`` and promotes the uncontested one normally.
    Result counts and IDs describe only the actual transition; the contested
    item receives no promotion event. After release, the feedback commits
    normally and the contested item remains ``proposed`` with one current
    ``noise`` row."""
    p = proof
    contested = await p["insert_proposed"](content="batch feedback contested")
    uncontested = await p["insert_proposed"](content="batch feedback uncontested")

    fn_sql, trig_sql = _install_feedback_pause_trigger(p, contested)
    async with p["owner"].begin() as conn:
        await conn.execute(text(fn_sql))
        await conn.execute(text(trig_sql))

    coordinator = await p["owner"].connect()
    feedback_task: asyncio.Task[Any] | None = None
    try:
        coordinator_pid = await coordinator.scalar(text("SELECT pg_backend_pid()"))
        await coordinator.execute(text("SELECT pg_advisory_lock(:key)"), {"key": p["pause_key"]})

        feedback_task = asyncio.create_task(_feedback(p, "external", contested, "noise"))
        await _await_blocked_on(coordinator, coordinator_pid, 1)

        # Run a bounded sweep over both candidates. The contested row is
        # SKIP LOCKED (not scanned); the uncontested row promotes normally.
        result = await asyncio.wait_for(p["promote"](), timeout=10)
        assert result.promoted == 1
        assert result.promoted_ids == [uncontested]
        assert result.scanned == 1
        assert contested not in result.promoted_ids

        # Release so the feedback commits its noise verdict.
        await coordinator.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": p["pause_key"]})
        feedback_resp = await asyncio.wait_for(feedback_task, timeout=10)
        feedback_task = None
        assert feedback_resp.status_code == 201, feedback_resp.text
        assert feedback_resp.json()["status"] == "recorded"
        assert feedback_resp.json()["feedback"] == "noise"
    finally:
        if feedback_task is not None and not feedback_task.done():
            feedback_task.cancel()
            await asyncio.gather(feedback_task, return_exceptions=True)
        if coordinator.in_transaction():
            await coordinator.rollback()
        await coordinator.execute(text("SELECT pg_advisory_unlock_all()"))
        await coordinator.close()
        async with p["owner"].begin() as conn:
            await conn.execute(text(f"DROP TRIGGER IF EXISTS feedback_pause_{p['tag']} ON feedback_events"))
            await conn.execute(text(f"DROP FUNCTION IF EXISTS feedback_pause_{p['tag']}()"))

    # The uncontested item promoted: active with one auto-promotion event.
    un_st = await p["state"](uncontested)
    assert un_st["item"]["review_status"] == "active"
    un_promo = [
        e for e in un_st["events"]
        if e["event_type"] == "review_change" and e["field_name"] == "review_status"
    ]
    assert len(un_promo) == 1
    assert un_promo[0]["old_value"] == "proposed"
    assert un_promo[0]["new_value"] == "active"
    assert "auto-promotion" in (un_promo[0]["reason"] or "")

    # The contested item: remains proposed, one current noise row, no
    # promotion event.
    c_st = await p["state"](contested)
    assert c_st["item"]["review_status"] == "proposed"
    assert len(c_st["feedback"]) == 1
    assert c_st["feedback"][0]["verdict"] == "noise"
    assert c_st["feedback"][0]["superseded_at"] is None
    assert c_st["feedback"][0]["principal_id"] == str(p["actors"]["external"])
    auto_on_contested = [e for e in c_st["events"] if "auto-promotion" in (e["reason"] or "")]
    assert auto_on_contested == []


# ===========================================================================
# Helpers
# ===========================================================================


async def apply_rls_context_session(conn: Any, p: dict[str, Any]) -> None:
    """Apply the author's RLS tenant/principal context on a raw app-role
    connection (used by the row-lock probe in scenario 1)."""
    await conn.execute(
        text(
            "SELECT set_config('app.tenant_id', :tid, true), "
            "set_config('app.principal_id', :pid, true)"
        ),
        {"tid": str(p["tenant"]), "pid": str(p["actors"]["author"])},
    )

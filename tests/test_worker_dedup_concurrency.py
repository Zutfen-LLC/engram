# ruff: noqa: E501
"""Executable app-role proof for P0-FIX-004C2: worker DEDUP serialization.

Proves that the worker's ``handle_conflict_check`` DEDUP branch
(``ConflictAction.DEDUP``) serializes safely against concurrent human review,
human verification, and reciprocal dedup jobs. Covers the eight required proofs:

  1. worker wins before human review — worker holds the canonical pair locks,
     a Bearer-authenticated human review waits behind them, worker commits
     exactly one rejection and one event, the original is unchanged, the human
     review resumes against the committed rejected state and returns its
     canonical terminal/ineligible response, no stale human event is written.
  2. human review wins first — a Bearer review (proposed→active activation,
     dispute, or rejection/archival) owns the newer item lock; the worker waits,
     observes human governance, and performs no rejection or event.
  3. human verification wins first — a Bearer verification owns the newer item
     lock; the worker waits, observes ``human_verified``, performs no
     rejection/invalidation, writes no dedup event, and verifier attribution is
     unchanged.
  4. worker wins before verification — worker owns the pair locks, verification
     waits, worker commits the rejection atomically, verification resumes and
     returns its canonical terminal/ineligible response, the rejected item is
     not human-verified afterward, exactly one automation event exists.
  5. original becomes ineligible first — a Bearer review transition moves the
     detected original away from active before the worker locks; the worker
     observes the original is no longer eligible and rejects neither item.
  6. idempotent rerun — the same dedup job run again is event-free and
     state-free (review_status stays rejected, valid_to unchanged, exactly one
     dedup event, original unchanged).
  7. reciprocal dedup stress — concurrent dedup jobs on opposite sides of the
     same pair complete within an explicit timeout with no deadlock; canonical
     pair order is used; creation ordering allows only the correct newer item to
     be rejected; the older/original remains active and unchanged; exactly one
     effective rejection and event occur; reversing task order is invariant.
  8. rollback atomicity — a PostgreSQL failure injected during event creation
     after the guarded update rolls back the rejection (valid_to unchanged, no
     event persists, original unchanged); after removing the injection the next
     normal run succeeds once.

The production worker handler is invoked directly over an independent app-role
session with FORCE RLS. Conflict detection is deterministically controlled by
monkeypatching ``engram.conflicts.detect_conflicts`` so the tests exercise the
production mutation branch without a live embedding provider. Human requests use
real Bearer credentials through ASGI. Synchronization is deterministic and
database-level: a test-only trigger calls ``pg_advisory_xact_lock`` at the
worker's guarded rejection write point, and the coordinator holds the matching
advisory lock to pause the worker while the human request runs. Overlap is
proven via PostgreSQL's blocker graph (``pg_blocking_pids``), not sleeps. The
owner connection is used only to arrange state, install triggers, and inspect
committed state.

Requires a live PostgreSQL with the v2 schema and the non-owner application
role; skips automatically when unreachable.

Scope: only the DEDUP branch (``ConflictAction.DEDUP``). ``AUTO_SUPERSEDE`` and
the flagging actions closed by P0-FIX-004C1/004C1A are intentionally out of
scope and untouched.
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

import engram.conflicts as conflicts_mod
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
from engram.jobs import enqueue_job
from engram.models import Job, MemoryItem
from engram.worker import handle_conflict_check, process_one_job

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
    # Actors: an agent author writes active items (the duplicate pair); a human
    # user reviews, verifies, and disputes. ``user_review`` holds both
    # ``write`` and ``review`` so it can dispute (write) and activate/reject
    # (review); verification is human-only (user/admin) and needs ``review``.
    actors = {
        "author": (uuid.uuid4(), "agent", [], None),
        "user_review": (uuid.uuid4(), "user", ["write", "review"], None),
    }
    keys: dict[str, str] = {}
    async with owner.begin() as conn:
        await conn.execute(
            text("INSERT INTO tenants (id,name,slug) VALUES (:id,:n,:n)"),
            {"id": tenant, "n": f"worker-dedup-{tag}"},
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
                        "label": f"worker-dedup-{tag}-{name}",
                    },
                )

    author_id = actors["author"][0]
    old = datetime.now(UTC).replace(microsecond=0)

    async def insert_item(
        *,
        content: str,
        review_status: str = "active",
        created_at: datetime | None = None,
        human_verified: bool = False,
        verified_by: uuid.UUID | None = None,
    ) -> uuid.UUID:
        item_id = uuid.uuid4()
        cts = created_at if created_at is not None else old
        async with owner.begin() as conn:
            cols = (
                "id,tenant_id,principal_id,content,content_hash,kind,visibility,"
                "review_status,memory_confidence,source_trust,importance,source_type,"
                "created_at,valid_from"
            )
            vals = (
                ":id,:tid,:pid,:content,:hash,'fact','tenant',:review_status,"
                ".81,.73,.64,'manual',:created,:created"
            )
            params: dict[str, Any] = {
                "id": item_id,
                "tid": tenant,
                "pid": author_id,
                "content": f"{tag}:{content}",
                "hash": f"sha256:{item_id.hex}",
                "review_status": review_status,
                "created": cts,
            }
            extra_cols = ""
            extra_vals = ""
            if human_verified:
                extra_cols += ",human_verified,verified_by"
                extra_vals += ",TRUE,:vby"
                params["vby"] = verified_by
            await conn.execute(
                text(
                    f"INSERT INTO memory_items ({cols}{extra_cols}) VALUES ({vals}{extra_vals})"
                ),
                params,
            )
        return item_id

    async def add_ready_embedding(item_id: uuid.UUID) -> None:
        """Attach a ready embedding so the profile lookup inside the handler succeeds."""
        from engram.db import apply_rls_context as _apply
        from engram.models import MemoryEmbedding

        async with owner_factory() as session:
            await _apply(session, tenant_id=str(tenant), principal_id=str(author_id))
            profile = (
                (
                    await session.execute(
                        text("SELECT * FROM embedding_profiles WHERE state='active' LIMIT 1")
                    )
                )
                .mappings()
                .first()
            )
            if profile is None:
                pytest.skip("no active embedding profile seeded")
            dim = int(profile["dimensions"])
            session.add(
                MemoryEmbedding(
                    memory_item_id=item_id,
                    tenant_id=tenant,
                    embedding_model=profile["model"],
                    embedding_dim=dim,
                    embedding=[1.0] + [0.0] * (dim - 1),
                    embedding_status="ready",
                )
            )
            await session.commit()

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

    # A deterministic ConflictResult the worker will see as the detection output.
    def force_detection(
        *, existing_item_id: uuid.UUID, action: str = "dedup", conflict_type: str | None = None
    ) -> None:
        from engram.conflicts import ConflictAction, ConflictResult, ConflictVerdict

        verdict = (
            ConflictVerdict.DUPLICATE
            if action == "dedup"
            else ConflictVerdict.CONTRADICT
        )
        result = ConflictResult(
            verdict=verdict,
            action=ConflictAction(action),
            existing_item_id=existing_item_id,
            similarity=0.97,
            classifier_confidence=0.95,
            conflict_type=conflict_type,
            reason=f"forced {action}",
            provenance={"provider": "test", "mode": "forced"},
        )

        async def fake_detect(_item: MemoryItem, _session: AsyncSession, **_kw: Any) -> Any:
            return result

        monkeypatch.setattr(conflicts_mod, "detect_conflicts", fake_detect)

    async def run_conflict_check(item_id: uuid.UUID) -> None:
        """Invoke the production worker handler over an app-role session.

        Builds a real Job row (so provenance/tenant routing match production),
        then calls ``handle_conflict_check`` directly — the same function
        ``process_one_job`` dispatches to. Uses a fresh app-role session with
        FORCE RLS scoped to the job's tenant.
        """
        async with app_factory() as session:
            await apply_rls_context(session, tenant_id=str(tenant), principal_id=str(author_id))
            job = Job(
                id=uuid.uuid4(),
                tenant_id=tenant,
                job_type="conflict.check",
                payload={"memory_item_id": str(item_id)},
                status="running",
                attempts=0,
                max_attempts=1,
            )
            session.add(job)
            await session.commit()
            job_id = job.id
        # Re-open a fresh session for the handler (mirrors process_one_job: the
        # job is claimed under one session, the handler runs under another).
        async with app_factory() as session:
            await apply_rls_context(session, tenant_id=str(tenant), principal_id=str(author_id))
            from engram.models import Job as JobModel

            job_obj = await session.get(JobModel, job_id)
            assert job_obj is not None
            await handle_conflict_check(session, job_obj)

    async def run_conflict_check_via_process(item_id: uuid.UUID) -> None:
        """Enqueue + process a conflict.check job through the production worker loop."""
        async with owner_factory() as session:
            await enqueue_job(
                session,
                tenant_id=str(tenant),
                job_type="conflict.check",
                payload={"memory_item_id": str(item_id)},
            )
        await process_one_job(
            worker_id="test",
            session_factory=owner_factory,
            app_session_factory=app_factory,
            job_types=["conflict.check"],
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
        "owner_factory": owner_factory,
        "app_factory": app_factory,
        "tenant": tenant,
        "author_id": author_id,
        "actors": {k: v[0] for k, v in actors.items()},
        "keys": keys,
        "insert_item": insert_item,
        "add_ready_embedding": add_ready_embedding,
        "state": state,
        "force_detection": force_detection,
        "run_conflict_check": run_conflict_check,
        "run_conflict_check_via_process": run_conflict_check_via_process,
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
                text(f"DROP TRIGGER IF EXISTS worker_dedup_pause_{tag} ON memory_items")
            )
            await conn.execute(text(f"DROP FUNCTION IF EXISTS worker_dedup_pause_{tag}()"))
            await conn.execute(
                text(f"DROP TRIGGER IF EXISTS worker_dedup_event_fail_{tag} ON item_events")
            )
            await conn.execute(text(f"DROP FUNCTION IF EXISTS worker_dedup_event_fail_{tag}()"))
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


async def _review(
    p: dict[str, Any], actor: str, item_id: uuid.UUID, status: str, **body: Any
) -> Any:
    return await p["client"].post(
        f"/v1/items/{item_id}/review",
        json={"review_status": status, **body},
        headers=_headers(p, actor),
    )


async def _verify(p: dict[str, Any], actor: str, item_id: uuid.UUID, **body: Any) -> Any:
    return await p["client"].post(
        f"/v1/items/{item_id}/verify",
        json={**body} if body else None,
        headers=_headers(p, actor),
    )


async def _await_blocked_on(
    coordinator: Any, coordinator_pid: int, expected: int, *, wait_event: str = "advisory"
) -> None:
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


async def _install_worker_pause_trigger(p: dict[str, Any], *, item_id: uuid.UUID) -> None:
    """Pause the worker's guarded rejection UPDATE on ``item_id`` via an advisory lock.

    The trigger fires BEFORE UPDATE OF valid_to on the target row (the guarded
    rejection sets valid_to from NULL to the rejection timestamp). It takes the
    advisory lock the coordinator holds, so the worker holds the pair locks but
    does not commit.
    """
    trigger = f"worker_dedup_pause_{p['tag']}"
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


async def _drop_worker_pause_trigger(p: dict[str, Any]) -> None:
    trigger = f"worker_dedup_pause_{p['tag']}"
    async with p["owner"].begin() as conn:
        await conn.execute(text(f"DROP TRIGGER IF EXISTS {trigger} ON memory_items"))
        await conn.execute(text(f"DROP FUNCTION IF EXISTS {trigger}()"))


async def _install_counterpart_review_pause_trigger(
    p: dict[str, Any], *, counterpart_id: uuid.UUID
) -> str:
    """Pause a human review's UPDATE on the counterpart row via an advisory lock."""
    trigger = f"worker_dedup_pause_{p['tag']}"
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
                f"AND OLD.id = '{counterpart_id}') "
                f"EXECUTE FUNCTION {trigger}()"
            )
        )
    return trigger


async def _install_verify_pause_trigger(
    p: dict[str, Any], *, item_id: uuid.UUID
) -> str:
    """Pause a human verification's UPDATE on the item row via an advisory lock.

    The verify endpoint takes SELECT ... FOR UPDATE on the row (via
    ``_require_eligible_item(for_update=True)``) before its UPDATE of
    human_verified runs, so the row lock is held while the trigger blocks.
    """
    trigger = f"worker_dedup_pause_{p['tag']}"
    async with p["owner"].begin() as conn:
        await conn.execute(
            text(
                f"CREATE FUNCTION {trigger}() RETURNS trigger LANGUAGE plpgsql AS $$ "
                f"BEGIN PERFORM pg_advisory_xact_lock({p['pause_key']}); RETURN NEW; END $$"
            )
        )
        await conn.execute(
            text(
                f"CREATE TRIGGER {trigger} BEFORE UPDATE OF human_verified "
                f"ON memory_items FOR EACH ROW WHEN (OLD.tenant_id = '{p['tenant']}' "
                f"AND OLD.id = '{item_id}') "
                f"EXECUTE FUNCTION {trigger}()"
            )
        )
    return trigger


async def _drop_trigger(p: dict[str, Any], trigger: str) -> None:
    async with p["owner"].begin() as conn:
        await conn.execute(text(f"DROP TRIGGER IF EXISTS {trigger} ON memory_items"))
        await conn.execute(text(f"DROP FUNCTION IF EXISTS {trigger}()"))


async def _worker_pid_blocked_on_coordinator(
    coordinator: Any, coordinator_pid: int
) -> int:
    row = (
        await coordinator.execute(
            text(
                "SELECT pid FROM pg_stat_activity"
                " WHERE :coordinator_pid = ANY(pg_blocking_pids(pid))"
                " AND wait_event_type = 'Lock' AND wait_event = 'advisory'"
            ),
            {"coordinator_pid": coordinator_pid},
        )
    ).first()
    assert row is not None, "worker did not reach the paused UPDATE"
    return int(row[0])


def _dedup_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        e
        for e in events
        if e["event_type"] == "conflict_detected" and e["field_name"] == "review_status"
    ]


# ===========================================================================
# 1. Worker wins before human review
# ===========================================================================


async def test_worker_wins_before_human_review(proof: dict[str, Any]) -> None:
    """The worker owns both pair locks (paused at its guarded rejection UPDATE).
    A Bearer-authenticated human review request on the newer item starts during
    this window and waits behind the worker's row lock. The worker commits
    exactly one rejection + one event; the original is unchanged. The human
    review resumes against the committed rejected state and returns its
    canonical terminal/ineligible response (a transition out of 'rejected' that
    is structurally invalid is 409; same-state is a no-op 200). No stale human
    event is written. Final state and event history form a valid serial order."""
    p = proof
    old_item = await p["insert_item"](
        content="dedup-worker-wins-old", created_at=datetime(2026, 1, 1, tzinfo=UTC)
    )
    new_item = await p["insert_item"](
        content="dedup-worker-wins-new", created_at=datetime(2026, 1, 2, tzinfo=UTC)
    )
    for iid in (old_item, new_item):
        await p["add_ready_embedding"](iid)
    p["force_detection"](existing_item_id=old_item)

    await _install_worker_pause_trigger(p, item_id=new_item)
    coordinator = await p["owner"].connect()
    worker_task: asyncio.Task[None] | None = None
    review_task: asyncio.Task[Any] | None = None
    try:
        coordinator_pid = await coordinator.scalar(text("SELECT pg_backend_pid()"))
        await coordinator.execute(text("SELECT pg_advisory_lock(:key)"), {"key": p["pause_key"]})

        worker_task = asyncio.create_task(p["run_conflict_check"](new_item))
        # Worker reaches its paused guarded rejection holding the pair locks.
        await _await_blocked_on(coordinator, coordinator_pid, 1)
        worker_pid = await _worker_pid_blocked_on_coordinator(coordinator, coordinator_pid)

        async def submit_review() -> Any:
            async with AsyncClient(
                transport=ASGITransport(app=create_app()), base_url="http://test"
            ) as client:
                return await client.post(
                    f"/v1/items/{new_item}/review",
                    json={"review_status": "disputed", "reason": "review after worker reject"},
                    headers=_headers(p, "user_review"),
                )

        review_task = asyncio.create_task(submit_review())
        # The human review waits behind the worker's pair lock on the newer item.
        await _await_blocked_on_pid(coordinator, worker_pid, 1)
        assert not review_task.done(), "review must wait behind worker"

        # Release: worker commits the rejection atomically (one event).
        await coordinator.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": p["pause_key"]})
        await asyncio.wait_for(worker_task, timeout=10)

        # Review resumes from the committed rejected state: rejected -> disputed
        # is structurally invalid (409). This is the route's canonical
        # terminal/ineligible response for a transition out of 'rejected'.
        review_resp = await asyncio.wait_for(review_task, timeout=10)
        assert review_resp.status_code == 409, review_resp.text
    finally:
        for task in (worker_task, review_task):
            if task is not None and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        if coordinator.in_transaction():
            await coordinator.rollback()
        await coordinator.execute(text("SELECT pg_advisory_unlock_all()"))
        await coordinator.close()
        await _drop_worker_pause_trigger(p)

    # Worker committed exactly one rejection + one event.
    st_new = await p["state"](new_item)
    st_old = await p["state"](old_item)
    assert st_new["item"]["review_status"] == "rejected"
    assert st_new["item"]["valid_to"] is not None
    cd = _dedup_events(st_new["events"])
    assert len(cd) == 1, [dict(e) for e in st_new["events"]]
    assert cd[0]["old_value"] == "active"
    assert cd[0]["new_value"] == "rejected"
    # The original is unchanged.
    assert st_old["item"]["review_status"] == "active"
    assert st_old["item"]["valid_to"] is None
    assert st_old["item"]["superseded_by"] is None
    assert _dedup_events(st_old["events"]) == []
    # No stale human review event was written (the 409 wrote nothing).
    review_events = [
        e for e in st_new["events"] if e["event_type"] == "review_change"
    ]
    assert review_events == []


# ===========================================================================
# 2. Human review wins first (activation, dispute, rejection, archival)
# ===========================================================================


@pytest.mark.parametrize(
    "requested,expected_status",
    [
        ("active", 200),  # proposed -> active activation (human governance)
        ("disputed", 200),  # active -> dispute (human governance)
        ("rejected", 200),  # active -> rejected (terminal human decision)
        ("archived", 200),  # active -> archived (terminal human decision)
    ],
)
async def test_human_review_wins_first_worker_does_not_reject(
    proof: dict[str, Any], requested: str, expected_status: int
) -> None:
    """A Bearer-authenticated human review owns the newer item lock (paused at
    its UPDATE). The worker dedup job starts, waits behind the human-held lock,
    the human review commits, the worker reloads the current state and observes
    human governance (a committed human review_change event from a user actor,
    or a terminal state), and performs no rejection or invalidation — no dedup
    event is written. The human state and event remain authoritative. Where a
    terminal human decision already matches 'rejected', the worker does not add
    a second automated rejection event or replace the timestamp."""
    p = proof
    # Start the newer item from 'proposed' for the activation case, 'active'
    # for dispute/rejection/archival so the requested transition is valid.
    start_status = "proposed" if requested == "active" else "active"
    old_item = await p["insert_item"](
        content=f"dedup-human-review-{requested}-old",
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    new_item = await p["insert_item"](
        content=f"dedup-human-review-{requested}-new",
        created_at=datetime(2026, 1, 2, tzinfo=UTC),
        review_status=start_status,
    )
    for iid in (old_item, new_item):
        await p["add_ready_embedding"](iid)
    p["force_detection"](existing_item_id=old_item)

    trigger = await _install_counterpart_review_pause_trigger(p, counterpart_id=new_item)
    coordinator = await p["owner"].connect()
    review_task: asyncio.Task[Any] | None = None
    worker_task: asyncio.Task[None] | None = None
    try:
        coordinator_pid = await coordinator.scalar(text("SELECT pg_backend_pid()"))
        await coordinator.execute(text("SELECT pg_advisory_lock(:key)"), {"key": p["pause_key"]})

        async def submit_review() -> Any:
            async with AsyncClient(
                transport=ASGITransport(app=create_app()), base_url="http://test"
            ) as client:
                return await client.post(
                    f"/v1/items/{new_item}/review",
                    json={"review_status": requested, "reason": "human first"},
                    headers=_headers(p, "user_review"),
                )

        review_task = asyncio.create_task(submit_review())
        # The human review reaches its paused UPDATE, holding the newer item's
        # FOR UPDATE row lock.
        await _await_blocked_on(coordinator, coordinator_pid, 1)
        reviewer_pid = await _worker_pid_blocked_on_coordinator(coordinator, coordinator_pid)

        # Worker runs while the human owns the newer item lock; it waits for the
        # pair lock (which includes the newer item row).
        worker_task = asyncio.create_task(p["run_conflict_check"](new_item))
        await _await_blocked_on_pid(coordinator, reviewer_pid, 1)
        assert not worker_task.done(), "worker must wait behind human review"

        # Release: human review commits first.
        await coordinator.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": p["pause_key"]})
        review_resp = await asyncio.wait_for(review_task, timeout=10)
        assert review_resp.status_code == expected_status, review_resp.text
        # Worker resumes, observes human governance, does not reject.
        await asyncio.wait_for(worker_task, timeout=10)
    finally:
        for task in (worker_task, review_task):
            if task is not None and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        if coordinator.in_transaction():
            await coordinator.rollback()
        await coordinator.execute(text("SELECT pg_advisory_unlock_all()"))
        await coordinator.close()
        await _drop_trigger(p, trigger)

    st_new = await p["state"](new_item)
    st_old = await p["state"](old_item)
    # The worker did not reject or invalidate the newer item.
    assert st_new["item"]["valid_to"] is None, st_new["item"]
    assert st_new["item"]["review_status"] == requested, st_new["item"]
    # No automation dedup event was written.
    cd = _dedup_events(st_new["events"])
    assert cd == [], [dict(e) for e in st_new["events"]]
    # The human review event remains authoritative.
    review_events = [
        e
        for e in st_new["events"]
        if e["event_type"] == "review_change" and e["field_name"] == "review_status"
    ]
    assert len(review_events) == 1
    assert review_events[0]["new_value"] == requested
    assert review_events[0]["actor_principal_id"] == str(p["actors"]["user_review"])
    # The original is unchanged.
    assert st_old["item"]["review_status"] == "active"
    assert st_old["item"]["valid_to"] is None


# ===========================================================================
# 3. Human verification wins first
# ===========================================================================


async def test_human_verification_wins_first_worker_does_not_reject(
    proof: dict[str, Any],
) -> None:
    """A Bearer-authenticated human verification owns the newer item lock
    (paused at its UPDATE of human_verified). The worker dedup job starts,
    waits behind the human-held lock, the verification commits, the worker
    reloads the current state and observes ``human_verified = TRUE``, performs
    no rejection or invalidation, writes no dedup event, and verifier
    attribution remains unchanged."""
    p = proof
    old_item = await p["insert_item"](
        content="dedup-verify-first-old", created_at=datetime(2026, 1, 1, tzinfo=UTC)
    )
    new_item = await p["insert_item"](
        content="dedup-verify-first-new", created_at=datetime(2026, 1, 2, tzinfo=UTC)
    )
    for iid in (old_item, new_item):
        await p["add_ready_embedding"](iid)
    p["force_detection"](existing_item_id=old_item)

    trigger = await _install_verify_pause_trigger(p, item_id=new_item)
    coordinator = await p["owner"].connect()
    verify_task: asyncio.Task[Any] | None = None
    worker_task: asyncio.Task[None] | None = None
    try:
        coordinator_pid = await coordinator.scalar(text("SELECT pg_backend_pid()"))
        await coordinator.execute(text("SELECT pg_advisory_lock(:key)"), {"key": p["pause_key"]})

        async def submit_verify() -> Any:
            async with AsyncClient(
                transport=ASGITransport(app=create_app()), base_url="http://test"
            ) as client:
                return await client.post(
                    f"/v1/items/{new_item}/verify",
                    json={"reason": "human verify first"},
                    headers=_headers(p, "user_review"),
                )

        verify_task = asyncio.create_task(submit_verify())
        # The verification reaches its paused UPDATE, holding the newer item's
        # FOR UPDATE row lock.
        await _await_blocked_on(coordinator, coordinator_pid, 1)
        verifier_pid = await _worker_pid_blocked_on_coordinator(coordinator, coordinator_pid)

        # Worker runs while the human owns the newer item lock; it waits.
        worker_task = asyncio.create_task(p["run_conflict_check"](new_item))
        await _await_blocked_on_pid(coordinator, verifier_pid, 1)
        assert not worker_task.done(), "worker must wait behind verification"

        # Release: verification commits first.
        await coordinator.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": p["pause_key"]})
        verify_resp = await asyncio.wait_for(verify_task, timeout=10)
        assert verify_resp.status_code == 200, verify_resp.text
        # Worker resumes, observes human_verified, does not reject.
        await asyncio.wait_for(worker_task, timeout=10)
    finally:
        for task in (worker_task, verify_task):
            if task is not None and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        if coordinator.in_transaction():
            await coordinator.rollback()
        await coordinator.execute(text("SELECT pg_advisory_unlock_all()"))
        await coordinator.close()
        await _drop_trigger(p, trigger)

    st_new = await p["state"](new_item)
    st_old = await p["state"](old_item)
    # The worker did not reject or invalidate.
    assert st_new["item"]["review_status"] == "active"
    assert st_new["item"]["valid_to"] is None
    # human_verified remains TRUE with verifier attribution unchanged.
    assert st_new["item"]["human_verified"] is True
    assert str(st_new["item"]["verified_by"]) == str(p["actors"]["user_review"])
    # No automation dedup event.
    cd = _dedup_events(st_new["events"])
    assert cd == [], [dict(e) for e in st_new["events"]]
    # The verify event remains.
    verify_events = [e for e in st_new["events"] if e["event_type"] == "verify"]
    assert len(verify_events) == 1
    assert verify_events[0]["actor_principal_id"] == str(p["actors"]["user_review"])
    # Original unchanged.
    assert st_old["item"]["review_status"] == "active"
    assert st_old["item"]["valid_to"] is None


# ===========================================================================
# 4. Worker wins before verification
# ===========================================================================


async def test_worker_wins_before_verification(proof: dict[str, Any]) -> None:
    """The worker owns both pair locks (paused at its guarded rejection). A
    Bearer-authenticated human verification starts during the window, waits
    behind the worker's lock, the worker commits the rejection atomically, the
    verification resumes and returns its canonical terminal/ineligible response
    (a rejected item is terminal and cannot be human-verified -> 409). The
    rejected item is not human-verified afterward; exactly one automation event
    exists."""
    p = proof
    old_item = await p["insert_item"](
        content="dedup-worker-before-verify-old", created_at=datetime(2026, 1, 1, tzinfo=UTC)
    )
    new_item = await p["insert_item"](
        content="dedup-worker-before-verify-new", created_at=datetime(2026, 1, 2, tzinfo=UTC)
    )
    for iid in (old_item, new_item):
        await p["add_ready_embedding"](iid)
    p["force_detection"](existing_item_id=old_item)

    await _install_worker_pause_trigger(p, item_id=new_item)
    coordinator = await p["owner"].connect()
    worker_task: asyncio.Task[None] | None = None
    verify_task: asyncio.Task[Any] | None = None
    try:
        coordinator_pid = await coordinator.scalar(text("SELECT pg_backend_pid()"))
        await coordinator.execute(text("SELECT pg_advisory_lock(:key)"), {"key": p["pause_key"]})

        worker_task = asyncio.create_task(p["run_conflict_check"](new_item))
        await _await_blocked_on(coordinator, coordinator_pid, 1)
        worker_pid = await _worker_pid_blocked_on_coordinator(coordinator, coordinator_pid)

        async def submit_verify() -> Any:
            async with AsyncClient(
                transport=ASGITransport(app=create_app()), base_url="http://test"
            ) as client:
                return await client.post(
                    f"/v1/items/{new_item}/verify",
                    json={"reason": "verify after worker reject"},
                    headers=_headers(p, "user_review"),
                )

        verify_task = asyncio.create_task(submit_verify())
        # Verification waits behind the worker's pair lock.
        await _await_blocked_on_pid(coordinator, worker_pid, 1)
        assert not verify_task.done(), "verification must wait behind worker"

        # Release: worker commits the rejection atomically.
        await coordinator.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": p["pause_key"]})
        await asyncio.wait_for(worker_task, timeout=10)

        # Verification resumes from the committed rejected state: terminal items
        # cannot be human-verified (409) — the route's canonical response.
        verify_resp = await asyncio.wait_for(verify_task, timeout=10)
        assert verify_resp.status_code == 409, verify_resp.text
    finally:
        for task in (worker_task, verify_task):
            if task is not None and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        if coordinator.in_transaction():
            await coordinator.rollback()
        await coordinator.execute(text("SELECT pg_advisory_unlock_all()"))
        await coordinator.close()
        await _drop_worker_pause_trigger(p)

    st_new = await p["state"](new_item)
    st_old = await p["state"](old_item)
    # Worker committed the rejection.
    assert st_new["item"]["review_status"] == "rejected"
    assert st_new["item"]["valid_to"] is not None
    # The rejected item is not human-verified afterward.
    assert st_new["item"]["human_verified"] is False
    assert st_new["item"]["verified_by"] is None
    # Exactly one automation event.
    cd = _dedup_events(st_new["events"])
    assert len(cd) == 1
    assert cd[0]["old_value"] == "active"
    assert cd[0]["new_value"] == "rejected"
    # No verify event was written (the 409 wrote nothing).
    verify_events = [e for e in st_new["events"] if e["event_type"] == "verify"]
    assert verify_events == []
    # Original unchanged.
    assert st_old["item"]["review_status"] == "active"
    assert st_old["item"]["valid_to"] is None


# ===========================================================================
# 5. Original becomes ineligible first
# ===========================================================================


@pytest.mark.parametrize("review_status", ["rejected", "disputed", "archived"])
async def test_original_becomes_ineligible_first_worker_skips(
    proof: dict[str, Any], review_status: str
) -> None:
    """The detected original is transitioned away from active (via a real
    Bearer review) before the worker obtains pair locks. The worker observes
    the original is no longer eligible (review_status != 'active') and rejects
    neither item. Neither item is modified by the worker; no dedup event is
    written."""
    p = proof
    old_item = await p["insert_item"](
        content=f"dedup-orig-ineligible-{review_status}-old",
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    new_item = await p["insert_item"](
        content=f"dedup-orig-ineligible-{review_status}-new",
        created_at=datetime(2026, 1, 2, tzinfo=UTC),
    )
    for iid in (old_item, new_item):
        await p["add_ready_embedding"](iid)
    p["force_detection"](existing_item_id=old_item)

    # Transition the original away from active via a real Bearer review request.
    resp = await _review(p, "user_review", old_item, review_status, reason="original ineligible")
    assert resp.status_code == 200, resp.text

    before_new = await p["state"](new_item)
    before_old = await p["state"](old_item)
    await p["run_conflict_check"](new_item)
    after_new = await p["state"](new_item)
    after_old = await p["state"](old_item)

    # Worker rejects neither item; no dedup event.
    assert after_new["item"] == before_new["item"]
    assert after_new["events"] == before_new["events"]
    cd = _dedup_events(after_new["events"])
    assert cd == []
    assert after_new["item"]["review_status"] == "active"
    assert after_new["item"]["valid_to"] is None
    # The original keeps the human transition; worker did not touch it.
    assert after_old["item"] == before_old["item"]
    assert after_old["item"]["review_status"] == review_status


# ===========================================================================
# 6. Idempotent rerun
# ===========================================================================


async def test_idempotent_rerun_is_event_free(proof: dict[str, Any]) -> None:
    """Run the same dedup job after a successful automated rejection.
    ``review_status`` remains rejected, ``valid_to`` is unchanged, exactly one
    dedup event exists, and the original item remains unchanged."""
    p = proof
    old_item = await p["insert_item"](
        content="dedup-idempotent-old", created_at=datetime(2026, 1, 1, tzinfo=UTC)
    )
    new_item = await p["insert_item"](
        content="dedup-idempotent-new", created_at=datetime(2026, 1, 2, tzinfo=UTC)
    )
    for iid in (old_item, new_item):
        await p["add_ready_embedding"](iid)
    p["force_detection"](existing_item_id=old_item)

    await p["run_conflict_check"](new_item)
    first = await p["state"](new_item)
    assert first["item"]["review_status"] == "rejected"
    assert first["item"]["valid_to"] is not None
    cd1 = _dedup_events(first["events"])
    assert len(cd1) == 1
    first_valid_to = first["item"]["valid_to"]

    # Rerun the same dedup job.
    await p["run_conflict_check"](new_item)
    second = await p["state"](new_item)

    # review_status remains rejected; valid_to is unchanged; no duplicate event.
    assert second["item"]["review_status"] == "rejected"
    assert second["item"]["valid_to"] == first_valid_to
    assert second["events"] == first["events"]
    cd2 = _dedup_events(second["events"])
    assert len(cd2) == 1

    # Original item remains unchanged.
    st_old = await p["state"](old_item)
    assert st_old["item"]["review_status"] == "active"
    assert st_old["item"]["valid_to"] is None
    assert _dedup_events(st_old["events"]) == []


# ===========================================================================
# 7. Reciprocal dedup stress
# ===========================================================================


@pytest.mark.parametrize("iteration", range(3))
async def test_reciprocal_dedup_pair_no_deadlock(
    proof: dict[str, Any], iteration: int
) -> None:
    """Run concurrent dedup jobs for opposite sides of the same pair. Both
    complete within an explicit timeout; no deadlock; canonical pair order is
    used; creation ordering allows only the correct newer item to be rejected;
    the older/original item remains active and unchanged; exactly one effective
    rejection and event occur. Reversing task order does not change the
    invariant."""
    p = proof
    item_a = await p["insert_item"](
        content=f"dedup-reciprocal-a-{iteration}", created_at=datetime(2026, 1, 1, tzinfo=UTC)
    )
    item_b = await p["insert_item"](
        content=f"dedup-reciprocal-b-{iteration}", created_at=datetime(2026, 1, 2, tzinfo=UTC)
    )
    for iid in (item_a, item_b):
        await p["add_ready_embedding"](iid)

    # item_b is newer (created later). item_a's job detects item_b (so it would
    # be the older side and must NOT act); item_b's job detects item_a (newer
    # side, should act). Reversing request order on odd iterations proves
    # canonical pair lock ordering regardless of submission order.
    from engram.conflicts import ConflictAction, ConflictResult, ConflictVerdict

    result_a_detects_b = ConflictResult(
        verdict=ConflictVerdict.DUPLICATE,
        action=ConflictAction.DEDUP,
        existing_item_id=item_b,
        similarity=0.97,
        classifier_confidence=0.95,
        conflict_type="duplicate",
        reason="forced",
        provenance={"provider": "test"},
    )
    result_b_detects_a = ConflictResult(
        verdict=ConflictVerdict.DUPLICATE,
        action=ConflictAction.DEDUP,
        existing_item_id=item_a,
        similarity=0.97,
        classifier_confidence=0.95,
        conflict_type="duplicate",
        reason="forced",
        provenance={"provider": "test"},
    )

    original_detect = conflicts_mod.detect_conflicts

    async def detect_dispatch(item: MemoryItem, session: AsyncSession, **kw: Any) -> Any:
        if item.id == item_a:
            return result_a_detects_b
        if item.id == item_b:
            return result_b_detects_a
        return await original_detect(item, session, **kw)

    import engram.conflicts as cm

    cm.detect_conflicts = detect_dispatch  # type: ignore[assignment]  # noqa: SLF001
    try:
        jobs = [item_a, item_b] if iteration % 2 else [item_b, item_a]
        await asyncio.wait_for(
            asyncio.gather(p["run_conflict_check"](jobs[0]), p["run_conflict_check"](jobs[1])),
            timeout=20,
        )
    finally:
        cm.detect_conflicts = original_detect  # noqa: SLF001

    # Only the newer item (item_b) is rejected; the older (item_a) is untouched.
    st_a = await p["state"](item_a)
    st_b = await p["state"](item_b)
    assert st_b["item"]["review_status"] == "rejected"
    assert st_b["item"]["valid_to"] is not None
    assert st_a["item"]["review_status"] == "active"
    assert st_a["item"]["valid_to"] is None
    assert st_a["item"]["superseded_by"] is None
    # Exactly one effective rejection and event (on item_b).
    cd_b = _dedup_events(st_b["events"])
    cd_a = _dedup_events(st_a["events"])
    assert len(cd_b) == 1
    assert cd_b[0]["old_value"] == "active"
    assert cd_b[0]["new_value"] == "rejected"
    assert cd_a == []


# ===========================================================================
# 8. Rollback atomicity
# ===========================================================================


async def test_rollback_atomicity_on_event_failure(proof: dict[str, Any]) -> None:
    """Inject a PostgreSQL failure during event creation after the guarded
    rejection UPDATE attempt. The rejection rolls back: ``valid_to`` remains
    unchanged, no event persists, the original remains unchanged. After removing
    the failure injection, the next normal run succeeds once."""
    p = proof
    old_item = await p["insert_item"](
        content="dedup-rollback-old", created_at=datetime(2026, 1, 1, tzinfo=UTC)
    )
    new_item = await p["insert_item"](
        content="dedup-rollback-new", created_at=datetime(2026, 1, 2, tzinfo=UTC)
    )
    for iid in (old_item, new_item):
        await p["add_ready_embedding"](iid)
    p["force_detection"](existing_item_id=old_item)

    # Install a trigger that raises on INSERT into item_events for the newer
    # item — this fires AFTER the guarded rejection UPDATE but BEFORE the
    # transaction commits, so the whole transaction (rejection + event) rolls
    # back atomically.
    fail_fn = f"worker_dedup_event_fail_{p['tag']}"
    async with p["owner"].begin() as conn:
        await conn.execute(
            text(
                f"CREATE FUNCTION {fail_fn}() RETURNS trigger LANGUAGE plpgsql AS $$ "
                f"BEGIN RAISE EXCEPTION 'injected event failure for dedup rollback'; "
                f"RETURN NEW; END $$"
            )
        )
        await conn.execute(
            text(
                f"CREATE TRIGGER {fail_fn} BEFORE INSERT ON item_events "
                f"FOR EACH ROW WHEN (NEW.item_id = '{new_item}'::uuid "
                f"AND NEW.event_type = 'conflict_detected') "
                f"EXECUTE FUNCTION {fail_fn}()"
            )
        )

    before_new = await p["state"](new_item)
    before_old = await p["state"](old_item)
    assert before_new["item"]["valid_to"] is None

    # The worker run must raise (the event INSERT fails); the rejection rolls back.
    with pytest.raises(Exception, match="injected event failure for dedup rollback"):
        await p["run_conflict_check"](new_item)

    after_new = await p["state"](new_item)
    after_old = await p["state"](old_item)
    # valid_to remains unchanged; no event persists.
    assert after_new["item"]["valid_to"] is None, after_new["item"]
    assert after_new["item"]["review_status"] == "active"
    assert _dedup_events(after_new["events"]) == []
    # Original remains unchanged.
    assert after_old["item"] == before_old["item"]

    # Remove the failure injection.
    async with p["owner"].begin() as conn:
        await conn.execute(text(f"DROP TRIGGER IF EXISTS {fail_fn} ON item_events"))
        await conn.execute(text(f"DROP FUNCTION IF EXISTS {fail_fn}()"))

    # The next normal run succeeds once.
    await p["run_conflict_check"](new_item)
    final = await p["state"](new_item)
    assert final["item"]["review_status"] == "rejected"
    assert final["item"]["valid_to"] is not None
    cd = _dedup_events(final["events"])
    assert len(cd) == 1
    assert cd[0]["old_value"] == "active"
    assert cd[0]["new_value"] == "rejected"
    # Original remains unchanged.
    final_old = await p["state"](old_item)
    assert final_old["item"]["review_status"] == "active"
    assert final_old["item"]["valid_to"] is None
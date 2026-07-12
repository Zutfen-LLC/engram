"""Background job worker (ENG-AUD-008).

Polls the ``jobs`` table and runs handlers off the request path:
``embedding.generate``, ``conflict.check``, ``classification.refine``,
``promotion.path_a``, and ``retention.sweep``.

RLS posture (hybrid, per ENG-AUD-008):

* **Claim / lock / retry / dead bookkeeping** runs through the **owner** session
  (``session_factory`` → ``engram.db.owner_session_factory``). The owner role
  bypasses RLS, which is required for a globally fair cross-tenant
  ``FOR UPDATE SKIP LOCKED`` claim. This is queue coordination, not tenant data
  access.
* **Payload processing** runs through a fresh **app-role** session
  (``app_session_factory`` → ``engram.db.async_session_factory``) with
  ``apply_rls_context`` set to the job's tenant and the tenant's seeded
  ``admin`` principal. So the actual memory mutations are RLS-enforced.
* The job's ``tenant_id`` is treated as *routing context*, not proof of
  authorization: every handler re-loads the target row under the app-role
  session and confirms the row's ``tenant_id`` matches before mutating.

The worker exits nonzero only on fatal setup errors — an ordinary job failure
retries (and eventually dead-letters) without taking the process down.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID

from sqlalchemy import insert, or_, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from engram.config import settings
from engram.db import _DEFAULT_PRINCIPAL_NAME, apply_rls_context
from engram.internal_actors import (
    CLASSIFICATION_AUTOMATION_INTERNAL_KEY,
    CONFLICT_AUTOMATION_INTERNAL_KEY,
    resolve_internal_system_actor,
)
from engram.jobs import (
    claim_next_job,
    mark_job_failed_or_retry,
    mark_job_succeeded,
)
from engram.models import ItemEvent, Job, MemoryItem, Principal

if TYPE_CHECKING:
    from engram.conflicts import ConflictAction, ConflictResult

logger = logging.getLogger(__name__)

# A handler runs entirely under an app-role session scoped to the job's tenant.
JobHandler = Callable[[AsyncSession, Job], Awaitable[None]]

_LAST_ERROR_TRUNC = 4000


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _truncate_error(exc: BaseException | str) -> str:
    msg = str(exc)
    return msg if len(msg) <= _LAST_ERROR_TRUNC else msg[:_LAST_ERROR_TRUNC]


async def _resolve_tenant_admin(owner_session: AsyncSession, tenant_id: str) -> str:
    """Resolve a tenant's seeded admin principal id for RLS context + audit.

    Runs through the owner session (cross-tenant lookup). Falls back to the
    first principal in the tenant if the seeded admin name is absent.
    """
    row = (
        await owner_session.execute(
            select(Principal.id).where(
                Principal.tenant_id == tenant_id,
                Principal.name == _DEFAULT_PRINCIPAL_NAME,
            )
        )
    ).scalar_one_or_none()
    if row is not None:
        return str(row)
    fallback = (
        await owner_session.execute(
            select(Principal.id)
            .where(Principal.tenant_id == tenant_id)
            .order_by(Principal.created_at.asc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if fallback is None:
        raise RuntimeError(f"no principal found for tenant {tenant_id}")
    return str(fallback)


async def _insert_event(
    session: AsyncSession,
    *,
    item_id: UUID | str,
    event_type: str,
    field_name: str | None,
    old_value: Any,
    new_value: Any,
    actor_principal_id: UUID | str | None,
    reason: str | None,
) -> None:
    """Write an item_events audit row (mirrors the PATCH path's helper)."""
    await session.execute(
        insert(ItemEvent).values(
            id=uuid.uuid4(),
            item_id=item_id,
            event_type=event_type,
            field_name=field_name,
            old_value=_stringify(old_value),
            new_value=_stringify(new_value),
            actor_principal_id=actor_principal_id,
            reason=reason,
            created_at=_utcnow(),
        )
    )


def _stringify(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    import json

    try:
        return json.dumps(value, sort_keys=True, default=str)
    except (TypeError, ValueError):
        return str(value)


def _parse_uuid(value: object) -> UUID:
    """Parse a payload value to UUID; raises ValueError on bad input."""
    if isinstance(value, UUID):
        return value
    if isinstance(value, str):
        return UUID(value)
    raise ValueError(f"expected a UUID, got {type(value).__name__}")


def _payload_item_id(job: Job) -> UUID:
    raw = job.payload.get("memory_item_id")
    if raw is None:
        raise ValueError("payload missing memory_item_id")
    return _parse_uuid(raw)


# ---------------------------------------------------------------------------
# Handlers — each runs under an app-role session scoped to the job's tenant.
# They re-load the target row and confirm tenant_id matches before mutating.
# ---------------------------------------------------------------------------


async def _reload_item(session: AsyncSession, item_id: UUID) -> MemoryItem | None:
    return (
        await session.execute(select(MemoryItem).where(MemoryItem.id == item_id))
    ).scalar_one_or_none()


def _is_expired_or_inactive(item: MemoryItem) -> bool:
    """Skip jobs for items that should no longer be enriched.

    A memory is effectively gone from the active set when it is rejected,
    invalidated, superseded, or archived. Refining/enriching such an item has no
    observable effect and could even resurrect stale state, so handlers skip.
    """
    if item.valid_to is not None:
        return True
    if item.review_status == "rejected":
        return True
    if item.review_status == "archived":
        return True
    return item.superseded_by is not None


async def handle_embedding_generate(session: AsyncSession, job: Job) -> None:
    """Generate and store the embedding for a memory item.

    Idempotent: if the embedding row is already ready, succeed without a provider
    call. On provider failure the row is marked failed and the job retries (then
    dead-letters). After a ready embedding, if ``conflict_check_on_write`` is
    enabled, enqueues a ``conflict.check`` job so semantic dedup/auto-supersede
    runs off the write path.
    """
    # Lazy imports keep the module importable when the openai package is absent.
    import inspect

    from engram.embedding_profiles import get_active_profile, get_profile_by_id
    from engram.embeddings import STATUS_FAILED, STATUS_READY, generate_embedding
    from engram.models import MemoryEmbedding

    item_id = _payload_item_id(job)
    raw_profile_id = job.payload.get("profile_id")
    profile = (
        await get_profile_by_id(session, _parse_uuid(raw_profile_id))
        if raw_profile_id is not None
        else await get_active_profile(session)
    )
    payload_key = job.payload.get("profile_key")
    if payload_key is not None and str(payload_key) != profile.profile_key:
        raise ValueError("embedding job profile_id/profile_key mismatch")
    item = await _reload_item(session, item_id)
    if item is None or _is_expired_or_inactive(item):
        logger.info("embedding.generate id=%s skipped: item gone/inactive", item_id)
        return
    # Re-verify tenant routing context.
    if str(item.tenant_id) != str(job.tenant_id):
        raise RuntimeError(f"job tenant {job.tenant_id} != item tenant {item.tenant_id}")

    emb = (
        await session.execute(
            select(MemoryEmbedding).where(
                MemoryEmbedding.memory_item_id == item_id,
                MemoryEmbedding.profile_id == profile.id,
            )
        )
    ).scalar_one_or_none()
    if emb is not None and emb.embedding_status == STATUS_READY:
        logger.info("embedding.generate id=%s already ready, no-op", item_id)
        return

    try:
        # Compatibility with one-argument test/provider shims while the public
        # implementation receives the durable profile contract explicitly.
        if len(inspect.signature(generate_embedding).parameters) >= 2:
            vector = await generate_embedding(item.content, profile)
        else:
            vector = await generate_embedding(item.content)
    except Exception:
        # Provider call failed — persist the failed status (committed below)
        # before re-raising so the row records the failure even though the job
        # will retry/dead-letter. Without this commit the failed-status write
        # would be rolled back with the raising transaction.
        if emb is not None:
            emb.embedding_status = STATUS_FAILED
            await session.commit()
        raise
    if vector is None:
        # Provider disabled or returned nothing — mark failed so the job retries
        # and eventually dead-letters rather than spinning.
        if emb is not None:
            emb.embedding_status = STATUS_FAILED
            await session.commit()
        raise RuntimeError("embedding provider returned no vector")

    if emb is None:
        from engram.embeddings import create_embedding_placeholder

        emb = await create_embedding_placeholder(session, item_id, job.tenant_id, profile)
    if len(vector) != profile.dimensions:
        emb.embedding_status = STATUS_FAILED
        await session.commit()
        raise ValueError(
            f"embedding dimension {len(vector)} does not match profile "
            f"{profile.profile_key} ({profile.dimensions})"
        )
    emb.embedding = vector
    emb.embedding_model = profile.model
    emb.embedding_dim = profile.dimensions
    emb.embedding_status = STATUS_READY
    await session.commit()

    # Enqueue conflict check now that the embedding is ready.
    if settings.conflict_check_on_write and profile.state == "active":
        from engram.jobs import enqueue_job

        await enqueue_job(
            session,
            tenant_id=job.tenant_id,
            job_type="conflict.check",
            payload={
                "memory_item_id": str(item_id),
                "profile_id": str(profile.id),
                "profile_key": profile.profile_key,
            },
            dedupe_key=f"conflict:{item_id}:{profile.id}",
        )


async def handle_conflict_check(session: AsyncSession, job: Job) -> None:
    """Run embedding-dependent conflict detection off the write path.

    Applies conservative eventual *state transitions* (not the old immediate
    response semantics): semantic duplicate → reject+invalidate the new item;
    auto-supersede → supersede the old item; contradiction/scope overlap/proposed
    supersede → set conflict metadata and demote to proposed. Idempotent.
    """
    from engram.conflicts import ConflictAction, detect_conflicts
    from engram.embedding_profiles import get_active_profile, get_profile_by_id

    item_id = _payload_item_id(job)
    raw_profile_id = job.payload.get("profile_id")
    profile = (
        await get_profile_by_id(session, _parse_uuid(raw_profile_id))
        if raw_profile_id is not None
        else await get_active_profile(session)
    )
    payload_key = job.payload.get("profile_key")
    if payload_key is not None and str(payload_key) != profile.profile_key:
        raise ValueError("conflict job profile_id/profile_key mismatch")
    if profile.state != "active":
        logger.info(
            "conflict.check id=%s skipped: profile %s is %s",
            item_id,
            profile.profile_key,
            profile.state,
        )
        return
    item = await _reload_item(session, item_id)
    if item is None or _is_expired_or_inactive(item):
        logger.info("conflict.check id=%s skipped: item gone/inactive", item_id)
        return
    if str(item.tenant_id) != str(job.tenant_id):
        raise RuntimeError(f"job tenant {job.tenant_id} != item tenant {item.tenant_id}")

    result = await detect_conflicts(item, session, profile=profile)
    if result is None:
        return

    action = result.action

    # Creation-order guard: detect_conflicts finds the nearest neighbor by
    # embedding distance, not by age. conflict.check runs for EVERY item once its
    # embedding is ready, so without a guard the older item's job would find the
    # newer item as a neighbor and act on it — inverting the dedup/supersede
    # direction. Establish ordering: the job's item is the "newer" one (the
    # duplicate / superseder) when it was created after the neighbor, or — for a
    # deterministic tiebreaker when both share a created_at (e.g. same
    # transaction) — when its id sorts later. Flag actions are symmetric and
    # need no guard.
    existing_created_at = (
        await session.execute(
            select(MemoryItem.created_at).where(MemoryItem.id == result.existing_item_id)
        )
    ).scalar_one_or_none()
    if existing_created_at is None:
        await session.commit()
        return
    if item.created_at > existing_created_at:
        item_is_newer = True
    elif item.created_at == existing_created_at:
        # Same timestamp: deterministic id-based tiebreaker so exactly one of the
        # two items' jobs acts (the higher-id item is treated as "newer").
        item_is_newer = str(item.id) > str(result.existing_item_id)
    else:
        item_is_newer = False

    if action == ConflictAction.DEDUP:
        if not item_is_newer:
            # The job's item is the original; the newer neighbor's own
            # conflict.check will handle the dedup. Avoid acting twice.
            await session.commit()
            return
        # Semantic duplicate — serialize the rejection. Detection is only a
        # proposal (P0-FIX-004C2): lock both rows in canonical order, revalidate
        # mutation authority from the locked rows (including human-governance
        # precedence), perform a guarded rejection, and write the event only
        # after the transition is confirmed — all in one transaction. The
        # original item is never mutated.
        await _apply_dedup(
            session,
            job=job,
            job_item=item,
            counterpart_id=result.existing_item_id,
            result=result,
        )
        await session.commit()
        return

    if action == ConflictAction.AUTO_SUPERSEDE:
        # The NEW item supersedes the OLD neighbor. Only act when the job's item
        # is the newer one; otherwise the neighbor's own job handles it.
        if not item_is_newer:
            await session.commit()
            return
        # Supersede the OLD item with the new one. Idempotent.
        existing = await _reload_item(session, result.existing_item_id)
        if existing is None or existing.superseded_by == item.id:
            await session.commit()
            return
        actor = await resolve_internal_system_actor(
            session,
            tenant_id=job.tenant_id,
            internal_key=CONFLICT_AUTOMATION_INTERNAL_KEY,
        )
        await _insert_event(
            session,
            item_id=item.id,
            event_type="conflict_detected",
            field_name="superseded_by",
            old_value=None,
            new_value=str(result.existing_item_id),
            actor_principal_id=actor,
            reason=json.dumps(
                {
                    "action": action.value,
                    "existing_item_id": str(result.existing_item_id),
                    "reason": result.reason,
                    "worker_operation": "conflict.check",
                    "job_id": str(job.id),
                    "item_author_principal_id": str(item.principal_id),
                    "internal_actor_key": CONFLICT_AUTOMATION_INTERNAL_KEY,
                    **result.provenance,
                },
                sort_keys=True,
            ),
        )
        await session.execute(
            update(MemoryItem)
            .where(MemoryItem.id == result.existing_item_id)
            .values(superseded_by=item.id, valid_to=_utcnow())
        )
        await session.commit()
        return

    # FLAG_CONTRADICTION / PROPOSED_SUPERSEDE / FLAG_SCOPE_OVERLAP: set
    # conflict metadata and demote to proposed (only if currently active). Only
    # the NEWER item is flagged — mirroring the original write-path semantics
    # where the newly-written item is the one marked — so that flagging item1
    # (which demotes it out of the active set) doesn't make it invisible to
    # item2's conflict.check neighbor query (which requires active neighbors).
    #
    # P0-FIX-004C1: detection runs unlocked (it is expensive and only a
    # proposal). Mutation is serialized: lock BOTH rows in canonical UUID order
    # with SELECT ... FOR UPDATE, reload from the locked rows, revalidate every
    # mutation authority fact from that locked state, and perform a guarded
    # UPDATE ... RETURNING. The event is written only after the guarded update
    # confirms the transition, in the same transaction. The locked rows — not
    # the detection snapshots — are mutation authority.
    if action not in (
        ConflictAction.FLAG_CONTRADICTION,
        ConflictAction.FLAG_SCOPE_OVERLAP,
        ConflictAction.PROPOSED_SUPERSEDE,
    ):
        # Defensive: an unknown action that is not DEDUP/AUTO_SUPERSEDE/flagging
        # must not reach the legacy unguarded path.
        await session.commit()
        return
    if not item_is_newer:
        await session.commit()
        return

    await _apply_flagging(
        session,
        job=job,
        job_item=item,
        counterpart_id=result.existing_item_id,
        action=action,
        result=result,
    )
    await session.commit()


# Flagging actions serialized by this slice (P0-FIX-004C1). Stored as their
# string values so this module-level constant does not require importing the
# ``ConflictAction`` enum (which pulls in the openai dependency) at import time.
# The under-lock revalidation asserts the proposed action's value still belongs
# to this flagging family.
_FLAGGING_ACTION_VALUES: frozenset[str] = frozenset(
    {"flag_contradiction", "flag_scope_overlap", "proposed_supersede"}
)

# Review states the flagging branch may demote FROM (active -> proposed). Every
# other permitted-but-non-demoting state is preserved as-is. Terminal/inactive
# states cause the worker to skip all mutation and event creation.
_DEMOTE_PERMITTED_FROM: frozenset[str] = frozenset({"active", "proposed", "disputed"})
# Completed human conflict decisions the worker must never reopen.
_COMPLETED_DECISIONS: frozenset[str] = frozenset({"accepted", "rejected", "merged"})

# Review states the DEDUP branch may reject FROM (P0-FIX-004C2). A semantic
# duplicate may be active, proposed, or disputed when detected; terminal
# (rejected/archived) and invalidated/superseded rows are skipped. The guarded
# UPDATE re-checks this set so a concurrent transition out of it is a no-op.
_DEDUP_PERMITTED_FROM: frozenset[str] = frozenset({"active", "proposed", "disputed"})
# Authenticated principal types whose review/verification decisions constitute
# human governance and outrank later automated dedup rejection. Agents and
# ordinary system principals do NOT become human governors by holding review
# scope (see P0-FIX-004C2 locked policy).
_HUMAN_GOVERNOR_TYPES: frozenset[str] = frozenset({"user", "admin"})


async def _lock_conflict_pair(
    session: AsyncSession,
    *,
    job_item_id: UUID,
    counterpart_id: UUID,
    tenant_id: str,
) -> tuple[MemoryItem, MemoryItem] | None:
    """Lock both conflict rows in canonical UUID order (SELECT ... FOR UPDATE).

    Canonical pair locking (mirrors ``resolve_conflict`` in review.py): rows are
    locked by deterministic ``id`` ordering — independent of detection order — so
    two reciprocal worker jobs cannot deadlock. Both rows must belong to the job
    tenant; a missing row or tenant mismatch fails closed (returns ``None``).
    Self-conflicts are rejected.
    """
    if job_item_id == counterpart_id:
        return None
    pair_ids = sorted((job_item_id, counterpart_id), key=str)
    dialect_name = session.bind.dialect.name if session.bind is not None else None
    stmt = (
        select(MemoryItem)
        .where(MemoryItem.id.in_(pair_ids), MemoryItem.tenant_id == tenant_id)
        .order_by(MemoryItem.id)
    )
    if dialect_name == "postgresql":
        stmt = stmt.with_for_update()
    locked = list((await session.execute(stmt)).scalars().all())
    if len(locked) != 2:
        return None
    by_id = {row.id: row for row in locked}
    job_item = by_id.get(job_item_id)
    counterpart = by_id.get(counterpart_id)
    if job_item is None or counterpart is None:
        return None
    if str(job_item.tenant_id) != tenant_id or str(counterpart.tenant_id) != tenant_id:
        return None
    return job_item, counterpart


async def _apply_flagging(
    session: AsyncSession,
    *,
    job: Job,
    job_item: MemoryItem,
    counterpart_id: UUID,
    action: ConflictAction,
    result: ConflictResult,
) -> None:
    """Serialize the flagging mutation: lock, revalidate, guarded write, event.

    Detection snapshots (``job_item``, ``result``) are a *proposal*. The locked
    rows are mutation authority. The guard in the ``UPDATE ... RETURNING``
    re-checks every mutation authority fact, so a zero-row update is a truthful
    skip (no event, no mutation) — never permission to write a stale event.
    """
    tenant_id = str(job.tenant_id)
    locked = await _lock_conflict_pair(
        session,
        job_item_id=job_item.id,
        counterpart_id=counterpart_id,
        tenant_id=tenant_id,
    )
    if locked is None:
        return
    locked_job_item, locked_counterpart = locked

    # Under-lock revalidation. Each check below is a mutation-authority fact
    # derived from the locked rows; the detection snapshots are NOT authority.
    # The job item must still be live (valid_to IS NULL, superseded_by IS NULL,
    # not rejected/archived).
    if locked_job_item.valid_to is not None or locked_job_item.superseded_by is not None:
        return
    if locked_job_item.review_status in {"rejected", "archived"}:
        # Terminal review decision first → worker skips all mutation and event.
        return
    # The counterpart must remain live and suitable for the detected
    # relationship. Conflict detection (``detect_conflicts``) only ever
    # returns active, non-expired neighbours, so the locked counterpart
    # must still satisfy that same eligibility predicate: ``review_status
    # == 'active'``, ``valid_to IS NULL``, and ``superseded_by IS NULL``.
    # A human review transition on the counterpart (rejected / disputed /
    # archived) may land between detection and the pair lock without
    # touching ``valid_to`` or ``superseded_by`` — so the active review
    # state must be rechecked explicitly here, not inferred from those
    # columns. The counterpart row remains locked through the target
    # update and commit, so it cannot change again during this transition.
    if locked_counterpart.review_status != "active":
        return
    if locked_counterpart.valid_to is not None or locked_counterpart.superseded_by is not None:
        return
    if str(locked_counterpart.id) != str(result.existing_item_id):
        return
    # The detected action must still belong to this slice's flagging family.
    if action.value not in _FLAGGING_ACTION_VALUES:
        return
    # The job item must remain the newer side per the creation-time + UUID
    # tiebreak rule, evaluated against the locked counterpart.
    if locked_job_item.created_at > locked_counterpart.created_at:
        still_newer = True
    elif locked_job_item.created_at == locked_counterpart.created_at:
        still_newer = str(locked_job_item.id) > str(locked_counterpart.id)
    else:
        still_newer = False
    if not still_newer:
        return

    review_status = locked_job_item.review_status
    # Conflict-decision preservation: never overwrite a completed human decision.
    resolution_status = locked_job_item.conflict_resolution_status
    if resolution_status in _COMPLETED_DECISIONS:
        return
    # Review-state preservation: demote only active -> proposed. Preserve
    # proposed/disputed. Anything else not in the permitted set skips.
    if review_status not in _DEMOTE_PERMITTED_FROM:
        return
    new_review = "proposed" if review_status == "active" else review_status

    existing_counterpart = locked_job_item.conflicts_with_item_id
    existing_type = locked_job_item.conflict_type
    existing_resolution = locked_job_item.conflict_resolution_status
    target_counterpart = result.existing_item_id
    target_type = result.conflict_type

    # Existing conflict preservation (single-slot conflict model).
    if existing_counterpart is not None:
        if (
            existing_counterpart == target_counterpart
            and existing_type == target_type
            and existing_resolution == "unresolved"
            and review_status == new_review
        ):
            # Same unresolved relationship: idempotent no-op — no event, no mutation.
            return
        # A different relationship already occupies the single conflict slot.
        # Do not silently replace it. Multi-conflict support is later work.
        return

    # Guarded transition. The guard re-checks every mutation-authority fact so a
    # concurrent change between revalidation and the write is still caught.
    update_stmt = (
        update(MemoryItem)
        .where(
            MemoryItem.id == locked_job_item.id,
            MemoryItem.tenant_id == tenant_id,
            MemoryItem.valid_to.is_(None),
            MemoryItem.superseded_by.is_(None),
            MemoryItem.review_status.in_(tuple(_DEMOTE_PERMITTED_FROM)),
            or_(
                MemoryItem.conflict_resolution_status.is_(None),
                MemoryItem.conflict_resolution_status == "unresolved",
            ),
            MemoryItem.conflicts_with_item_id.is_(None),
        )
        .values(
            conflicts_with_item_id=target_counterpart,
            conflict_type=target_type,
            conflict_resolution_status="unresolved",
            review_status=new_review,
        )
        .returning(MemoryItem.id)
    )
    guard_result = await session.execute(
        update_stmt, execution_options={"synchronize_session": False}
    )
    if guard_result.scalar_one_or_none() is None:
        # The transition did not occur — no event is written. The transaction
        # commits as a no-op; a concurrent writer won the mutation authority.
        return

    actor = await resolve_internal_system_actor(
        session,
        tenant_id=tenant_id,
        internal_key=CONFLICT_AUTOMATION_INTERNAL_KEY,
    )
    payload = {
        "verdict": result.verdict.value,
        "action": action.value,
        "conflict_type": result.conflict_type,
        "similarity": result.similarity,
        "existing_item_id": str(result.existing_item_id),
        "reason": result.reason,
        "worker_operation": "conflict.check",
        "job_id": str(job.id),
        "item_author_principal_id": str(locked_job_item.principal_id),
        "internal_actor_key": CONFLICT_AUTOMATION_INTERNAL_KEY,
        **result.provenance,
    }
    await _insert_event(
        session,
        item_id=locked_job_item.id,
        event_type="conflict_detected",
        field_name="conflicts_with_item_id",
        old_value=existing_counterpart,
        new_value=str(target_counterpart),
        actor_principal_id=actor,
        reason=json.dumps(payload, sort_keys=True),
    )


async def _has_human_governance(
    session: AsyncSession,
    *,
    item: MemoryItem,
    tenant_id: str,
) -> bool:
    """True if a committed human governance decision protects ``item`` from
    automated dedup rejection (P0-FIX-004C2).

    Human governance is determined from authoritative stored provenance — never
    from caller-supplied actor fields or mutable names:

    * ``human_verified = TRUE`` on the locked row (with its authenticated
      ``verified_by`` attribution written by the verify endpoint, which only
      admits ``user``/``admin`` principals); or
    * a committed ``review_change`` event on the item whose authenticated actor
      principal is of type ``user`` or ``admin`` (the review/bulk-archive
      endpoints always set the actor to the authenticated caller). Promotion's
      ``review_change`` events use the ``review_automation`` internal system
      actor (``type='system'``) and do NOT count as human governance — agents
      and ordinary systems do not become human governors by holding review scope.

    Must be evaluated while the item's row lock is held: human review and
    verification both lock the item with ``SELECT ... FOR UPDATE`` before
    writing their events, so the shared item lock defines the serial order and
    the predicate cannot be bypassed by the concurrent human path.
    """
    if item.human_verified:
        return True
    human_event = (
        await session.execute(
            select(ItemEvent.id)
            .join(ItemEvent.item)  # memory_items row (tenant-scoped by RLS)
            .where(
                ItemEvent.item_id == item.id,
                ItemEvent.event_type == "review_change",
                ItemEvent.field_name == "review_status",
                ItemEvent.actor_principal_id.is_not(None),
                # Actor principal type must be a human governor. The join to
                # principals is tenant-scoped by RLS on principals (FORCE RLS).
                ItemEvent.actor_principal_id.in_(
                    select(Principal.id).where(
                        Principal.tenant_id == tenant_id,
                        Principal.type.in_(tuple(_HUMAN_GOVERNOR_TYPES)),
                    )
                ),
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    return human_event is not None


async def _apply_dedup(
    session: AsyncSession,
    *,
    job: Job,
    job_item: MemoryItem,
    counterpart_id: UUID,
    result: ConflictResult,
) -> None:
    """Serialize the DEDUP rejection: lock, revalidate, guarded reject, event.

    Detection snapshots (``job_item``, ``result``) are a *proposal* (P0-FIX-004C2).
    The locked rows are mutation authority. The guard in the
    ``UPDATE ... RETURNING`` re-checks every mutation-authority fact, so a
    zero-row update is a truthful skip (no event, no mutation) — never
    permission to write a stale event. The original item is never mutated.

    A committed human governance decision (human verification or a human
    review-state decision) outranks later automated dedup rejection: the worker
    skips all mutation and writes no event.
    """
    tenant_id = str(job.tenant_id)
    locked = await _lock_conflict_pair(
        session,
        job_item_id=job_item.id,
        counterpart_id=counterpart_id,
        tenant_id=tenant_id,
    )
    if locked is None:
        return
    locked_job_item, locked_counterpart = locked

    # ---- Under-lock revalidation: the NEWER/job item ---------------------
    # Must still be live (valid_to IS NULL, superseded_by IS NULL) and not in a
    # terminal review state. Terminal (rejected/archived) or invalidated rows
    # are an idempotent no-op: do not update again, do not replace valid_to,
    # do not emit another event.
    if locked_job_item.valid_to is not None or locked_job_item.superseded_by is not None:
        return
    if locked_job_item.review_status in {"rejected", "archived"}:
        return
    # The job item must remain the newer side per the creation-time + UUID
    # tiebreak rule, evaluated against the locked counterpart.
    if locked_job_item.created_at > locked_counterpart.created_at:
        still_newer = True
    elif locked_job_item.created_at == locked_counterpart.created_at:
        still_newer = str(locked_job_item.id) > str(locked_counterpart.id)
    else:
        still_newer = False
    if not still_newer:
        return
    # Human-governance precedence: a committed human review/verification
    # decision protects the item from automated dedup rejection. Evaluated
    # while the item lock is held.
    if await _has_human_governance(session, item=locked_job_item, tenant_id=tenant_id):
        return
    # The job item must remain in a state eligible for automated dedup.
    if locked_job_item.review_status not in _DEDUP_PERMITTED_FROM:
        return

    # ---- Under-lock revalidation: the EXISTING/original item -------------
    # The original must still satisfy the detector's database eligibility
    # predicate: review_status='active', valid_to IS NULL. superseded_by IS
    # NULL is checked defensively (supersession sets valid_to, but be explicit).
    # A human review transition on the original may land between detection and
    # the pair lock without touching valid_to, so the active review state is
    # rechecked explicitly here. The original must match the detected id.
    if str(locked_counterpart.id) != str(result.existing_item_id):
        return
    if locked_counterpart.review_status != "active":
        return
    if locked_counterpart.valid_to is not None or locked_counterpart.superseded_by is not None:
        return

    # ---- Guarded rejection ----------------------------------------------
    # The guard re-checks every mutation-authority fact so a concurrent change
    # between revalidation and the write is still caught. The human-governance
    # row-state (human_verified) is re-checked here; the human-event predicate
    # is stored outside the item row but was evaluated above under the same
    # lock, and the human path cannot write its event without first acquiring
    # this row's FOR UPDATE lock.
    old_review_status = locked_job_item.review_status
    reject_at = _utcnow()
    update_stmt = (
        update(MemoryItem)
        .where(
            MemoryItem.id == locked_job_item.id,
            MemoryItem.tenant_id == tenant_id,
            MemoryItem.valid_to.is_(None),
            MemoryItem.superseded_by.is_(None),
            MemoryItem.review_status.in_(tuple(_DEDUP_PERMITTED_FROM)),
            MemoryItem.human_verified.is_(False),
        )
        .values(review_status="rejected", valid_to=reject_at)
        .returning(MemoryItem.id)
    )
    guard_result = await session.execute(
        update_stmt, execution_options={"synchronize_session": False}
    )
    if guard_result.scalar_one_or_none() is None:
        # The transition did not occur — no event is written. The transaction
        # commits as a no-op; a concurrent writer won the mutation authority.
        return

    # ---- Event after the guarded rejection succeeds ---------------------
    actor = await resolve_internal_system_actor(
        session,
        tenant_id=tenant_id,
        internal_key=CONFLICT_AUTOMATION_INTERNAL_KEY,
    )
    provenance = {
        "action": result.action.value,
        "existing_item_id": str(result.existing_item_id),
        "reason": result.reason,
        "worker_operation": "conflict.check",
        "job_id": str(job.id),
        "item_author_principal_id": str(locked_job_item.principal_id),
        "internal_actor_key": CONFLICT_AUTOMATION_INTERNAL_KEY,
        **result.provenance,
    }
    await _insert_event(
        session,
        item_id=locked_job_item.id,
        event_type="conflict_detected",
        field_name="review_status",
        old_value=old_review_status,
        new_value="rejected",
        actor_principal_id=actor,
        reason=json.dumps(provenance, sort_keys=True),
    )


# Visibility ordering: a refine job may only narrow, never widen (ENG-AUD-005).
_VISIBILITY_RANK: dict[str, int] = {
    "private": 0,
    "workspace": 1,
    "tenant": 2,
    "public": 3,
}


def _can_narrow(current: str, proposed: str) -> bool:
    return _VISIBILITY_RANK.get(proposed, 1) < _VISIBILITY_RANK.get(current, 1)


async def handle_classification_refine(session: AsyncSession, job: Job) -> None:
    """Refine kind/wing/room/confidence/visibility via an LLM, conservatively.

    Only the LLM path runs here (the request path uses rule-only classification).
    May improve kind/wing/room above the confidence threshold, blend
    memory_confidence (source-authority-capped, monotonic-up so it never
    destabilizes), and NARROW visibility (never widen). Never mutates content.
    Idempotent: equal proposed values record provenance but change nothing.
    """
    import json

    from engram.classification import classify

    item_id = _payload_item_id(job)
    item = await _reload_item(session, item_id)
    if item is None or _is_expired_or_inactive(item):
        logger.info("classification.refine id=%s skipped: item gone/inactive", item_id)
        return
    if str(item.tenant_id) != str(job.tenant_id):
        raise RuntimeError(f"job tenant {job.tenant_id} != item tenant {item.tenant_id}")

    result = await classify(item.content, item.tenant_id, session)
    actor = await resolve_internal_system_actor(
        session,
        tenant_id=job.tenant_id,
        internal_key=CLASSIFICATION_AUTOMATION_INTERNAL_KEY,
    )
    provenance = {
        "worker_operation": "classification.refine",
        "job_id": str(job.id),
        "item_author_principal_id": str(item.principal_id),
        "internal_actor_key": CLASSIFICATION_AUTOMATION_INTERNAL_KEY,
    }
    changed = False

    # kind/wing/room only above the confidence threshold.
    if result.confidence >= settings.classification_confidence_threshold:
        if result.suggested_kind and result.suggested_kind != item.kind:
            await _insert_event(
                session,
                item_id=item.id,
                event_type="metadata_patch",
                field_name="kind",
                old_value=item.kind,
                new_value=result.suggested_kind,
                actor_principal_id=actor,
                reason=json.dumps({**provenance, "reason": result.reason}, sort_keys=True),
            )
            await session.execute(
                update(MemoryItem)
                .where(MemoryItem.id == item.id)
                .values(kind=result.suggested_kind)
            )
            changed = True
        if result.suggested_wing and result.suggested_wing != (item.wing or ""):
            await _insert_event(
                session,
                item_id=item.id,
                event_type="metadata_patch",
                field_name="wing",
                old_value=item.wing,
                new_value=result.suggested_wing,
                actor_principal_id=actor,
                reason=json.dumps({**provenance, "reason": result.reason}, sort_keys=True),
            )
            await session.execute(
                update(MemoryItem)
                .where(MemoryItem.id == item.id)
                .values(wing=result.suggested_wing)
            )
            changed = True
        if result.suggested_room and result.suggested_room != (item.room or ""):
            await _insert_event(
                session,
                item_id=item.id,
                event_type="metadata_patch",
                field_name="room",
                old_value=item.room,
                new_value=result.suggested_room,
                actor_principal_id=actor,
                reason=json.dumps({**provenance, "reason": result.reason}, sort_keys=True),
            )
            await session.execute(
                update(MemoryItem)
                .where(MemoryItem.id == item.id)
                .values(room=result.suggested_room)
            )
            changed = True

    # memory_confidence: source-authority-capped candidate, monotonic-up (max)
    # so refinement never destabilizes (ENG-AUD-005) AND is idempotent —
    # re-running with the same candidate is a no-op (max is stable), so a refine
    # job cannot oscillate the value.
    candidate = min(item.source_trust, result.confidence)
    blended = max(item.memory_confidence, candidate)
    if blended - item.memory_confidence > settings.classification_refine_min_delta:
        await _insert_event(
            session,
            item_id=item.id,
            event_type="metadata_patch",
            field_name="memory_confidence",
            old_value=item.memory_confidence,
            new_value=blended,
            actor_principal_id=actor,
            reason=json.dumps({**provenance, "reason": result.reason}, sort_keys=True),
        )
        await session.execute(
            update(MemoryItem).where(MemoryItem.id == item.id).values(memory_confidence=blended)
        )
        changed = True

    # Visibility: NARROW only (never widen). ENG-AUD-005.
    if result.suggested_visibility is not None and _can_narrow(
        item.visibility, result.suggested_visibility
    ):
        await _insert_event(
            session,
            item_id=item.id,
            event_type="metadata_patch",
            field_name="visibility",
            old_value=item.visibility,
            new_value=result.suggested_visibility,
            actor_principal_id=actor,
            reason=json.dumps({**provenance, "reason": result.reason}, sort_keys=True),
        )
        await session.execute(
            update(MemoryItem)
            .where(MemoryItem.id == item.id)
            .values(visibility=result.suggested_visibility)
        )
        changed = True

    if not changed:
        # Record provenance that refinement ran and decided to change nothing,
        # so a rerun is observably idempotent.
        await _insert_event(
            session,
            item_id=item.id,
            event_type="classification",
            field_name="kind",
            old_value=None,
            new_value=json.dumps(
                {
                    "source": "llm_refine",
                    "provider": result.provenance.get("provider", "openai"),
                    "result": "no_change",
                    "reason": result.reason,
                    **provenance,
                },
                sort_keys=True,
            ),
            actor_principal_id=actor,
            reason=json.dumps(
                {**provenance, "reason": "LLM refine produced no change (idempotent)"},
                sort_keys=True,
            ),
        )

    await session.commit()


async def handle_promotion_path_a(session: AsyncSession, job: Job) -> None:
    """Run Path A auto-promotion for the job's tenant. Thin wrapper."""
    from engram.promotion import auto_promote_proposed_memories

    result = await auto_promote_proposed_memories(session, str(job.tenant_id), source="worker")
    logger.info(
        "promotion.path_a tenant=%s scanned=%s promoted=%s",
        job.tenant_id,
        result.scanned,
        result.promoted,
    )


async def handle_retention_sweep(session: AsyncSession, job: Job) -> None:
    """Retention sweep stub (retention logic not implemented). Idempotent no-op."""
    logger.info(
        "retention.sweep tenant=%s: no-op (retention logic deferred)",
        job.tenant_id,
    )


async def handle_recall_telemetry(session: AsyncSession, job: Job) -> None:
    """Apply recall counters/timestamps off the synchronous recall path (ENG-AUD-011 / F18).

    Payload: ``{tenant_id, principal_id, mode, recall_log_id, item_ids,
    recalled_at, request_id}`` — see engram.recall.execute_startup_recall.

    Idempotency (requirement 8): ``recall_logs.telemetry_applied_at`` is the
    claim. The claim UPDATE and the item-counter UPDATE run in the same
    transaction, committed together — so either both apply exactly once, or
    (on any failure before commit) neither applies and the retry sees
    ``telemetry_applied_at IS NULL`` again and safely re-attempts. A retry
    that lands after a successful commit finds the claim already set and is a
    pure no-op: it CANNOT double-increment counters.

    Handles deleted/expired items without failing: the UPDATE only touches
    rows that still exist and match this job's tenant; a hard-deleted item
    simply matches zero rows, and an expired/rejected item still gets its
    counters bumped (harmless bookkeeping — recall counts are historical
    telemetry, not eligibility state).
    """
    recall_log_id = job.payload.get("recall_log_id")
    if recall_log_id is None:
        raise ValueError("recall.telemetry payload missing recall_log_id")
    raw_item_ids = job.payload.get("item_ids") or []
    if not isinstance(raw_item_ids, list):
        raise ValueError("recall.telemetry payload item_ids must be a list")
    item_ids = [_parse_uuid(v) for v in raw_item_ids]
    mode = job.payload.get("mode", "startup")

    recalled_at_raw = job.payload.get("recalled_at")
    recalled_at = datetime.fromisoformat(recalled_at_raw) if recalled_at_raw else _utcnow()

    if not item_ids:
        logger.info("recall.telemetry recall_log_id=%s: no item_ids, no-op", recall_log_id)
        return

    moment = _utcnow()
    claimed = (
        await session.execute(
            text(
                "UPDATE recall_logs SET telemetry_applied_at = :moment "
                "WHERE id = :id AND tenant_id = :tenant_id AND telemetry_applied_at IS NULL "
                "RETURNING id"
            ),
            {
                "moment": moment,
                "id": _parse_uuid(recall_log_id),
                "tenant_id": job.tenant_id,
            },
        )
    ).scalar_one_or_none()

    if claimed is None:
        # Already applied by a prior successful run of this job (or a
        # concurrent worker that won the race) — safe no-op.
        await session.commit()
        logger.info(
            "recall.telemetry recall_log_id=%s already applied, no-op", recall_log_id
        )
        return

    values: dict[str, Any] = {
        "recall_count": MemoryItem.recall_count + 1,
        "last_recalled_at": recalled_at,
    }
    if mode == "startup":
        values["startup_recall_count"] = MemoryItem.startup_recall_count + 1

    await session.execute(
        update(MemoryItem)
        .where(MemoryItem.id.in_(item_ids), MemoryItem.tenant_id == job.tenant_id)
        .values(**values)
    )
    await session.commit()
    logger.info(
        "recall.telemetry recall_log_id=%s tenant=%s mode=%s items=%s applied",
        recall_log_id,
        job.tenant_id,
        mode,
        len(item_ids),
    )


# Registry of job type → handler.
JOB_HANDLERS: dict[str, JobHandler] = {
    "embedding.generate": handle_embedding_generate,
    "conflict.check": handle_conflict_check,
    "classification.refine": handle_classification_refine,
    "promotion.path_a": handle_promotion_path_a,
    "retention.sweep": handle_retention_sweep,
    "recall.telemetry": handle_recall_telemetry,
}


async def process_one_job(
    *,
    worker_id: str,
    session_factory: async_sessionmaker[AsyncSession],
    app_session_factory: async_sessionmaker[AsyncSession],
    job_types: list[str] | None = None,
    lease_stale_after: int | None = None,
) -> bool:
    """Claim and process at most one job. Returns True if a job was processed.

    Claim/lock bookkeeping runs through the owner ``session_factory``; payload
    processing runs through a fresh app-role ``app_session_factory`` scoped to
    the job's tenant. Job status transitions (succeeded/failed) run through the
    owner session.
    """
    if lease_stale_after is not None:
        async with session_factory() as owner:
            from engram.jobs import reclaim_stale_jobs

            await reclaim_stale_jobs(owner, lease_stale_after_seconds=lease_stale_after)

    async with session_factory() as owner:
        job = await claim_next_job(owner, worker_id=worker_id, job_types=job_types)

    if job is None:
        return False

    started = _utcnow()
    handler = JOB_HANDLERS.get(job.job_type)
    if handler is None:
        async with session_factory() as owner:
            await mark_job_failed_or_retry(
                owner, job.id, f"no handler for job_type {job.job_type!r}"
            )
        logger.error("worker=%s no handler for type=%s", worker_id, job.job_type)
        return True

    tenant_id = str(job.tenant_id)
    try:
        # Resolve the tenant's admin principal under the owner session, then
        # open an app-role session scoped to that tenant for payload work.
        async with session_factory() as owner:
            principal_id = await _resolve_tenant_admin(owner, tenant_id)

        async with app_session_factory() as app:
            await apply_rls_context(app, tenant_id=tenant_id, principal_id=principal_id)
            await handler(app, job)
    except Exception as exc:  # noqa: BLE001 — any handler error retries/dead-letters
        logger.exception(
            "worker=%s job %s (%s) failed: %s",
            worker_id,
            job.id,
            job.job_type,
            _truncate_error(exc),
        )
        async with session_factory() as owner:
            await mark_job_failed_or_retry(owner, job.id, exc)
        return True

    async with session_factory() as owner:
        await mark_job_succeeded(owner, job.id)

    duration = (_utcnow() - started).total_seconds()
    logger.info(
        "worker=%s job %s (%s) succeeded in %.3fs",
        worker_id,
        job.id,
        job.job_type,
        duration,
    )
    return True


async def run_worker(
    *,
    worker_id: str,
    session_factory: async_sessionmaker[AsyncSession],
    app_session_factory: async_sessionmaker[AsyncSession],
    once: bool = False,
    poll_interval: float | None = None,
    job_types: list[str] | None = None,
    max_jobs: int | None = None,
    lease_stale_after: int | None = None,
) -> int:
    """Run the worker loop.

    With ``once=True`` claims and processes at most one job then exits.
    Otherwise polls indefinitely. Exits 0 on normal completion; nonzero only on
    fatal setup errors (ordinary job failures do NOT stop the loop).
    """
    interval = poll_interval if poll_interval is not None else settings.job_poll_interval_seconds
    stale = (
        lease_stale_after
        if lease_stale_after is not None
        else settings.job_lease_stale_after_seconds
    )

    logger.info(
        "worker=%s starting (once=%s, poll=%.2fs, types=%s)",
        worker_id,
        once,
        interval,
        job_types or "(all)",
    )

    processed = 0
    while True:
        try:
            did = await process_one_job(
                worker_id=worker_id,
                session_factory=session_factory,
                app_session_factory=app_session_factory,
                job_types=job_types,
                lease_stale_after=stale,
            )
        except Exception:  # noqa: BLE001 — fatal-but-loopable errors
            logger.exception("worker=%s error in process_one_job; continuing", worker_id)
            did = False

        if did:
            processed += 1

        if once:
            return 0
        if max_jobs is not None and processed >= max_jobs:
            logger.info("worker=%s reached max_jobs=%s, exiting", worker_id, max_jobs)
            return 0

        await asyncio.sleep(interval)


__all__ = [
    "JOB_HANDLERS",
    "process_one_job",
    "run_worker",
]

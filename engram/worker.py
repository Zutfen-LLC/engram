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

from engram.authority import authority_allows_supersession, qualifies_for_auto_supersession
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
from engram.memory_context import (
    INTERNAL_MEMORY_CONTEXT_VERSION,
    LEGACY_MEMORY_CONTEXT_VERSION,
    ResolvedMemoryContext,
    context_provenance,
    memory_context_from_ingest,
)
from engram.models import (
    CandidateIngest,
    EmbeddingProfile,
    ItemEvent,
    Job,
    MemoryEmbedding,
    MemoryItem,
    Principal,
)

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
    ingest_id: UUID | None = None,
) -> None:
    """Write an item_events audit row (mirrors the PATCH path's helper)."""
    tenant_id = (
        await session.execute(select(MemoryItem.tenant_id).where(MemoryItem.id == item_id))
    ).scalar_one()
    provenance: dict[str, object] = {
        "tenant_id": tenant_id,
        "memory_context_version": INTERNAL_MEMORY_CONTEXT_VERSION,
    }
    if ingest_id is not None:
        ingest = await session.scalar(
            select(CandidateIngest).where(CandidateIngest.id == ingest_id)
        )
        if ingest is None:
            raise ValueError("candidate ingest provenance is unavailable")
        # Audit-event provenance is descriptive metadata, not an authorization
        # boundary (the handler already resolved the worker context and would
        # have skipped on a provenance failure). The three outcomes are kept
        # explicit and truthfully distinct:
        #   * valid execution authority  -> exact caller profile/API-key provenance;
        #   * genuine legacy ingest      -> legacy-unprofiled-v0 (compatibility);
        #   * missing/corrupt/incoherent v2 authority -> neutral internal-system-v1.
        # A v2 provenance failure must never be relabeled legacy: the two are
        # materially different states. No profile/revision/API-key identity is
        # ever fabricated. Catch only the provenance-reconstruction ValueError
        # from memory_context_from_ingest; unrelated database errors propagate.
        try:
            context = await memory_context_from_ingest(session, ingest)
        except ValueError:
            logger.warning(
                "%s ingest=%s audit provenance unavailable: execution authority "
                "could not be reconstructed; recording neutral internal provenance",
                event_type,
                ingest_id,
            )
            provenance = {
                "tenant_id": tenant_id,
                "memory_context_version": INTERNAL_MEMORY_CONTEXT_VERSION,
            }
        else:
            if context is None:
                provenance = {
                    "tenant_id": tenant_id,
                    "memory_context_version": LEGACY_MEMORY_CONTEXT_VERSION,
                }
            else:
                provenance = context_provenance(context)
    await session.execute(
        insert(ItemEvent).values(
            id=uuid.uuid4(),
            item_id=item_id,
            **provenance,
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


async def _job_memory_context(
    session: AsyncSession, job: Job
) -> ResolvedMemoryContext | None:
    raw_ingest_id = job.payload.get("ingest_id")
    if raw_ingest_id is None:
        return None
    ingest = await session.scalar(
        select(CandidateIngest).where(CandidateIngest.id == _parse_uuid(raw_ingest_id))
    )
    if ingest is None or str(ingest.tenant_id) != str(job.tenant_id):
        raise ValueError("candidate ingest provenance is unavailable")
    return await memory_context_from_ingest(session, ingest)


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
    # Telemetry correlation (ENG-METER-001): carried through the job payload so
    # the worker-originated provider call is attributed to the original
    # candidate. Absent on older/backfill payloads — None is valid.
    raw_correlation_id = job.payload.get("correlation_id")
    raw_ingest_id = job.payload.get("ingest_id")
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
            vector = await generate_embedding(
                item.content,
                profile,
                tenant_id=item.tenant_id,
                principal_id=item.principal_id,
                workspace_id=item.workspace_id,
                operation="embedding_document",
                usage_class="async_enrichment",
                correlation_id=_parse_uuid(raw_correlation_id) if raw_correlation_id else None,
                ingest_id=_parse_uuid(raw_ingest_id) if raw_ingest_id else None,
                job_id=job.id,
            )
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

        conflict_payload: dict[str, object] = {
            "memory_item_id": str(item_id),
            "profile_id": str(profile.id),
            "profile_key": profile.profile_key,
        }
        if raw_correlation_id is not None:
            conflict_payload["correlation_id"] = str(raw_correlation_id)
        if raw_ingest_id is not None:
            conflict_payload["ingest_id"] = str(raw_ingest_id)
        await enqueue_job(
            session,
            tenant_id=job.tenant_id,
            job_type="conflict.check",
            payload=conflict_payload,
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

    raw_correlation_id = job.payload.get("correlation_id")
    raw_ingest_id = job.payload.get("ingest_id")
    try:
        memory_context = await _job_memory_context(session, job)
    except ValueError:
        logger.exception("conflict.check id=%s skipped: invalid memory context", item_id)
        return
    result = await detect_conflicts(
        item,
        session,
        profile=profile,
        correlation_id=_parse_uuid(raw_correlation_id) if raw_correlation_id else None,
        ingest_id=_parse_uuid(raw_ingest_id) if raw_ingest_id else None,
        job_id=job.id,
        usage_class="async_enrichment",
        memory_context=memory_context,
    )
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
            profile=profile,
            memory_context=memory_context,
        )
        await session.commit()
        return

    if action == ConflictAction.AUTO_SUPERSEDE:
        # The NEW item supersedes the OLD neighbor. Only act when the job's item
        # is the newer one; otherwise the neighbor's own job handles it.
        if not item_is_newer:
            await session.commit()
            return
        # Serialize the supersession (P0-FIX-004D): detection is only a
        # proposal. Lock both rows in canonical order, revalidate mutation
        # authority from the locked rows (authority, human-governance
        # precedence, current detector eligibility, creation direction), and
        # perform a guarded UPDATE ... RETURNING on the OLD row before writing
        # a truthful event — all in one transaction. The new item is never
        # mutated by this branch.
        await _apply_auto_supersede(
            session,
            job=job,
            job_item=item,
            counterpart_id=result.existing_item_id,
            result=result,
            profile=profile,
            memory_context=memory_context,
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
        memory_context=memory_context,
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
    memory_context: ResolvedMemoryContext | None,
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
    if memory_context is not None:
        from engram.memory_access import apply_write_eligibility

        eligible_counterpart = await session.scalar(
            apply_write_eligibility(
                select(MemoryItem.id).where(MemoryItem.id == counterpart_id),
                memory_context,
            )
        )
        if eligible_counterpart is None:
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
    memory_context: ResolvedMemoryContext | None,
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
        memory_context=memory_context,
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
        ingest_id=_parse_uuid(job.payload["ingest_id"]) if job.payload.get("ingest_id") else None,
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


async def _lock_and_verify_active_profile(
    session: AsyncSession,
    *,
    profile: EmbeddingProfile,
) -> bool:
    """Lock and revalidate the active embedding profile row at mutation authority.

    A conflict result produced using profile P may mutate trust state only
    while P is still the active profile at mutation-authority time. The detector
    ran unlocked with a pre-lock snapshot of P (``state == "active"`` plus the
    detector-relevant immutable/vector-space fields). A concurrent profile
    cutover (``activate_profile`` retires the active row and activates a
    replacement) can retire P before the worker obtains profile mutation
    authority, in which case AUTO_SUPERSEDE is a mutation-free, event-free
    no-op.

    This helper reloads the specific ``EmbeddingProfile`` row with
    ``SELECT ... FOR UPDATE`` on PostgreSQL and verifies:

    * the row still exists;
    * ``id == profile.id``;
    * ``state == "active"``;
    * ``profile_key``, ``dimensions``, ``provider``, ``model`` and
      ``distance_metric`` still match the validated proposal object (the
      detector-relevant immutable/vector-space fields).

    Returns ``True`` iff P is still the active profile under the lock.

    Global lock order: ``memory_items`` pair (canonical UUID order) →
    ``embedding_profiles`` row → ``memory_embeddings`` pair (canonical
    ``memory_item_id`` order). ``activate_profile`` updates profile rows but
    does NOT acquire ``memory_items`` row locks, so a worker that already holds
    the item pair lock and then locks the profile row cannot deadlock with a
    cutover that takes only the profile row lock — the item lock is not on the
    cutover's lock path, so there is no reverse profile→item lock cycle. If the
    worker obtains the profile lock while P is active first, the cutover waits;
    the worker completes and then the cutover resumes. This defines a serial
    order. Do not rerun semantic detection or classification under lock.
    """
    dialect_name = session.bind.dialect.name if session.bind is not None else None
    # Use a core SELECT that returns a fresh row mapping (not the ORM identity
    # map): the session already holds the pre-lock ``EmbeddingProfile`` instance
    # from ``get_active_profile``, so an ORM ``select(EmbeddingProfile)`` would
    # return the stale identity-mapped instance. ``populate_existing()`` would
    # also work, but a core column query is unambiguous about reading the
    # locked DB row's current committed state.
    cols = (
        EmbeddingProfile.id,
        EmbeddingProfile.state,
        EmbeddingProfile.profile_key,
        EmbeddingProfile.dimensions,
        EmbeddingProfile.provider,
        EmbeddingProfile.model,
        EmbeddingProfile.distance_metric,
    )
    stmt = select(*cols).where(EmbeddingProfile.id == profile.id)
    if dialect_name == "postgresql":
        stmt = stmt.with_for_update()
    row = (await session.execute(stmt)).mappings().one_or_none()
    if row is None:
        return False
    if row["state"] != "active":
        return False
    if row["profile_key"] != profile.profile_key:
        return False
    if int(row["dimensions"]) != int(profile.dimensions):
        return False
    if row["provider"] != profile.provider:
        return False
    if row["model"] != profile.model:
        return False
    return str(row["distance_metric"]) == profile.distance_metric


async def _lock_and_verify_pair_embeddings(
    session: AsyncSession,
    *,
    job_item_id: UUID,
    counterpart_id: UUID,
    profile: EmbeddingProfile,
    tenant_id: str,
) -> bool:
    """Lock and revalidate the embedding rows the detector relied on.

    ``detect_conflicts`` requires BOTH items to have a ready, non-null embedding
    for the job's validated profile and dimensions: the job item's embedding is
    the query vector, and the original must appear in the same-kind, ready-
    embedding neighbour query. An embedding's ``embedding_status`` can change
    concurrently (``handle_embedding_generate`` updates ``memory_embeddings``
    rows directly) WITHOUT taking the ``memory_items`` row lock that the pair
    lock holds, so the item lock alone cannot prevent a stale eligibility
    decision. This helper locks the relevant ``memory_embeddings`` rows with
    ``SELECT ... FOR UPDATE`` and revalidates readiness from the locked rows.

    Lock order (no deadlock with the embedding job): the caller already holds
    the ``memory_items`` pair locks; this then locks ``memory_embeddings`` rows
    in canonical ``memory_item_id`` order. The embedding job updates only
    ``memory_embeddings`` (it never locks ``memory_items``), so there is no
    cross-table lock cycle (dedup takes mi→me; the embedding job takes only me).

    Returns ``True`` iff both items still have a ready, non-null embedding row
    for ``profile.id``/``profile.dimensions`` belonging to ``tenant_id``.
    """
    pair_ids = sorted((job_item_id, counterpart_id), key=str)
    dialect_name = session.bind.dialect.name if session.bind is not None else None
    stmt = (
        select(MemoryEmbedding)
        .where(
            MemoryEmbedding.tenant_id == tenant_id,
            MemoryEmbedding.memory_item_id.in_(pair_ids),
            MemoryEmbedding.profile_id == profile.id,
            MemoryEmbedding.embedding_dim == profile.dimensions,
        )
        .order_by(MemoryEmbedding.memory_item_id)
    )
    if dialect_name == "postgresql":
        stmt = stmt.with_for_update()
    locked = list((await session.execute(stmt)).scalars().all())
    # One ready embedding row per item/profile (uq_memory_embeddings_item_profile).
    by_item: dict[UUID, MemoryEmbedding] = {}
    for row in locked:
        # Last write wins is harmless: the unique constraint guarantees one row
        # per (tenant, item, profile); a duplicate would only arise from a
        # concurrent insert racing this read, which the FOR UPDATE serializes.
        by_item[row.memory_item_id] = row
    for needed in (job_item_id, counterpart_id):
        emb = by_item.get(needed)
        if emb is None:
            return False
        if emb.embedding_status != "ready":
            return False
        if emb.embedding is None:
            return False
    return True


def _workspace_scope_matches(a: MemoryItem, b: MemoryItem) -> bool:
    """Exact workspace-scope match, including both-null semantics.

    Mirrors ``detect_conflicts``: a workspace-scoped item matches only items in
    the same workspace, and a tenant/public-scoped item (``workspace_id IS
    NULL``) matches only other NULL-scope items.
    """
    if a.workspace_id is None or b.workspace_id is None:
        return a.workspace_id is None and b.workspace_id is None
    return str(a.workspace_id) == str(b.workspace_id)


async def _apply_dedup(
    session: AsyncSession,
    *,
    job: Job,
    job_item: MemoryItem,
    counterpart_id: UUID,
    result: ConflictResult,
    profile: EmbeddingProfile,
    memory_context: ResolvedMemoryContext | None,
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

    Under the canonical pair locks the worker revalidates the full detector
    database-eligibility predicate — not just review-state/validity, but also
    kind match, exact workspace scope (both-null semantics), and ready non-null
    embeddings for the job's validated profile on both items. Embedding status
    can change concurrently without taking the item row lock, so the relevant
    ``memory_embeddings`` rows are locked and revalidated here too.
    """
    tenant_id = str(job.tenant_id)
    locked = await _lock_conflict_pair(
        session,
        job_item_id=job_item.id,
        counterpart_id=counterpart_id,
        tenant_id=tenant_id,
        memory_context=memory_context,
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

    # ---- Detector eligibility: kind, workspace scope, embeddings ----------
    # ``detect_conflicts`` selects the original via the same-kind, same-
    # workspace-scope, active, live neighbour query, and requires both items to
    # carry a ready non-null embedding for the job's validated profile. These
    # facts can change after detection (e.g. classification refinement can
    # change an item's kind; an embedding can be re-marked failed), so they are
    # revalidated from the locked rows. This is database eligibility
    # revalidation, not a full redetection: no semantic similarity or classifier
    # is rerun. A mismatch is a mutation-free, event-free skip — the worker must
    # not reject the newer item as a duplicate of an original the detector would
    # no longer return.
    if locked_job_item.kind != locked_counterpart.kind:
        return
    if not _workspace_scope_matches(locked_job_item, locked_counterpart):
        return
    if not await _lock_and_verify_pair_embeddings(
        session,
        job_item_id=locked_job_item.id,
        counterpart_id=locked_counterpart.id,
        profile=profile,
        tenant_id=tenant_id,
    ):
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
        ingest_id=_parse_uuid(job.payload["ingest_id"]) if job.payload.get("ingest_id") else None,
    )


async def _apply_auto_supersede(
    session: AsyncSession,
    *,
    job: Job,
    job_item: MemoryItem,
    counterpart_id: UUID,
    result: ConflictResult,
    profile: EmbeddingProfile,
    memory_context: ResolvedMemoryContext | None,
) -> None:
    """Serialize the AUTO_SUPERSEDE mutation: lock, revalidate, guarded write,
    truthful event.

    Detection snapshots (``job_item``, ``result``) are a *proposal*
    (P0-FIX-004D). The locked rows are mutation authority. AUTO_SUPERSEDE
    mutates only the OLD/counterpart row:

        old.superseded_by = new.id
        old.valid_to = one captured UTC timestamp

    The new/job item remains live and is never mutated by this branch.

    A committed human governance decision (human verification or a human
    review-state decision) on EITHER row outranks later automated supersession:
    the worker skips all mutation and writes no event.

    Under the canonical pair locks the worker revalidates the full detector
    database-eligibility predicate — not just review-state/validity, but also
    kind match, exact workspace scope (both-null semantics), and ready non-null
    embeddings for the job's validated profile on both items — plus the current
    authority hierarchy and the canonical high-confidence classifier threshold.
    Authority is rechecked from the locked rows, never from
    ``result.provenance`` (which captures the pre-lock detection snapshot).

    AUTO_SUPERSEDE is a valid action only for ``ConflictVerdict.REFINE``: a
    proposal whose verdict is not REFINE is a malformed proposal and is a
    mutation-free, event-free no-op.

    The active embedding profile is revalidated at mutation-authority time
    (between the ``memory_items`` pair lock and the ``memory_embeddings`` pair
    lock) by locking the specific ``embedding_profiles`` row with
    ``SELECT ... FOR UPDATE`` and verifying it is still active with matching
    detector-relevant immutable/vector-space fields. If the profile has been
    retired by a concurrent cutover, AUTO_SUPERSEDE is a mutation-free,
    event-free no-op. Global lock order: ``memory_items`` pair (canonical UUID
    order) → ``embedding_profiles`` row → ``memory_embeddings`` pair (canonical
    ``memory_item_id`` order). ``activate_profile`` updates profile rows but
    does not acquire ``memory_items`` row locks, so no reverse profile→item
    lock cycle is introduced.
    """
    from engram.conflicts import HIGH_CLASSIFIER_CONFIDENCE, ConflictAction, ConflictVerdict

    tenant_id = str(job.tenant_id)
    locked = await _lock_conflict_pair(
        session,
        job_item_id=job_item.id,
        counterpart_id=counterpart_id,
        tenant_id=tenant_id,
        memory_context=memory_context,
    )
    if locked is None:
        return
    locked_job_item, locked_counterpart = locked

    # ---- Under-lock revalidation: the NEWER/job item ------------------------
    # The new item is the superseder; it must remain live, unsuperseded, not
    # rejected/archived, and otherwise eligible for the detected conflict
    # path. The new item is NOT mutated, but if it has since left the eligible
    # set the supersession is no longer meaningful.
    if locked_job_item.valid_to is not None or locked_job_item.superseded_by is not None:
        return
    if locked_job_item.review_status in {"rejected", "archived"}:
        return
    # The job item must remain the newer side per the creation-time + UUID
    # tiebreak rule, evaluated against the locked counterpart. A stale
    # pre-lock direction check is not mutation authority.
    if locked_job_item.created_at > locked_counterpart.created_at:
        still_newer = True
    elif locked_job_item.created_at == locked_counterpart.created_at:
        still_newer = str(locked_job_item.id) > str(locked_counterpart.id)
    else:
        still_newer = False
    if not still_newer:
        return
    # Human-governance precedence on the NEW item: a committed human
    # verification or human review-state decision protects it from automated
    # supersession authority. Evaluated while the item lock is held.
    if await _has_human_governance(session, item=locked_job_item, tenant_id=tenant_id):
        return
    # The proposed result must still be AUTO_SUPERSEDE derived from a REFINE
    # verdict, and meet the canonical high-confidence classifier threshold.
    # AUTO_SUPERSEDE is only a valid derivation of REFINE; a malformed proposal
    # (any other verdict paired with AUTO_SUPERSEDE) is a mutation-free,
    # event-free no-op. No LLM re-classification runs under lock.
    if result.verdict is not ConflictVerdict.REFINE:
        return
    if result.action is not ConflictAction.AUTO_SUPERSEDE:
        return
    if result.classifier_confidence < HIGH_CLASSIFIER_CONFIDENCE:
        return
    # Authority revalidation from the locked rows (not result.provenance). The
    # new item's current authority must still allow supersession of the old
    # item, and must still qualify for automatic supersession.
    new_authority = int(locked_job_item.authority)
    old_authority = int(locked_counterpart.authority)
    if not authority_allows_supersession(new_authority=new_authority, old_authority=old_authority):
        return
    if not qualifies_for_auto_supersession(new_authority):
        return

    # ---- Under-lock revalidation: the OLD/counterpart item ----------------
    # The old item is the mutation target. The locked counterpart must equal
    # the detected id, belong to the job tenant (already checked by the pair
    # lock), be distinct (already checked), and remain in the detector's
    # active-live eligibility predicate. Existing terminal, invalidated,
    # archived, disputed, rejected, or superseded state wins; skip without
    # replacing timestamps or links.
    if str(locked_counterpart.id) != str(result.existing_item_id):
        return
    if locked_counterpart.review_status != "active":
        return
    if locked_counterpart.valid_to is not None or locked_counterpart.superseded_by is not None:
        return
    # Human-governance precedence on the OLD item: a committed human
    # verification or human review-state decision protects it from being
    # auto-superseded.
    if await _has_human_governance(session, item=locked_counterpart, tenant_id=tenant_id):
        return

    # ---- Active profile revalidation at mutation authority ---------------
    # The detector ran unlocked with a pre-lock snapshot of profile P. A
    # concurrent profile cutover can retire P before the worker obtains profile
    # mutation authority. Lock the specific ``embedding_profiles`` row with
    # SELECT ... FOR UPDATE and verify P is still active with matching
    # detector-relevant immutable/vector-space fields. Lock order:
    # memory_items pair → embedding_profiles row → memory_embeddings pair.
    # If P is no longer active, AUTO_SUPERSEDE is a mutation-free, event-free
    # no-op. activate_profile does not take memory_items row locks, so no
    # reverse profile→item lock cycle is introduced.
    if not await _lock_and_verify_active_profile(session, profile=profile):
        return

    # ---- Detector eligibility: kind, workspace scope, embeddings ----------
    # ``detect_conflicts`` selects the counterpart via the same-kind, same-
    # workspace-scope, active, live neighbour query, and requires both items to
    # carry a ready non-null embedding for the job's validated profile. These
    # facts can change after detection, so they are revalidated from the locked
    # rows. This is database eligibility revalidation, not a full redetection:
    # no semantic similarity or classifier is rerun. A mismatch is a
    # mutation-free, event-free skip — the worker must not supersede an old
    # item the detector would no longer return.
    if locked_job_item.kind != locked_counterpart.kind:
        return
    if not _workspace_scope_matches(locked_job_item, locked_counterpart):
        return
    if not await _lock_and_verify_pair_embeddings(
        session,
        job_item_id=locked_job_item.id,
        counterpart_id=locked_counterpart.id,
        profile=profile,
        tenant_id=tenant_id,
    ):
        return

    # ---- Guarded supersession of the OLD row -------------------------------
    # The guard re-checks every mutation-authority fact so a concurrent change
    # between revalidation and the write is still caught. At minimum the guard
    # repeats tenant, id, active review state, valid_to IS NULL, and
    # superseded_by IS NULL. A zero-row result is a truthful no-op and emits no
    # event.
    new_id = locked_job_item.id
    supersede_at = _utcnow()
    update_stmt = (
        update(MemoryItem)
        .where(
            MemoryItem.id == locked_counterpart.id,
            MemoryItem.tenant_id == tenant_id,
            MemoryItem.valid_to.is_(None),
            MemoryItem.superseded_by.is_(None),
            MemoryItem.review_status == "active",
        )
        .values(superseded_by=new_id, valid_to=supersede_at)
        .returning(MemoryItem.id)
    )
    guard_result = await session.execute(
        update_stmt, execution_options={"synchronize_session": False}
    )
    if guard_result.scalar_one_or_none() is None:
        # The transition did not occur — no event is written. The transaction
        # commits as a no-op; a concurrent writer won the mutation authority.
        return

    # ---- Truthful event after mutation confirmation ------------------------
    # Only after RETURNING confirms the transition, insert one automation event
    # in the same transaction. The event is attached to the MUTATED OLD item
    # (the row whose superseded_by actually changed), records the actual new
    # superseded_by value (the new item id), and uses the conflict_automation
    # internal actor. State/event rollback is atomic: if the event INSERT
    # fails, the transaction rolls back both valid_to and superseded_by.
    actor = await resolve_internal_system_actor(
        session,
        tenant_id=tenant_id,
        internal_key=CONFLICT_AUTOMATION_INTERNAL_KEY,
    )
    # Canonical event-role fields are authoritative and cannot be overwritten
    # by untrusted/stale detector provenance. Provenance is namespaced under
    # ``detector_provenance`` so hostile/colliding provider data is preserved
    # without namespace collision and can never replace old_item_id,
    # new_item_id, action, job id, reason, actor key, or author identity.
    payload = {
        "action": result.action.value,
        "verdict": result.verdict.value,
        "old_item_id": str(locked_counterpart.id),
        "new_item_id": str(new_id),
        "existing_item_id": str(result.existing_item_id),
        "reason": result.reason,
        "worker_operation": "conflict.check",
        "job_id": str(job.id),
        "item_author_principal_id": str(locked_job_item.principal_id),
        "internal_actor_key": CONFLICT_AUTOMATION_INTERNAL_KEY,
        "detector_provenance": result.provenance,
    }
    await _insert_event(
        session,
        item_id=locked_counterpart.id,
        event_type="conflict_detected",
        field_name="superseded_by",
        old_value=None,
        new_value=str(new_id),
        actor_principal_id=actor,
        reason=json.dumps(payload, sort_keys=True),
        ingest_id=_parse_uuid(job.payload["ingest_id"]) if job.payload.get("ingest_id") else None,
    )


# Visibility ordering: a refine job may only narrow, never widen (ENG-AUD-005).
_VISIBILITY_RANK: dict[str, int] = {
    "private": 0,
    "workspace": 1,
    "tenant": 2,
    "public": 3,
}


def _can_narrow(current: str, proposed: str, *, workspace_id: UUID | None) -> bool:
    """Whether ``proposed`` is a strict narrowing of ``current``.

    ENG-SCOPE-001: a refine job may never propose ``workspace`` for an item
    with no ``workspace_id`` — that combination is the exact invariant this
    slice eliminates, so it is rejected here regardless of rank.
    """
    if proposed == "workspace" and workspace_id is None:
        return False
    return _VISIBILITY_RANK.get(proposed, 1) < _VISIBILITY_RANK.get(current, 1)


async def _reload_item_for_update(session: AsyncSession, item_id: UUID) -> MemoryItem | None:
    """Reload an item with SELECT ... FOR UPDATE for serialized mutation."""
    return (
        await session.execute(select(MemoryItem).where(MemoryItem.id == item_id).with_for_update())
    ).scalar_one_or_none()


async def _guarded_field_update(
    session: AsyncSession,
    *,
    item_id: UUID,
    tenant_id: UUID,
    field_name: str,
    old_value: Any,
    new_value: Any,
    actor: UUID,
    reason: str | None,
    provenance: dict[str, Any],
    result_reason: str,
    ingest_id: UUID | None,
) -> bool:
    """Guarded UPDATE for a single metadata field. Re-checks the old value in
    the WHERE clause, uses RETURNING to confirm the mutation, and writes the
    event only after success. Returns True if the field was actually mutated.
    """
    # Use is_not_distinct_from (not ==) so NULL old values match correctly
    # (wing/room are nullable; == NULL always returns NULL, not true).
    guard_stmt = (
        update(MemoryItem)
        .where(
            MemoryItem.id == item_id,
            MemoryItem.tenant_id == tenant_id,
            getattr(MemoryItem, field_name).is_not_distinct_from(old_value),
        )
        .values(**{field_name: new_value})
        .returning(MemoryItem.id)
    )
    guard_result = await session.execute(
        guard_stmt, execution_options={"synchronize_session": False}
    )
    if guard_result.scalar_one_or_none() is None:
        return False

    import json

    await _insert_event(
        session,
        item_id=item_id,
        event_type="metadata_patch",
        field_name=field_name,
        old_value=old_value,
        new_value=new_value,
        actor_principal_id=actor,
        reason=json.dumps({**provenance, "reason": result_reason}, sort_keys=True),
        ingest_id=ingest_id,
    )
    return True


async def handle_classification_refine(session: AsyncSession, job: Job) -> None:
    """Refine kind/wing/room/confidence/visibility via an LLM, conservatively.

    Only the LLM path runs here (the request path uses rule-only classification).
    May improve kind/wing/room above the confidence threshold, blend
    memory_confidence (source-authority-capped, monotonic-up so it never
    destabilizes), and NARROW visibility (never widen). Never mutates content.
    Idempotent: equal proposed values record provenance but change nothing.

    Serialization: the expensive LLM classification runs unlocked on a stale
    snapshot. The row is then locked via SELECT ... FOR UPDATE and each
    proposed change is revalidated against the locked row's current values.
    Each mutation uses a guarded UPDATE ... RETURNING that re-checks the old
    value; a concurrent writer that changed the field between detection and
    mutation produces a zero-row result and the change is skipped (no stale
    event). Events are written only after RETURNING confirms the mutation.
    """
    import json

    from engram.classification import classify
    from engram.classification_evidence import (
        bind_run,
        bound_run_for_item,
        new_run,
    )

    item_id = _payload_item_id(job)
    # Phase 1: unlocked detection on a stale snapshot.
    item = await _reload_item(session, item_id)
    if item is None or _is_expired_or_inactive(item):
        logger.info("classification.refine id=%s skipped: item gone/inactive", item_id)
        return
    if str(item.tenant_id) != str(job.tenant_id):
        raise RuntimeError(f"job tenant {job.tenant_id} != item tenant {item.tenant_id}")
    if await bound_run_for_item(session, item_id) is not None:
        logger.info("classification.refine id=%s skipped: evidence already bound", item_id)
        return

    result = await classify(
        item.content,
        item.tenant_id,
        session,
        principal_id=item.principal_id,
        workspace_id=item.workspace_id,
        source_type=item.source_type,
        correlation_id=(
            _parse_uuid(job.payload["correlation_id"])
            if job.payload.get("correlation_id")
            else None
        ),
        ingest_id=(
            _parse_uuid(job.payload["ingest_id"]) if job.payload.get("ingest_id") else None
        ),
        job_id=job.id,
    )
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

    # Phase 2: lock the row and revalidate against current state.
    locked_item = await _reload_item_for_update(session, item_id)
    if locked_item is None or _is_expired_or_inactive(locked_item):
        logger.info("classification.refine id=%s skipped: item gone/inactive after lock", item_id)
        return
    if str(locked_item.tenant_id) != str(job.tenant_id):
        return
    if await bound_run_for_item(session, item_id, for_update=True) is not None:
        logger.info(
            "classification.refine id=%s skipped after lock: evidence already bound", item_id
        )
        return
    initial_kind = locked_item.kind
    initial_visibility = locked_item.visibility

    run = new_run(
        tenant_id=locked_item.tenant_id,
        principal_id=actor,
        content=locked_item.content,
        source_type=locked_item.source_type,
        workspace_id=locked_item.workspace_id,
        context=None,
        result=result,
        ingest_id=(
            _parse_uuid(job.payload["ingest_id"]) if job.payload.get("ingest_id") else None
        ),
    )
    session.add(run)
    bind_run(run, locked_item)

    # kind/wing/room only above the confidence threshold.
    if result.confidence >= settings.classification_confidence_threshold:
        if result.suggested_kind and result.suggested_kind != locked_item.kind:  # noqa: SIM102
            if await _guarded_field_update(
                session,
                item_id=item_id,
                tenant_id=job.tenant_id,
                field_name="kind",
                old_value=locked_item.kind,
                new_value=result.suggested_kind,
                actor=actor,
                reason=None,
                provenance=provenance,
                result_reason=result.reason,
                ingest_id=(
                    _parse_uuid(job.payload["ingest_id"])
                    if job.payload.get("ingest_id")
                    else None
                ),
            ):
                changed = True
        if result.suggested_wing and result.suggested_wing != (locked_item.wing or ""):  # noqa: SIM102
            if await _guarded_field_update(
                session,
                item_id=item_id,
                tenant_id=job.tenant_id,
                field_name="wing",
                old_value=locked_item.wing,
                new_value=result.suggested_wing,
                actor=actor,
                reason=None,
                provenance=provenance,
                result_reason=result.reason,
                ingest_id=(
                    _parse_uuid(job.payload["ingest_id"])
                    if job.payload.get("ingest_id")
                    else None
                ),
            ):
                changed = True
        if result.suggested_room and result.suggested_room != (locked_item.room or ""):  # noqa: SIM102
            if await _guarded_field_update(
                session,
                item_id=item_id,
                tenant_id=job.tenant_id,
                field_name="room",
                old_value=locked_item.room,
                new_value=result.suggested_room,
                actor=actor,
                reason=None,
                provenance=provenance,
                result_reason=result.reason,
                ingest_id=(
                    _parse_uuid(job.payload["ingest_id"])
                    if job.payload.get("ingest_id")
                    else None
                ),
            ):
                changed = True

    # Visibility: NARROW only (never widen). ENG-AUD-005.
    # Revalidate against the locked row's current visibility — a concurrent
    # PATCH may have already widened or narrowed it.
    if result.suggested_visibility is not None and _can_narrow(  # noqa: SIM102
        locked_item.visibility,
        result.suggested_visibility,
        workspace_id=locked_item.workspace_id,
    ):
        if await _guarded_field_update(
            session,
            item_id=item_id,
            tenant_id=job.tenant_id,
            field_name="visibility",
            old_value=locked_item.visibility,
            new_value=result.suggested_visibility,
            actor=actor,
            reason=None,
            provenance=provenance,
            result_reason=result.reason,
            ingest_id=(
                _parse_uuid(job.payload["ingest_id"])
                if job.payload.get("ingest_id")
                else None
            ),
        ):
            changed = True

    # The receipt, item evidence, classification event, and targeted delayed
    # promotion job share this transaction.
    from engram.promotion import schedule_evidence_promotion_if_qualified

    await session.flush()
    current_item = (
        await session.execute(
            select(MemoryItem)
            .where(MemoryItem.id == item_id, MemoryItem.tenant_id == job.tenant_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if current_item is None or _is_expired_or_inactive(current_item):
        return
    current_run = await bound_run_for_item(session, item_id, for_update=True)
    receipt_matches_item = (
        current_run is not None
        and current_run.id == run.id
        and current_item.kind == current_run.suggested_kind
    )
    promotion_job_id: UUID | None = None
    promotion_schedule: dict[str, str] = {}
    if receipt_matches_item:
        assert current_run is not None
        promotion_job_id = await schedule_evidence_promotion_if_qualified(
            session,
            current_item,
            current_run,
            diagnostics=promotion_schedule,
        )
    await session.flush()
    await _insert_event(
        session,
        item_id=item_id,
        event_type="classification",
        field_name="kind",
        old_value=None,
        new_value=json.dumps(
            {
                "source": "llm_refine",
                "classification_run_id": str(run.id),
                "classification_version": run.classification_version,
                "retention_policy_version": run.retention_policy_version,
                "source_type": current_item.source_type,
                "source_trust": current_item.source_trust,
                "source_confidence_prior": current_item.source_confidence_prior,
                "default_memory_confidence": current_item.source_confidence_prior,
                "final_memory_confidence": current_item.memory_confidence,
                "taxonomy_confidence": result.taxonomy_confidence,
                "confidence": result.taxonomy_confidence,
                "retention_confidence": result.retention_confidence,
                "retention_disposition": result.retention_disposition,
                "requested_visibility": initial_visibility,
                "suggested_visibility": result.suggested_visibility,
                "previous_kind": initial_kind,
                "final_kind": current_item.kind,
                "final_review_status": current_item.review_status,
                "previous_visibility": initial_visibility,
                "final_visibility": current_item.visibility,
                "visibility_narrowed": current_item.visibility != initial_visibility,
                "promotion_receipt_matches_item": receipt_matches_item,
                "promotion_job_id": str(promotion_job_id) if promotion_job_id else None,
                "promotion_schedule_status": promotion_schedule.get("status", "not_scheduled"),
                "promotion_schedule_blocker": promotion_schedule.get("blocker"),
                "classification_provenance": run.provenance,
                "provider": run.provenance.get("provider", "openai"),
                "result": "changed" if changed else "no_change",
                "reason": run.reason,
                **provenance,
            },
            sort_keys=True,
        ),
        actor_principal_id=actor,
        reason=json.dumps({**provenance, "reason": run.reason}, sort_keys=True),
        ingest_id=_parse_uuid(job.payload["ingest_id"]) if job.payload.get("ingest_id") else None,
    )

    await session.commit()


async def handle_promotion_path_a(session: AsyncSession, job: Job) -> None:
    """Run a compatible full sweep or a fail-closed targeted Path A job."""
    from engram.promotion import auto_promote_item, auto_promote_proposed_memories

    raw_item_id = job.payload.get("memory_item_id")
    if raw_item_id is None:
        result = await auto_promote_proposed_memories(session, str(job.tenant_id), source="worker")
    else:
        raw_run_id = job.payload.get("classification_run_id")
        if raw_run_id is None:
            raise ValueError("targeted promotion job missing classification_run_id")
        # Targeted candidate-origin promotion reconstructs the remember-time
        # execution authority. Missing v2 authority is a permanent condition
        # (a pre-025 queued job, a deleted/corrupt execution row) that retry
        # cannot heal, and replaying under candidate-origin authority is
        # forbidden. Mirror conflict.check's intentional fail-closed skip so
        # the outer loop marks the job succeeded instead of retrying it toward
        # a dead letter. Catch only the provenance-reconstruction ValueError;
        # genuine transient DB failures and programming defects still reach the
        # retry/dead-letter machinery via the outer handler.
        try:
            memory_context = await _job_memory_context(session, job)
        except ValueError:
            logger.warning(
                "promotion.path_a item=%s skipped: execution authority unavailable",
                raw_item_id,
            )
            return
        result = await auto_promote_item(
            session,
            str(job.tenant_id),
            _parse_uuid(raw_item_id),
            _parse_uuid(raw_run_id),
            memory_context=memory_context,
        )
    logger.info(
        "promotion.path_a tenant=%s scanned=%s promoted=%s",
        job.tenant_id,
        result.scanned,
        result.promoted,
    )


async def handle_retention_sweep(session: AsyncSession, job: Job) -> None:
    """Boundedly remove expired, unbound classification receipts."""
    from engram.classification_evidence import cleanup_expired_unbound_runs

    removed = await cleanup_expired_unbound_runs(session, job.tenant_id)
    await session.commit()
    logger.info(
        "retention.sweep tenant=%s: removed_expired_classification_runs=%s",
        job.tenant_id,
        removed,
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
        logger.info("recall.telemetry recall_log_id=%s already applied, no-op", recall_log_id)
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
    Otherwise processes available jobs serially without delay and sleeps for
    the poll interval only while idle or after an unexpected loop error. Exits
    0 on normal completion; nonzero only on fatal setup errors (ordinary job
    failures do NOT stop the loop).
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

        if did:
            continue

        await asyncio.sleep(interval)


__all__ = [
    "JOB_HANDLERS",
    "process_one_job",
    "run_worker",
]

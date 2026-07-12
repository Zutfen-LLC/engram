"""Review, verification, and conflict resolution endpoints."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, Literal, NoReturn
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from engram.api.routes.memory import (
    _insert_item_event,
    _now_dt,
    _require_eligible_item,
    _resolve_actor_and_delegation,
    _resolve_principal,
)
from engram.auth import REVIEW_SCOPE, WRITE_OR_REVIEW_SCOPE, Principal
from engram.db import get_session
from engram.memory_access import eligibility_expression, eligibility_sql, resolve_workspace_scope
from engram.models import MemoryItem
from engram.review_policy import (
    TransitionOutcome,
    can_human_verify,
    can_resolve_conflict,
    evaluate_transition,
    required_scope_for_review_transition,
)

router = APIRouter()

_ACTOR_PRINCIPAL_ID_DEPRECATION = (
    "Deprecated and ignored — the event actor is always the authenticated "
    "caller. Use on_behalf_of_principal_id for admin-scoped delegation."
)
_ON_BEHALF_OF_DESCRIPTION = (
    "Admin-only. Records this principal as the represented party in the "
    "event's audit metadata. Does not change who the event actor is — the "
    "authenticated caller remains the actor."
)


class ReviewChangeRequest(BaseModel):
    # Validated by the transition policy only after the item eligibility check,
    # preserving non-disclosing 404 behavior for inaccessible item ids.
    review_status: str
    reason: str | None = None
    review_notes: str | None = None
    actor_principal_id: UUID | None = Field(
        default=None, deprecated=True, description=_ACTOR_PRINCIPAL_ID_DEPRECATION
    )
    on_behalf_of_principal_id: UUID | None = Field(
        default=None, description=_ON_BEHALF_OF_DESCRIPTION
    )


class VerifyRequest(BaseModel):
    reason: str | None = None
    on_behalf_of_principal_id: UUID | None = Field(
        default=None, description=_ON_BEHALF_OF_DESCRIPTION
    )


class ConflictResolution(BaseModel):
    resolution: Literal["accepted", "rejected", "merged"]
    reason: str | None = None
    on_behalf_of_principal_id: UUID | None = Field(
        default=None, description=_ON_BEHALF_OF_DESCRIPTION
    )


class ConflictItem(BaseModel):
    id: UUID
    content: str
    kind: str
    conflict_type: str
    conflicts_with_item_id: UUID
    conflict_resolution_status: str
    review_status: str
    created_at: datetime


class ConflictListResponse(BaseModel):
    items: list[ConflictItem]
    total: int


class ConflictResolutionResponse(BaseModel):
    id: UUID
    counterpart_id: UUID
    status: Literal["resolved", "unchanged"]
    conflict_resolution_status: Literal["accepted", "rejected", "merged"]
    resolved_at: datetime | None = None
    resolved_by: UUID | None = None
    resolver_attribution_status: Literal["recorded", "legacy_unknown"]
    event: dict[str, Any] | None = None


class StaleItem(BaseModel):
    id: UUID
    content: str
    kind: str
    wing: str | None = None
    room: str | None = None
    review_status: str
    importance: float
    memory_confidence: float
    last_recalled_at: datetime | None = None
    recall_count: int
    created_at: datetime
    valid_from: datetime
    last_verified_at: datetime | None = None


class StaleListResponse(BaseModel):
    items: list[StaleItem]
    total: int
    days: int


class BulkArchiveRequest(BaseModel):
    item_ids: list[UUID]
    reason: str | None = None
    actor_principal_id: UUID | None = Field(
        default=None, deprecated=True, description=_ACTOR_PRINCIPAL_ID_DEPRECATION
    )
    on_behalf_of_principal_id: UUID | None = Field(
        default=None, description=_ON_BEHALF_OF_DESCRIPTION
    )


class BulkArchiveResponse(BaseModel):
    archived: list[UUID]
    archived_count: int
    skipped: list[UUID]
    skipped_count: int


class ReviewStatsResponse(BaseModel):
    by_review_status: dict[str, int]
    by_kind: dict[str, int]
    by_confidence: dict[str, int]
    total: int


async def _resolve_tenant_id(session: AsyncSession) -> UUID:
    row = await session.execute(text("SELECT current_setting('app.tenant_id', true)"))
    tid_str = row.scalar()
    if not tid_str:
        raise HTTPException(status_code=403, detail="no tenant context")
    return UUID(str(tid_str))


@router.get("/review/queue", response_model=None, dependencies=[Depends(REVIEW_SCOPE)])
async def review_queue(
    kind: str | None = None,
    workspace: str | None = None,
    limit: int = 50,
) -> NoReturn:
    """Items awaiting review (review_status='proposed')."""
    raise NotImplementedError


@router.get(
    "/review/conflicts", response_model=ConflictListResponse, dependencies=[Depends(REVIEW_SCOPE)]
)
async def conflict_queue(
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> ConflictListResponse:
    """Items with unresolved conflicts (conflict_resolution_status='unresolved')."""
    tenant_id = await _resolve_tenant_id(session)
    principal_id, _ = await _resolve_principal(session, tenant_id)
    counterpart = aliased(MemoryItem)
    stmt = (
        select(
            MemoryItem.id,
            MemoryItem.content,
            MemoryItem.kind,
            MemoryItem.conflict_type,
            MemoryItem.conflicts_with_item_id,
            MemoryItem.conflict_resolution_status,
            MemoryItem.review_status,
            MemoryItem.created_at,
        )
        .join(counterpart, counterpart.id == MemoryItem.conflicts_with_item_id)
        .where(
            MemoryItem.tenant_id == tenant_id,
            eligibility_expression(principal_id),
            counterpart.tenant_id == tenant_id,
            eligibility_expression(principal_id, item_entity=counterpart),
            MemoryItem.conflict_resolution_status == "unresolved",
            MemoryItem.conflicts_with_item_id.is_not(None),
        )
        .order_by(MemoryItem.created_at.desc(), MemoryItem.id.desc())
    )
    rows = (await session.execute(stmt)).mappings().all()
    items = [
        ConflictItem(
            id=row["id"],
            content=row["content"],
            kind=row["kind"],
            conflict_type=row["conflict_type"],
            conflicts_with_item_id=row["conflicts_with_item_id"],
            conflict_resolution_status=row["conflict_resolution_status"],
            review_status=row["review_status"],
            created_at=row["created_at"],
        )
        for row in rows
    ]
    return ConflictListResponse(items=items, total=len(items))


@router.get(
    "/review/stale", response_model=StaleListResponse, dependencies=[Depends(REVIEW_SCOPE)]
)
async def stale_items(
    days: int = 90,
    workspace: str | None = None,
    kind: str | None = None,
    limit: int = 100,
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> StaleListResponse:
    """Active items not recalled in N days.

    An item counts as stale when ``COALESCE(last_recalled_at, valid_from)`` is
    older than ``days`` ago. A never-recalled item is measured from
    ``valid_from`` — a NULL ``last_recalled_at`` does not exempt it. Only
    ``active`` items that are not invalidated or superseded are considered.
    """
    if days < 0:
        raise HTTPException(status_code=422, detail="days must be non-negative")
    tenant_id = await _resolve_tenant_id(session)
    principal_id, _ = await _resolve_principal(session, tenant_id)
    limit = max(1, min(limit, 500))
    # Keep this as a datetime so asyncpg binds it as TIMESTAMPTZ.  Passing an
    # ISO string works with permissive test backends but is rejected by the
    # real PostgreSQL driver once the comparison type is inferred.
    cutoff = datetime.now(UTC) - timedelta(days=days)

    clauses = [
        "tenant_id = :caller_tenant_id",
        eligibility_sql(),
        "review_status = 'active'",
        "valid_to IS NULL",
        "superseded_by IS NULL",
        "COALESCE(last_recalled_at, valid_from) < :cutoff",
    ]
    params: dict[str, Any] = {
        "caller_tenant_id": str(tenant_id),
        "caller_principal_id": str(principal_id),
        "cutoff": cutoff,
        "limit": limit,
    }
    if workspace is not None:
        ws_id, accessible = await resolve_workspace_scope(
            session,
            tenant_id=tenant_id,
            principal_id=principal_id,
            workspace=workspace,
        )
        if not accessible:
            return StaleListResponse(items=[], total=0, days=days)
        clauses.append("workspace_id = :workspace_id")
        params["workspace_id"] = str(ws_id)
    if kind is not None:
        clauses.append("kind = :kind")
        params["kind"] = kind
    sql = (
        "SELECT id, content, kind, wing, room, review_status, importance, "
        "memory_confidence, last_recalled_at, recall_count, created_at, "
        "valid_from, last_verified_at "
        "FROM memory_items WHERE "
        + " AND ".join(clauses)
        + " ORDER BY COALESCE(last_recalled_at, valid_from) ASC, created_at ASC, id ASC "
        "LIMIT :limit"
    )
    rows = (await session.execute(text(sql), params)).mappings().all()
    items = [StaleItem(**dict(row)) for row in rows]
    return StaleListResponse(items=items, total=len(items), days=days)


@router.get(
    "/review/stats", response_model=ReviewStatsResponse, dependencies=[Depends(REVIEW_SCOPE)]
)
async def review_stats(
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> ReviewStatsResponse:
    """Hygiene report: counts by review_status, kind, and confidence buckets.

    Counts only current memories (``valid_to IS NULL``). Confidence buckets:
    low (< 0.4), medium (0.4–< 0.7), high (>= 0.7).
    """
    tenant_id = await _resolve_tenant_id(session)
    principal_id, _ = await _resolve_principal(session, tenant_id)
    params = {
        "caller_tenant_id": str(tenant_id),
        "caller_principal_id": str(principal_id),
    }
    base = f"{eligibility_sql()} AND tenant_id = :caller_tenant_id AND valid_to IS NULL "

    status_rows = (
        await session.execute(
            text(
                "SELECT review_status, count(*) FROM memory_items "
                f"WHERE {base}"
                "GROUP BY review_status"
            ),
            params,
        )
    ).all()
    by_review_status: dict[str, int] = {str(row[0]): int(row[1]) for row in status_rows}

    kind_rows = (
        await session.execute(
            text(
                "SELECT kind, count(*) FROM memory_items "
                f"WHERE {base}"
                "GROUP BY kind"
            ),
            params,
        )
    ).all()
    by_kind: dict[str, int] = {str(row[0]): int(row[1]) for row in kind_rows}

    conf_rows = (
        await session.execute(
            text(
                "SELECT CASE "
                "WHEN memory_confidence < 0.4 THEN 'low' "
                "WHEN memory_confidence < 0.7 THEN 'medium' "
                "ELSE 'high' END AS bucket, count(*) "
                "FROM memory_items "
                f"WHERE {base}"
                "GROUP BY bucket"
            ),
            params,
        )
    ).all()
    by_confidence: dict[str, int] = {"low": 0, "medium": 0, "high": 0}
    for row in conf_rows:
        by_confidence[str(row[0])] = int(row[1])

    total = sum(by_review_status.values())
    return ReviewStatsResponse(
        by_review_status=by_review_status,
        by_kind=by_kind,
        by_confidence=by_confidence,
        total=total,
    )


@router.post("/items/{item_id}/review", response_model=None)
async def change_review_status(
    item_id: UUID,
    req: ReviewChangeRequest,
    session: AsyncSession = Depends(get_session),  # noqa: B008
    caller: Principal = Depends(WRITE_OR_REVIEW_SCOPE),  # noqa: B008
) -> dict[str, Any]:
    """Change review_status (proposed -> active, dispute, etc.). Writes item_event.

    Mixed-purpose endpoint (V2-BL-004): collaborative actions (dispute,
    self-withdrawal) only need `write`; privileged review decisions
    (activation, reactivation, rejection, non-author archival) additionally
    require `review`. Scope is necessary but not sufficient — the existing
    principal-type/authorship policy below still decides whether a
    scope-eligible caller may actually perform the transition. Error order is
    deliberate: item eligibility (404) resolves before the transition-scope
    check (403), so a write-scoped caller can't use the 403/404 split to
    probe for inaccessible items.

    The event actor is always the authenticated caller (never the item's
    author, and never the deprecated ``actor_principal_id`` request field) —
    this is what lets the external-dispute predicate
    (``engram.promotion.has_external_dispute_event``) trust that a
    ``disputed`` transition genuinely came from a principal other than the
    item's author, even if the caller supplies the author's id in the request
    body.
    """
    tenant_id = await _resolve_tenant_id(session)
    principal_id, principal_type = await _resolve_principal(session, tenant_id)
    item = await _require_eligible_item(
        session, item_id, tenant_id=tenant_id, principal_id=principal_id, for_update=True
    )
    is_author = UUID(str(item["principal_id"])) == principal_id
    required_scope = required_scope_for_review_transition(
        current_status=str(item["review_status"]),
        requested_status=req.review_status,
        is_author=is_author,
    )
    if required_scope is not None and not caller.has_scope(required_scope):
        raise HTTPException(
            status_code=403, detail=f"Requires scope: {required_scope}"
        )
    decision = evaluate_transition(
        principal_id=principal_id,
        principal_type=principal_type,
        item_author_principal_id=UUID(str(item["principal_id"])),
        current_status=str(item["review_status"]),
        requested_status=req.review_status,
    )
    if decision.outcome is TransitionOutcome.INVALID:
        raise HTTPException(status_code=409, detail="invalid review-state transition")
    if decision.outcome is TransitionOutcome.FORBIDDEN:
        raise HTTPException(
            status_code=403,
            detail="The authenticated principal is not authorized for this review transition.",
        )
    actor, on_behalf_of = await _resolve_actor_and_delegation(
        session, tenant_id=tenant_id, requested_on_behalf_of=req.on_behalf_of_principal_id
    )
    if decision.outcome is TransitionOutcome.NOOP:
        # Delegation is validated above even though a no-op writes nothing, so
        # an unauthorized on_behalf_of_principal_id can't ride a same-state
        # request to a silent 200 (Problem 2 / V2-BL-003A).
        await session.commit()
        return {"item": item, "event": None}
    event = await _insert_item_event(
        session,
        item_id=item_id,
        event_type="review_change",
        field_name="review_status",
        old_value=item.get("review_status"),
        new_value=req.review_status,
        actor_principal_id=actor,
        on_behalf_of_principal_id=on_behalf_of,
        reason=req.reason,
    )
    assignments = ["review_status = :review_status"]
    params: dict[str, Any] = {"review_status": req.review_status, "item_id": str(item_id)}
    if req.review_notes is not None:
        assignments.append("review_notes = :review_notes")
        params["review_notes"] = req.review_notes
    await session.execute(
        text(f"UPDATE memory_items SET {', '.join(assignments)} WHERE id = :item_id"),
        params,
    )
    updated = await _require_eligible_item(
        session, item_id, tenant_id=tenant_id, principal_id=principal_id
    )
    await session.commit()
    return {"item": updated, "event": event}


@router.post(
    "/items/{item_id}/verify", response_model=None, dependencies=[Depends(REVIEW_SCOPE)]
)
async def verify_item(
    item_id: UUID,
    req: VerifyRequest | None = None,
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> dict[str, Any]:
    """Mark an eligible, non-terminal item as verified by the human caller."""
    tenant_id = await _resolve_tenant_id(session)
    principal_id, principal_type = await _resolve_principal(session, tenant_id)
    item = await _require_eligible_item(
        session, item_id, tenant_id=tenant_id, principal_id=principal_id, for_update=True
    )
    if not can_human_verify(principal_type):
        raise HTTPException(status_code=403, detail="human verification requires a user or admin")
    if item["review_status"] in {"rejected", "archived"}:
        raise HTTPException(status_code=409, detail="terminal items cannot be human-verified")
    actor, on_behalf_of = await _resolve_actor_and_delegation(
        session,
        tenant_id=tenant_id,
        requested_on_behalf_of=req.on_behalf_of_principal_id if req else None,
    )
    now = _now_dt()
    reason = req.reason if req else None
    if item.get("human_verified"):
        if UUID(str(item["verified_by"])) != actor:
            raise HTTPException(
                status_code=409, detail="item was already verified by another principal"
            )
        await session.commit()
        return {"item": item, "event": None}
    event = await _insert_item_event(
        session,
        item_id=item_id,
        event_type="verify",
        field_name="human_verified",
        old_value=item.get("human_verified"),
        new_value=True,
        actor_principal_id=actor,
        on_behalf_of_principal_id=on_behalf_of,
        reason=reason,
    )
    await session.execute(
        text(
            "UPDATE memory_items SET human_verified = TRUE, verified_by = :verified_by, "
            "verified_at = :verified_at, last_verified_at = :last_verified_at WHERE id = :item_id"
        ),
        {
            "verified_by": str(actor),
            "verified_at": now,
            "last_verified_at": now,
            "item_id": str(item_id),
        },
    )
    updated = await _require_eligible_item(
        session, item_id, tenant_id=tenant_id, principal_id=principal_id
    )
    await session.commit()
    return {"item": updated, "event": event}


@router.post(
    "/items/{item_id}/resolve-conflict",
    response_model=ConflictResolutionResponse,
    dependencies=[Depends(REVIEW_SCOPE)],
)
async def resolve_conflict(
    item_id: UUID,
    req: ConflictResolution,
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> ConflictResolutionResponse:
    """Human adjudication of conflict metadata, serialized over the item pair."""
    tenant_id = await _resolve_tenant_id(session)
    principal_id, principal_type = await _resolve_principal(session, tenant_id)

    # This first eligible read identifies the candidate pair only.  The locked
    # rows below are the mutation authority.
    item_data = await _require_eligible_item(
        session, item_id, tenant_id=tenant_id, principal_id=principal_id
    )
    counterpart_id = item_data.get("conflicts_with_item_id")
    if counterpart_id is None:
        raise HTTPException(status_code=422, detail="item has no conflict to resolve")
    candidate_counterpart_id = UUID(str(counterpart_id))
    if candidate_counterpart_id == item_id:
        raise HTTPException(status_code=409, detail="conflict changed; retry")

    pair_ids = sorted((item_id, candidate_counterpart_id), key=str)
    pair_stmt = (
        select(MemoryItem)
        .where(
            MemoryItem.id.in_(pair_ids),
            MemoryItem.tenant_id == tenant_id,
            eligibility_expression(principal_id),
        )
        .order_by(MemoryItem.id)
        .with_for_update()
    )
    locked_items = list((await session.execute(pair_stmt)).scalars().all())
    if len(locked_items) != 2:
        raise HTTPException(status_code=404, detail="Item not found")
    locked_by_id = {row.id: row for row in locked_items}
    target = locked_by_id.get(item_id)
    counterpart = locked_by_id.get(candidate_counterpart_id)
    if target is None or counterpart is None:
        raise HTTPException(status_code=404, detail="Item not found")
    if (
        target.tenant_id != tenant_id
        or counterpart.tenant_id != tenant_id
        or target.conflicts_with_item_id is None
        or target.conflicts_with_item_id != candidate_counterpart_id
        or target.id == target.conflicts_with_item_id
    ):
        raise HTTPException(status_code=409, detail="conflict changed; retry")

    # Both resources have been resolved before actor-class authorization is
    # disclosed. Scope admission remains the route dependency above.
    if not can_resolve_conflict(principal_type):
        raise HTTPException(
            status_code=403, detail="principal type may not resolve conflicts"
        )
    actor, on_behalf_of = await _resolve_actor_and_delegation(
        session, tenant_id=tenant_id, requested_on_behalf_of=req.on_behalf_of_principal_id
    )
    if on_behalf_of is not None:
        represented_internal_key = (
            await session.execute(
                text("SELECT internal_key FROM principals WHERE id = :pid"),
                {"pid": str(on_behalf_of)},
            )
        ).scalar_one_or_none()
        if represented_internal_key is not None:
            raise HTTPException(status_code=404, detail="principal not found")

    current_status = target.conflict_resolution_status
    if current_status in {"accepted", "rejected", "merged"}:
        if current_status != req.resolution:
            raise HTTPException(status_code=409, detail="conflict is already resolved")
        return ConflictResolutionResponse(
            id=item_id,
            counterpart_id=candidate_counterpart_id,
            status="unchanged",
            conflict_resolution_status=req.resolution,
            resolved_at=target.conflict_resolved_at,
            resolved_by=target.conflict_resolved_by,
            resolver_attribution_status=(
                "recorded" if target.conflict_resolved_by is not None else "legacy_unknown"
            ),
            event=None,
        )
    if current_status != "unresolved":
        raise HTTPException(status_code=409, detail="conflict changed; retry")

    resolved_at = _now_dt()
    update_result = await session.execute(
        update(MemoryItem)
        .where(
            MemoryItem.id == item_id,
            MemoryItem.tenant_id == tenant_id,
            MemoryItem.conflict_resolution_status == "unresolved",
            MemoryItem.conflicts_with_item_id == candidate_counterpart_id,
        )
        .values(
            conflict_resolution_status=req.resolution,
            conflict_resolved_by=actor,
            conflict_resolved_at=resolved_at,
        )
        .returning(MemoryItem.id)
    )
    if update_result.scalar_one_or_none() is None:
        await session.rollback()
        raise HTTPException(status_code=409, detail="conflict changed; retry")
    event = await _insert_item_event(
        session,
        item_id=item_id,
        event_type="conflict_resolution",
        field_name="conflict_resolution_status",
        old_value="unresolved",
        new_value=req.resolution,
        actor_principal_id=actor,
        on_behalf_of_principal_id=on_behalf_of,
        reason=req.reason,
    )
    await session.commit()
    return ConflictResolutionResponse(
        id=item_id,
        counterpart_id=candidate_counterpart_id,
        status="resolved",
        conflict_resolution_status=req.resolution,
        resolved_at=resolved_at,
        resolved_by=actor,
        resolver_attribution_status="recorded",
        event=event,
    )


@router.post(
    "/items/bulk-archive", response_model=BulkArchiveResponse, dependencies=[Depends(REVIEW_SCOPE)]
)
async def bulk_archive(
    req: BulkArchiveRequest,
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> BulkArchiveResponse:
    """Archive multiple items: set review_status='archived'.

    Writes an ``item_events`` audit row per changed item, then updates the
    column. Items already in a terminal state (``archived``/``rejected``) and
    items not found in the caller's tenant are skipped. Archival excludes
    items from default recall without invalidating them (they're still true,
    just not actively useful).
    """
    tenant_id = await _resolve_tenant_id(session)
    principal_id, _ = await _resolve_principal(session, tenant_id)
    requested = list(dict.fromkeys(req.item_ids))  # de-dup, preserve order
    if not requested:
        return BulkArchiveResponse(
            archived=[], archived_count=0, skipped=[], skipped_count=0
        )

    fetch_placeholders: list[str] = []
    fetch_params: dict[str, Any] = {
        "tenant_id": str(tenant_id),
        "caller_principal_id": str(principal_id),
    }
    for i, item_id in enumerate(requested):
        fetch_placeholders.append(f":id{i}")
        fetch_params[f"id{i}"] = str(item_id)
    lock_suffix = " FOR UPDATE" if session.bind.dialect.name == "postgresql" else ""
    fetch_sql = text(
        "SELECT id, review_status, principal_id FROM memory_items "
        "WHERE tenant_id = :tenant_id AND "
        f"{eligibility_sql()} AND "
        f"CAST(id AS TEXT) IN ({', '.join(fetch_placeholders)}){lock_suffix}"
    )
    rows = (await session.execute(fetch_sql, fetch_params)).all()
    found: dict[str, tuple[str, object]] = {
        str(row[0]): (str(row[1]), row[2]) for row in rows
    }

    _, principal_type = await _resolve_principal(session, tenant_id)
    to_archive: list[UUID] = []
    skipped: list[UUID] = []
    for item_id in requested:
        entry = found.get(str(item_id))
        if entry is None or entry[0] in ("archived", "rejected"):
            skipped.append(item_id)
            continue
        decision = evaluate_transition(
            principal_id=principal_id,
            principal_type=principal_type,
            item_author_principal_id=UUID(str(entry[1])),
            current_status=entry[0],
            requested_status="archived",
        )
        if decision.allowed:
            to_archive.append(item_id)
        else:
            raise HTTPException(
                status_code=403,
                detail="The authenticated principal is not authorized to archive these items.",
            )

    actor, on_behalf_of = await _resolve_actor_and_delegation(
        session, tenant_id=tenant_id, requested_on_behalf_of=req.on_behalf_of_principal_id
    )

    for item_id in to_archive:
        old_status, _principal_id = found[str(item_id)]
        await _insert_item_event(
            session,
            item_id=item_id,
            event_type="review_change",
            field_name="review_status",
            old_value=old_status,
            new_value="archived",
            actor_principal_id=actor,
            on_behalf_of_principal_id=on_behalf_of,
            reason=req.reason,
        )

    if to_archive:
        update_placeholders: list[str] = []
        update_params: dict[str, Any] = {"tenant_id": str(tenant_id)}
        for i, item_id in enumerate(to_archive):
            update_placeholders.append(f":uid{i}")
            update_params[f"uid{i}"] = str(item_id)
        await session.execute(
            text(
                "UPDATE memory_items SET review_status = 'archived' "
                "WHERE tenant_id = :tenant_id AND "
                f"CAST(id AS TEXT) IN ({', '.join(update_placeholders)})"
            ),
            update_params,
        )

    await session.commit()
    return BulkArchiveResponse(
        archived=to_archive,
        archived_count=len(to_archive),
        skipped=skipped,
        skipped_count=len(skipped),
    )

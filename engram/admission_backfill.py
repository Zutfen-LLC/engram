"""Bounded, restartable legacy import of admission decisions (issue #159).

This is a *snapshot of currently observable state*, never a reconstruction of
a historical policy evaluation. The distinction is the whole point of the
``legacy_import`` mode, and it is enforced here rather than merely documented:

* an item that is already ``active`` gets outcome ``not_applicable`` with no
  ``selected_basis``. It is not a live proposal for this evaluation, and this
  import has no evidence about which lane — if any — admitted it years ago.
  Calling it ``admitted`` would claim this assessment authorized a transition
  it did not make;
* a live proposal that current policy would otherwise admit gets ``unknown``,
  not ``would_admit`` or ``admitted``: the promotion-time conflict recheck was
  never run for it, so the required state genuinely cannot be interpreted
  safely — and ``unknown`` is never coerced into "insufficient evidence";
* ``conflict_recheck_status`` is always ``unavailable_legacy``: no recheck
  fact was recorded, and an honest gap is not a ``clear`` result;
* no ``linked_item_event_id`` is ever written. A legacy import is never
  ``admitted``, so it never authorized an audit event, and the historical
  ``review_change`` reasons do not carry enough stable policy-input identity
  to bind one safely. Linkage stays unavailable rather than guessed;
* #157 references come from real rows or are absent. None is ever
  manufactured for pre-#157 state.

Reapplication is idempotent: a decision that hashes identically to one already
imported for the item is skipped, so a restarted or repeated run converges
rather than appending duplicates. A later authoritative evaluation supersedes
the imported projection by ordinary precedence, without rewriting the import
row.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from engram.admission_assessment import (
    POLICY_PROFILE_KEY,
    LaneQualification,
    build_decision,
    evidence_assessment_refs,
    insert_assessment,
    policy_config_payload,
    project_current,
)
from engram.models import AdmissionAssessment, MemoryItem

# A single invocation never scans more than this many items, whatever the
# caller asks for: the command is an operator tool, not a migration, and an
# unbounded row-per-item pass is exactly what the schema migration refused to
# do.
MAX_BACKFILL_LIMIT = 1000

BACKFILL_TRIGGER_TYPE = "legacy_import"


@dataclass
class BackfillResult:
    """Counts and the cursor to resume from."""

    tenant_id: str
    scanned: int = 0
    imported: int = 0
    skipped_existing: int = 0
    dry_run: bool = False
    last_item_id: uuid.UUID | None = None
    outcomes: dict[str, int] = field(default_factory=dict)

    def summarize(self) -> str:
        action = "would_import" if self.dry_run else "imported"
        outcomes = ", ".join(f"{name}={count}" for name, count in sorted(self.outcomes.items()))
        resume = f" resume_after={self.last_item_id}" if self.last_item_id else ""
        return (
            f"tenant={self.tenant_id} scanned={self.scanned} {action}={self.imported} "
            f"skipped_existing={self.skipped_existing}"
            f"{' [' + outcomes + ']' if outcomes else ''}{resume}"
        )


async def backfill_admission_assessments(
    session: AsyncSession,
    tenant_id: str,
    *,
    limit: int = 100,
    after_item_id: uuid.UUID | None = None,
    dry_run: bool = False,
    now: datetime | None = None,
) -> BackfillResult:
    """Import one bounded, deterministic page of current ``proposed``/``active`` items.

    Ordering is by item ID so a run is restartable from ``--after`` and two
    runs over the same page do the same work. Nothing here mutates item state,
    review status, or any existing audit row.
    """
    from engram.promotion import (
        _admission_decision_inputs,
        _admission_timing,
        _config,
        _config_values,
        _lane_qualification,
        assess_promotion_candidate,
        load_promotion_support,
    )

    moment = now or datetime.now(UTC)
    bounded = max(1, min(limit, MAX_BACKFILL_LIMIT))
    result = BackfillResult(tenant_id=tenant_id, dry_run=dry_run)
    config = await _config(session, tenant_id)
    _, threshold, min_age, evidence_enabled, evidence_threshold = _config_values(config)

    query = select(MemoryItem).where(
        MemoryItem.tenant_id == uuid.UUID(tenant_id),
        MemoryItem.review_status.in_(("proposed", "active")),
        MemoryItem.valid_to.is_(None),
    )
    if after_item_id is not None:
        query = query.where(MemoryItem.id > after_item_id)
    items = list(
        (await session.scalars(query.order_by(MemoryItem.id.asc()).limit(bounded))).all()
    )
    if not items:
        return result
    support_map = await load_promotion_support(session, items)
    for item in items:
        result.scanned += 1
        result.last_item_id = item.id
        support = support_map[item.id]
        candidate = assess_promotion_candidate(
            item,
            support,
            confidence_threshold=threshold,
            min_age_hours=min_age,
            evidence_enabled=evidence_enabled,
            evidence_threshold=evidence_threshold,
            now=moment,
            conflict_recheck_status="unavailable_legacy",
        )
        live_proposal = item.review_status == "proposed" and item.superseded_by is None
        lanes = _lane_qualification(
            item,
            support,
            confidence_threshold=threshold,
            min_age_hours=min_age,
            evidence_enabled=evidence_enabled,
            evidence_threshold=evidence_threshold,
            now=moment,
        )
        if not live_proposal:
            # An already-active item: record that it is not a live proposal
            # for this policy evaluation, and claim nothing about how it got
            # there. No lane, no blockers, no eligibility clock.
            outcome = "not_applicable"
            selected_basis = None
            blockers: list[str] = []
            neutral_lanes = LaneQualification(False, False, False, False)
        elif not candidate.blockers and candidate.selected_basis is not None:
            # Current policy would otherwise admit, but the promotion-time
            # conflict recheck was never run for this item, so the state
            # required to decide is genuinely uninterpretable here.
            outcome = "unknown"
            selected_basis = candidate.selected_basis
            blockers = []
            neutral_lanes = lanes
        else:
            outcome = None  # let the shared classifier decide from blockers
            selected_basis = candidate.selected_basis
            blockers = list(candidate.blockers)
            neutral_lanes = lanes
        cooling_start, eligible_at, next_evaluation_at = (
            (None, None, None)
            if not live_proposal
            else _admission_timing(item, candidate, lanes)
        )
        decision = build_decision(
            item=item,
            run=support.classification_run,
            mode="legacy_import",
            mutated=False,
            live_proposal=live_proposal,
            blockers=blockers,
            selected_basis=selected_basis,
            lanes=neutral_lanes,
            decision_inputs=_admission_decision_inputs(
                item,
                candidate,
                support,
                lanes,
                min_age_hours=min_age,
                evidence_enabled=evidence_enabled,
            ),
            policy_config=policy_config_payload(
                confidence_threshold=threshold,
                min_age_hours=min_age,
                evidence_enabled=evidence_enabled,
                evidence_threshold=evidence_threshold,
                kind_auto_promote_allowed=candidate.kind_auto_promote_allowed,
            ),
            conflict_recheck_status="unavailable_legacy",
            cooling_period_start=cooling_start,
            eligible_at=eligible_at,
            next_evaluation_at=next_evaluation_at,
            legacy_evidence_unavailable=True,
            outcome_override=outcome,  # type: ignore[arg-type]
        )
        result.outcomes[decision.outcome] = result.outcomes.get(decision.outcome, 0) + 1
        existing = await session.scalar(
            select(AdmissionAssessment.id).where(
                AdmissionAssessment.tenant_id == item.tenant_id,
                AdmissionAssessment.memory_item_id == item.id,
                AdmissionAssessment.mode == "legacy_import",
                AdmissionAssessment.decision_hash == decision.hash(),
            )
        )
        if existing is not None:
            result.skipped_existing += 1
            continue
        result.imported += 1
        if dry_run:
            continue
        row = await insert_assessment(
            session,
            decision,
            trigger_type=BACKFILL_TRIGGER_TYPE,
            trigger_id=f"backfill:{tenant_id}",
            invocation_source="cli.admission-assessments-backfill",
            evaluated_at=moment,
            classification_run_id=candidate.classification_run_id,
            evidence_refs=await evidence_assessment_refs(
                session, tenant_id=item.tenant_id, memory_item_id=item.id
            ),
        )
        # A legacy import may hold the projection only while nothing
        # authoritative does; precedence in project_current guarantees a later
        # real evaluation supersedes it without this row being rewritten.
        await project_current(session, row)
    if dry_run:
        await session.rollback()
    else:
        await session.commit()
    return result


__all__ = [
    "BACKFILL_TRIGGER_TYPE",
    "MAX_BACKFILL_LIMIT",
    "POLICY_PROFILE_KEY",
    "BackfillResult",
    "backfill_admission_assessments",
]

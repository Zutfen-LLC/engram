"""Bounded, read-only simulation for ``risk_aware_shadow_v1`` (issue #158).

This module also owns the shared bulk V2 resolver (issue #186) —
:func:`resolve_bulk_v2_decisions` — so the #158 simulator/operator surfaces
and the #160 shadow-recall evaluator resolve "what is the effective V2
decision?" through one code path and cannot drift.
"""

from __future__ import annotations

import uuid
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final, Literal, cast

from sqlalchemy import literal, select
from sqlalchemy.ext.asyncio import AsyncSession

from engram.admission_assessment import digest
from engram.admission_policy import (
    V2_SCHEMA_VERSION,
    AdmissionItemState,
    AdmissionPolicyDecision,
    EffectiveAssessmentState,
    LoadedAdmissionPolicy,
    evaluate_admission_profile,
    load_admission_policy,
)
from engram.assessments import (
    effective_assessment_selection,
    effective_assessment_selection_bulk,
)
from engram.config import settings
from engram.conflicts import check_promotion_conflict
from engram.memory_access import read_eligibility_expression
from engram.memory_context import ResolvedMemoryContext
from engram.models import AdmissionAssessment, MemoryItem
from engram.promotion import (
    PromotionCandidate,
    PromotionSupport,
    _config,
    _config_values,
    assess_promotion_candidate,
    load_promotion_support,
)
from engram.review_policy import TrustedReviewOperation, evaluate_transition

MAX_SHADOW_SIMULATION_LIMIT = 100
SHADOW_PROFILE_KEY: Final[Literal["risk_aware_shadow_v1"]] = "risk_aware_shadow_v1"
SHADOW_TRIGGER_TYPE = "admission.shadow_simulation"
SHADOW_INVOCATION_SOURCE = "admission.shadow"
SelectionStatus = Literal[
    "selected", "absent", "disabled", "stale", "mismatched", "failed", "uncalibrated"
]
PolicyRiskState = Literal["low", "medium", "high", "unknown", "not_applicable"]

# Canonical V2 resolution vocabulary (issue #186). ``current`` is the only
# status that can carry positive admission authority:
#
# * ``missing``   — no V2 row has ever been persisted for the item;
# * ``stale``     — the latest row's decision hash no longer matches the
#                   exact current evaluation (item state, effective #157
#                   selection, or time-dependent outcome changed since);
# * ``mismatched``— the row was recorded under a different policy artifact
#                   digest (the checked-in policy changed);
# * ``unsupported``— the latest row under the profile key is not a V2 row.
#
# The fresh evaluation below IS the verification: the row is trusted only
# when re-evaluating current state under the current artifact reproduces its
# decision hash — never because a pointer says so.
V2ResolutionStatus = Literal["current", "missing", "stale", "mismatched", "unsupported"]

# ``load_promotion_support`` runs four bounded queries regardless of window
# size; the bulk #157 selection runs at most two (assessment rows, then the
# lateral evidence manifest for items that have rows); the latest-row lookup
# is one. The resolver's query count is therefore constant in the window.
_SUPPORT_QUERY_COUNT: Final[int] = 4
_SELECTION_QUERY_COUNT: Final[int] = 2
_LATEST_ROW_QUERY_COUNT: Final[int] = 1


@dataclass(frozen=True)
class ShadowComparison:
    """One safe comparison between current Path A and a V2 shadow decision."""

    item_id: uuid.UUID
    path_a_compat: dict[str, Any]
    shadow: AdmissionPolicyDecision

    def surface_differences(self) -> dict[str, bool]:
        current_allowed = bool(self.path_a_compat["would_promote"])
        return {
            surface: (decision == "allow") != current_allowed
            for surface, decision in self.shadow.surface_decisions.items()
        }

    def safe_payload(self) -> dict[str, Any]:
        return {
            "item_id": self.item_id,
            "path_a_compat": self.path_a_compat,
            "shadow_profile_key": self.shadow.profile_key,
            "shadow_policy_version": self.shadow.policy_version,
            "shadow_policy_digest": self.shadow.policy_config_digest,
            "shadow_decision_hash": self.shadow.decision_hash,
            "highest_admission_tier": self.shadow.highest_admission_tier,
            "surface_decisions": dict(self.shadow.surface_decisions),
            "risk_state": self.shadow.risk_state,
            "epistemic_state": self.shadow.epistemic_state,
            "retention_state": self.shadow.retention_state,
            "blocker_codes": list(self.shadow.blocker_codes),
            "reason_codes": list(self.shadow.reason_codes),
            "next_actions": list(self.shadow.next_actions),
            "observation_window_hours": self.shadow.observation_window_hours,
            "eligible_at": self.shadow.eligible_at,
            "next_evaluation_at": self.shadow.next_evaluation_at,
            "surface_differs_from_path_a": self.surface_differences(),
        }

    def detail_payload(self) -> dict[str, Any]:
        return {
            **self.safe_payload(),
            "effective_memory_assessment_refs": [
                dict(ref) for ref in self.shadow.effective_assessment_refs
            ],
        }


@dataclass(frozen=True)
class ShadowSimulationPage:
    comparisons: tuple[ShadowComparison, ...]
    scanned_count: int
    next_after: uuid.UUID | None
    changed_admissions: int
    changed_exclusions: int
    changed_review_routing: int
    strata_counts: dict[str, dict[str, int]]


@dataclass(frozen=True)
class ResolvedV2Decision:
    """One item's effective V2 state under ``risk_aware_shadow_v1``.

    ``decision`` is always the exact fresh evaluation against current item
    state, the effective #157 selection, the loaded policy artifact, and the
    caller's evaluation time — the same evaluation :func:`simulate_item`
    produces, which is what makes #158/#160 parity mechanical rather than
    aspirational. ``assessment`` is the latest persisted V2 shadow row (the
    durable artifact the decision binds to), and ``status`` says whether that
    row still *is* the fresh decision (``current``) or why it cannot carry
    authority. Nothing here mutates: no projection is written, no review
    status changes, no provider is called.
    """

    item_id: uuid.UUID
    status: V2ResolutionStatus
    decision: AdmissionPolicyDecision
    assessment: AdmissionAssessment | None


@dataclass(frozen=True)
class BulkV2Resolution:
    """The resolved V2 state for one bounded candidate window."""

    policy: LoadedAdmissionPolicy
    evaluation_time: datetime
    query_count: int
    items: Mapping[uuid.UUID, ResolvedV2Decision]

    def summary(self) -> dict[str, Any]:
        """Safe operator-facing resolution summary (no item identity)."""
        counts = Counter(resolved.status for resolved in self.items.values())
        return {
            "profile_key": self.policy.profile_key,
            "policy_version": self.policy.policy_version,
            "policy_artifact_digest": self.policy.artifact_digest,
            "evaluation_time": self.evaluation_time.isoformat(),
            "resolved_count": len(self.items),
            "resolution_status_counts": dict(sorted(counts.items())),
            "query_count": self.query_count,
        }


def _evaluate_shadow_decision(
    item: MemoryItem,
    *,
    support: PromotionSupport,
    selection_values: dict[str, Any],
    policy: LoadedAdmissionPolicy,
    evaluation_time: datetime,
) -> AdmissionPolicyDecision:
    """The one shared #158 evaluation: snapshot in, exact decision out.

    Both the simulator (:func:`simulate_item` / :func:`simulate_tenant_page`)
    and the #186 bulk resolver evaluate through this helper, so per-surface
    decisions and decision hashes are identical by construction.
    """
    assessment = _assessment_state(selection_values, policy)
    return evaluate_admission_profile(
        _item_state(item, support, assessment), assessment, policy, evaluation_time
    )


async def resolve_bulk_v2_decisions(
    session: AsyncSession,
    *,
    items: list[MemoryItem],
    context: ResolvedMemoryContext,
    evaluation_time: datetime,
    policy: LoadedAdmissionPolicy | None = None,
) -> BulkV2Resolution:
    """Resolve the effective V2 decision for a bounded window, in bulk.

    The query count is constant in the window (support + bulk #157 selection
    + one latest-row lookup); there is no per-item N-query resolution. Every
    item gets an entry — an item with no persisted V2 row resolves
    ``missing`` (explicit, distinguishable from a policy ``unknown``
    decision), never as low-risk or review-active. The resolver reads only:
    it never projects a shadow row current, mutates review state, enqueues
    work, or calls a provider. All items must belong to one tenant and be
    read-eligible for ``context`` (the caller's RLS session enforces this).
    """
    resolved_policy = policy or load_admission_policy(SHADOW_PROFILE_KEY)
    if not items:
        return BulkV2Resolution(resolved_policy, evaluation_time, 0, {})
    query_count = _SUPPORT_QUERY_COUNT
    support = await load_promotion_support(session, items)
    selections: dict[uuid.UUID, dict[str, Any]] = {
        item.id: {"selection_status": "disabled", "combined": None} for item in items
    }
    if settings.assessment_selection_enabled:
        query_count += _SELECTION_QUERY_COUNT
        selections = await effective_assessment_selection_bulk(session, items, context)
    query_count += _LATEST_ROW_QUERY_COUNT
    latest_rows = (
        await session.scalars(
            select(AdmissionAssessment)
            .where(
                AdmissionAssessment.tenant_id == items[0].tenant_id,
                AdmissionAssessment.memory_item_id.in_([item.id for item in items]),
                AdmissionAssessment.policy_profile_key == SHADOW_PROFILE_KEY,
            )
            .order_by(
                AdmissionAssessment.memory_item_id,
                AdmissionAssessment.evaluated_at.desc(),
                AdmissionAssessment.id.desc(),
            )
            .distinct(AdmissionAssessment.memory_item_id)
        )
    ).all()
    by_item = {row.memory_item_id: row for row in latest_rows}
    resolved: dict[uuid.UUID, ResolvedV2Decision] = {}
    for item in items:
        decision = _evaluate_shadow_decision(
            item,
            support=support[item.id],
            selection_values=selections.get(
                item.id, {"selection_status": "disabled", "combined": None}
            ),
            policy=resolved_policy,
            evaluation_time=evaluation_time,
        )
        row = by_item.get(item.id)
        if row is None:
            status: V2ResolutionStatus = "missing"
        elif row.schema_version != V2_SCHEMA_VERSION:
            status = "unsupported"
        elif row.policy_config_digest != resolved_policy.artifact_digest:
            status = "mismatched"
        elif row.decision_hash != decision.decision_hash:
            status = "stale"
        else:
            status = "current"
        resolved[item.id] = ResolvedV2Decision(
            item_id=item.id, status=status, decision=decision, assessment=row
        )
    return BulkV2Resolution(
        policy=resolved_policy,
        evaluation_time=evaluation_time,
        query_count=query_count,
        items=resolved,
    )


def _assessment_state(
    values: dict[str, Any], policy: LoadedAdmissionPolicy
) -> EffectiveAssessmentState:
    """Turn #157's selected public projection into a policy input snapshot."""
    if not settings.assessment_selection_enabled:
        return EffectiveAssessmentState(
            selection_status="disabled",
            contract_hash=None,
            assessment_refs=(),
            risk_state="unknown",
            epistemic_state="unknown",
            retention_state="unknown",
            calibrated=False,
        )
    selection_status = values.get("selection_status")
    combined = values.get("combined")
    if selection_status in {"disabled", "absent", "stale", "mismatched", "failed"}:
        return EffectiveAssessmentState(
            selection_status=cast(SelectionStatus, selection_status),
            contract_hash=None,
            assessment_refs=(),
            risk_state="unknown",
            epistemic_state="unknown",
            retention_state="unknown",
            calibrated=False,
        )
    if not isinstance(combined, dict):
        return EffectiveAssessmentState(
            selection_status="absent",
            contract_hash=None,
            assessment_refs=(),
            risk_state="unknown",
            epistemic_state="unknown",
            retention_state="unknown",
            calibrated=False,
        )
    contract_hash = combined.get("contract_hash")
    dimensions = combined.get("dimensions")
    if not isinstance(contract_hash, str) or not isinstance(dimensions, dict):
        return EffectiveAssessmentState(
            selection_status="failed",
            contract_hash=None,
            assessment_refs=(),
            risk_state="unknown",
            epistemic_state="unknown",
            retention_state="unknown",
            calibrated=False,
        )
    selection_matches_policy = (
        settings.assessment_policy_version == policy.assessment_policy_version
        and settings.assessment_effective_contract_hash == contract_hash
        and contract_hash in policy.accepted_contract_hashes
    )
    status = "selected" if selection_matches_policy else "mismatched"
    raw_risk = dimensions.get("risk", "unknown")
    risk = "medium" if raw_risk == "moderate" else raw_risk
    if risk not in {"low", "medium", "high", "unknown"}:
        risk = "unknown"
    epistemic = dimensions.get("epistemic_state", "unknown")
    if epistemic not in {
        "supported", "contested", "insufficient_evidence", "unknown", "not_applicable"
    }:
        epistemic = "unknown"
    retention = dimensions.get("retention_disposition", "unknown")
    if retention not in {"retain", "transient", "noise", "uncertain"}:
        retention = "unknown"
    score = dimensions.get("epistemic")
    calibrated = isinstance(score, dict) and score.get("status") == "calibrated"
    if status == "selected" and epistemic == "supported" and not calibrated:
        status = "uncalibrated"
    ref = {
        "assessment_id": str(combined.get("assessment_id", "")),
        "contract_hash": contract_hash,
        "canonical_hash": str(combined.get("canonical_hash", "")),
        "purpose": "combined",
        "assertion_mode": str(dimensions.get("assertion_mode", "unknown")),
        "origin": str(dimensions.get("origin", "unknown")),
    }
    return EffectiveAssessmentState(
        selection_status=cast(SelectionStatus, status),
        contract_hash=contract_hash,
        assessment_refs=(ref,),
        risk_state=cast(PolicyRiskState, risk),
        epistemic_state=epistemic,
        retention_state=retention,
        calibrated=calibrated,
    )


def _item_state(
    item: MemoryItem, support: PromotionSupport, assessment: EffectiveAssessmentState
) -> AdmissionItemState:
    review = evaluate_transition(
        principal_id=item.principal_id,
        principal_type="system",
        item_author_principal_id=item.principal_id,
        current_status=item.review_status,
        requested_status="active",
        trusted_operation=TrustedReviewOperation.PROMOTION,
    )
    return AdmissionItemState(
        item_id=str(item.id),
        tenant_id=str(item.tenant_id),
        content_hash=item.content_hash,
        kind=item.kind,
        source_type=item.source_type,
        assertion_mode=(
            str(assessment.assessment_refs[0].get("assertion_mode", "unknown"))
            if assessment.assessment_refs
            else "unknown"
        ),
        origin=(
            str(assessment.assessment_refs[0].get("origin", "unknown"))
            if assessment.assessment_refs
            else "unknown"
        ),
        review_status=item.review_status,
        created_at=item.created_at,
        valid_to=item.valid_to,
        superseded_by=str(item.superseded_by) if item.superseded_by else None,
        unresolved_conflict=item.conflict_resolution_status == "unresolved",
        external_dispute=support.has_external_dispute or support.has_external_noise_feedback,
        governed_review_required=not review.allowed or not bool(
            support.kind and support.kind.enabled and support.kind.auto_promote_from_inferred
        ),
        human_verified=bool(item.human_verified),
    )


def _compat_payload(candidate: PromotionCandidate) -> dict[str, Any]:
    """Adapt, rather than duplicate, the existing Path A evaluator result."""
    return {
        "policy_profile_key": "path_a_compat",
        "would_promote": candidate.would_promote,
        "selected_basis": candidate.selected_basis,
        "outcome": "would_admit" if candidate.would_promote else "withhold",
        "blocker_codes": sorted(set(candidate.blockers)),
        "eligible_at": candidate.eligible_at,
        "next_evaluation_at": candidate.eligible_at if "age" in candidate.blockers else None,
    }


async def simulate_item(
    session: AsyncSession,
    *,
    item: MemoryItem,
    context: ResolvedMemoryContext,
    evaluation_time: datetime,
    policy: LoadedAdmissionPolicy | None = None,
    support: PromotionSupport | None = None,
) -> ShadowComparison:
    """Compare one eligible item without mutating lifecycle or queue state."""
    resolved_policy = policy or load_admission_policy(SHADOW_PROFILE_KEY)
    item_support = support or (await load_promotion_support(session, [item]))[item.id]
    config = await _config(session, str(item.tenant_id))
    enabled, threshold, min_age, evidence_enabled, evidence_threshold = _config_values(config)
    path_a = assess_promotion_candidate(
        item,
        item_support,
        confidence_threshold=threshold,
        min_age_hours=min_age,
        evidence_enabled=evidence_enabled,
        evidence_threshold=evidence_threshold,
        now=evaluation_time,
        conflict_recheck_status="not_run",
    )
    if not enabled:
        path_a.would_promote = False
        path_a.blockers.append("promotion_disabled")
    elif path_a.would_promote:
        conflict = await check_promotion_conflict(session, item, memory_context=context)
        path_a.conflict_recheck_status = "blocked" if conflict else "clear"
        if conflict is not None:
            path_a.would_promote = False
            path_a.blockers.append("conflict_recheck")
    values = await effective_assessment_selection(session, item, context)
    shadow = _evaluate_shadow_decision(
        item,
        support=item_support,
        selection_values=values,
        policy=resolved_policy,
        evaluation_time=evaluation_time,
    )
    return ShadowComparison(item.id, _compat_payload(path_a), shadow)


async def simulate_items_preloaded(
    session: AsyncSession,
    *,
    items: list[MemoryItem],
    context: ResolvedMemoryContext,
    evaluation_time: datetime,
    policy: LoadedAdmissionPolicy | None = None,
    config_values: tuple[bool, float, int, bool, float] | None = None,
) -> tuple[ShadowComparison, ...]:
    """Simulate a page with bulk-loaded support and #157 selection.

    The same comparison :func:`simulate_item` produces per item — including
    the shared ``_evaluate_shadow_decision`` core — but with promotion
    support, tenant config, and effective #157 selection loaded in bulk (the
    same bulk selection the #186 V2 resolver uses), instead of per-item
    queries. Only the promotion-time conflict recheck remains per-item, and
    only for items Path A would otherwise promote.
    """
    resolved_policy = policy or load_admission_policy(SHADOW_PROFILE_KEY)
    if not items:
        return ()
    support = await load_promotion_support(session, items)
    selections = await effective_assessment_selection_bulk(session, items, context)
    if config_values is None:
        config = await _config(session, str(items[0].tenant_id))
        config_values = _config_values(config)
    enabled, threshold, min_age, evidence_enabled, evidence_threshold = config_values
    comparisons: list[ShadowComparison] = []
    for item in items:
        item_support = support[item.id]
        path_a = assess_promotion_candidate(
            item,
            item_support,
            confidence_threshold=threshold,
            min_age_hours=min_age,
            evidence_enabled=evidence_enabled,
            evidence_threshold=evidence_threshold,
            now=evaluation_time,
            conflict_recheck_status="not_run",
        )
        if not enabled:
            path_a.would_promote = False
            path_a.blockers.append("promotion_disabled")
        elif path_a.would_promote:
            conflict = await check_promotion_conflict(session, item, memory_context=context)
            path_a.conflict_recheck_status = "blocked" if conflict else "clear"
            if conflict is not None:
                path_a.would_promote = False
                path_a.blockers.append("conflict_recheck")
        shadow = _evaluate_shadow_decision(
            item,
            support=item_support,
            selection_values=selections.get(
                item.id, {"selection_status": "disabled", "combined": None}
            ),
            policy=resolved_policy,
            evaluation_time=evaluation_time,
        )
        comparisons.append(ShadowComparison(item.id, _compat_payload(path_a), shadow))
    return tuple(comparisons)


async def simulate_tenant_page(
    session: AsyncSession,
    *,
    context: ResolvedMemoryContext,
    evaluation_time: datetime,
    limit: int,
    after: uuid.UUID | None = None,
    workspace_id: uuid.UUID | None = None,
) -> ShadowSimulationPage:
    """Return one eligibility-filtered, keyset-paginated shadow page."""
    if not 1 <= limit <= MAX_SHADOW_SIMULATION_LIMIT:
        raise ValueError(f"limit must be between 1 and {MAX_SHADOW_SIMULATION_LIMIT}")
    query = select(MemoryItem).where(read_eligibility_expression(context))
    if workspace_id is not None:
        query = query.where(MemoryItem.workspace_id == workspace_id)
    if after is not None:
        query = query.where(MemoryItem.id > literal(after))
    items = list((await session.scalars(query.order_by(MemoryItem.id).limit(limit + 1))).all())
    page_items = items[:limit]
    policy = load_admission_policy(SHADOW_PROFILE_KEY)
    comparisons = await simulate_items_preloaded(
        session,
        items=page_items,
        context=context,
        evaluation_time=evaluation_time,
        policy=policy,
    )
    changed_admissions = sum(
        comparison.path_a_compat["would_promote"]
        and comparison.shadow.surface_decisions["semantic_governed"] != "allow"
        for comparison in comparisons
    )
    changed_exclusions = sum(
        not comparison.path_a_compat["would_promote"]
        and comparison.shadow.surface_decisions["semantic_governed"] == "allow"
        for comparison in comparisons
    )
    changed_review = sum(
        comparison.shadow.surface_decisions["semantic_governed"] == "review_required"
        and comparison.path_a_compat["outcome"] != "review_required"
        for comparison in comparisons
    )
    strata: dict[str, dict[str, int]] = {
        "source_type": {},
        "assertion_mode": {},
        "kind": {},
        "risk": {},
        "epistemic": {},
        "evidence": {},
    }
    for item, comparison in zip(page_items, comparisons, strict=True):
        values = {
            "source_type": item.source_type,
            "assertion_mode": (
                str(comparison.shadow.effective_assessment_refs[0].get("assertion_mode", "unknown"))
                if comparison.shadow.effective_assessment_refs
                else "unknown"
            ),
            "kind": item.kind,
            "risk": comparison.shadow.risk_state,
            "epistemic": comparison.shadow.epistemic_state,
            "evidence": "available" if comparison.shadow.effective_assessment_refs else "missing",
        }
        for key, value in values.items():
            strata[key][value] = strata[key].get(value, 0) + 1
    return ShadowSimulationPage(
        comparisons=comparisons,
        scanned_count=len(page_items),
        next_after=page_items[-1].id if len(items) > limit else None,
        changed_admissions=changed_admissions,
        changed_exclusions=changed_exclusions,
        changed_review_routing=changed_review,
        strata_counts=strata,
    )


async def persist_shadow_comparison(
    session: AsyncSession,
    *,
    comparison: ShadowComparison,
    item: MemoryItem,
    actor_principal_id: uuid.UUID,
    evaluated_at: datetime,
    trigger_id: str,
) -> AdmissionAssessment:
    """Append one V2 shadow row.  It never projects current or mutates an item."""
    decision = comparison.shadow
    outcome = (
        "not_applicable"
        if "not_live_proposal" in decision.blocker_codes
        else "blocked"
        if all(value == "blocked" for value in decision.surface_decisions.values())
        else "review_required"
        if "review_required" in decision.surface_decisions.values()
        else "would_admit"
        if decision.surface_decisions["semantic_governed"] == "allow"
        else "insufficient_evidence"
    )
    row = AdmissionAssessment(
        id=uuid.uuid4(),
        tenant_id=item.tenant_id,
        memory_item_id=item.id,
        schema_version=decision.schema_version,
        mode="shadow",
        trigger_type=SHADOW_TRIGGER_TYPE,
        trigger_id=trigger_id,
        invocation_source=SHADOW_INVOCATION_SOURCE,
        actor_principal_id=actor_principal_id,
        evaluated_at=evaluated_at,
        item_content_hash=item.content_hash,
        input_digest=digest(
            {
                "item_id": str(item.id),
                "content_hash": item.content_hash,
                "shadow_decision_hash": decision.decision_hash,
            }
        ),
        resulting_state_digest=None,
        policy_profile_key=decision.profile_key,
        policy_contract_version=decision.policy_version,
        policy_config_digest=decision.policy_config_digest,
        selected_basis=None,
        outcome=outcome,
        blocker_codes=list(decision.blocker_codes),
        reason_codes=list(decision.reason_codes),
        decision_inputs={
            "risk_state": decision.risk_state,
            "epistemic_state": decision.epistemic_state,
            "retention_state": decision.retention_state,
        },
        available_memory_assessment_refs=[],
        risk_state=decision.risk_state,
        epistemic_state=decision.epistemic_state,
        retention_state=decision.retention_state,
        effective_memory_assessment_refs=[dict(ref) for ref in decision.effective_assessment_refs],
        highest_admission_tier=decision.highest_admission_tier,
        surface_decisions=dict(decision.surface_decisions),
        observation_window_hours=decision.observation_window_hours,
        conflict_recheck_status="not_run_preview",
        cooling_period_start=item.created_at if decision.observation_window_hours else None,
        eligible_at=decision.eligible_at,
        next_evaluation_at=decision.next_evaluation_at,
        next_actions=list(decision.next_actions),
        decision_hash=decision.decision_hash,
    )
    session.add(row)
    await session.flush()
    return row


__all__ = [
    "MAX_SHADOW_SIMULATION_LIMIT",
    "SHADOW_PROFILE_KEY",
    "BulkV2Resolution",
    "ResolvedV2Decision",
    "ShadowComparison",
    "ShadowSimulationPage",
    "persist_shadow_comparison",
    "resolve_bulk_v2_decisions",
    "simulate_item",
    "simulate_items_preloaded",
    "simulate_tenant_page",
]

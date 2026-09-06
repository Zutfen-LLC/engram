"""Separated recall signals and the recall admission gate (issue #160).

ENG-RECALL-003 replaces the single blended ``trust_score`` multiplier with
distinct, inspectable signal families that never feed each other:

* **Relevance** — semantic similarity (and, in the legacy profile only,
  relationship/tunnel bonuses). Computed by retrieval, untouched here.
* **Utility** — explicit importance plus freshness. Affects ordering of
  already-admitted items only. Deliberately excludes ``source_trust``,
  ``memory_confidence``, ``human_verified`` (epistemic inputs) and
  ``recall_count`` / exposure counters (feedback-loop safeguard: prior serving
  can never become evidence).
* **Epistemic state** — ``supported`` / ``contested`` / ``insufficient_evidence``
  / ``unknown``, derived from review, conflict, and verification state.
  Unknown evidence is *marked*, never converted into a numeric trust floor.
* **Governance/admission** — the admit/withhold decision with reason codes,
  bound to the durable ``admission_assessments`` projection (#159) when one
  exists. Admission reads governance state only — never similarity,
  importance, or exposure — so popularity can never buy admission.
* **Risk** — structured ``warning_codes`` (machine-readable) alongside the
  legacy free-text ``warnings``.

All pure functions here are deterministic and unit-tested without a DB; the
only coroutine is the bounded bulk assessment loader. Scoring identity is
pinned by :data:`SIGNALS_VERSION` / :data:`RECALL_ADMISSION_POLICY_VERSION`
and surfaced on every served item and recall log.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final, Literal

from sqlalchemy.ext.asyncio import AsyncSession

from engram.config import settings
from engram.models import MemoryItem
from engram.recall_profiles import RecallProfileSpec

SIGNALS_VERSION: Final[Literal["recall-signals-v1"]] = "recall-signals-v1"
RECALL_ADMISSION_POLICY_VERSION: Final[Literal["recall-admission-v1"]] = "recall-admission-v1"

EpistemicState = Literal["supported", "contested", "insufficient_evidence", "unknown"]

# ---- utility weights ----
#
# utility = 0.7 * importance + 0.3 * freshness   (30-day linear decay, anchored
# on valid_from/created_at). Importance is the caller's explicit priority;
# freshness is task/context fit. Both are ordering signals among admitted
# items — never admission or epistemic evidence.
_UTILITY_W_IMPORTANCE: Final = 0.7
_UTILITY_W_FRESHNESS: Final = 0.3
_UTILITY_FRESHNESS_HALFLIFE_DAYS: Final = 30.0

# ---- rank shape ----
#
# rank = similarity * (UTILITY_RANK_FLOOR + (1 - UTILITY_RANK_FLOOR) * utility)
#
# Relevance stays dominant (utility is compressed into the upper half of the
# multiplier); utility then breaks ordering among equally relevant admitted
# items. Deterministic, reproducible, and explainable from published inputs.
_UTILITY_RANK_FLOOR: Final = 0.5

# Free-text mirrors of the machine-readable warning codes (kept for callers
# that render human warnings; codes are the contract, text is presentation).
_WARNING_TEXT: Final[dict[str, str]] = {
    "unreviewed": "unreviewed",
    "evidence_unknown": "evidence state unknown",
    "conflict_unresolved": "unresolved conflicts",
    "disputed": "disputed — pending resolution",
    "admission_assessment_stale": "admission assessment stale",
    "admission_legacy_import": "legacy-imported admission state",
    "low_confidence": "low confidence",
}


@dataclass(frozen=True)
class AdmissionAssessmentBinding:
    """The durable admission state (#159) of one item, resolved for recall.

    ``status`` is the digest-verified projection status; ``missing`` never
    appears here because a missing projection yields no binding at all.
    """

    assessment_id: str
    status: Literal["current", "stale", "legacy_import"]
    outcome: str


@dataclass(frozen=True)
class RecallAdmissionDecision:
    """One item's admit/withhold decision under one recall profile."""

    profile: str
    decision: Literal["admit", "withhold"]
    reason_codes: tuple[str, ...]
    assessment_id: str | None = None
    assessment_status: str | None = None
    assessment_outcome: str | None = None

    def payload(self) -> dict[str, Any]:
        """The safe per-item admission block served with recalled items."""
        return {
            "profile": self.profile,
            "decision": self.decision,
            "policy_version": RECALL_ADMISSION_POLICY_VERSION,
            "reason_codes": list(self.reason_codes),
            "assessment_id": self.assessment_id,
            "assessment_status": self.assessment_status,
            "assessment_outcome": self.assessment_outcome,
        }


# ---- utility ----


def compute_utility_score(
    *,
    importance: float,
    created_at: datetime | None,
    valid_from: datetime | None,
    now: datetime,
) -> float:
    """Explicit priority plus freshness, bounded to ``[0, 1]``.

    Epistemic inputs (source trust, confidence, verification) and exposure
    counters (recall counts) are deliberately not parameters — they must never
    move utility.
    """
    anchor = valid_from or created_at
    freshness = 0.0
    if anchor is not None:
        days = max(0.0, (now - anchor).total_seconds() / 86400.0)
        freshness = max(0.0, 1.0 - days / _UTILITY_FRESHNESS_HALFLIFE_DAYS)
    utility = _UTILITY_W_IMPORTANCE * importance + _UTILITY_W_FRESHNESS * freshness
    return round(max(0.0, min(1.0, utility)), 4)


# ---- epistemic state ----


def derive_epistemic_state(
    *,
    review_status: str,
    human_verified: bool,
    conflict_resolution_status: str | None,
) -> EpistemicState:
    """Classify the item's evidence state — never a number, never blended.

    Precedence: an unadmitted proposal is ``unknown`` regardless of other
    signals (even human verification — it predates admission); an unresolved
    conflict or dispute is ``contested``; human verification is ``supported``;
    anything else has been governance-admitted without human evidence, which
    is honestly ``insufficient_evidence``.
    """
    if review_status == "proposed":
        return "unknown"
    if review_status == "disputed" or conflict_resolution_status == "unresolved":
        return "contested"
    if human_verified:
        return "supported"
    return "insufficient_evidence"


# ---- ranking ----


def compute_signal_rank_score(*, similarity: float, utility: float) -> float:
    """Rank admitted items: relevance-dominant, utility as the ordering term."""
    multiplier = _UTILITY_RANK_FLOOR + (1.0 - _UTILITY_RANK_FLOOR) * max(0.0, min(1.0, utility))
    return round(max(0.0, min(1.0, similarity)) * multiplier, 4)


# ---- structured warnings ----


def structured_warning_codes(
    *,
    review_status: str,
    conflict_resolution_status: str | None,
    epistemic_state: str | None = None,
    assessment_status: str | None = None,
    memory_confidence: float | None = None,
) -> list[str]:
    """Machine-readable handling codes for one admitted item.

    Codes are the contract (SDK/MCP render or branch on them); the free-text
    ``warnings`` list is derived from these via :data:`_WARNING_TEXT`.
    Emitted in a fixed order so payloads are byte-stable for equal state.
    """
    codes: list[str] = []
    if review_status == "proposed":
        codes.append("unreviewed")
    if epistemic_state == "unknown":
        codes.append("evidence_unknown")
    if conflict_resolution_status == "unresolved" or review_status == "disputed":
        codes.append("conflict_unresolved")
    if review_status == "disputed":
        codes.append("disputed")
    if assessment_status == "stale":
        codes.append("admission_assessment_stale")
    elif assessment_status == "legacy_import":
        codes.append("admission_legacy_import")
    if memory_confidence is not None and memory_confidence < 0.5:
        codes.append("low_confidence")
    return codes


# ---- admission ----


def _admit_reviewed(
    *,
    profile: RecallProfileSpec,
    assessment: AdmissionAssessmentBinding | None,
    strict_stale: bool,
) -> RecallAdmissionDecision:
    """Admission for governance-admitted (active) items.

    ``strict_stale`` (governed): a stale assessment cannot authorize serving —
    withhold. Exploratory marks instead (its purpose is bounded uncertainty),
    but an explicit policy ``blocked`` outcome withholds in both profiles.
    A ``legacy_import`` projection is a stored snapshot, never a claim, so it
    can only ever be marked, not authoritative.
    """
    base = ("admitted_review_active",)
    if assessment is None:
        return RecallAdmissionDecision(
            profile=profile.key, decision="admit", reason_codes=base
        )
    if assessment.status == "stale":
        if strict_stale:
            # A stale decision cannot authorize serving; no binding is claimed.
            return RecallAdmissionDecision(
                profile=profile.key,
                decision="withhold",
                reason_codes=("admission_assessment_stale",),
                assessment_status=assessment.status,
            )
        return RecallAdmissionDecision(
            profile=profile.key,
            decision="admit",
            reason_codes=base + ("admission_assessment_stale",),
            assessment_id=assessment.assessment_id,
            assessment_status=assessment.status,
            assessment_outcome=assessment.outcome,
        )
    if assessment.outcome == "blocked":
        return RecallAdmissionDecision(
            profile=profile.key,
            decision="withhold",
            reason_codes=("admission_blocked",),
            assessment_id=assessment.assessment_id,
            assessment_status=assessment.status,
            assessment_outcome=assessment.outcome,
        )
    if assessment.status == "legacy_import":
        return RecallAdmissionDecision(
            profile=profile.key,
            decision="admit",
            reason_codes=base + ("admission_legacy_import",),
            assessment_id=assessment.assessment_id,
            assessment_status=assessment.status,
            assessment_outcome=assessment.outcome,
        )
    return RecallAdmissionDecision(
        profile=profile.key,
        decision="admit",
        reason_codes=base,
        assessment_id=assessment.assessment_id,
        assessment_status=assessment.status,
        assessment_outcome=assessment.outcome,
    )


def decide_recall_admission(
    item: MemoryItem,
    *,
    profile: RecallProfileSpec,
    stay_kinds: set[str],
    assessment: AdmissionAssessmentBinding | None = None,
) -> RecallAdmissionDecision:
    """Admit or withhold one item for one profile's serving mode.

    Reads governance state only (review status, disputed stay-kind policy,
    durable admission outcome) and dispatches on the profile's admission flags
    (``RecallProfileSpec.admits_proposals`` / ``strict_stale``), never its key.
    Similarity, importance, and exposure are not inputs by construction — a
    highly similar or important proposal cannot enter a governed packet, and
    repeated serving cannot raise admission. An explicit policy ``blocked``
    outcome withholds in every profile; a stale assessment withholds when
    ``strict_stale`` and is merely marked otherwise.
    """
    if item.review_status == "active":
        return _admit_reviewed(
            profile=profile, assessment=assessment, strict_stale=profile.strict_stale
        )

    if item.review_status == "disputed":
        # Same doctrine as startup recall: a governed stay kind stays in
        # recall while its dispute is unresolved; everything else leaves. An
        # explicit policy block still wins, exactly as for active items.
        if assessment is not None and assessment.outcome == "blocked":
            return RecallAdmissionDecision(
                profile=profile.key,
                decision="withhold",
                reason_codes=("admission_blocked",),
                assessment_id=assessment.assessment_id,
                assessment_status=assessment.status,
                assessment_outcome=assessment.outcome,
            )
        if item.kind in stay_kinds:
            return RecallAdmissionDecision(
                profile=profile.key,
                decision="admit",
                reason_codes=("admitted_disputed_stay_kind",),
                assessment_id=assessment.assessment_id if assessment is not None else None,
                assessment_status=assessment.status if assessment is not None else None,
                assessment_outcome=assessment.outcome if assessment is not None else None,
            )
        return RecallAdmissionDecision(
            profile=profile.key,
            decision="withhold",
            reason_codes=("review_status_ineligible",),
        )

    if item.review_status == "proposed":
        if profile.admits_proposals:
            if assessment is not None and assessment.outcome == "blocked":
                return RecallAdmissionDecision(
                    profile=profile.key,
                    decision="withhold",
                    reason_codes=("admission_blocked",),
                    assessment_id=assessment.assessment_id,
                    assessment_status=assessment.status,
                    assessment_outcome=assessment.outcome,
                )
            reason_codes: tuple[str, ...] = ("exploratory_proposal",)
            if assessment is not None and assessment.status == "stale":
                reason_codes = reason_codes + ("admission_assessment_stale",)
            return RecallAdmissionDecision(
                profile=profile.key,
                decision="admit",
                reason_codes=reason_codes,
                assessment_id=assessment.assessment_id if assessment is not None else None,
                assessment_status=assessment.status if assessment is not None else None,
                assessment_outcome=assessment.outcome if assessment is not None else None,
            )
        # Governed (and any future strict profile): unadmitted evidence is
        # excluded no matter how relevant or important.
        return RecallAdmissionDecision(
            profile=profile.key,
            decision="withhold",
            reason_codes=("proposed_not_admitted",),
        )

    return RecallAdmissionDecision(
        profile=profile.key,
        decision="withhold",
        reason_codes=("review_status_ineligible",),
    )


# ---- per-item served payload ----


def signal_item_fields(
    item: MemoryItem,
    *,
    decision: RecallAdmissionDecision,
    similarity: float,
    now: datetime,
) -> dict[str, Any]:
    """Build the additive per-item signal fields for an admitted item.

    Returns the separated-signal block (relevance/utility/epistemic/risk +
    the admission receipt) that ``execute_semantic_recall`` merges into the
    served item dict. No blended ``trust_score`` is produced or accepted here.
    """
    utility = compute_utility_score(
        importance=item.importance,
        created_at=item.created_at,
        valid_from=item.valid_from,
        now=now,
    )
    epistemic_state = derive_epistemic_state(
        review_status=item.review_status,
        human_verified=item.human_verified,
        conflict_resolution_status=item.conflict_resolution_status,
    )
    codes = structured_warning_codes(
        review_status=item.review_status,
        conflict_resolution_status=item.conflict_resolution_status,
        epistemic_state=epistemic_state,
        assessment_status=decision.assessment_status,
        memory_confidence=item.memory_confidence,
    )
    rank = compute_signal_rank_score(similarity=similarity, utility=utility)
    reasons = [
        f"relevance {similarity:.2f}",
        f"utility {utility:.2f}",
        f"admission {decision.profile}:{','.join(decision.reason_codes)}",
    ]
    return {
        "score": rank,
        "relevance_score": round(similarity, 4),
        "utility_score": utility,
        "epistemic_state": epistemic_state,
        "warning_codes": codes,
        "warnings": [_WARNING_TEXT[code] for code in codes],
        "reasons": reasons,
        "admission": decision.payload(),
        "signals_version": SIGNALS_VERSION,
    }


# ---- bulk assessment loading ----


async def load_admission_bindings(
    session: AsyncSession,
    *,
    tenant_id: str,
    items: Sequence[MemoryItem],
) -> dict[uuid.UUID, AdmissionAssessmentBinding]:
    """Digest-verified admission bindings for a bounded candidate window.

    Short-circuits to empty while ``admission_assessment_capture_enabled`` is
    off (the default): no rows can exist, so recall pays no extra queries.
    Items with no recorded projection are absent from the result — callers
    treat absence as ``missing``, which the rule-based gate already handles.
    """
    if not settings.admission_assessment_capture_enabled or not items:
        return {}
    from engram.admission_assessment import resolve_bulk_admissions

    resolved = await resolve_bulk_admissions(session, list(items))
    bindings: dict[uuid.UUID, AdmissionAssessmentBinding] = {}
    for item_id, state in resolved.items():
        row = state.assessment
        if row is None or state.status == "missing":
            continue
        bindings[item_id] = AdmissionAssessmentBinding(
            assessment_id=str(row.id),
            status=state.status,
            outcome=str(row.outcome),
        )
    return bindings


__all__ = [
    "RECALL_ADMISSION_POLICY_VERSION",
    "SIGNALS_VERSION",
    "AdmissionAssessmentBinding",
    "EpistemicState",
    "RecallAdmissionDecision",
    "compute_signal_rank_score",
    "compute_utility_score",
    "decide_recall_admission",
    "derive_epistemic_state",
    "load_admission_bindings",
    "signal_item_fields",
    "structured_warning_codes",
]

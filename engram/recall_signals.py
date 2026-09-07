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
  Since issue #188 this local derivation is **legacy/local-profile only**: on
  the V2-bound candidate profiles the served evidence state is the exact
  ``risk_aware_shadow_v1`` fresh evaluation the admission decision already
  consumed (the ``evidence`` block), never a second item-local
  interpretation of review/verification state.
* **Governance/admission** — for the V2-bound candidate profiles
  (``governed`` / ``exploratory``, issue #186) the admit/withhold decision is
  the exact #158 ``risk_aware_shadow_v1`` per-surface decision
  (``RecallProfileSpec.v2_surface``), resolved in bulk by the shared
  ``admission_shadow.resolve_bulk_v2_decisions`` resolver — never reconstructed
  from ``review_status`` or the #159 Path-A binding. Recall-local rules
  survive only as fail-closed defense in depth: the #159 ``blocked``/``stale``
  durable outcome and the mechanically-expressible lifecycle facts can
  withhold, but nothing local can authorize inclusion.
* **Risk** — structured ``warning_codes`` (machine-readable) alongside the
  legacy free-text ``warnings``.

All pure functions here are deterministic and unit-tested without a DB; the
only coroutine is the bounded bulk assessment loader. Scoring identity is
pinned by :data:`SIGNALS_VERSION` / :data:`RECALL_ADMISSION_POLICY_VERSION`
and surfaced on every served item and recall log.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final, Literal, Protocol, cast

from sqlalchemy.ext.asyncio import AsyncSession

from engram.admission_policy import AdmissionPolicyDecision
from engram.models import MemoryItem
from engram.recall_profiles import RecallProfileSpec

SIGNALS_VERSION: Final[Literal["recall-signals-v1"]] = "recall-signals-v1"
# v2 (issue #186): candidate admission consumes the exact #158 V2 per-surface
# decision instead of reconstructing policy from review status.
RECALL_ADMISSION_POLICY_VERSION: Final[Literal["recall-admission-v2"]] = "recall-admission-v2"

# The one V2 policy the candidate profiles consume. Mirrors
# ``admission_shadow.SHADOW_PROFILE_KEY`` (the checked-in artifact's
# ``profile_key``); pinned against drift by the unit tests.
V2_ADMISSION_PROFILE_KEY: Final[Literal["risk_aware_shadow_v1"]] = "risk_aware_shadow_v1"

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
#
# No ``memory_confidence``-derived code exists here by design: that column is
# the historical source-policy prior for automated captures, not epistemic
# confidence (issue #160), and a generic "low confidence" warning on this path
# would reintroduce exactly the conflation #160 removes. Epistemic state stays
# ``unknown``/``insufficient_evidence`` until #157 enrichment lands.
_WARNING_TEXT: Final[dict[str, str]] = {
    "unreviewed": "unreviewed",
    "evidence_unknown": "evidence state unknown",
    "evidence_contested": "evidence contested",
    "evidence_insufficient": "insufficient evidence",
    "conflict_unresolved": "unresolved conflicts",
    "disputed": "disputed — pending resolution",
    # Neutral by design: the exact exploratory surface may legitimately allow
    # a high-risk item, so the mirror must not invent a review requirement
    # the served decision never imposed (issue #188 review finding).
    "risk_high": "high risk",
    "risk_unknown": "unknown risk",
    "admission_assessment_stale": "admission assessment stale",
    "admission_legacy_import": "legacy-imported admission state",
}

# The epistemic presentation vocabulary of the canonical V2 fresh evaluation
# (issue #188). ``not_applicable`` is deliberately absent from the admissible
# presentation set: the V2 gate does not ordinarily admit it, and an admitted
# combination that carries it is a contract break handled fail-closed (see
# :class:`V2EvidenceContractError`), never reinterpreted.
_V2_PRESENTABLE_EPISTEMIC_STATES: Final[frozenset[str]] = frozenset(
    {"supported", "contested", "insufficient_evidence", "unknown"}
)


class V2EvidenceContractError(ValueError):
    """An admitted V2-bound item whose bound evidence state cannot be presented.

    Raised only for impossible admitted combinations (a non-current
    resolution, a non-``allow`` surface decision, or an unpresentable
    epistemic state such as ``not_applicable`` reaching the served payload) —
    the fail-closed alternative to inventing a safe-looking interpretation.
    """


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
class V2PersistedIdentity:
    """What the persisted V2 shadow row says about itself (issue #186).

    Read from the row's own columns — never from the fresh evaluation — so a
    non-current row keeps its own identity: an ``unsupported`` row reports the
    schema it was actually recorded under, a ``mismatched`` row the artifact
    digest it was actually evaluated against, a ``stale`` row the decision
    hash it actually recorded. Safe deterministic identity only.
    """

    assessment_id: str
    schema_version: str
    policy_contract_version: str
    policy_artifact_digest: str
    decision_hash: str

    def payload(self) -> dict[str, Any]:
        return {
            "assessment_id": self.assessment_id,
            "schema_version": self.schema_version,
            "policy_contract_version": self.policy_contract_version,
            "policy_artifact_digest": self.policy_artifact_digest,
            "decision_hash": self.decision_hash,
        }


@dataclass(frozen=True)
class V2FreshEvaluation:
    """What re-evaluating the item now under the current policy produces.

    The same evaluation the shared resolver runs (the #158 simulator's exact
    decision): current schema and policy identity, the fresh decision hash,
    the per-surface outcome this binding consumed, and the bounded state and
    code sets. This is the verification basis — a persisted row is ``current``
    exactly when its recorded identity agrees with this evaluation's.
    """

    schema_version: str
    policy_version: str
    policy_artifact_digest: str
    decision_hash: str
    surface_decision: str | None
    highest_admission_tier: str | None = None
    risk_state: str | None = None
    epistemic_state: str | None = None
    retention_state: str | None = None
    effective_assessment_refs: tuple[Mapping[str, str], ...] = ()
    observation_window_hours: int | None = None
    eligible_at: datetime | None = None
    next_evaluation_at: datetime | None = None
    blocker_codes: tuple[str, ...] = ()
    reason_codes: tuple[str, ...] = ()
    next_actions: tuple[str, ...] = ()

    def payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "policy_version": self.policy_version,
            "policy_artifact_digest": self.policy_artifact_digest,
            "decision_hash": self.decision_hash,
            "surface_decision": self.surface_decision,
            "highest_admission_tier": self.highest_admission_tier,
            "risk_state": self.risk_state,
            "epistemic_state": self.epistemic_state,
            "retention_state": self.retention_state,
            "effective_assessment_refs": [dict(ref) for ref in self.effective_assessment_refs],
            "observation_window_hours": self.observation_window_hours,
            "eligible_at": self.eligible_at.isoformat() if self.eligible_at else None,
            "next_evaluation_at": (
                self.next_evaluation_at.isoformat() if self.next_evaluation_at else None
            ),
            "blocker_codes": list(self.blocker_codes),
            "reason_codes": list(self.reason_codes),
            "next_actions": list(self.next_actions),
        }


@dataclass(frozen=True)
class V2SurfaceBinding:
    """The exact #158 V2 decision a candidate admission consumed (issue #186).

    Persisted-row identity and fresh-evaluation identity are carried
    separately and never collapsed: ``persisted`` is what the durable
    artifact says (``None`` when no row exists), ``fresh`` is what the
    current policy evaluates now (``None`` only when no resolution ran at
    all). For ``current`` they agree; for every other status the differing
    fields stay individually visible (stale hashes, mismatched digests,
    unsupported schemas). Alongside them: resolution status, the exact
    surface and its fresh decision, and the profile key. No provider output,
    no extraction spans, no content, no conflict identities beyond
    policy-level codes.
    """

    profile_key: str
    resolution_status: str
    surface: str
    persisted: V2PersistedIdentity | None
    fresh: V2FreshEvaluation | None

    @property
    def assessment_id(self) -> str | None:
        """The persisted row's id — the durable artifact a current row is."""
        return self.persisted.assessment_id if self.persisted is not None else None

    @property
    def surface_decision(self) -> str | None:
        """The fresh evaluation's decision on this binding's exact surface."""
        return self.fresh.surface_decision if self.fresh is not None else None

    def payload(self) -> dict[str, Any]:
        return {
            "profile_key": self.profile_key,
            "resolution_status": self.resolution_status,
            "surface": self.surface,
            "surface_decision": self.surface_decision,
            "persisted": self.persisted.payload() if self.persisted is not None else None,
            "fresh": self.fresh.payload() if self.fresh is not None else None,
        }


class V2ResolutionLike(Protocol):
    """The resolver result shape the surface gate consumes.

    Implemented by ``admission_shadow.ResolvedV2Decision``; kept structural so
    this module stays import-light and unit-testable without the DB stack.
    Read-only properties so Literal-typed implementations satisfy the
    protocol covariantly.
    """

    @property
    def status(self) -> str: ...

    @property
    def decision(self) -> AdmissionPolicyDecision: ...

    @property
    def assessment(self) -> Any: ...


@dataclass(frozen=True)
class RecallAdmissionDecision:
    """One item's admit/withhold decision under one recall profile."""

    profile: str
    decision: Literal["admit", "withhold"]
    reason_codes: tuple[str, ...]
    assessment_id: str | None = None
    assessment_status: str | None = None
    assessment_outcome: str | None = None
    # Exact #158 V2 surface consumed (V2-bound profiles only).
    surface: str | None = None
    surface_decision: str | None = None
    v2: V2SurfaceBinding | None = None

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
            "surface": self.surface,
            "surface_decision": self.surface_decision,
            "v2": self.v2.payload() if self.v2 is not None else None,
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
    risk_state: str | None = None,
) -> list[str]:
    """Machine-readable handling codes for one admitted item.

    Codes are the contract (SDK/MCP render or branch on them); the free-text
    ``warnings`` list is derived from these via :data:`_WARNING_TEXT`.
    Emitted in a fixed order so payloads are byte-stable for equal state.

    The epistemic codes derive from whatever ``epistemic_state`` the caller
    passes — the locally derived state on a legacy/local profile, the
    canonical V2 fresh evaluation on a V2-bound profile (issue #188):
    ``unknown`` → ``evidence_unknown``, ``contested`` → ``evidence_contested``,
    ``insufficient_evidence`` → ``evidence_insufficient``, ``supported`` → no
    evidence-quality warning. ``risk_state`` is a V2-bound input only: a
    ``high`` or ``unknown`` risk must stay unmistakable even though the
    exploratory surface allowed the item. ``memory_confidence`` is
    deliberately not a parameter: it is the legacy source-policy prior, not
    epistemic confidence, and must never produce a warning that reads as a
    factual-confidence claim.
    """
    codes: list[str] = []
    if review_status == "proposed":
        codes.append("unreviewed")
    if epistemic_state == "unknown":
        codes.append("evidence_unknown")
    elif epistemic_state == "contested":
        codes.append("evidence_contested")
    elif epistemic_state == "insufficient_evidence":
        codes.append("evidence_insufficient")
    if conflict_resolution_status == "unresolved" or review_status == "disputed":
        codes.append("conflict_unresolved")
    if review_status == "disputed":
        codes.append("disputed")
    if risk_state == "high":
        codes.append("risk_high")
    elif risk_state == "unknown":
        codes.append("risk_unknown")
    if assessment_status == "stale":
        codes.append("admission_assessment_stale")
    elif assessment_status == "legacy_import":
        codes.append("admission_legacy_import")
    return codes


# ---- admission ----

# Canonical V2 resolution statuses (mirroring ``admission_shadow``'s closed
# vocabulary). Only ``current`` can carry positive admission authority; every
# other status fails closed with its own reason code so operators can tell a
# missing decision from a stale one from a policy mismatch.
V2_RESOLUTION_STATUSES: Final[tuple[str, ...]] = (
    "current",
    "missing",
    "stale",
    "mismatched",
    "unsupported",
)


def build_v2_surface_binding(
    resolution: V2ResolutionLike | None,
    *,
    surface: str,
) -> V2SurfaceBinding:
    """Assemble the safe binding block from one resolved V2 decision.

    ``resolution=None`` means no V2 state could be resolved at all — the
    binding still names the surface and the explicit ``missing`` status (with
    neither persisted nor fresh identity) so a withheld item's payload never
    reads as "no policy ran". When a resolution exists, the persisted identity
    is read from the row's own columns and the fresh identity from the
    decision the resolver just evaluated; the two are never mixed, so a
    non-current row keeps its own schema, digests, and hash next to the
    current evaluation's.
    """
    if resolution is None:
        return V2SurfaceBinding(
            profile_key=V2_ADMISSION_PROFILE_KEY,
            resolution_status="missing",
            surface=surface,
            persisted=None,
            fresh=None,
        )
    decision = resolution.decision
    assessment = resolution.assessment
    persisted = (
        V2PersistedIdentity(
            assessment_id=str(assessment.id),
            schema_version=str(assessment.schema_version),
            policy_contract_version=str(assessment.policy_contract_version),
            policy_artifact_digest=str(assessment.policy_config_digest),
            decision_hash=str(assessment.decision_hash),
        )
        if assessment is not None
        else None
    )
    fresh = V2FreshEvaluation(
        schema_version=decision.schema_version,
        policy_version=decision.policy_version,
        policy_artifact_digest=decision.policy_config_digest,
        decision_hash=decision.decision_hash,
        surface_decision=decision.surface_decisions.get(surface),
        highest_admission_tier=decision.highest_admission_tier,
        risk_state=decision.risk_state,
        epistemic_state=decision.epistemic_state,
        retention_state=decision.retention_state,
        effective_assessment_refs=tuple(decision.effective_assessment_refs),
        observation_window_hours=decision.observation_window_hours,
        eligible_at=decision.eligible_at,
        next_evaluation_at=decision.next_evaluation_at,
        blocker_codes=tuple(decision.blocker_codes),
        reason_codes=tuple(decision.reason_codes),
        next_actions=tuple(decision.next_actions),
    )
    return V2SurfaceBinding(
        profile_key=decision.profile_key,
        resolution_status=resolution.status,
        surface=surface,
        persisted=persisted,
        fresh=fresh,
    )


def _assessment_withhold(
    profile: RecallProfileSpec,
    assessment: AdmissionAssessmentBinding | None,
) -> RecallAdmissionDecision | None:
    """Durable-admission withholds that win over every other branch.

    An explicit policy ``blocked`` outcome withholds in every profile and
    every review status; a ``stale`` projection cannot authorize serving
    under a strict (governed) profile, again regardless of review status.
    These are recall-local defense in depth (issue #159): they can only
    withhold — they can never fabricate the positive V2 authority the
    candidate profiles now require (issue #186).
    """
    if assessment is None:
        return None
    if assessment.outcome == "blocked":
        return RecallAdmissionDecision(
            profile=profile.key,
            decision="withhold",
            reason_codes=("admission_blocked",),
            assessment_id=assessment.assessment_id,
            assessment_status=assessment.status,
            assessment_outcome=assessment.outcome,
        )
    if assessment.status == "stale" and profile.strict_stale:
        return RecallAdmissionDecision(
            profile=profile.key,
            decision="withhold",
            reason_codes=("admission_assessment_stale",),
            assessment_id=assessment.assessment_id,
            assessment_status=assessment.status,
            assessment_outcome=assessment.outcome,
        )
    return None


def _marking_codes(assessment: AdmissionAssessmentBinding | None) -> tuple[str, ...]:
    """Non-withholding assessment facts an admitted item carries as marks.

    A non-strict (exploratory) stale projection, and any ``legacy_import``
    projection, are stored snapshots — they are reported, never trusted as
    authorization.
    """
    if assessment is None:
        return ()
    if assessment.status == "stale":
        return ("admission_assessment_stale",)
    if assessment.status == "legacy_import":
        return ("admission_legacy_import",)
    return ()


def decide_recall_admission(
    item: MemoryItem,
    *,
    profile: RecallProfileSpec,
    stay_kinds: set[str],
    assessment: AdmissionAssessmentBinding | None = None,
    v2_resolution: V2ResolutionLike | None = None,
) -> RecallAdmissionDecision:
    """Admit or withhold one item for one profile's serving mode.

    V2-bound profiles (``RecallProfileSpec.v2_surface`` set — governed and
    exploratory since issue #186) consume the exact #158 ``risk_aware_shadow_v1``
    per-surface decision resolved in bulk by the shared resolver; ``review_status``
    is no longer a positive admission source and never was allowed to widen a
    boundary. The V2-bound path evaluates its own recall-local defense in depth
    (the #159 ``blocked``/strict-``stale`` binding, then the lifecycle facts the
    corpus window already enforces) *before* consulting the surface decision,
    with the same precedence the generic pre-check below would apply — but,
    unlike that pre-check, it retains the resolved V2 binding on the withheld
    result, so a local withhold never misreports the V2 state as absent.

    Non-V2 profiles keep the pre-#186 review-status policy unchanged (legacy
    never runs this gate at all; the branches remain for any future strict
    local profile): the #159 durable binding (``blocked`` everywhere;
    ``stale`` when the profile is strict) withholds first, then review
    status decides. Similarity, importance, and exposure are not inputs by
    construction — a highly similar or important item cannot buy admission,
    and repeated serving cannot raise it.
    """
    if profile.v2_surface is not None:
        return _decide_v2_surface(
            item, profile=profile, resolution=v2_resolution, assessment=assessment
        )

    withheld = _assessment_withhold(profile, assessment)
    if withheld is not None:
        return withheld

    def _admit(base: tuple[str, ...]) -> RecallAdmissionDecision:
        return RecallAdmissionDecision(
            profile=profile.key,
            decision="admit",
            reason_codes=base + _marking_codes(assessment),
            assessment_id=assessment.assessment_id if assessment is not None else None,
            assessment_status=assessment.status if assessment is not None else None,
            assessment_outcome=assessment.outcome if assessment is not None else None,
        )

    if item.review_status == "active":
        return _admit(("admitted_review_active",))

    if item.review_status == "disputed":
        # Same doctrine as startup recall: a governed stay kind stays in
        # recall while its dispute is unresolved; everything else leaves.
        # (Blocked and strict-stale assessments already withheld above.)
        if item.kind in stay_kinds:
            return _admit(("admitted_disputed_stay_kind",))
        return RecallAdmissionDecision(
            profile=profile.key,
            decision="withhold",
            reason_codes=("review_status_ineligible",),
        )

    if item.review_status == "proposed":
        if profile.admits_proposals:
            # Exploratory: unadmitted evidence may be inspected, marked as
            # the unknown-evidence state it is — never ranked as trusted.
            return _admit(("exploratory_proposal",))
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


def v2_local_gate_withhold_reason(
    item: MemoryItem,
    *,
    profile: RecallProfileSpec,
    assessment: AdmissionAssessmentBinding | None,
) -> str | None:
    """The recall-local defense-in-depth verdict for a V2-bound profile.

    Returns the withhold reason when a mechanically-independent local rule
    (the #159 ``blocked``/strict-``stale`` binding, or the lifecycle facts the
    corpus window already enforces) withholds, or ``None`` when only the V2
    surface decision remains. Local rules can never authorize inclusion
    (issue #186), so ``None`` here means "the V2 gate decides", not "admit".
    """
    withheld = _assessment_withhold(profile, assessment)
    if withheld is not None:
        return withheld.reason_codes[0]
    live_proposal = (
        item.review_status == "proposed"
        and item.valid_to is None
        and item.superseded_by is None
    )
    if not live_proposal:
        return "v2_item_not_live"
    if item.conflict_resolution_status == "unresolved":
        return "conflict_unresolved"
    return None


def _decide_v2_surface(
    item: MemoryItem,
    *,
    profile: RecallProfileSpec,
    resolution: V2ResolutionLike | None,
    assessment: AdmissionAssessmentBinding | None,
) -> RecallAdmissionDecision:
    """The V2-bound admission gate: the exact #158 surface decision or nothing.

    Fail-closed matrix (issue #186): a non-``current`` resolution (missing,
    stale, mismatched, unsupported) withholds with its own explicit reason
    code; a current resolution withholds unless the *exact* surface decision
    is ``allow``. Nothing here can admit on review status, age, confidence,
    importance, exposure, source priors, or a Path-A (#159) binding — the
    legacy ``assessment_*`` payload fields keep their #159 meaning; all V2
    identity lives in the ``v2`` block.

    Ordering: recall-local hard boundaries report first (#159 binding, then
    the lifecycle facts the corpus window already enforces), then V2
    resolution availability, then the exact surface decision. A decision
    withheld by a local rule still carries the full V2 binding — including
    its resolution status and the #159 assessment identity that withheld it
    — so no V2 state is ever hidden, but the most fundamental boundary is
    the stated reason.
    """
    surface = profile.v2_surface
    assert surface is not None  # guarded by the caller dispatching on v2_surface
    binding = build_v2_surface_binding(resolution, surface=surface)

    def _withhold(reason: str) -> RecallAdmissionDecision:
        return RecallAdmissionDecision(
            profile=profile.key,
            decision="withhold",
            reason_codes=(reason,),
            assessment_id=assessment.assessment_id if assessment is not None else None,
            assessment_status=assessment.status if assessment is not None else None,
            assessment_outcome=assessment.outcome if assessment is not None else None,
            surface=surface,
            surface_decision=binding.surface_decision,
            v2=binding,
        )

    # Defense in depth mirroring the policy's own mechanically-expressible
    # lifecycle rules (the corpus window already enforced them pre-LIMIT).
    local_reason = v2_local_gate_withhold_reason(item, profile=profile, assessment=assessment)
    if local_reason is not None:
        return _withhold(local_reason)

    if resolution is None or resolution.status == "missing":
        return _withhold("v2_decision_missing")
    if resolution.status not in V2_RESOLUTION_STATUSES:
        # An unknown resolver vocabulary is a contract break, not a policy
        # outcome — fail closed rather than guessing what it means.
        return _withhold("v2_decision_unsupported")
    if resolution.status != "current":
        return _withhold(f"v2_decision_{resolution.status}")

    surface_decision = binding.surface_decision
    if surface_decision is None:
        return _withhold("v2_surface_unsupported")
    if surface_decision != "allow":
        return _withhold(f"v2_surface_{surface_decision}")

    # A current resolution always binds the persisted row the decision was
    # verified against.
    assert binding.assessment_id is not None
    return RecallAdmissionDecision(
        profile=profile.key,
        decision="admit",
        # The exact policy outcome, plus any non-withholding #159 marks the
        # item carries (exploratory stale / legacy_import snapshots).
        reason_codes=("admitted_v2_surface_allow",) + _marking_codes(assessment),
        assessment_id=assessment.assessment_id if assessment is not None else None,
        assessment_status=assessment.status if assessment is not None else None,
        assessment_outcome=assessment.outcome if assessment is not None else None,
        surface=surface,
        surface_decision=surface_decision,
        v2=binding,
    )


# ---- per-item served payload ----


def build_v2_evidence_fields(binding: V2SurfaceBinding) -> dict[str, Any]:
    """The canonical served evidence block for an admitted V2-bound item.

    A pure projection of the :class:`V2SurfaceBinding` the admission decision
    already consumed (issue #188): every field is read from the binding's own
    fresh evaluation — never re-derived from item state, never re-selected
    from ``memory_assessments``, never a second policy evaluation. The
    identity invariant this guarantees is mechanical:

    * ``evidence.profile_key`` is the binding's profile key;
    * policy version, artifact digest, decision hash, epistemic/risk/
      retention state, and effective assessment refs are the fresh
      evaluation's exact values;
    * ``v2_resolution_status`` is the binding's resolution status.

    Only an admitted combination may be presented: a non-``current``
    resolution, a non-``allow`` surface decision, or an epistemic state
    outside the presentable vocabulary (``not_applicable``, ``None``) raises
    :class:`V2EvidenceContractError` — those shapes cannot produce an
    admitted item under the #186 gate, so reaching one here is a contract
    break that must fail closed rather than be reinterpreted.
    """
    fresh = binding.fresh
    if (
        binding.resolution_status != "current"
        or fresh is None
        or fresh.surface_decision != "allow"
    ):
        raise V2EvidenceContractError(
            f"admitted item carries a non-admissible V2 binding: "
            f"resolution_status={binding.resolution_status!r} "
            f"surface_decision={binding.surface_decision!r}"
        )
    epistemic_state = fresh.epistemic_state
    if (
        epistemic_state is None
        or epistemic_state not in _V2_PRESENTABLE_EPISTEMIC_STATES
    ):
        raise V2EvidenceContractError(
            f"admitted item carries an unpresentable V2 epistemic state: "
            f"{epistemic_state!r}"
        )
    return {
        "source": "v2_fresh_evaluation",
        "profile_key": binding.profile_key,
        "policy_version": fresh.policy_version,
        "policy_artifact_digest": fresh.policy_artifact_digest,
        "decision_hash": fresh.decision_hash,
        "v2_resolution_status": binding.resolution_status,
        "epistemic_state": epistemic_state,
        "risk_state": fresh.risk_state,
        "retention_state": fresh.retention_state,
        "effective_assessment_refs": [dict(ref) for ref in fresh.effective_assessment_refs],
    }


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

    Evidence authority (issue #188): on a V2-bound profile
    (``decision.v2`` present) the served epistemic state and the structured
    ``evidence`` block are exact projections of the already-bound V2 fresh
    evaluation — the same state the admission policy consumed. The item-local
    review/conflict/verification heuristic is not consulted and can never
    contradict the canonical state. Non-V2 decisions keep the local
    derivation (no candidate profile uses it today; it exists for legacy
    local-profile compatibility and is byte-stable).
    """
    utility = compute_utility_score(
        importance=item.importance,
        created_at=item.created_at,
        valid_from=item.valid_from,
        now=now,
    )
    evidence: dict[str, Any] | None = None
    risk_state: str | None = None
    if decision.v2 is not None:
        evidence = build_v2_evidence_fields(decision.v2)
        epistemic_state = cast(EpistemicState, evidence["epistemic_state"])
        risk_state = evidence["risk_state"]
    else:
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
        risk_state=risk_state,
    )
    rank = compute_signal_rank_score(similarity=similarity, utility=utility)
    reasons = [
        f"relevance {similarity:.2f}",
        f"utility {utility:.2f}",
        f"admission {decision.profile}:{','.join(decision.reason_codes)}",
    ]
    fields = {
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
    if evidence is not None:
        # The canonical evidence block: an exact mirror of the V2 fresh
        # evaluation admission consumed — including the top-level
        # epistemic state above, which is this block's own value.
        fields["evidence"] = evidence
    return fields


# ---- bulk assessment loading ----


async def load_admission_bindings(
    session: AsyncSession,
    *,
    tenant_id: str,
    items: Sequence[MemoryItem],
) -> dict[uuid.UUID, AdmissionAssessmentBinding]:
    """Digest-verified admission bindings for a bounded candidate window.

    Read visibility is deliberately NOT conditioned on
    ``admission_assessment_capture_enabled``: that flag governs the *capture*
    of new authoritative assessments (issue #159 rollout/rollback), not the
    resolution of already-persisted projections. Disabling capture must never
    hide an existing ``blocked``/``stale`` decision from recall enforcement —
    the rollback invariant is "no new capture effect", not "no reads".
    Items with no recorded projection are absent from the result — callers
    treat absence as ``missing``, which the rule-based gate already handles.
    """
    if not items:
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
    "V2_ADMISSION_PROFILE_KEY",
    "V2_RESOLUTION_STATUSES",
    "V2SurfaceBinding",
    "V2EvidenceContractError",
    "V2FreshEvaluation",
    "V2PersistedIdentity",
    "AdmissionAssessmentBinding",
    "EpistemicState",
    "RecallAdmissionDecision",
    "build_v2_evidence_fields",
    "build_v2_surface_binding",
    "compute_signal_rank_score",
    "compute_utility_score",
    "decide_recall_admission",
    "derive_epistemic_state",
    "load_admission_bindings",
    "signal_item_fields",
    "structured_warning_codes",
    "v2_local_gate_withhold_reason",
]

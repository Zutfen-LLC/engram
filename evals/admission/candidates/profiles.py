"""Declared candidate policy profiles for #162C.

Each profile is a structural experiment, not a production default. Decision
code consumes only :class:`PolicyInput` (captured decision-time state) and the
canonical production evaluator for its own baseline states. Human labels are
structurally absent from every signature below.

Profiles
--------
P0 ``candidate-current-compat-v1``  exact parity with the canonical evaluator.
P1 ``candidate-tier-separated-v1``  separate storage / governed / startup /
    review decisions instead of one promotion Boolean.
P2 ``candidate-evidence-recovery-v1``  missing/insufficient evidence routes to
    defer + review instead of a terminal hold; unknown epistemics stay unknown.
P3 ``candidate-kind-decoupled-v1``  storage decisions survive kind/taxonomy
    uncertainty; protected kinds stay fail-closed; review instead of parking.

Every parameter value below is a production constant or an explicitly
declared structural value; nothing was fitted against any label.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Final

from engram.promotion_policy import (
    DEFAULT_EVIDENCE_THRESHOLD,
    EVIDENCE_TAXONOMY_MINIMUM,
)
from engram.promotion_readiness import (
    EVIDENCE_STATE_BOUND_QUALIFIED,
    READINESS_COOLING,
    READINESS_ELIGIBLE_NOW,
)
from evals.admission.candidates.contract import (
    CandidateDeclaration,
    CandidateParameters,
    CandidatePolicy,
    CandidateResult,
    StorageDisposition,
    TriState,
)
from evals.admission.policy import ConfigSnapshot, PolicyInput, evaluate

# --- candidate reason-code vocabulary (closed, prefixed) ---------------------
REASON_REVIEW_KIND_PROTECTED = "review_protected_kind"
REASON_REVIEW_EVIDENCE_STARVED = "review_evidence_starved"
REASON_REVIEW_STORAGE_REJECT_CONFLICT = "review_reject_conflict_or_dispute"
REASON_STORAGE_REJECT_CONFLICT = "storage_reject_conflict_or_dispute"
REASON_STORAGE_REJECT_NOT_LIVE = "storage_reject_not_live_or_superseded"
REASON_STORAGE_RETAIN_TAXONOMY_UNCERTAIN = "storage_retained_taxonomy_uncertain"
REASON_RETAIN_QUALIFIED = "retain_qualified"
REASON_RETAIN_COOLING = "retain_cooling"
REASON_DEFER_EVIDENCE = "defer_insufficient_evidence"
REASON_DEFER_WINDOW = "defer_deferral_window_open"
REASON_DEFER_KIND_GATE = "defer_kind_gate_with_storage_value"
REASON_DEFER_EPISTEMIC_UNKNOWN = "defer_epistemic_state_unknown"
REASON_NO_BASIS = "defer_no_promotion_basis"
REASON_UNKNOWN_POLICY_STATE = "unknown_policy_state"
REASON_P0_MIRROR = "p0_mirror_of_current"

_PROTECTED_KINDS: Final[frozenset[str]] = frozenset({"doctrine", "invariant"})
_GOVERNANCE_BLOCKERS: Final[frozenset[str]] = frozenset(
    {"conflict", "external_dispute", "review_policy"}
)
#: Evidence-shape blockers: the item has no usable retention evidence.
_EVIDENCE_STARVED_BLOCKERS: Final[frozenset[str]] = frozenset(
    {
        "no_retention_evidence",
        "missing_source_prior",
        "evidence_score",
        "retention_disposition",
        "evidence_disabled",
    }
)

ALL_REASON_CODES: Final[frozenset[str]] = frozenset(
    {
        REASON_REVIEW_KIND_PROTECTED,
        REASON_REVIEW_EVIDENCE_STARVED,
        REASON_REVIEW_STORAGE_REJECT_CONFLICT,
        REASON_STORAGE_REJECT_CONFLICT,
        REASON_STORAGE_REJECT_NOT_LIVE,
        REASON_STORAGE_RETAIN_TAXONOMY_UNCERTAIN,
        REASON_RETAIN_QUALIFIED,
        REASON_RETAIN_COOLING,
        REASON_DEFER_EVIDENCE,
        REASON_DEFER_WINDOW,
        REASON_DEFER_KIND_GATE,
        REASON_DEFER_EPISTEMIC_UNKNOWN,
        REASON_NO_BASIS,
        REASON_UNKNOWN_POLICY_STATE,
        REASON_P0_MIRROR,
    }
)


def base_signals() -> tuple[str, ...]:
    """Signals every profile reads. Declared, not discovered at runtime."""
    return (
        "source_type",
        "kind",
        "kind_enabled",
        "kind_auto_promote",
        "review_status",
        "live",
        "superseded",
        "created_at",
        "memory_confidence",
        "retention_confidence",
        "retention_disposition",
        "retention_evidence_at",
        "conflict_resolution_status",
        "external_dispute",
        "external_noise",
        "receipt",
        "receipt.taxonomy_confidence",
        "evidence_state",
        "job_state",
    )


def _candidate_eligible(item: PolicyInput) -> bool:
    return item.live and not item.superseded and item.review_status == "proposed"


def _governance_blocked(item: PolicyInput) -> bool:
    return (
        item.conflict_resolution_status == "unresolved"
        or item.external_dispute
        or item.external_noise
    )


def _evidence_starved(item: PolicyInput) -> bool:
    receipt = item.receipt
    return (
        receipt is None
        or item.retention_evidence_at is None
        or item.retention_confidence is None
        or item.source_confidence_prior is None
    )


class P0CurrentCompat(CandidatePolicy):
    """Exact parity with the canonical current evaluator.

    Storage semantics mirror ``would_promote`` and the canonical readiness
    states one-for-one: eligible -> retain; cooling -> retain (waiting);
    missing/below-threshold evidence -> defer (current policy treats these as
    retryable states, not terminal); kind-policy/taxonomy/conflict/dispute
    terminal states -> reject (current policy parks them); not-a-candidate ->
    reject. Automatic admission is ``would_promote`` exactly.
    """

    def __init__(self, declaration: CandidateDeclaration) -> None:
        self.declaration = declaration

    def evaluate(
        self, item: PolicyInput, config: ConfigSnapshot | None, now: datetime
    ) -> CandidateResult:
        result = evaluate(item, config, now)
        reasons: list[str] = [REASON_P0_MIRROR]
        required = base_signals()
        if result.current_policy_version == "unknown":
            return CandidateResult(
                candidate_policy_version=self.declaration.policy_version,
                storage_disposition="unknown",
                automatic_admission="unknown",
                governed_semantic_eligibility="unknown",
                startup_eligibility="unknown",
                human_review_required="unknown",
                reason_codes=(REASON_UNKNOWN_POLICY_STATE,),
                required_signals=required,
                unavailable_signals=("config_snapshot",),
            )
        automatic: TriState = "yes" if result.would_promote is True else "no"
        blockers = set(result.blocker_codes)
        state = result.readiness_state
        storage: StorageDisposition
        if state == "not_a_promotion_candidate":
            storage = "reject"
            reasons.append(REASON_STORAGE_REJECT_NOT_LIVE)
        elif automatic == "yes":
            storage = "retain"
            reasons.append(REASON_RETAIN_QUALIFIED)
        elif state == READINESS_COOLING:
            storage = "retain"
            reasons.append(REASON_RETAIN_COOLING)
        elif state in ("missing_evidence", "below_evidence_threshold") or blockers & (
            _EVIDENCE_STARVED_BLOCKERS
        ):
            storage = "defer"
            reasons.append(REASON_DEFER_EVIDENCE)
        elif state in (
            "blocked_by_kind_policy",
            "below_taxonomy_confidence_minimum",
            "retention_disposition_not_retain",
            "blocked_by_conflict_or_dispute",
            "blocked_by_review_policy",
            "malformed_or_stale_evidence",
            "evidence_lane_disabled",
            "below_legacy_confidence_threshold",
        ) or blockers & {"evidence_inconsistent", "evidence_version"}:
            storage = "reject"
            reasons.append(REASON_STORAGE_REJECT_CONFLICT)
        else:  # pragma: no cover - exhaustive readiness vocabulary above
            storage = "defer"
            reasons.append(REASON_NO_BASIS)
        # P0 does not invent governed/startup/review structure the current
        # policy does not have: they follow the single promotion Boolean.
        governed: TriState = "yes" if result.would_promote is True else "no"
        startup: TriState = "yes" if result.would_promote is True else "no"
        review: TriState = "no"
        return CandidateResult(
            candidate_policy_version=self.declaration.policy_version,
            storage_disposition=storage,
            automatic_admission=automatic,
            governed_semantic_eligibility=governed,
            startup_eligibility=startup,
            human_review_required=review,
            reason_codes=tuple(dict.fromkeys(reasons)),
            required_signals=required,
            evidence_state=result.evidence_state,
            selected_lane=result.current_selected_lane,
            next_evaluation_at=result.eligible_at if storage == "defer" else None,
            next_action="automatic_admission"
            if automatic == "yes"
            else "wait"
            if storage == "defer"
            else "reject"
            if storage == "reject"
            else "unknown",
        )


class TieredProfile(CandidatePolicy):
    """Shared engine for P1/P2/P3 structural variants.

    Automatic admission is evidence-lane-qualified-only in every variant: it
    requires a bound-qualified evidence receipt on the retention_evidence lane
    with no governance blocker, so candidate auto is a strict subset of
    current-policy auto. Variant differences live in the storage ladder and
    review routing, implemented via the flags below.
    """

    def __init__(self, declaration: CandidateDeclaration) -> None:
        self.declaration = declaration
        p = declaration.parameters
        self._taxonomy_min = p.taxonomy_minimum
        self._deferral_window = timedelta(hours=p.deferral_window_hours)
        #: P2: evidence-starved retained-value candidates route to review.
        self._review_evidence_starved = declaration.policy_version.startswith(
            "candidate-evidence-recovery"
        )
        #: P3: ordinary-kind taxonomy/kind-gate cases retain storage value.
        self._decouple_kind_gate = declaration.policy_version.startswith(
            "candidate-kind-decoupled"
        )

    # -- tier decisions ---------------------------------------------------

    def _governed(
        self, item: PolicyInput, eligible_receipt: bool
    ) -> TriState:
        if _governance_blocked(item):
            return "no"
        if item.kind in _PROTECTED_KINDS or not item.kind_auto_promote:
            return "no"
        if not eligible_receipt:
            return "unknown"
        if item.receipt is not None and item.receipt.taxonomy_confidence >= self._taxonomy_min:
            return "yes"
        return "unknown"

    def evaluate(
        self, item: PolicyInput, config: ConfigSnapshot | None, now: datetime
    ) -> CandidateResult:
        result = evaluate(item, config, now)
        reasons: list[str] = []
        required = base_signals()
        if result.current_policy_version == "unknown":
            return CandidateResult(
                candidate_policy_version=self.declaration.policy_version,
                storage_disposition="unknown",
                automatic_admission="unknown",
                governed_semantic_eligibility="unknown",
                startup_eligibility="unknown",
                human_review_required="unknown",
                reason_codes=(REASON_UNKNOWN_POLICY_STATE,),
                required_signals=required,
                unavailable_signals=("config_snapshot",),
            )
        blockers = set(result.blocker_codes)
        state = result.readiness_state
        eligible_receipt = result.evidence_state == EVIDENCE_STATE_BOUND_QUALIFIED
        review: TriState = "no"
        storage: StorageDisposition
        # --- automatic admission (identical across variants) --------------
        # Candidate auto admits ONLY via a bound-qualified evidence receipt.
        # A current-policy promotion that qualified through the legacy
        # confidence lane (no receipt) is deliberately NOT auto-admitted by
        # candidates: candidate auto is a strict subset of current auto.
        auto_ok = bool(
            result.would_promote is True
            and eligible_receipt
            and result.current_selected_lane == "retention_evidence"
            and not blockers & _GOVERNANCE_BLOCKERS
        )
        # --- governance review routing ------------------------------------
        if _governance_blocked(item) and not _candidate_eligible(item):
            storage = "reject"
            reasons.append(REASON_STORAGE_REJECT_NOT_LIVE)
        elif _governance_blocked(item):
            review = "yes"
            reasons.append(REASON_REVIEW_STORAGE_REJECT_CONFLICT)
            storage = (
                "retain"
                if result.would_promote is True
                else "defer"
            )
        # --- storage ladder -------------------------------------------------
        elif not _candidate_eligible(item):
            storage = "reject"
            reasons.append(REASON_STORAGE_REJECT_NOT_LIVE)
        elif auto_ok:
            storage = "retain"
            reasons.append(REASON_RETAIN_QUALIFIED)
        elif state == READINESS_COOLING:
            storage = "retain"
            reasons.append(REASON_RETAIN_COOLING)
        elif (
            item.kind in _PROTECTED_KINDS and not item.kind_auto_promote
        ):
            # Protected kinds are never parked silently and never auto: they
            # route to review regardless of evidence shape (fail-closed).
            storage = "defer"
            review = "yes"
            reasons.append(REASON_REVIEW_KIND_PROTECTED)
        elif _evidence_starved(item) or blockers & _EVIDENCE_STARVED_BLOCKERS:
            storage = "defer"
            reasons.append(REASON_DEFER_EVIDENCE)
            if self._review_evidence_starved and item.kind not in _PROTECTED_KINDS:
                review = "yes"
                reasons.append(REASON_REVIEW_EVIDENCE_STARVED)
        elif state in (
            "below_taxonomy_confidence_minimum",
            "blocked_by_kind_policy",
        ):
            if self._decouple_kind_gate and item.kind not in _PROTECTED_KINDS:
                # P3 hypothesis: taxonomy uncertainty does not negate storage
                # worth for ordinary kinds. Governed stays unknown (uncertain
                # taxonomy), startup stays closed.
                storage = "retain"
                reasons.append(REASON_STORAGE_RETAIN_TAXONOMY_UNCERTAIN)
            elif self._decouple_kind_gate:
                storage = "defer"
                review = "yes"
                reasons.append(REASON_REVIEW_KIND_PROTECTED)
            else:
                storage = "defer"
                review = "no"
                reasons.append(REASON_DEFER_KIND_GATE)
        elif state in (
            "retention_disposition_not_retain",
            "malformed_or_stale_evidence",
            "below_legacy_confidence_threshold",
            "evidence_lane_disabled",
        ) or blockers & {"evidence_inconsistent", "evidence_version"}:
            # The retention signal itself says do-not-retain / cannot trust
            # the evidence: not storage-worthy without human review in P2,
            # plain deferral elsewhere.
            storage = "defer"
            reasons.append(REASON_DEFER_EVIDENCE)
            if self._review_evidence_starved:
                review = "yes"
                reasons.append(REASON_REVIEW_EVIDENCE_STARVED)
        else:  # pragma: no cover - exhaustive readiness vocabulary above
            storage = "defer"
            reasons.append(REASON_NO_BASIS)
        governed: TriState = (
            self._governed(item, eligible_receipt) if storage == "retain" else "no"
        )
        # Startup requires: retained + governed-yes + bound receipt + matured
        # window. Never implied by retention alone.
        startup: TriState
        if storage == "retain" and governed == "yes" and eligible_receipt:
            startup = "yes" if state == READINESS_ELIGIBLE_NOW else "unknown"
        else:
            startup = "no"
        if review == "yes":
            reasons.append(REASON_DEFER_EPISTEMIC_UNKNOWN)
        next_at: datetime | None = None
        if storage == "defer":
            next_at = result.eligible_at or (now + self._deferral_window)
            if self._deferral_window > timedelta(0):
                reasons.append(REASON_DEFER_WINDOW)
        return CandidateResult(
            candidate_policy_version=self.declaration.policy_version,
            storage_disposition=storage,
            automatic_admission="yes" if auto_ok else "no",
            governed_semantic_eligibility=governed,
            startup_eligibility=startup,
            human_review_required=review,
            reason_codes=tuple(dict.fromkeys(reasons)),
            required_signals=required,
            evidence_state=result.evidence_state,
            selected_lane=result.current_selected_lane,
            next_evaluation_at=next_at,
            next_action="automatic_admission"
            if auto_ok
            else "review"
            if review == "yes"
            else "wait"
            if storage == "defer"
            else "reject"
            if storage == "reject"
            else "unknown",
        )


def _declaration(
    policy_version: str,
    hypothesis: str,
    *,
    taxonomy_minimum: float = EVIDENCE_TAXONOMY_MINIMUM,
    deferral_window_hours: int = 0,
    parameter_provenance: str,
    review_routing: tuple[str, ...],
    storage_semantics: str,
    protected: str,
    unknown_behavior: str,
) -> CandidateDeclaration:
    return CandidateDeclaration(
        policy_version=policy_version,
        hypothesis=hypothesis,
        required_input_signals=base_signals(),
        protected_kind_behavior=protected,
        unknown_behavior=unknown_behavior,
        automatic_admission_conditions=(
            "would_promote true on retention_evidence lane",
            "evidence_state == bound-qualified",
            "no conflict/external_dispute/review_policy blocker",
        ),
        review_routing_conditions=review_routing,
        storage_semantics=storage_semantics,
        governed_semantics=(
            "yes only for non-protected auto-promote kinds with bound receipt "
            "and taxonomy_confidence >= taxonomy_minimum; no when governed "
            "authority is denied; unknown when taxonomy evidence is absent"
        ),
        startup_semantics=(
            "yes only when storage retain AND governed yes AND bound-qualified "
            "receipt AND cooling matured; unknown while cooling; never implied "
            "by retention alone"
        ),
        parameters=CandidateParameters(
            evidence_threshold=DEFAULT_EVIDENCE_THRESHOLD,
            taxonomy_minimum=taxonomy_minimum,
            legacy_confidence_threshold=0.7,
            deferral_window_hours=deferral_window_hours,
            deferral_window_provenance="declared-profile-parameter-162c-v1",
        ),
        parameter_provenance=parameter_provenance,
    )


def build_profiles() -> tuple[CandidatePolicy, ...]:
    """The frozen profile set. Order is part of the freeze artifact."""
    p0 = P0CurrentCompat(
        _declaration(
            "candidate-current-compat-v1",
            "Exact parity with the canonical current evaluator for runner "
            "validation; not a structural hypothesis.",
            parameter_provenance="production constants verbatim (promotion_policy.py)",
            review_routing=(),
            storage_semantics=(
                "mirror of canonical would_promote/readiness states one-for-one"
            ),
            protected="identical to current policy (kind_policy blocker parks)",
            unknown_behavior="unknown policy version propagates to all tiers",
        )
    )
    p1 = TieredProfile(
        _declaration(
            "candidate-tier-separated-v1",
            "H2: separating storage / governed / startup / automatic / review "
            "decisions recovers useful memories without looser automatic "
            "admission, because under-admission partly comes from collapsing "
            "the tiers into one Boolean.",
            deferral_window_hours=168,
            parameter_provenance=(
                "evidence/taxonomy/legacy constants verbatim from production; "
                "deferral window 168h declared structurally (one week "
                "re-evaluation cadence), chosen before any label was seen"
            ),
            review_routing=(
                "review only for conflict/dispute governance cases and "
                "protected kinds without auto-promote authority",
            ),
            storage_semantics=(
                "retain when qualified or cooling; defer when evidence is "
                "starved or disposition not retain; reject only when not a "
                "live candidate"
            ),
            protected="defer + review, never auto, never silent parking",
            unknown_behavior="unknown policy version propagates to all tiers",
        )
    )
    p2 = TieredProfile(
        _declaration(
            "candidate-evidence-recovery-v1",
            "H3: missing/insufficient retention evidence or source prior can "
            "route useful candidates to defer + human review instead of a "
            "terminal hold, without treating missing evidence as support.",
            deferral_window_hours=168,
            parameter_provenance=(
                "evidence/taxonomy/legacy constants verbatim from production; "
                "deferral window 168h declared structurally before labels"
            ),
            review_routing=(
                "adds review routing for evidence-starved and non-retain-"
                "disposition cases (P2-specific); conflict/dispute and "
                "protected-kind routing as P1",
            ),
            storage_semantics=(
                "defer replaces every evidence-derived terminal hold; missing "
                "evidence stays unknown epistemically (defer, not retain)"
            ),
            protected="defer + review, never auto, never silent parking",
            unknown_behavior="unknown policy version propagates to all tiers",
        )
    )
    p3 = TieredProfile(
        _declaration(
            "candidate-kind-decoupled-v1",
            "H4: kind/taxonomy gating can be separated from storage "
            "worthiness for ordinary kinds while protected kinds stay "
            "fail-closed behind review.",
            deferral_window_hours=168,
            parameter_provenance=(
                "evidence/taxonomy/legacy constants verbatim from production; "
                "deferral window 168h declared structurally before labels"
            ),
            review_routing=(
                "review for protected kinds failing taxonomy minimum; ordinary "
                "kind-gate cases retain storage value without review",
            ),
            storage_semantics=(
                "ordinary-kind taxonomy/kind-gate cases retain storage value; "
                "protected kinds defer + review; evidence rules as P1"
            ),
            protected=(
                "protected kinds remain fail-closed: defer + review when "
                "taxonomy confidence is below minimum or authority absent"
            ),
            unknown_behavior="unknown policy version propagates to all tiers",
        )
    )
    return (p0, p1, p2, p3)

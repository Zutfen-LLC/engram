"""Pure, versioned policy for Promotion Path A v2."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal

# Literal typing only; the values and runtime behavior are unchanged. It lets
# downstream closed Literal contracts (admission evaluation results) accept
# these constants without duplicating the version strings.
LEGACY_PROMOTION_POLICY_VERSION: Literal["promotion-legacy-v1"] = "promotion-legacy-v1"
EVIDENCE_PROMOTION_POLICY_VERSION: Literal["promotion-evidence-v1"] = (
    "promotion-evidence-v1"
)
EVIDENCE_SOURCE_PRIOR_WEIGHT = 0.20
EVIDENCE_RETENTION_WEIGHT = 0.80
EVIDENCE_SCORE_CEILING = 0.85
EVIDENCE_TAXONOMY_MINIMUM = 0.70
DEFAULT_EVIDENCE_THRESHOLD = 0.70
# Upper bound of the classifier's retention_confidence output (the
# ClassificationResult clamp). Distinct from the score ceiling: a threshold at
# or below the ceiling can still be unreachable when the required retention
# confidence exceeds what a classifier is allowed to emit.
EVIDENCE_RETENTION_MAX = 0.95

PromotionBasis = Literal["legacy_confidence", "retention_evidence"]


class PromotionPolicyError(ValueError):
    """A score input is invalid and must fail closed."""


def _finite_in_range(value: float, *, name: str, upper: float) -> None:
    if not math.isfinite(value) or not 0.0 <= value <= upper:
        raise PromotionPolicyError(f"{name} must be finite and between 0.0 and {upper}")


def evidence_score_v1(source_confidence_prior: float, retention_confidence: float) -> float:
    """Return the unrounded, capped v1 retention-evidence promotion score."""
    _finite_in_range(source_confidence_prior, name="source_confidence_prior", upper=1.0)
    _finite_in_range(retention_confidence, name="retention_confidence", upper=0.95)
    return min(
        EVIDENCE_SCORE_CEILING,
        EVIDENCE_SOURCE_PRIOR_WEIGHT * source_confidence_prior
        + EVIDENCE_RETENTION_WEIGHT * retention_confidence,
    )


def required_retention_confidence_v1(
    source_confidence_prior: float, evidence_threshold: float
) -> float | None:
    """Minimum retention confidence for the v1 score to reach ``evidence_threshold``.

    Solves ``min(EVIDENCE_SCORE_CEILING, w_prior * prior + w_ret * retention)
    >= threshold`` for ``retention`` under the v1 weights. Returns ``None``
    when the threshold is unreachable under the current formula: a threshold
    above the score ceiling, or a required retention confidence above
    ``EVIDENCE_RETENTION_MAX`` (the classifier clamp). A non-positive
    requirement clamps to ``0.0``. Raises :class:`PromotionPolicyError` for
    invalid inputs (non-finite prior / threshold), mirroring
    :func:`evidence_score_v1` — callers must fail closed, never coerce.
    """
    _finite_in_range(source_confidence_prior, name="source_confidence_prior", upper=1.0)
    if not math.isfinite(evidence_threshold):
        raise PromotionPolicyError("evidence_threshold must be finite")
    if evidence_threshold > EVIDENCE_SCORE_CEILING:
        return None
    required = (
        evidence_threshold - EVIDENCE_SOURCE_PRIOR_WEIGHT * source_confidence_prior
    ) / EVIDENCE_RETENTION_WEIGHT
    if required <= 0.0:
        return 0.0
    if required > EVIDENCE_RETENTION_MAX:
        return None
    return required


@dataclass(frozen=True)
class PromotionLaneAssessment:
    basis: PromotionBasis
    trust_qualified: bool
    age_qualified: bool
    score: float | None = None
    threshold: float | None = None


@dataclass(frozen=True)
class PromotionCandidateAssessment:
    legacy: PromotionLaneAssessment
    evidence: PromotionLaneAssessment
    selected_basis: PromotionBasis | None


def choose_basis(
    *,
    legacy_trust_qualified: bool,
    legacy_age_qualified: bool,
    evidence_trust_qualified: bool,
    evidence_age_qualified: bool,
    legacy_score: float | None = None,
    legacy_threshold: float | None = None,
    evidence_score: float | None = None,
    evidence_threshold: float | None = None,
) -> PromotionCandidateAssessment:
    """Choose the evidence lane first when both independently pass."""
    legacy = PromotionLaneAssessment(
        "legacy_confidence",
        legacy_trust_qualified,
        legacy_age_qualified,
        legacy_score,
        legacy_threshold,
    )
    evidence = PromotionLaneAssessment(
        "retention_evidence",
        evidence_trust_qualified,
        evidence_age_qualified,
        evidence_score,
        evidence_threshold,
    )
    selected: PromotionBasis | None = None
    if evidence.trust_qualified and evidence.age_qualified:
        selected = "retention_evidence"
    elif legacy.trust_qualified and legacy.age_qualified:
        selected = "legacy_confidence"
    return PromotionCandidateAssessment(legacy, evidence, selected)

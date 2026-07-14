"""Pure, versioned policy for Promotion Path A v2."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal

LEGACY_PROMOTION_POLICY_VERSION = "promotion-legacy-v1"
EVIDENCE_PROMOTION_POLICY_VERSION = "promotion-evidence-v1"
EVIDENCE_SOURCE_PRIOR_WEIGHT = 0.20
EVIDENCE_RETENTION_WEIGHT = 0.80
EVIDENCE_SCORE_CEILING = 0.85
EVIDENCE_TAXONOMY_MINIMUM = 0.70
DEFAULT_EVIDENCE_THRESHOLD = 0.70

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

"""Observe canonical promotion policy without a database or label input."""

from __future__ import annotations

import uuid
from typing import Literal

from pydantic import AwareDatetime, Field

from engram.classification import RetentionDisposition
from engram.models import ClassificationRun, MemoryItem, MemoryKind, TenantConfig
from engram.promotion import PromotionSupport, _config_values, assess_promotion_candidate
from engram.promotion_policy import (
    EVIDENCE_PROMOTION_POLICY_VERSION,
    LEGACY_PROMOTION_POLICY_VERSION,
)
from engram.promotion_readiness import (
    _evidence_trust,
    classify_readiness,
    evidence_state_of,
)
from evals.admission.schema import ContentHash, Record, Token


class ConfigSnapshot(Record):
    auto_promote_enabled: bool
    auto_promote_confidence_threshold: float = Field(ge=0, le=1)
    auto_promote_min_age_hours: int = Field(ge=0)
    auto_promote_evidence_enabled: bool
    auto_promote_evidence_threshold: float = Field(ge=0, le=1)


class Receipt(Record):
    content_hash: ContentHash
    source_type: Token
    suggested_kind: Token
    taxonomy_confidence: float = Field(ge=0, le=1)
    retention_confidence: float = Field(ge=0, le=1)
    retention_disposition: RetentionDisposition
    created_at: AwareDatetime
    bound_at: AwareDatetime | None
    classification_version: Token
    retention_policy_version: Token
    binding_matches: bool


class PolicyInput(Record):
    sample_id: Token
    content_hash: ContentHash
    source_type: Token
    kind: Token
    review_status: Literal["active", "proposed", "rejected", "archived", "disputed"]
    created_at: AwareDatetime
    memory_confidence: float = Field(ge=0, le=1)
    source_confidence_prior: float | None = Field(ge=0, le=1)
    retention_confidence: float | None = Field(ge=0, le=1)
    retention_disposition: str | None
    retention_evidence_at: AwareDatetime | None
    conflict_resolution_status: Token | None
    live: bool
    superseded: bool
    kind_enabled: bool
    kind_auto_promote: bool
    external_dispute: bool
    external_noise: bool
    receipt: Receipt | None
    job_state: Literal["missing", "scheduled", "overdue", "dead", "unknown"]
    recalled: Literal["yes", "no", "unknown"]


PolicyVersion = Literal[
    "promotion-legacy-v1",
    "promotion-evidence-v1",
    "none",
    "unknown",
]


class PolicyEvaluationResult(Record):
    sample_id: Token
    current_review_status: str
    actual_kind: str
    # ``none``: known policy evaluated; no promotion basis was selected.
    # ``unknown``: policy/configuration could not be established.
    current_policy_version: PolicyVersion
    current_selected_lane: str
    would_promote: bool | None
    blocker_codes: tuple[str, ...]
    evidence_state: str
    cooling_start: AwareDatetime | None
    eligible_at: AwareDatetime | None
    current_job_state: str
    readiness_state: str
    terminal_under_current_policy: bool | None
    conflict_recheck_status: Literal["not_run"] = "not_run"


def _policy_version(selected_basis: str | None) -> PolicyVersion:
    """Map a selected promotion basis to its closed policy-version contract.

    Called only with known configuration: a completed evaluation that selected
    no promotion basis reports ``none``. ``unknown`` is reserved for the
    missing-configuration path and never produced here.
    """
    if selected_basis == "retention_evidence":
        return EVIDENCE_PROMOTION_POLICY_VERSION
    if selected_basis == "legacy_confidence":
        return LEGACY_PROMOTION_POLICY_VERSION
    return "none"


def evaluate(
    item: PolicyInput, config: ConfigSnapshot | None, now: AwareDatetime
) -> PolicyEvaluationResult:
    if now.tzinfo is None:
        raise ValueError("timezone_required")
    if config is None:
        return PolicyEvaluationResult(
            sample_id=item.sample_id,
            current_review_status=item.review_status,
            actual_kind=item.kind,
            current_policy_version="unknown",
            current_selected_lane="unknown",
            would_promote=None,
            blocker_codes=(),
            evidence_state="unknown",
            cooling_start=None,
            eligible_at=None,
            current_job_state=item.job_state,
            readiness_state="unknown",
            terminal_under_current_policy=None,
        )
    identity = uuid.uuid5(uuid.NAMESPACE_URL, item.sample_id)
    tenant = uuid.UUID(int=0)
    memory = MemoryItem(
        id=identity,
        tenant_id=tenant,
        principal_id=tenant,
        kind=item.kind,
        content_hash=item.content_hash,
        source_type=item.source_type,
        review_status=item.review_status,
        created_at=item.created_at,
        memory_confidence=item.memory_confidence,
        source_confidence_prior=item.source_confidence_prior,
        retention_confidence=item.retention_confidence,
        retention_disposition=item.retention_disposition,
        retention_evidence_at=item.retention_evidence_at,
        conflict_resolution_status=item.conflict_resolution_status,
    )
    run = None
    if item.receipt:
        values = item.receipt.model_dump(exclude={"binding_matches"})
        run = ClassificationRun(
            **values,
            id=identity,
            tenant_id=tenant,
            memory_item_id=identity if item.receipt.binding_matches else tenant,
        )
    support = PromotionSupport(
        MemoryKind(
            name=item.kind,
            enabled=item.kind_enabled,
            auto_promote_from_inferred=item.kind_auto_promote,
        ),
        run,
        item.external_dispute,
        item.external_noise,
    )
    enabled, threshold, age, evidence_enabled, evidence_threshold = _config_values(
        TenantConfig(**config.model_dump())
    )
    candidate = assess_promotion_candidate(
        memory,
        support,
        confidence_threshold=threshold,
        min_age_hours=age,
        evidence_enabled=evidence_enabled,
        evidence_threshold=evidence_threshold,
        now=now,
    )
    eligible_item = item.live and not item.superseded and item.review_status == "proposed"
    readiness = classify_readiness(
        is_candidate=eligible_item,
        blockers=candidate.blockers,
        legacy_trust_qualified=item.memory_confidence >= threshold,
        evidence_trust_qualified=_evidence_trust(
            memory, run, evidence_enabled=evidence_enabled, evidence_threshold=evidence_threshold
        ),
        selected_basis=candidate.selected_basis,
    )
    return PolicyEvaluationResult(
        sample_id=item.sample_id,
        current_review_status=item.review_status,
        actual_kind=item.kind,
        # Known configuration: a completed evaluation that selects no promotion
        # basis reports "none", not "unknown". "unknown" is reserved for the
        # missing-configuration path above.
        current_policy_version=_policy_version(candidate.selected_basis),
        current_selected_lane=candidate.selected_basis or "none",
        would_promote=enabled and eligible_item and candidate.would_promote,
        blocker_codes=tuple(candidate.blockers),
        evidence_state=evidence_state_of(memory, run, evidence_threshold=evidence_threshold),
        cooling_start=candidate.cooling_period_start,
        eligible_at=candidate.eligible_at,
        current_job_state=item.job_state,
        readiness_state=readiness.readiness_state if enabled else "disabled",
        terminal_under_current_policy=readiness.terminal_under_current_policy,
    )

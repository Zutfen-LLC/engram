"""Versioned, non-authoritative admission evaluation artifacts."""

from __future__ import annotations

import hashlib
from typing import Annotated, Any, Literal, Self

import rfc8785
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from engram.promotion import PROMOTION_BLOCKER_CODES
from engram.safety import has_secrets

Token = Annotated[str, Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.:-]{0,127}$")]
Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
ContentHash = Annotated[str, Field(pattern=r"^(sha256:)?[0-9a-f]{64}$")]
Judgment = Literal["yes", "no", "unknown"]
Quality = Literal["adequate", "inadequate", "unknown", "unavailable", "not_applicable"]
Kind = Literal[
    "preference",
    "fact",
    "observation",
    "decision",
    "procedure",
    "summary",
    "doctrine",
    "invariant",
    "diary_entry",
    "unknown",
]


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class Dimensions(Record):
    atomic: Judgment
    proposition_count: Literal["zero", "one", "multiple", "unknown"]
    attribution: Quality
    source_span: Quality
    evidence_span: Quality
    assertion_origin: Literal["direct_user", "agent_inference", "unknown", "unavailable"]
    expected_kind: Kind
    expected_subject_or_domain: Token
    expected_scope: Literal["private", "workspace", "tenant", "unknown"]
    retention_value: Literal["retain", "do_not_retain", "uncertain"]
    epistemic_state: Literal[
        "adequately_supported",
        "weakly_supported",
        "contradicted",
        "contested",
        "ambiguous",
        "unverifiable",
        "unknown",
    ]
    factual_outcome: (
        Literal[
            "verified_correct",
            "verified_incorrect",
            "became_outdated",
            "not_verifiable",
            "not_yet_known",
        ]
        | None
    )
    consequence: Literal["low", "medium", "high", "unknown"]
    expected_storage_disposition: Literal["retain", "reject", "defer", "unknown"]
    expected_startup_eligibility: Judgment
    expected_governed_semantic_eligibility: Judgment
    human_review_required: Judgment
    acceptable_abstention: Judgment
    conflict_expected: Judgment
    dispute_expected: Judgment
    supersession_expected: Judgment
    temporal_validity_issue: Judgment
    scope_visibility_concern: Judgment
    evidence_independence: Literal["known_independent", "known_shared_root", "unknown"]
    expected_blockers: tuple[Token, ...] | None
    expected_next_action: Literal["automatic_admission", "review", "wait", "reject", "unknown"]

    @model_validator(mode="after")
    def blocker_vocabulary(self) -> Self:
        if self.expected_blockers is not None and not set(self.expected_blockers).issubset(
            PROMOTION_BLOCKER_CODES
        ):
            raise ValueError("unknown_blocker_code")
        return self


class Usefulness(Record):
    task_ref: Token
    context_ref: Token
    useful: Judgment


class HumanJudgment(Record):
    adjudicator_ref: Token
    adjudicated_at: AwareDatetime
    adjudicator_confidence: Literal["low", "medium", "high", "unknown"]
    reason_code: Token
    dimensions: Dimensions
    usefulness: Usefulness | None = None


class LabelRecord(Record):
    sample_id: Token
    label_schema_version: Literal["engram-admission-label-v1"]
    dataset_id: Token
    dataset_version: Token
    source_sample_ref: Token | None = None
    content_hash: ContentHash | None = None
    fixture_role: Literal[
        "ordinary_claim",
        "stale_claim",
        "incorrect_claim",
        "ambiguous_claim",
        "contested_claim",
        "conflict_peer",
        "distractor",
        "adversarial",
        "non_propositional",
    ]
    label_origin: Literal["synthetic_authored", "human_adjudicated"]
    reviewer_a: HumanJudgment
    reviewer_b: HumanJudgment | None
    resolution: HumanJudgment | None
    disagreement: Literal["none", "unresolved", "resolved"]

    @model_validator(mode="after")
    def review_contract(self) -> Self:
        a, b = self.reviewer_a, self.reviewer_b
        if b and a.adjudicator_ref == b.adjudicator_ref:
            raise ValueError("distinct_reviewers_required")
        differs = b is not None and (a.dimensions != b.dimensions or a.usefulness != b.usefulness)
        if differs != (self.disagreement != "none"):
            raise ValueError("disagreement_mismatch")
        if self.disagreement == "resolved" and self.resolution is None:
            raise ValueError("resolution_required")
        if self.disagreement != "resolved" and self.resolution is not None:
            raise ValueError("unexpected_resolution")
        if self.label_origin == "human_adjudicated":
            high = any(j and j.dimensions.consequence == "high" for j in (a, b, self.resolution))
            if high and b is None:
                raise ValueError("dual_review_required")
        return self

    def final_dimensions(self) -> Dimensions | None:
        if self.disagreement == "unresolved":
            return None
        return (self.resolution or self.reviewer_a).dimensions


class Sampling(Record):
    selection_method: Literal["census", "stratified_hash"]
    selection_seed: Token
    strata: tuple[
        Literal[
            "source_type",
            "kind",
            "review_status",
            "blocker",
            "evidence_state",
            "selected_lane",
            "age_bucket",
            "conflict",
            "dispute",
            "recalled",
            "labeled_consequence",
        ],
        ...,
    ]
    per_stratum: Annotated[int, Field(ge=1)]
    excluded_strata: tuple[Token, ...] = ()


class Manifest(Record):
    manifest_schema_version: Literal["engram-eval-dataset-manifest-v1"]
    dataset_id: Token
    dataset_version: Token
    label_schema_version: Literal["engram-admission-label-v1"]
    created_at: AwareDatetime
    snapshot_as_of: AwareDatetime
    source_class: Literal["synthetic", "dogfood", "incident", "sanitized"]
    code_sha: Annotated[str, Field(pattern=r"^[0-9a-f]{40}$")]
    sample_count: Annotated[int, Field(ge=0)]
    eligible_population_count: Annotated[int, Field(ge=0)]
    allowed_use: Literal["evaluation_only"]
    privacy_class: Literal[
        "public_synthetic", "sanitized_fixture", "private_dogfood", "private_incident"
    ]
    sampling: Sampling
    sample_ids: tuple[Token, ...]
    sample_content_hashes: tuple[ContentHash, ...]
    data_digest: Digest
    stratum_counts: tuple[tuple[Token, int], ...]

    @model_validator(mode="after")
    def membership(self) -> Self:
        if has_secrets(self.model_dump_json()):
            raise ValueError("manifest_secret_rejected")
        if (
            len(self.sample_ids) != self.sample_count
            or len(self.sample_content_hashes) != self.sample_count
            or len(set(self.sample_ids)) != self.sample_count
            or self.sample_count > self.eligible_population_count
        ):
            raise ValueError("membership_mismatch")
        if self.source_class in ("dogfood", "incident") and not self.privacy_class.startswith(
            "private_"
        ):
            raise ValueError("private_source_required")
        return self


def digest(value: Any) -> str:
    return hashlib.sha256(rfc8785.dumps(value)).hexdigest()

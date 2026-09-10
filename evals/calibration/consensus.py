"""Frozen three-frontier-model consensus review protocol (#206, ENG-CALIBRATION-001G).

This module freezes the review-methodology correction for #202 BEFORE any
model review executes. It must not be edited after the methodology PR merges
without a new protocol version and explicit re-freeze.

Frozen values (protocol version ``eng-calibration-consensus-206-v1``):

- reviewer families (exactly three): claude-opus, gpt-astra, glm-5-3-max;
- consensus-critical fields (exact agreement required on ALL five):
  expected_kind, retention_value, epistemic_state, consequence,
  acceptable_abstention;
- diagnostic-only fields (never create human cases by disagreement):
  every other ``Dimensions`` field;
- escalation triggers: any critical-field disagreement, any
  unknown/uncertain/ambiguous critical value, malformed/unparseable output,
  refusal, provider failure, any reviewer confidence below ``medium`` on its
  own judgment, and any reviewer assigning ``consequence=high``;
- audit: 15% of otherwise consensus-accepted cases, selected deterministically
  by frozen sample ID and seed ``202-model-consensus-audit-v1`` with marginal
  coverage of source type, kind, review status, and age bucket;
- audit escalation threshold: material audit disagreement on any critical
  field for more than 5% of audited cases, any single audited consensus error
  with adjudicated ``consequence=high``, or any material calibration
  reversal, escalates ALL remaining consensus cases to human review;
- no majority voting: 2-of-3 agreement never qualifies as consensus;
- model judgments are never stored or reported as ``human_adjudicated``.

Failure semantics are orthogonal and truthful (FIX-6):

- ``parse_status``: ``parsed`` (valid judgment parsed from response bytes),
  ``malformed`` (response bytes exist but do not parse as a judgment),
  ``absent`` (no model response exists — provider execution failed);
- ``outcome_status``: ``judged``, ``refused`` (response received, explicit
  refusal), ``malformed`` (response received, schema parse failed),
  ``provider_error`` (no usable response / provider execution failed).
"""

from __future__ import annotations

import hashlib
import hmac
import math
from collections.abc import Mapping, Sequence
from typing import Annotated, Any, Literal, Self

import rfc8785
from pydantic import AwareDatetime, Field, model_validator

from evals.admission.schema import Digest, Record, Token
from evals.calibration.freeze import (
    AUDIT_COVERAGE_AXES,
    LABEL_GUIDE_VERSION,
    FrameRow,
    SamplingManifest,
)
from evals.calibration.provider_metadata import verify_evidence_against_artifact

CONSENSUS_PROTOCOL_VERSION: Literal["eng-calibration-consensus-206-v1"] = (
    "eng-calibration-consensus-206-v1"
)
MODEL_REVIEW_SCHEMA: Literal["engram-calibration-model-review-206-v2"] = (
    "engram-calibration-model-review-206-v2"
)
CORRELATION_REPORT_SCHEMA: Literal["engram-calibration-correlation-206-v1"] = (
    "engram-calibration-correlation-206-v1"
)
PROVENANCE_WRAPPER_SCHEMA: Literal["engram-calibration-consensus-provenance-206-v1"] = (
    "engram-calibration-consensus-provenance-206-v1"
)
CONSENSUS_LEDGER_SCHEMA: Literal["engram-calibration-consensus-ledger-206-v1"] = (
    "engram-calibration-consensus-ledger-206-v1"
)
AUDIT_SAMPLE_RATE = 0.15
AUDIT_SELECTION_SEED = "202-model-consensus-audit-v1"
AUDIT_DISAGREEMENT_RATE_THRESHOLD = 0.05
MIN_REVIEWER_CONFIDENCE = "medium"
CONFIDENCE_ORDER: dict[str, int] = {
    "low": 0,
    "medium": 1,
    "high": 2,
    "unknown": 0,
}
CRITICAL_FIELDS: tuple[str, ...] = (
    "expected_kind",
    "retention_value",
    "epistemic_state",
    "consequence",
    "acceptable_abstention",
)
# Values that make a judgment non-consensus-eligible even when all three
# reviewers agree exactly (unknown / uncertain / ambiguous epistemics).
NON_CONSENSUS_VALUES: dict[str, frozenset[str]] = {
    "expected_kind": frozenset({"unknown"}),
    "retention_value": frozenset({"uncertain"}),
    "epistemic_state": frozenset({"unknown", "ambiguous"}),
    "consequence": frozenset({"unknown"}),
    "acceptable_abstention": frozenset({"unknown"}),
}
REVIEWER_FAMILIES: tuple[str, ...] = ("claude-opus", "gpt-astra", "glm-5-3-max")
REVIEWER_SLOTS: tuple[str, ...] = ("model_a", "model_b", "model_c")
FAMILY_BY_SLOT: dict[str, str] = dict(zip(REVIEWER_SLOTS, REVIEWER_FAMILIES, strict=True))
SlotName = Literal["model_a", "model_b", "model_c"]
ModelIdentifier = Annotated[str, Field(min_length=1, max_length=256)]
# Orthogonal failure semantics (FIX-6):
#   parsed     -> response bytes parsed into a valid judgment
#   malformed  -> response bytes exist, judgment-schema parse failed
#   absent     -> NO response bytes exist (provider execution failed)
ParseStatus = Literal["parsed", "malformed", "absent"]
#   judged         -> valid parsed judgment
#   refused        -> response received, explicit refusal / no judgment
#   malformed      -> response received but schema parsing failed
#   provider_error -> no model response / provider execution failed
OutcomeStatus = Literal["judged", "refused", "malformed", "provider_error"]
CriticalFieldVocabulary: dict[str, set[str]] = {
    "expected_kind": {
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
    },
    "retention_value": {"retain", "do_not_retain", "uncertain"},
    "epistemic_state": {
        "adequately_supported",
        "weakly_supported",
        "contradicted",
        "contested",
        "ambiguous",
        "unverifiable",
        "unknown",
    },
    "consequence": {"low", "medium", "high", "unknown"},
    "acceptable_abstention": {"yes", "no", "unknown"},
}
# Every Dimensions field that is not consensus-critical is diagnostic-only:
# reviewers MAY return these fields, and disagreement on them NEVER creates a
# human case or blocks consensus acceptance.
DIAGNOSTIC_FIELDS: tuple[str, ...] = (
    "atomic",
    "proposition_count",
    "attribution",
    "source_span",
    "evidence_span",
    "assertion_origin",
    "expected_subject_or_domain",
    "expected_scope",
    "factual_outcome",
    "expected_storage_disposition",
    "expected_startup_eligibility",
    "expected_governed_semantic_eligibility",
    "human_review_required",
    "conflict_expected",
    "dispute_expected",
    "supersession_expected",
    "temporal_validity_issue",
    "scope_visibility_concern",
    "evidence_independence",
    "expected_blockers",
    "expected_next_action",
)

# Frozen calibration-outcome polarity used ONLY by the material-calibration
# reversal rule (FIX-3). The taxonomy dimension's outcome depends on the
# provider's later ``suggested_kind``; that dependency is represented
# explicitly and the rule fails closed until it can be evaluated.
REVERSAL_POLARITY: dict[str, dict[str, str]] = {
    "retention_value": {
        "retain": "positive",
        "do_not_retain": "negative",
        "uncertain": "unknown",
    },
    "epistemic_state": {
        "adequately_supported": "positive",
        "weakly_supported": "positive",
        "contradicted": "negative",
        "contested": "negative",
        "unverifiable": "negative",
        "ambiguous": "unknown",
        "unknown": "unknown",
    },
}


def digest_of(value: Any) -> str:
    return hashlib.sha256(rfc8785.dumps(value)).hexdigest()


def _audit_rank(sample_id: str, seed: str = AUDIT_SELECTION_SEED) -> tuple[str, str]:
    return (
        hmac.new(seed.encode(), sample_id.encode(), hashlib.sha256).hexdigest(),
        sample_id,
    )


class ReviewerIdentity(Record):
    """Provenance for one first-pass model reviewer lane."""

    reviewer_slot: SlotName
    reviewer_family: str
    provider_model_identifier: ModelIdentifier
    reviewer_config_digest: Digest
    prompt_digest: Digest
    label_guide_version: str = LABEL_GUIDE_VERSION

    @model_validator(mode="after")
    def family_matches_slot(self) -> Self:
        if FAMILY_BY_SLOT[self.reviewer_slot] != self.reviewer_family:
            raise ValueError("reviewer_family_does_not_match_frozen_slot")
        return self

    def lane_identity_digest(self) -> Digest:
        return digest_of(self.model_dump(mode="json"))


class ModelJudgment(Record):
    """Parsed critical fields (plus optional diagnostics) from one reviewer."""

    fields: dict[str, Any]
    reviewer_confidence: Literal["low", "medium", "high", "unknown"] = "unknown"

    @model_validator(mode="after")
    def closed_critical_vocabulary(self) -> Self:
        missing = set(CRITICAL_FIELDS) - set(self.fields)
        if missing:
            raise ValueError(f"missing_critical_fields:{','.join(sorted(missing))}")
        extra = set(self.fields) - set(CRITICAL_FIELDS) - set(DIAGNOSTIC_FIELDS)
        if extra:
            raise ValueError(f"unknown_review_fields:{','.join(sorted(extra))}")
        for name, vocabulary in CriticalFieldVocabulary.items():
            if self.fields[name] not in vocabulary:
                raise ValueError(f"critical_field_out_of_vocabulary:{name}")
        return self

    def critical(self) -> dict[str, Any]:
        return {name: self.fields[name] for name in CRITICAL_FIELDS}


class ExecutionEvidence(Record):
    """FIX-R5-1: OBSERVED execution identity, supplied by the executor.

    Every ``actual_*`` field describes the executor that REALLY produced the
    response — provider-reported metadata when the runtime exposes it, or an
    explicitly-marked ``executor_attestation`` when it cannot. These values
    are NEVER derived from (or copied out of) the expected lane
    ``ReviewerIdentity``: the executor wrapper observes them independently at
    execution time.

    ``request_generation`` / ``request_item_digest`` tie the execution to one
    exact emitted request item (immutable batch generation + canonical digest
    of the emitted request line; see ``verify_request_batch`` in
    ``evals.calibration.ingestion``). Provider-error executions reference the
    attempted request the same way.

    ``identity_source`` records HOW the actual identity was observed:

    - ``provider_metadata``: machine-verifiable provider metadata — the
      evidence embeds a digest-bound provider metadata artifact
      (``provider_metadata``, FIX-R6-2) whose bytes MECHANICALLY DERIVE the
      reported model identifier and provider request/response IDs. REQUIRED
      for a lane to freeze — only machine-verified execution identity can
      ground a consensus reviewer lane;
    - ``executor_attestation``: the executor attests the identity but the
      environment cannot machine-verify it (no provider metadata artifact
      can be captured). An honest, protected representation — ingestible
      and preserved, but a lane carrying one can NEVER freeze as a valid
      consensus reviewer lane, and no invented request ID can upgrade it.
    """

    evidence_schema: Literal["engram-calibration-execution-evidence-206-v1"] = (
        "engram-calibration-execution-evidence-206-v1"
    )
    campaign_id: str
    actual_reviewer_slot: SlotName
    actual_reviewer_family: str
    actual_provider_model_identifier: ModelIdentifier
    actual_configuration_digest: Digest
    actual_prompt_digest: Digest
    request_generation: int = Field(ge=1)
    request_item_digest: Digest
    executed_at: AwareDatetime
    executor_status: Literal["completed", "provider_error"]
    # Executor/session/run identity — who actually executed the request.
    executor_identity: str
    identity_source: Literal["provider_metadata", "executor_attestation"]
    # Optional provider-reported request/response IDs, preserved and bound.
    provider_request_id: str | None = None
    provider_response_id: str | None = None
    # FIX-R6-2: the digest-bound provider metadata artifact. REQUIRED for
    # ``identity_source == "provider_metadata"`` — the model identifier and
    # provider IDs above must be the MECHANICAL DERIVATION of these bytes
    # (verified at schema validation and re-derived at every boundary).
    # Structurally impossible for ``executor_attestation`` (None).
    provider_metadata: dict[str, Any] | None = None

    @model_validator(mode="after")
    def evidence_contract(self) -> Self:
        if FAMILY_BY_SLOT[self.actual_reviewer_slot] != self.actual_reviewer_family:
            raise ValueError("actual_family_does_not_match_attested_slot")
        if self.identity_source == "provider_metadata":
            if not (self.provider_request_id and self.provider_response_id):
                raise ValueError("provider_metadata_identity_requires_provider_ids")
            # FIX-R6-2: a provider-metadata LABEL is not provider-metadata
            # VERIFICATION — the embedded artifact must digest-bind its raw
            # metadata and mechanically derive the claimed identity.
            verify_evidence_against_artifact(self)
        else:
            if self.provider_metadata is not None:
                raise ValueError("executor_attestation_must_not_claim_provider_metadata")
        if not self.executor_identity:
            raise ValueError("execution_evidence_requires_executor_identity")
        return self

    def evidence_digest(self) -> Digest:
        return digest_of(self.model_dump(mode="json"))


class ExecutionReceipt(Record):
    """FIX-R4-1 / FIX-R5-1: immutable proof of which executor actually
    produced a response.

    DERIVED from ``ExecutionEvidence`` (``from_evidence`` is the only
    production derivation path) — never constructed from the expected lane
    ``ReviewerIdentity``. The identity fields below are the ATTESTED ACTUAL
    executor identity; every one is compared for exact equality against the
    frozen ``ReviewerIdentity`` at ingestion, freeze, load, and final ledger
    verification, so a response produced by any other provider, model
    version, configuration, or prompt can never be accepted into this lane.

    ``request_generation`` / ``request_item_digest`` tie the response to one
    exact emitted request item: the immutable batch generation and the
    canonical digest of the emitted request item (see
    ``verify_request_batch`` in ``evals.calibration.ingestion``).
    Provider-error responses reference the attempted request the same way.

    ``evidence`` / ``evidence_digest`` bind the exact observed evidence the
    receipt was derived from: a receipt whose fields disagree with its own
    embedded evidence cannot validate.
    """

    receipt_schema: Literal["engram-calibration-execution-receipt-206-v2"] = (
        "engram-calibration-execution-receipt-206-v2"
    )
    campaign_id: str
    # ATTESTED ACTUAL executor identity (derived from ExecutionEvidence).
    reviewer_slot: SlotName
    reviewer_family: str
    provider_model_identifier: ModelIdentifier
    reviewer_config_digest: Digest
    prompt_digest: Digest
    request_generation: int = Field(ge=1)
    request_item_digest: Digest
    executed_at: AwareDatetime
    executor_status: Literal["completed", "provider_error"]
    executor_identity: str
    identity_source: Literal["provider_metadata", "executor_attestation"]
    provider_request_id: str | None = None
    provider_response_id: str | None = None
    provider_metadata: dict[str, Any] | None = None
    evidence: ExecutionEvidence
    evidence_digest: Digest

    @model_validator(mode="after")
    def derived_from_evidence(self) -> Self:
        evidence = self.evidence
        derived_fields = (
            self.campaign_id == evidence.campaign_id
            and self.reviewer_slot == evidence.actual_reviewer_slot
            and self.reviewer_family == evidence.actual_reviewer_family
            and self.provider_model_identifier == evidence.actual_provider_model_identifier
            and self.reviewer_config_digest == evidence.actual_configuration_digest
            and self.prompt_digest == evidence.actual_prompt_digest
            and self.request_generation == evidence.request_generation
            and self.request_item_digest == evidence.request_item_digest
            and self.executed_at == evidence.executed_at
            and self.executor_status == evidence.executor_status
            and self.executor_identity == evidence.executor_identity
            and self.identity_source == evidence.identity_source
            and self.provider_request_id == evidence.provider_request_id
            and self.provider_response_id == evidence.provider_response_id
            and self.provider_metadata == evidence.provider_metadata
        )
        if not derived_fields:
            raise ValueError("execution_receipt_not_derived_from_its_evidence")
        if self.evidence_digest != evidence.evidence_digest():
            raise ValueError("execution_receipt_evidence_digest_mismatch")
        return self

    @classmethod
    def from_evidence(cls, evidence: ExecutionEvidence) -> ExecutionReceipt:
        """The ONE production derivation path (FIX-R5-1)."""
        return cls(
            campaign_id=evidence.campaign_id,
            reviewer_slot=evidence.actual_reviewer_slot,
            reviewer_family=evidence.actual_reviewer_family,
            provider_model_identifier=evidence.actual_provider_model_identifier,
            reviewer_config_digest=evidence.actual_configuration_digest,
            prompt_digest=evidence.actual_prompt_digest,
            request_generation=evidence.request_generation,
            request_item_digest=evidence.request_item_digest,
            executed_at=evidence.executed_at,
            executor_status=evidence.executor_status,
            executor_identity=evidence.executor_identity,
            identity_source=evidence.identity_source,
            provider_request_id=evidence.provider_request_id,
            provider_response_id=evidence.provider_response_id,
            provider_metadata=evidence.provider_metadata,
            evidence=evidence,
            evidence_digest=evidence.evidence_digest(),
        )

    def matches_reviewer_identity(self, reviewer: ReviewerIdentity, campaign_id: str) -> bool:
        """Compare the ATTESTED ACTUAL identity EXACTLY against the frozen
        expected lane identity (FIX-R5-1)."""
        return (
            self.campaign_id == campaign_id
            and self.reviewer_slot == reviewer.reviewer_slot
            and self.reviewer_family == reviewer.reviewer_family
            and self.provider_model_identifier == reviewer.provider_model_identifier
            and self.reviewer_config_digest == reviewer.reviewer_config_digest
            and self.prompt_digest == reviewer.prompt_digest
        )


class ModelReviewRecord(Record):
    """One reviewer's first-pass artifact for one sample.

    Never interchangeable with a human judgment: distinct schema name, slot
    and family provenance required, and the frozen #202 ledger validators
    reject any attempt to feed these rows in as reviewer labels.

    Failure semantics (orthogonal, FIX-6):

    - ``(parsed, judged)``: valid judgment; raw response digest required.
    - ``(malformed, refused)``: response received, explicit refusal; raw
      response digest required; error code required.
    - ``(malformed, malformed)``: response received, parse failed; raw
      response digest required; error code required.
    - ``(absent, provider_error)``: NO response bytes; raw response digest
      must be ``None``; error code required.

    FIX-R4-1 (execution provenance): the record carries an immutable
    execution receipt binding the response to an ACTUAL emitted request item
    and the ACTUAL executor identity reported by the execution wrapper —
    never merely the lane configuration:

    - ``execution``: the actual provider/model/config identity that claims to
      have produced the response (compared exactly against the frozen
      ``ReviewerIdentity`` at ingestion/freeze/load/verify);
    - ``request_generation`` / ``request_item_digest``: the immutable request
      batch generation and the canonical digest of the exact emitted request
      item this response answers.

    FIX-R4-2 (derivation provenance): for parsed judgments the stored
    ``judgment`` must be derived by the frozen deterministic parser from the
    exact preserved response bytes; ``parser_version`` records the frozen
    parser identity and is required exactly on parsed records.
    """

    review_schema: Literal["engram-calibration-model-review-206-v2"] = MODEL_REVIEW_SCHEMA
    protocol_version: str
    campaign_id: str
    sampling_manifest_digest: str
    source_packet_digest: str
    sample_id: Token
    reviewer_slot: SlotName
    reviewer_family: str
    provider_model_identifier: ModelIdentifier
    reviewer_config_digest: str
    prompt_digest: str
    label_guide_version: str
    captured_at: AwareDatetime
    parse_status: ParseStatus
    outcome_status: OutcomeStatus
    execution: ExecutionReceipt | None = None
    request_generation: int | None = None
    request_item_digest: Digest | None = None
    parser_version: str | None = None
    reviewer_confidence: Literal["low", "medium", "high", "unknown"] = "unknown"
    judgment: ModelJudgment | None = None
    raw_response_digest: Digest | None = None
    error_code: Token | None = None

    @model_validator(mode="after")
    def status_contract(self) -> Self:
        if self.protocol_version != CONSENSUS_PROTOCOL_VERSION:
            raise ValueError("protocol_version_mismatch")
        if FAMILY_BY_SLOT[self.reviewer_slot] != self.reviewer_family:
            raise ValueError("reviewer_family_does_not_match_frozen_slot")
        if self.label_guide_version != LABEL_GUIDE_VERSION:
            raise ValueError("label_guide_version_mismatch")
        if self.execution is None or self.request_generation is None:
            raise ValueError("model_review_record_requires_execution_receipt")
        if self.request_item_digest is None:
            raise ValueError("model_review_record_requires_request_item_digest")
        if self.parse_status == "parsed":
            if self.outcome_status != "judged":
                raise ValueError("parsed_review_requires_judged_outcome")
            if self.judgment is None:
                raise ValueError("parsed_review_requires_judgment")
            if self.error_code is not None:
                raise ValueError("judged_review_must_not_carry_error_code")
            if self.judgment.reviewer_confidence != self.reviewer_confidence:
                raise ValueError("reviewer_confidence_must_match_judgment")
            if self.raw_response_digest is None:
                raise ValueError("parsed_review_requires_raw_response_digest")
            from evals.calibration.reviewer_instructions import RESPONSE_PARSER_VERSION

            if self.parser_version != RESPONSE_PARSER_VERSION:
                raise ValueError("parsed_review_requires_frozen_parser_version")
        else:
            if self.parser_version is not None:
                raise ValueError("unparsed_review_must_not_carry_parser_version")
            if self.parse_status == "malformed":
                # Response bytes exist (refusal or unparseable output).
                if self.outcome_status not in ("refused", "malformed"):
                    raise ValueError("malformed_response_requires_refused_or_malformed_outcome")
                if self.judgment is not None:
                    raise ValueError("unparseable_review_must_not_carry_judgment")
                if self.error_code is None:
                    raise ValueError("failed_review_requires_error_code")
                if self.raw_response_digest is None:
                    raise ValueError("response_received_requires_raw_response_digest")
            else:  # absent: provider execution failed, no response bytes
                if self.outcome_status != "provider_error":
                    raise ValueError("absent_response_requires_provider_error_outcome")
                if self.judgment is not None:
                    raise ValueError("provider_error_must_not_carry_judgment")
                if self.error_code is None:
                    raise ValueError("failed_review_requires_error_code")
                if self.raw_response_digest is not None:
                    raise ValueError("provider_error_without_response_must_not_carry_digest")
        return self

    def record_digest(self) -> Digest:
        return digest_of(self.model_dump(mode="json"))


class LaneFreeze(Record):
    """Completion attestation for one reviewer lane (all frozen cases)."""

    lane_schema: Literal["engram-calibration-model-lane-206-v1"] = (
        "engram-calibration-model-lane-206-v1"
    )
    protocol_version: str
    campaign_id: str
    reviewer: ReviewerIdentity
    sampling_manifest_digest: str
    source_packet_digest: str
    # FIX-R3-3: the exact neutral packet bytes the lane reviewed against,
    # carried from the lane authority into the frozen attestation.
    neutral_packet_sha256: str
    sample_ids: tuple[Token, ...]
    record_digests: tuple[Digest, ...]

    @model_validator(mode="after")
    def lane_membership(self) -> Self:
        if self.protocol_version != CONSENSUS_PROTOCOL_VERSION:
            raise ValueError("protocol_version_mismatch")
        if len(self.sample_ids) != len(self.record_digests):
            raise ValueError("lane_membership_length_mismatch")
        if len(set(self.sample_ids)) != len(self.sample_ids):
            raise ValueError("lane_duplicate_sample_id")
        return self

    def lane_digest(self) -> Digest:
        return digest_of(self.model_dump(mode="json"))


def validate_lane_membership(
    lane: LaneFreeze, sampling: SamplingManifest, *, source_packet_digest: str
) -> None:
    """Fail closed unless the lane covers exactly the frozen sample membership."""
    if lane.sampling_manifest_digest != sampling.manifest_digest():
        raise ValueError("lane_sampling_manifest_mismatch")
    if not hmac.compare_digest(lane.source_packet_digest, source_packet_digest):
        raise ValueError("lane_source_packet_mismatch")
    if tuple(lane.sample_ids) != tuple(sampling.sample_ids):
        raise ValueError("lane_sample_membership_mismatch")


def validate_lane_isolation(lanes: Sequence[LaneFreeze]) -> None:
    """Prove lanes are provenance-independent: distinct slots and identities.

    Lane isolation at execution time is enforced by the harness design (each
    lane runs in a separate context with only its own packet); this check
    proves the provenance side: no two lanes may share a slot or a reviewer
    identity digest, and all three frozen slots must be present.
    """
    slots = [lane.reviewer.reviewer_slot for lane in lanes]
    if len(set(slots)) != len(slots):
        raise ValueError("duplicate_reviewer_slot")
    identities = [lane.reviewer.lane_identity_digest() for lane in lanes]
    if len(set(identities)) != len(identities):
        raise ValueError("duplicate_reviewer_identity")
    if set(slots) != set(REVIEWER_SLOTS):
        raise ValueError("missing_reviewer_lane")
    neutral_packet_digests = {lane.neutral_packet_sha256 for lane in lanes}
    if len(neutral_packet_digests) != 1:
        raise ValueError("lane_neutral_packet_digest_divergence")


def judgment_is_consensus_eligible(judgment: ModelJudgment) -> bool:
    """A single judgment is eligible only if confident and non-degenerate."""
    if CONFIDENCE_ORDER[judgment.reviewer_confidence] < CONFIDENCE_ORDER[MIN_REVIEWER_CONFIDENCE]:
        return False
    for name, forbidden in NON_CONSENSUS_VALUES.items():
        if judgment.fields[name] in forbidden:
            return False
    return True


def _outcome_reason(record: ModelReviewRecord) -> str | None:
    """Escalation reason for a non-judged record (truthful, orthogonal)."""
    if record.outcome_status == "refused":
        return "refused"
    if record.outcome_status == "provider_error":
        return "provider_error"
    if record.outcome_status == "malformed":
        return "malformed_review"
    return None


def classify_case(records_by_slot: Mapping[str, ModelReviewRecord]) -> dict[str, Any]:
    """Classify one case from its three first-pass model records.

    Returns ``consensus`` (unanimous exact agreement on all five critical
    fields with every judgment eligible) or the deduplicated escalation
    reasons that put the case in the mandatory human queue.
    """
    if set(records_by_slot) != set(REVIEWER_SLOTS):
        raise ValueError("case_requires_all_three_lanes")
    reasons: list[str] = []
    judgments: list[ModelJudgment] = []
    for slot in REVIEWER_SLOTS:
        record = records_by_slot[slot]
        if record.outcome_status != "judged" or record.parse_status != "parsed":
            reason = _outcome_reason(record)
            if reason is not None:
                reasons.append(reason)
        elif record.judgment is not None:
            judgments.append(record.judgment)
    if len(judgments) == len(REVIEWER_SLOTS):
        criticals = [j.critical() for j in judgments]
        unanimous = all(critical == criticals[0] for critical in criticals[1:])
        degenerate = any(
            judgment.fields[name] in forbidden
            for judgment in judgments
            for name, forbidden in NON_CONSENSUS_VALUES.items()
        )
        below_floor = any(not judgment_is_consensus_eligible(j) for j in judgments)
        if not unanimous:
            reasons.append("critical_field_disagreement")
        if degenerate:
            reasons.append("uncertain_unknown_or_ambiguous_critical_value")
        if below_floor:
            reasons.append("reviewer_below_confidence_floor")
    else:
        reasons.append("missing_parsed_judgment")
    high_signal = any(
        record.judgment is not None and record.judgment.fields.get("consequence") == "high"
        for record in records_by_slot.values()
    )
    if high_signal:
        reasons.append("high_consequence_signal")
    unique_reasons: list[str] = []
    for reason in reasons:
        if reason not in unique_reasons:
            unique_reasons.append(reason)
    consensus = not unique_reasons
    return {
        "consensus": consensus,
        "escalation_reasons": unique_reasons,
        "high_consequence_signal": high_signal,
    }


def unanimous_consensus_critical(judgments: Sequence[ModelJudgment]) -> dict[str, Any] | None:
    """Mechanically derive the unanimous consensus critical fields, or None."""
    if len(judgments) != len(REVIEWER_SLOTS):
        return None
    criticals = [j.critical() for j in judgments]
    first = criticals[0]
    for critical in criticals[1:]:
        if critical != first:
            return None
    return dict(first)


def audit_target_count(population: int, *, rate: float = AUDIT_SAMPLE_RATE) -> int:
    """Exact frozen audit size: ``ceil(rate * population)`` bounded by the pool."""
    if population <= 0:
        return 0
    return min(math.ceil(rate * population), population)


def select_audit_sample(
    consensus_sample_ids: Sequence[str],
    *,
    frame_rows: Mapping[str, FrameRow] | None = None,
    rate: float = AUDIT_SAMPLE_RATE,
    seed: str = AUDIT_SELECTION_SEED,
) -> tuple[str, ...]:
    """Deterministically select ``ceil(rate * N)`` consensus cases for audit.

    Label-blind: selection uses only consensus membership plus pre-existing
    frozen frame metadata (FIX-2). It NEVER reads judgment values beyond the
    binary fact that a case is consensus-eligible.

    When ``frame_rows`` is supplied, the frozen marginal-coverage algorithm
    runs (see ``select_audit_sample_with_coverage``): marginal cells across
    ``source_type``, ``kind``, ``review_status`` and ``age_bucket`` are
    covered first by frozen HMAC rank, then remaining slots fill globally by
    rank — never exceeding the frozen target count. Without frame metadata
    the selection is the pure global HMAC rank (usable only where no frame
    exists, e.g. degenerate tests).
    """
    if frame_rows is None:
        return _select_audit_sample_ranked(consensus_sample_ids, rate=rate, seed=seed)
    return select_audit_sample_with_coverage(
        consensus_sample_ids, frame_rows, rate=rate, seed=seed
    ).selected


def _select_audit_sample_ranked(
    consensus_sample_ids: Sequence[str],
    *,
    rate: float,
    seed: str,
) -> tuple[str, ...]:
    if not 0.0 < rate <= 1.0:
        raise ValueError("audit_rate_out_of_range")
    unique = sorted(set(consensus_sample_ids))
    if len(unique) != len(consensus_sample_ids):
        raise ValueError("audit_pool_membership_duplicate")
    target = audit_target_count(len(unique), rate=rate)
    ranked = sorted(unique, key=lambda sample_id: _audit_rank(sample_id, seed))
    return tuple(ranked[:target])


class AuditSelection(Record):
    """Frozen marginal-coverage audit selection plus its coverage evidence."""

    selected: tuple[str, ...]
    target_count: int
    population_count: int
    covered_cells: tuple[str, ...]
    uncovered_cells: tuple[str, ...]
    algorithm: Literal["marginal-coverage-greedy-hmac-v1"] = "marginal-coverage-greedy-hmac-v1"


def select_audit_sample_with_coverage(
    consensus_sample_ids: Sequence[str],
    frame_rows: Mapping[str, FrameRow],
    *,
    rate: float = AUDIT_SAMPLE_RATE,
    seed: str = AUDIT_SELECTION_SEED,
) -> AuditSelection:
    """Frozen 15% audit selection WITH marginal coverage (FIX-2, FIX-R2-6).

    Deterministic, label-blind algorithm (``marginal-coverage-greedy-hmac-v1``):

    1. Required marginal cells are derived from the consensus population per
       frozen axis (``source_type``, ``kind``, ``review_status``,
       ``age_bucket``) using only pre-existing frozen frame metadata.
    2. The frozen HMAC seed/rank is the ONLY ranking and tie-break primitive.
    3. Phase A greedily maximizes marginal coverage: while slots remain and
       uncovered cells exist, select the remaining case covering the MOST
       currently-uncovered cells, breaking ties by frozen HMAC rank.
    4. Phase B fills the remaining audit slots by global HMAC rank.
    5. The selection never exceeds ``ceil(rate * N)``.

    Truthful guarantee (FIX-R2-6, option B): this algorithm deterministically
    MAXIMIZES marginal coverage under the frozen greedy rule; it is NOT an
    exact set-cover solver, and ``uncovered_cells != ()`` does NOT prove that
    full coverage was mathematically infeasible at the target size — only
    that the frozen greedy rule left these cells uncovered. Uncovered cells
    are reported honestly, never silently claimed as covered.
    """
    if not 0.0 < rate <= 1.0:
        raise ValueError("audit_rate_out_of_range")
    unique = sorted(set(consensus_sample_ids))
    if len(unique) != len(consensus_sample_ids):
        raise ValueError("audit_pool_membership_duplicate")
    missing = [sid for sid in unique if sid not in frame_rows]
    if missing:
        raise ValueError("audit_frame_membership_missing")
    target = audit_target_count(len(unique), rate=rate)
    if target == 0:
        return AuditSelection(
            selected=(),
            target_count=0,
            population_count=len(unique),
            covered_cells=(),
            uncovered_cells=(),
        )
    ranked = sorted(unique, key=lambda sample_id: _audit_rank(sample_id, seed))

    def cells_of(sample_id: str) -> frozenset[str]:
        row = frame_rows[sample_id]
        return frozenset(f"{axis}={getattr(row, axis)}" for axis in AUDIT_COVERAGE_AXES)

    cells: set[str] = set()
    for sid in unique:
        cells |= cells_of(sid)
    covered: set[str] = set()
    selected: list[str] = []
    chosen: set[str] = set()
    # Phase A: greedy max-new-cell coverage, ties by frozen HMAC rank (FIX-R2-6).
    while len(selected) < target and covered < cells:
        remaining = [sid for sid in ranked if sid not in chosen]
        best: str | None = None
        best_gain = 0
        for sid in remaining:
            gain = len(cells_of(sid) - covered)
            if gain > best_gain:
                best, best_gain = sid, gain
        if best is None or best_gain <= 0:
            break
        selected.append(best)
        chosen.add(best)
        covered |= cells_of(best)
    # Phase B: global rank fill.
    for sid in ranked:
        if len(selected) >= target:
            break
        if sid not in chosen:
            selected.append(sid)
            chosen.add(sid)
    return AuditSelection(
        selected=tuple(selected),
        target_count=target,
        population_count=len(unique),
        covered_cells=tuple(sorted(covered)),
        uncovered_cells=tuple(sorted(cells - covered)),
    )


def material_disagreement(
    consensus_critical: Mapping[str, Any], human_final_critical: Mapping[str, Any]
) -> bool:
    """Material audit disagreement: human differs from unanimous model
    consensus on ANY of the five critical fields."""
    return any(
        consensus_critical.get(field) != human_final_critical.get(field)
        for field in CRITICAL_FIELDS
    )


def material_calibration_reversal(
    consensus_critical: Mapping[str, Any],
    human_final_critical: Mapping[str, Any],
    *,
    suggested_kind: str | None = None,
) -> bool | None:
    """Material calibration reversal (FIX-3), mechanically defined.

    A material disagreement materially reverses a calibration outcome when it
    flips the frozen calibration-outcome polarity (``REVERSAL_POLARITY``) of
    at least one dimension:

    - ``retention``: retain <-> do_not_retain (uncertain is unknown-polarity);
    - ``epistemic``: supported <-> not-supported;
    - ``taxonomy``: depends on the provider's later ``suggested_kind``; when
      ``suggested_kind`` is unavailable the taxonomy dimension CANNOT be
      evaluated and the rule FAILS CLOSED (returns True = treat as a
      reversal) whenever the expected_kind values have opposite
      correctness against the unavailable suggestion — i.e. whenever the
      human and consensus ``expected_kind`` differ. Callers that possess the
      later assessment evidence pass ``suggested_kind`` for exact evaluation.

    Returns ``True`` (reversal), ``False`` (provably no reversal), or ``None``
    only when there is no material disagreement at all.
    """
    if not material_disagreement(consensus_critical, human_final_critical):
        return None
    for field, polarity in REVERSAL_POLARITY.items():
        consensus_polarity = polarity.get(str(consensus_critical.get(field)), "unknown")
        human_polarity = polarity.get(str(human_final_critical.get(field)), "unknown")
        if (
            consensus_polarity != "unknown"
            and human_polarity != "unknown"
            and consensus_polarity != human_polarity
        ):
            return True
    # taxonomy: exact evaluation requires the later suggested_kind.
    if consensus_critical.get("expected_kind") != human_final_critical.get("expected_kind"):
        if suggested_kind is None:
            # Fail closed: cannot prove the taxonomy outcome did not reverse.
            return True
        if suggested_kind in ("", "unknown"):
            return False  # both outcomes unknown-polarity under the frozen mapping
        consensus_match = consensus_critical.get("expected_kind") == suggested_kind
        human_match = human_final_critical.get("expected_kind") == suggested_kind
        if consensus_match != human_match:
            return True
    return False


def audit_outcome(
    *,
    audited_count: int,
    material_disagreements: int,
    high_consequence_misses: int,
    material_reversals: int = 0,
) -> dict[str, Any]:
    """Apply the frozen audit escalation thresholds. Fails toward escalation.

    Escalation triggers (any one):

    - material disagreement rate strictly greater than 5% of audited cases;
    - any audited consensus error adjudicated ``consequence=high``;
    - any material calibration reversal (``material_calibration_reversal``
      returned True; fail-closed undeterminable cases must be passed here as
      reversals by the caller).
    """
    escalate = (
        (high_consequence_misses > 0)
        or (material_reversals > 0)
        or (
            audited_count > 0
            and (material_disagreements / audited_count) > AUDIT_DISAGREEMENT_RATE_THRESHOLD
        )
    )
    return {
        "audited_count": audited_count,
        "material_disagreements": material_disagreements,
        "high_consequence_misses": high_consequence_misses,
        "material_reversals": material_reversals,
        "material_disagreement_rate": (
            round(material_disagreements / audited_count, 4) if audited_count else None
        ),
        "escalate_full_human_review": escalate,
    }


def evaluate_audit_outcome(
    *,
    audit_results: Mapping[str, Mapping[str, Any]],
    records_by_lane: Mapping[str, Mapping[str, ModelReviewRecord]],
    suggested_kind_by_sample: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Derive the frozen audit outcome from audited-case human resolutions.

    ``audit_results`` maps audited sample ID -> ``{"human_final_critical": …}``
    — ONLY the human FINAL resolution. The model-consensus side is NEVER
    caller-supplied (FIX-R2-2): it is derived here from the frozen three
    records per sample in ``records_by_lane`` via
    ``unanimous_consensus_critical``. An audited sample lacking three
    unanimous parsed judgments fails closed (it could not have been an
    audit-selected consensus case). High-consequence misses are audited
    consensus errors the human final resolution adjudicates as
    ``consequence=high``.
    """
    material = 0
    high_misses = 0
    reversals = 0
    for sample_id, result in audit_results.items():
        lane_records = records_by_lane.get(sample_id, {})
        judgments: list[ModelJudgment] = []
        for slot in REVIEWER_SLOTS:
            record = lane_records.get(slot)
            if record is not None and record.judgment is not None:
                judgments.append(record.judgment)
        if len(judgments) != len(REVIEWER_SLOTS):
            raise ValueError(f"audit_consensus_not_derivable_from_records:{sample_id}")
        consensus = unanimous_consensus_critical(judgments)
        if consensus is None:
            raise ValueError(f"audit_consensus_not_derivable_from_records:{sample_id}")
        human_final = result["human_final_critical"]
        if not material_disagreement(consensus, human_final):
            continue
        material += 1
        if human_final.get("consequence") == "high":
            high_misses += 1
        suggested = suggested_kind_by_sample.get(sample_id) if suggested_kind_by_sample else None
        if material_calibration_reversal(consensus, human_final, suggested_kind=suggested):
            reversals += 1
    return audit_outcome(
        audited_count=len(audit_results),
        material_disagreements=material,
        high_consequence_misses=high_misses,
        material_reversals=reversals,
    )


def audit_outcome_record_from_evidence(
    *,
    audit_selected_ids: Sequence[str],
    records_by_lane: Mapping[str, Mapping[str, ModelReviewRecord]],
    final_resolutions: Mapping[str, Mapping[str, Any]],
    suggested_kind_by_sample: Mapping[str, str] | None = None,
) -> AuditOutcomeRecord:
    """Derive the authoritative ``AuditOutcomeRecord`` from protected evidence.

    FIX-R2-2: the audit outcome is a DERIVED FACT. Consensus critical fields
    come from the frozen three model records; human authority comes from the
    protected human FINAL resolutions. Nothing caller-asserted enters the
    computation. Used by ``verify_consensus_ledger`` as the sole authority.
    """
    derived = evaluate_audit_outcome(
        audit_results={
            sample_id: {"human_final_critical": final_resolutions[sample_id]}
            for sample_id in audit_selected_ids
        },
        records_by_lane=records_by_lane,
        suggested_kind_by_sample=suggested_kind_by_sample,
    )
    return AuditOutcomeRecord(
        audited_count=derived["audited_count"],
        material_disagreements=derived["material_disagreements"],
        high_consequence_misses=derived["high_consequence_misses"],
        material_reversals=derived["material_reversals"],
        material_disagreement_rate=derived["material_disagreement_rate"],
        escalate_full_human_review=derived["escalate_full_human_review"],
    )


def derive_final_human_population(
    *,
    initial_queue_ids: Sequence[str],
    consensus_ids: Sequence[str],
    audit_selected_ids: Sequence[str],
    resolved_ids: Mapping[str, bool],
    audit_escalated: bool,
) -> dict[str, Any]:
    """Derive the FINAL required human population (FIX-3).

    Sequence enforced:

        three frozen lanes -> initial human queue + audit sample
          -> human audit/adjudication completed -> audit outcome evaluated
          -> PASS: unaudited consensus may remain cross_model_consensus
          -> ESCALATE: every remaining consensus case becomes human-required
          -> all required human resolutions complete -> final ledger

    ``resolved_ids`` maps required sample ID -> final resolution exists.
    """
    required: dict[str, tuple[str, ...]] = {}
    for sample_id in initial_queue_ids:
        required[sample_id] = ("initial_queue",)
    audit_set = set(audit_selected_ids)
    for sample_id in audit_selected_ids:
        existing = required.get(sample_id, ())
        required[sample_id] = existing + ("audit_selected",)
    expanded: list[str] = []
    if audit_escalated:
        for sample_id in consensus_ids:
            if sample_id in audit_set or sample_id in required:
                # audit-selected rows are already required; escalated queue
                # rows are already required
                if sample_id not in required:
                    required[sample_id] = ("audit_escalation_full_human_review",)
                continue
            required[sample_id] = ("audit_escalation_full_human_review",)
            expanded.append(sample_id)
    # unresolved = required cases lacking a final resolution
    unresolved = sorted(sid for sid in required if not resolved_ids.get(sid))
    return {
        "required_ids": tuple(sorted(required)),
        "reasons_by_sample": {sid: reason for sid, reason in sorted(required.items())},
        "expanded_by_escalation": tuple(sorted(expanded)),
        "escalated": audit_escalated,
        "unresolved": tuple(unresolved),
        "complete": not unresolved,
    }


class ConsensusProvenanceWrapper(Record):
    """Per-case provenance wrapper: how the final reference label was reached.

    ``final_label_origin`` is campaign-specific vocabulary that never collides
    with the frozen #202 ``label_origin`` values. ``cross_model_consensus``
    rows carry explicit provenance that NO human directly labeled them;
    ``human_audited_consensus`` rows were audit-selected and human-confirmed.
    """

    wrapper_schema: Literal["engram-calibration-consensus-provenance-206-v1"] = (
        "engram-calibration-consensus-provenance-206-v1"
    )
    protocol_version: str
    campaign_id: str
    sampling_manifest_digest: str
    source_packet_digest: str
    sample_id: Token
    first_pass_record_digests: tuple[Digest, ...]  # exactly three, slot order
    consensus_reached: bool
    entered_human_queue: bool
    queue_reasons: tuple[str, ...] = ()
    human_initial_judgment_digest: Digest | None = None
    final_label_origin: Literal[
        "cross_model_consensus", "human_adjudicated", "human_audited_consensus"
    ]
    final_dimensions: dict[str, Any]
    audit_selected: bool = False

    @model_validator(mode="after")
    def provenance_contract(self) -> Self:
        if self.protocol_version != CONSENSUS_PROTOCOL_VERSION:
            raise ValueError("protocol_version_mismatch")
        if len(self.first_pass_record_digests) != len(REVIEWER_SLOTS):
            raise ValueError("first_pass_record_count_mismatch")
        if self.final_label_origin == "cross_model_consensus":
            if not self.consensus_reached:
                raise ValueError("consensus_origin_requires_consensus")
            if self.entered_human_queue:
                raise ValueError("consensus_origin_requires_no_human_queue")
        else:
            if not self.entered_human_queue:
                raise ValueError("human_origin_requires_human_queue")
            if self.final_label_origin == "human_audited_consensus" and not self.consensus_reached:
                raise ValueError("audited_consensus_requires_consensus")
        if self.entered_human_queue and not self.queue_reasons:
            raise ValueError("human_queue_requires_reasons")
        if self.final_label_origin == "human_audited_consensus":
            if not self.audit_selected:
                raise ValueError("audited_origin_requires_audit_selection")
            if not self.consensus_reached:
                raise ValueError("audited_consensus_requires_consensus")
        elif self.final_label_origin == "human_adjudicated":
            # FIX-R3-5: an audit-selected consensus row the human OVERRODE
            # (final resolution differs on any critical field) is
            # legitimately human_adjudicated — even when the audit as a
            # whole does not escalate. Only a confirmed audited consensus
            # may carry human_audited_consensus.
            if self.audit_selected and not self.consensus_reached:
                # audit selection only ever targets consensus-classified
                # cases; a non-consensus audit-selected row cannot exist.
                raise ValueError("audit_selected_case_requires_consensus_classification")
        elif self.audit_selected:
            raise ValueError("audited_case_must_use_audited_origin")
        return self


class ReferenceLabel(Record):
    """One final reference label under the consensus protocol (#206).

    Carries exactly the five calibration-critical fields plus the campaign
    provenance vocabulary. NOTE (#206 correction): this type alone proves
    NOTHING about provenance. The ONLY normal path into floors/fitting is a
    ``VerifiedConsensusLedger`` produced by
    ``evals.calibration.ledger.verify_consensus_ledger`` from protected
    evidence; free-form lists of ``ReferenceLabel`` are rejected downstream.
    """

    label_schema: Literal["engram-calibration-reference-206-v1"] = (
        "engram-calibration-reference-206-v1"
    )
    sample_id: Token
    final_label_origin: Literal[
        "cross_model_consensus", "human_adjudicated", "human_audited_consensus"
    ]
    critical: dict[str, Any]

    @model_validator(mode="after")
    def closed_vocabulary(self) -> Self:
        if set(self.critical) != set(CRITICAL_FIELDS):
            raise ValueError("reference_label_requires_exactly_critical_fields")
        for name, vocabulary in CriticalFieldVocabulary.items():
            if self.critical[name] not in vocabulary:
                raise ValueError(f"critical_field_out_of_vocabulary:{name}")
        return self


class AuditOutcomeRecord(Record):
    """Frozen audit outcome bound into the final ledger."""

    audited_count: int
    material_disagreements: int
    high_consequence_misses: int
    material_reversals: int
    material_disagreement_rate: float | None
    escalate_full_human_review: bool


class ConsensusLedger(Record):
    """Final reference-label ledger under the consensus protocol.

    Replaces full-population dual review for #202's corrected methodology.
    ``check_floors`` and downstream fitting consume ONLY the final reference
    dimensions from these wrappers — never majority votes or raw model
    judgments. The ledger binds campaign/protocol/sampling/packet identity,
    all three lane digests, the human-queue evidence digest, the frozen audit
    outcome, and the exact final sample membership. It is authoritative only
    after ``verify_consensus_ledger`` re-derives every row from protected
    evidence (see evals.calibration.ledger).
    """

    ledger_schema: Literal["engram-calibration-consensus-ledger-206-v1"] = CONSENSUS_LEDGER_SCHEMA
    protocol_version: str
    campaign_id: str
    sampling_manifest_digest: str
    source_packet_digest: str
    # FIX-R4-4: the exact frozen dev/holdout split the ledger was verified
    # against (verified against the INDEPENDENTLY retained campaign-authority
    # digest, never derived from a caller-supplied split). Required by the
    # fitting/floor front doors; optional only for pre-fitting campaign
    # inspection paths that never feed calibration.
    split_manifest_digest: str | None = None
    lane_digests: tuple[Digest, ...]  # exactly three, slot order
    queue_evidence_sha256: str
    audit_outcome: AuditOutcomeRecord
    audit_selection: AuditSelection
    audit_seed: str = AUDIT_SELECTION_SEED
    audit_rate: float = AUDIT_SAMPLE_RATE
    wrappers: tuple[ConsensusProvenanceWrapper, ...]

    @model_validator(mode="after")
    def ledger_contract(self) -> Self:
        if self.protocol_version != CONSENSUS_PROTOCOL_VERSION:
            raise ValueError("protocol_version_mismatch")
        if len(self.lane_digests) != len(REVIEWER_SLOTS):
            raise ValueError("lane_count_mismatch")
        if self.audit_seed != AUDIT_SELECTION_SEED:
            raise ValueError("audit_seed_frozen")
        if self.audit_rate != AUDIT_SAMPLE_RATE:
            raise ValueError("audit_rate_frozen")
        ids = [wrapper.sample_id for wrapper in self.wrappers]
        if len(set(ids)) != len(ids):
            raise ValueError("duplicate_ledger_sample_id")
        return self

    def final_dimensions_by_sample(self) -> dict[str, dict[str, Any]]:
        return {wrapper.sample_id: wrapper.final_dimensions for wrapper in self.wrappers}


class CorrelationReport(Record):
    """Public-safe aggregate correlation report (no tenant content)."""

    report_schema: Literal["engram-calibration-correlation-206-v1"] = CORRELATION_REPORT_SCHEMA
    protocol_version: str
    campaign_id: str
    sampling_manifest_digest: str
    source_packet_digest: str
    reviewer_identities: tuple[ReviewerIdentity, ...]
    lane_digests: tuple[Digest, ...]
    expected_cases: int
    completion_counts: dict[str, int]
    consensus_count: int
    disagreement_count: int
    uncertain_count: int
    malformed_error_refusal_count: int
    mandatory_high_consequence_count: int
    queue_reason_overlap: dict[str, int]
    human_queue_count_before_audit: int
    audit_count: int
    total_human_workload: int
    per_dimension_agreement: dict[str, dict[str, Any]]
    pairwise_agreement: dict[str, dict[str, dict[str, int]]]
    threeway_agreement: dict[str, int]
    aggregate_by_axis: dict[str, dict[str, dict[str, int]]]
    audit: dict[str, Any]

    @model_validator(mode="after")
    def report_contract(self) -> Self:
        if self.protocol_version != CONSENSUS_PROTOCOL_VERSION:
            raise ValueError("protocol_version_mismatch")
        if len(self.reviewer_identities) != len(REVIEWER_SLOTS):
            raise ValueError("reviewer_identity_count_mismatch")
        if len(self.lane_digests) != len(REVIEWER_SLOTS):
            raise ValueError("lane_digest_count_mismatch")
        return self


def aggregate_by_axis_counts(
    consensus_ids: Sequence[str],
    audit_selected: Sequence[str],
    frame_rows: Mapping[str, FrameRow],
) -> dict[str, dict[str, dict[str, int]]]:
    """Privacy-safe marginal aggregates for the frozen coverage axes.

    Aggregate counts only — no sample IDs, no tenant content, no judgment
    values.
    """
    audit_set = set(audit_selected)
    result: dict[str, dict[str, dict[str, int]]] = {}
    for axis in AUDIT_COVERAGE_AXES:
        cells: dict[str, dict[str, int]] = {}
        for sid in consensus_ids:
            row = frame_rows.get(sid)
            if row is None:
                continue  # membership validated elsewhere; never guess a value
            value = str(getattr(row, axis))
            cell = cells.setdefault(value, {"consensus": 0, "audit_selected": 0})
            cell["consensus"] += 1
            if sid in audit_set:
                cell["audit_selected"] += 1
        result[axis] = dict(sorted(cells.items()))
    return result


def build_correlation_report(
    *,
    campaign_id: str,
    lanes: Sequence[LaneFreeze],
    records_by_lane: Mapping[str, Mapping[str, ModelReviewRecord]],
    sampling: SamplingManifest,
    source_packet_digest: str,
    frame_rows: Mapping[str, FrameRow] | None = None,
    audit: Mapping[str, Any] | None = None,
) -> CorrelationReport:
    """Aggregate three frozen lanes into the public-safe correlation report.

    Must be called only after all three lanes are frozen. Produces counts and
    digests only — no tenant content, private IDs, reviewer rationales, or
    protected labels. ``malformed_error_refusal_count`` counts UNIQUE
    affected cases (a case with multiple failure reasons counts once); the
    per-reason overlap is reported separately in ``queue_reason_overlap``.
    ``aggregate_by_axis`` is populated from the protected frame when frame
    rows are supplied (required for the real campaign).
    """
    audit = audit or {}
    validate_lane_isolation(lanes)
    for lane in lanes:
        validate_lane_membership(lane, sampling, source_packet_digest=source_packet_digest)
    expected = len(sampling.sample_ids)
    completions: dict[str, int] = {}
    judgments_by_sample: dict[str, dict[str, ModelJudgment]] = {}
    for lane in lanes:
        slot = lane.reviewer.reviewer_slot
        lane_records = records_by_lane[slot]
        if set(lane_records) != set(lane.sample_ids):
            raise ValueError("lane_records_membership_mismatch")
        completions[slot] = sum(
            1
            for record in lane_records.values()
            if record.parse_status == "parsed" and record.outcome_status == "judged"
        )
        for sample_id, record in lane_records.items():
            if record.judgment is not None:
                judgments_by_sample.setdefault(sample_id, {})[slot] = record.judgment
    classifications = {
        sample_id: classify_case(
            {slot: records_by_lane[slot][sample_id] for slot in REVIEWER_SLOTS}
        )
        for sample_id in sampling.sample_ids
    }
    consensus_ids = [sid for sid, c in classifications.items() if c["consensus"]]
    queue_ids = [sid for sid, c in classifications.items() if c["escalation_reasons"]]
    reason_counts: dict[str, int] = {}
    overlap_pairs: dict[str, int] = {}
    failure_case_ids: set[str] = set()
    FAILURE_REASONS = {"malformed_review", "refused", "provider_error"}
    for sample_id, classification in classifications.items():
        reasons = classification["escalation_reasons"]
        for reason in reasons:
            reason_counts[reason] = reason_counts.get(reason, 0) + 1
        if any(reason in FAILURE_REASONS for reason in reasons):
            failure_case_ids.add(sample_id)
        ordered = sorted(reasons)
        for i, first in enumerate(ordered):
            for second in ordered[i + 1 :]:
                key = f"{first}+{second}"
                overlap_pairs[key] = overlap_pairs.get(key, 0) + 1
    pairwise: dict[str, dict[str, dict[str, int]]] = {}
    threeway: dict[str, int] = {}
    for field_name in CRITICAL_FIELDS:
        field_pairwise: dict[str, dict[str, int]] = {}
        for i, left in enumerate(REVIEWER_SLOTS):
            for right in REVIEWER_SLOTS[i + 1 :]:
                agree = disagree = absent = 0
                for sample_id in sampling.sample_ids:
                    left_j = judgments_by_sample.get(sample_id, {}).get(left)
                    right_j = judgments_by_sample.get(sample_id, {}).get(right)
                    if left_j is None or right_j is None:
                        absent += 1
                        continue
                    if left_j.fields.get(field_name) == right_j.fields.get(field_name):
                        agree += 1
                    else:
                        disagree += 1
                field_pairwise[f"{left}:{right}"] = {
                    "agree": agree,
                    "disagree": disagree,
                    "absent": absent,
                }
        pairwise[field_name] = field_pairwise
        three_agree = 0
        for sample_id in sampling.sample_ids:
            per_slot = [judgments_by_sample.get(sample_id, {}).get(slot) for slot in REVIEWER_SLOTS]
            if any(j is None for j in per_slot):
                continue
            values = {j.fields[field_name] for j in per_slot}  # type: ignore[union-attr]
            if len(values) == 1:
                three_agree += 1
        threeway[field_name] = three_agree
    if frame_rows is not None:
        selection = select_audit_sample_with_coverage(consensus_ids, frame_rows)
        audit_selected = selection.selected
        aggregate_axis = aggregate_by_axis_counts(consensus_ids, audit_selected, frame_rows)
    else:
        audit_selected = select_audit_sample(consensus_ids)
        aggregate_axis = {}
    audit_payload = {
        "audit_selection_seed": AUDIT_SELECTION_SEED,
        "audit_rate": AUDIT_SAMPLE_RATE,
        "audit_count": len(audit_selected),
        **{str(key): value for key, value in audit.items()},
    }
    audit_payload["audit_count"] = len(audit_selected)
    workload = len(set(queue_ids) | set(audit_selected))
    return CorrelationReport(
        protocol_version=CONSENSUS_PROTOCOL_VERSION,
        campaign_id=campaign_id,
        sampling_manifest_digest=sampling.manifest_digest(),
        source_packet_digest=source_packet_digest,
        reviewer_identities=tuple(lane.reviewer for lane in lanes),
        lane_digests=tuple(lane.lane_digest() for lane in lanes),
        expected_cases=expected,
        completion_counts=completions,
        consensus_count=len(consensus_ids),
        disagreement_count=reason_counts.get("critical_field_disagreement", 0),
        uncertain_count=reason_counts.get("uncertain_unknown_or_ambiguous_critical_value", 0),
        malformed_error_refusal_count=len(failure_case_ids),
        mandatory_high_consequence_count=reason_counts.get("high_consequence_signal", 0),
        queue_reason_overlap=dict(sorted(overlap_pairs.items())),
        human_queue_count_before_audit=len(set(queue_ids)),
        audit_count=len(audit_selected),
        total_human_workload=workload,
        per_dimension_agreement={
            field_name: {
                "pairwise_agree": {
                    pair: counts["agree"] for pair, counts in pairwise[field_name].items()
                },
                "threeway_agree": threeway[field_name],
                "expected": expected,
            }
            for field_name in CRITICAL_FIELDS
        },
        pairwise_agreement=pairwise,
        threeway_agreement=threeway,
        aggregate_by_axis=aggregate_axis,
        audit=audit_payload,
    )

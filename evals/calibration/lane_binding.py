"""Canonical record-to-lane binding validation (#206 FIX-1).

One validator, used at BOTH acceptance time (before a model-review record is
persisted) and freeze/load time (when a lane attests what its records contain).
Every ``ModelReviewRecord`` must match the lane authority EXACTLY on:

    protocol_version, campaign_id, sampling_manifest_digest,
    source_packet_digest, reviewer_slot, reviewer_family,
    provider_model_identifier, reviewer_config_digest, prompt_digest,
    label_guide_version

plus sample membership in the frozen sampling manifest, unique
``(slot, sample_id)``, and digest agreement with the actual current record
bytes. A lane can therefore never attest "these 402 records were Claude Opus
with config X" when the records identify anything else.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from evals.admission.schema import Digest
from evals.calibration.consensus import (
    CONSENSUS_PROTOCOL_VERSION,
    LaneFreeze,
    ModelReviewRecord,
    ReviewerIdentity,
)
from evals.calibration.freeze import LABEL_GUIDE_VERSION, SamplingManifest

# Exact-equality identity fields between a record and its lane authority.
RECORD_LANE_IDENTITY_FIELDS: tuple[str, ...] = (
    "protocol_version",
    "campaign_id",
    "sampling_manifest_digest",
    "source_packet_digest",
    "reviewer_slot",
    "reviewer_family",
    "provider_model_identifier",
    "reviewer_config_digest",
    "prompt_digest",
    "label_guide_version",
)


def _field_mismatches(record: ModelReviewRecord, expected: Mapping[str, object]) -> list[str]:
    mismatches: list[str] = []
    for field in RECORD_LANE_IDENTITY_FIELDS:
        if getattr(record, field) != expected[field]:
            mismatches.append(field)
    return mismatches


def _lane_authority(
    *,
    reviewer: ReviewerIdentity,
    campaign_id: str,
    sampling: SamplingManifest,
    source_packet_digest: str,
) -> dict[str, object]:
    return {
        "protocol_version": CONSENSUS_PROTOCOL_VERSION,
        "campaign_id": campaign_id,
        "sampling_manifest_digest": sampling.manifest_digest(),
        "source_packet_digest": source_packet_digest,
        "reviewer_slot": reviewer.reviewer_slot,
        "reviewer_family": reviewer.reviewer_family,
        "provider_model_identifier": reviewer.provider_model_identifier,
        "reviewer_config_digest": reviewer.reviewer_config_digest,
        "prompt_digest": reviewer.prompt_digest,
        "label_guide_version": LABEL_GUIDE_VERSION,
    }


def validate_record_lane_binding(
    record: ModelReviewRecord,
    *,
    reviewer: ReviewerIdentity,
    campaign_id: str,
    sampling: SamplingManifest,
    source_packet_digest: str,
) -> None:
    """Fail closed unless the record is bound to this exact lane authority.

    Raises ``ValueError`` naming the first identity field that disagrees
    (``record_lane_identity_mismatch:<fields>``), or a specific contract error
    for membership/digest violations.

    FIX-R4-1: the record's embedded ACTUAL execution receipt must compare
    exactly with the frozen lane authority (actual provider/model/config
    identity == frozen ``ReviewerIdentity``; the record's attested request
    binding == the receipt's). Actual executor identity is never taken from
    the lane configuration: a record whose receipt names another executor is
    rejected here even when its top-level fields were copied from the lane.
    """
    authority = _lane_authority(
        reviewer=reviewer,
        campaign_id=campaign_id,
        sampling=sampling,
        source_packet_digest=source_packet_digest,
    )
    mismatches = _field_mismatches(record, authority)
    if mismatches:
        raise ValueError("record_lane_identity_mismatch:" + ",".join(mismatches))
    if record.sample_id not in set(sampling.sample_ids):
        raise ValueError("record_sample_not_in_sampling_manifest")
    if record.reviewer_slot != reviewer.reviewer_slot:
        raise ValueError("record_slot_mismatch")  # defensive; covered by identity fields
    execution = record.execution
    if execution is None:
        raise ValueError("record_requires_execution_receipt")  # defensive; schema-enforced
    if not execution.matches_reviewer_identity(reviewer, campaign_id):
        raise ValueError("record_execution_receipt_identity_mismatch")
    if (
        record.request_generation != execution.request_generation
        or record.request_item_digest != execution.request_item_digest
    ):
        raise ValueError("record_request_binding_disagrees_with_receipt")


def validate_records_lane_binding(
    records: Mapping[str, ModelReviewRecord],
    *,
    reviewer: ReviewerIdentity,
    campaign_id: str,
    sampling: SamplingManifest,
    source_packet_digest: str,
) -> None:
    """Validate every record in a lane against the lane authority.

    Additionally proves each ``(slot, sample_id)`` appears at most once and
    that the record set membership is exactly the frozen sampling membership
    (used at freeze/load time).
    """
    if len(set(records)) != len(records):
        raise ValueError("lane_duplicate_accepted_record")
    for sample_id in sorted(records):
        validate_record_lane_binding(
            records[sample_id],
            reviewer=reviewer,
            campaign_id=campaign_id,
            sampling=sampling,
            source_packet_digest=source_packet_digest,
        )
    if set(records) != set(sampling.sample_ids):
        raise ValueError("lane_sample_membership_mismatch")


def validate_lane_record_digests(
    lane: LaneFreeze,
    records: Mapping[str, ModelReviewRecord],
) -> None:
    """Prove the digests attested by the lane match the CURRENT record objects.

    Catches a record mutated (or substituted) after the lane freeze: the frozen
    ``record_digests`` tuple is recomputed from the live records and compared
    position-by-position in frozen sample order.
    """
    if tuple(lane.sample_ids) != tuple(records.keys()) and set(records) != set(lane.sample_ids):
        raise ValueError("lane_record_membership_mismatch")
    for sample_id, attested in zip(lane.sample_ids, lane.record_digests, strict=True):
        record = records.get(sample_id)
        if record is None:
            raise ValueError("lane_record_missing")
        actual: Digest = record.record_digest()
        if actual != attested:
            raise ValueError("lane_record_digest_mismatch")


def validate_lane_provenance(
    lane: LaneFreeze,
    records: Mapping[str, ModelReviewRecord],
    *,
    campaign_id: str,
    sampling: SamplingManifest,
    source_packet_digest: str,
) -> None:
    """Full freeze/load-time validation: identity, membership, and digests.

    This is the single entry point used both when freezing a lane and when
    loading a previously frozen lane, so the two paths can never diverge.
    """
    if lane.campaign_id != campaign_id:
        raise ValueError("lane_campaign_mismatch")
    if lane.protocol_version != CONSENSUS_PROTOCOL_VERSION:
        raise ValueError("protocol_version_mismatch")
    if lane.sampling_manifest_digest != sampling.manifest_digest():
        raise ValueError("lane_sampling_manifest_mismatch")
    if lane.source_packet_digest != source_packet_digest:
        raise ValueError("lane_source_packet_mismatch")
    if tuple(lane.sample_ids) != tuple(sampling.sample_ids):
        raise ValueError("lane_sample_membership_mismatch")
    validate_records_lane_binding(
        records,
        reviewer=lane.reviewer,
        campaign_id=lane.campaign_id,
        sampling=sampling,
        source_packet_digest=source_packet_digest,
    )
    validate_lane_record_digests(lane, records)


def audit_campaign_binding(
    *,
    campaign_id: str,
    sampling: SamplingManifest,
    source_packet_digest: str,
    lanes: Sequence[LaneFreeze],
) -> None:
    """Audit hook (#206 additional audit): every lane binds the same campaign,
    sampling manifest, and source packet. Directory layout is never sufficient
    provenance."""
    for lane in lanes:
        if lane.campaign_id != campaign_id:
            raise ValueError("lane_campaign_mismatch")
        if lane.sampling_manifest_digest != sampling.manifest_digest():
            raise ValueError("lane_sampling_manifest_mismatch")
        if lane.source_packet_digest != source_packet_digest:
            raise ValueError("lane_source_packet_mismatch")

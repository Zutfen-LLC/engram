"""Neutral model-review packet and protected reviewer-lane persistence (#206).

The model-review input is a deterministic projection of the ALREADY-FROZEN
blind packet evidence (#202 Round-2): the exact same cases in the same order,
with no reviewer hints, no provider scores, no policy outputs, and no labels.
Each reviewer lane materializes under its own protected directory so no lane
can read another lane's evidence through the harness.

FIX-1: every accepted record and every frozen/loaded lane is validated by the
canonical ``evals.calibration.lane_binding`` validator — a lane can never
attest records that identify another slot, family, model version, config,
prompt, guide, campaign, sampling manifest, or source packet.

FIX-6: the practical lane execution/ingestion workflow lives in
``evals.calibration.ingestion`` (request emission, structured JSONL
ingestion, resume, status, raw-response preservation, exact-402 freeze gate).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from evals.calibration.consensus import (
    CONSENSUS_PROTOCOL_VERSION,
    REVIEWER_SLOTS,
    LaneFreeze,
    ModelReviewRecord,
    ReviewerIdentity,
    validate_lane_isolation,
)
from evals.calibration.freeze import LABEL_GUIDE_VERSION, SamplingManifest
from evals.calibration.lane_binding import (
    validate_lane_provenance,
    validate_record_lane_binding,
    validate_records_lane_binding,
)
from evals.calibration.review import (
    BlindPacket,
    _packet_file_payload,
    write_protected_file,
)

NEUTRAL_PACKET_SCHEMA: Literal["engram-calibration-model-packet-206-v1"] = (
    "engram-calibration-model-packet-206-v1"
)

# Case fields carried into the neutral model packet. This is exactly the
# blind-packet case view (decision-time evidence) minus nothing substantive:
# no scores, suggestions, policy outputs, labels, or reviewer hints exist in
# the blind packet to begin with, and the projection adds none.
_PROJECTED_CASE_FIELDS = (
    "sample_id",
    "content",
    "governed_kind",
    "source_type",
    "review_status",
    "assertion_mode",
    "origin",
    "risk",
    "evidence_state",
    "age_days",
    "age_bucket",
    "input_size_bucket",
)


class NeutralModelPacket(BlindPacket):
    """Neutral projection of the frozen blind packet for model reviewers.

    Inherits the blind-packet digest contract; the packet digest binds the
    identical case membership/order and the source blind packet digest.
    """

    packet_schema: str = NEUTRAL_PACKET_SCHEMA
    source_packet_digest: str

    @classmethod
    def from_blind(cls, blind: BlindPacket, *, protocol_version: str) -> NeutralModelPacket:
        if protocol_version != CONSENSUS_PROTOCOL_VERSION:
            raise ValueError("protocol_version_mismatch")
        projected = [
            {key: case[key] for key in _PROJECTED_CASE_FIELDS if key in case}
            for case in blind.cases
        ]
        return cls(
            packet_id=blind.packet_id,
            sampling_manifest_digest=blind.sampling_manifest_digest,
            guide_version=blind.guide_version,
            reviewer_hint="neutral_model_review",
            cases=projected,
            source_packet_digest=_packet_file_payload_digest(blind),
        )


def _packet_file_payload_digest(blind: BlindPacket) -> str:
    import hashlib

    return hashlib.sha256(_packet_file_payload(blind)).hexdigest()


def write_neutral_packet(packet: NeutralModelPacket, protected_dir: Path) -> dict[str, str]:
    """Publish the neutral packet plus its manifest into a protected directory."""
    payload = _packet_file_payload(packet)
    name = f"{packet.packet_id}.neutral.json"
    write_protected_file(protected_dir / name, payload)
    import hashlib

    manifest = {name: hashlib.sha256(payload).hexdigest()}
    write_protected_file(
        protected_dir / "neutral-packet-manifest.json",
        (json.dumps(manifest, sort_keys=True, indent=2) + "\n").encode(),
    )
    return manifest


def lane_directory(protected_root: Path, reviewer_slot: str) -> Path:
    if reviewer_slot not in REVIEWER_SLOTS:
        raise ValueError("unknown_reviewer_slot")
    return protected_root / "lanes" / reviewer_slot


def append_review_record(
    record: ModelReviewRecord,
    protected_root: Path,
    *,
    reviewer: ReviewerIdentity | None = None,
    campaign_id: str | None = None,
    sampling: SamplingManifest | None = None,
    source_packet_digest: str | None = None,
) -> Path:
    """Append one review record to its lane, exclusively (resume-safe).

    Refuses to duplicate an already-accepted record for the same sample in
    the same lane: each (slot, sample_id) has at most one accepted record.

    With the optional lane-authority arguments (the normal ingestion path),
    the record is bound to the exact ``ReviewerIdentity`` BEFORE persistence
    (FIX-1): any identity-field disagreement — slot, family, provider/model
    identifier, config digest, prompt digest, guide version, campaign,
    sampling manifest, or source packet — is rejected here, not at freeze
    time.
    """
    if reviewer is not None:
        if campaign_id is None or sampling is None or source_packet_digest is None:
            raise ValueError("lane_authority_args_required_together")
        validate_record_lane_binding(
            record,
            reviewer=reviewer,
            campaign_id=campaign_id,
            sampling=sampling,
            source_packet_digest=source_packet_digest,
        )
        if record.reviewer_slot != reviewer.reviewer_slot:
            raise ValueError("record_slot_mismatch")
    lane_dir = lane_directory(protected_root, record.reviewer_slot)
    path = lane_dir / f"{record.sample_id}.json"
    if path.exists():
        raise ValueError("review_record_already_accepted")
    payload = (json.dumps(record.model_dump(mode="json"), sort_keys=True) + "\n").encode()
    write_protected_file(path, payload)
    return path


def load_lane_records(protected_root: Path, reviewer_slot: str) -> dict[str, ModelReviewRecord]:
    """Load the accepted records for one lane (interrupt/resume support)."""
    lane_dir = lane_directory(protected_root, reviewer_slot)
    records: dict[str, ModelReviewRecord] = {}
    if not lane_dir.exists():
        return records
    for path in sorted(lane_dir.glob("*.json")):
        if path.name in {"lane-freeze.json", "lane.json"}:
            continue
        record = ModelReviewRecord.model_validate(json.loads(path.read_text()))
        if record.sample_id in records:
            raise ValueError("lane_duplicate_accepted_record")
        records[record.sample_id] = record
    return records


def freeze_lane(
    *,
    protected_root: Path,
    reviewer: ReviewerIdentity,
    campaign_id: str,
    sampling: SamplingManifest,
    source_packet_digest: str,
) -> LaneFreeze:
    """Freeze one completed lane after exact membership is proven.

    FIX-1: every record is validated against the exact reviewer identity and
    campaign bindings via the canonical lane-binding validator BEFORE the
    attestation is constructed, so the frozen digest list provably describes
    records produced under this exact reviewer/lane authority.
    """
    records = load_lane_records(protected_root, reviewer.reviewer_slot)
    if set(records) != set(sampling.sample_ids):
        raise ValueError("lane_sample_membership_mismatch")
    validate_records_lane_binding(
        records,
        reviewer=reviewer,
        campaign_id=campaign_id,
        sampling=sampling,
        source_packet_digest=source_packet_digest,
    )
    ordered_ids = tuple(sampling.sample_ids)
    lane = LaneFreeze(
        protocol_version=CONSENSUS_PROTOCOL_VERSION,
        campaign_id=campaign_id,
        reviewer=reviewer,
        sampling_manifest_digest=sampling.manifest_digest(),
        source_packet_digest=source_packet_digest,
        sample_ids=ordered_ids,
        record_digests=tuple(records[sid].record_digest() for sid in ordered_ids),
    )
    write_protected_file(
        lane_directory(protected_root, reviewer.reviewer_slot) / "lane-freeze.json",
        (json.dumps(lane.model_dump(mode="json"), sort_keys=True) + "\n").encode(),
    )
    return lane


def _load_one_frozen_lane(
    protected_root: Path,
    reviewer: ReviewerIdentity,
    campaign_id: str,
    sampling: SamplingManifest,
    source_packet_digest: str,
) -> LaneFreeze:
    path = lane_directory(protected_root, reviewer.reviewer_slot) / "lane-freeze.json"
    if not path.exists():
        raise ValueError("lane_not_frozen")
    lane = LaneFreeze.model_validate(json.loads(path.read_text()))
    if lane.reviewer != reviewer:
        raise ValueError("lane_reviewer_identity_mismatch")
    records = load_lane_records(protected_root, reviewer.reviewer_slot)
    # Full FIX-1 validation: identity binding + membership + live digests.
    validate_lane_provenance(
        lane,
        records,
        campaign_id=campaign_id,
        sampling=sampling,
        source_packet_digest=source_packet_digest,
    )
    return lane


def load_frozen_lanes(
    protected_root: Path,
    *,
    campaign_id: str,
    sampling: SamplingManifest,
    source_packet_digest: str,
    reviewers: dict[str, ReviewerIdentity],
) -> tuple[LaneFreeze, ...]:
    """Load and validate all three previously-frozen lanes."""
    if set(reviewers) != set(REVIEWER_SLOTS):
        raise ValueError("reviewer_identity_required_for_every_slot")
    lanes = tuple(
        _load_one_frozen_lane(
            protected_root,
            reviewers[slot],
            campaign_id,
            sampling,
            source_packet_digest,
        )
        for slot in REVIEWER_SLOTS
    )
    validate_lane_isolation(lanes)
    return lanes


def records_by_lane_from_files(
    protected_root: Path,
) -> dict[str, dict[str, ModelReviewRecord]]:
    return {slot: load_lane_records(protected_root, slot) for slot in REVIEWER_SLOTS}


def label_guide_version() -> str:
    return LABEL_GUIDE_VERSION

"""Canonical raw-model-evidence verification (#206 FIX-R2-4).

A lane may only attest raw model evidence that actually exists and hashes to
the digest each accepted record claims:

- ``judged`` / ``refused`` / ``malformed`` records: the raw response file MUST
  exist in the lane's protected ``raw/`` directory and
  ``SHA256(raw bytes) == record.raw_response_digest``;
- ``provider_error`` records: ``record.raw_response_digest`` MUST be ``None``
  and NO raw-response artifact may be claimed as evidence for that sample.

The validator is part of the lane provenance/freeze/load/ledger authority
path: ``validate_lane_provenance_with_raw`` (used at freeze, load, AND final
ledger verification) fails closed if any accepted record lacks its bound raw
bytes, or if the bytes were lost, replaced, or mutated after ingestion.

Ingestion order (``LaneSession.ingest_response``) is interruption-safe: the
raw bytes are written EXCLUSIVELY BEFORE the accepted record is published, so
a crash can leave at most an orphan raw file without an accepted record —
never an accepted record without its evidence. Orphan raw files never count
as completed reviews (they carry no record).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from pathlib import Path

from evals.calibration.consensus import LaneFreeze, ModelReviewRecord


def raw_response_path(lane_root: Path, sample_id: str) -> Path:
    return lane_root / "raw" / f"{sample_id}.resp"


def lane_root_for(protected_root: Path, reviewer_slot: str) -> Path:
    return protected_root / "lanes" / reviewer_slot


def validate_record_raw_evidence(
    record: ModelReviewRecord,
    *,
    lane_root: Path,
) -> None:
    """Fail closed unless the record's raw-evidence claim is backed by bytes.

    - judged/refused/malformed: file exists and SHA256(bytes) == digest;
    - provider_error: digest is None and no raw artifact exists for the sample
      (an existing ``.resp`` file for a provider_error record means evidence
      is claimed that the record says does not exist — rejected).

    FIX-R4-2: for judged records the preserved bytes are re-parsed with the
    frozen deterministic parser and the derived judgment must equal the
    stored one — the record's classification can never disagree with its own
    preserved output bytes.
    """
    path = raw_response_path(lane_root, record.sample_id)
    if record.outcome_status == "provider_error":
        if record.raw_response_digest is not None:
            raise ValueError("provider_error_must_not_claim_raw_response_digest")
        if path.exists():
            raise ValueError("provider_error_must_not_have_raw_response_artifact")
        return
    if record.raw_response_digest is None:
        raise ValueError(f"record_raw_evidence_missing_digest:{record.sample_id}")
    if not path.is_file():
        raise ValueError(f"record_raw_evidence_file_missing:{record.sample_id}")
    payload = path.read_bytes()
    actual = hashlib.sha256(payload).hexdigest()
    if actual != record.raw_response_digest:
        raise ValueError(f"record_raw_evidence_digest_mismatch:{record.sample_id}")
    if record.parse_status == "parsed":
        from evals.calibration.reviewer_instructions import parse_model_response

        try:
            parsed = parse_model_response(payload, expected_sample_id=record.sample_id)
        except ValueError:
            raise ValueError(f"record_raw_evidence_sample_mismatch:{record.sample_id}") from None
        if parsed.classification != "judged" or parsed.judgment is None:
            raise ValueError(f"record_judgment_not_derivable_from_bytes:{record.sample_id}")
        if parsed.judgment != record.judgment:
            raise ValueError(f"record_judgment_disagrees_with_bytes:{record.sample_id}")
    elif record.outcome_status == "refused":
        from evals.calibration.reviewer_instructions import parse_model_response

        try:
            parsed = parse_model_response(payload, expected_sample_id=record.sample_id)
        except ValueError:
            raise ValueError(f"record_raw_evidence_sample_mismatch:{record.sample_id}") from None
        if parsed.classification != "refused":
            raise ValueError(f"record_refusal_not_derivable_from_bytes:{record.sample_id}")


def validate_lane_raw_evidence(
    records: Mapping[str, ModelReviewRecord],
    *,
    protected_root: Path,
    reviewer_slot: str,
) -> None:
    """Validate raw evidence for every accepted record in one lane."""
    lane_root = lane_root_for(protected_root, reviewer_slot)
    for sample_id in sorted(records):
        validate_record_raw_evidence(records[sample_id], lane_root=lane_root)


def load_frozen_lane_slot(lane_root: Path) -> str | None:
    """Read the reviewer slot from the lane authority file, if present."""
    authority_path = lane_root / "lane.json"
    if not authority_path.is_file():
        return None
    payload = json.loads(authority_path.read_text())
    reviewer = payload.get("reviewer") or {}
    slot = reviewer.get("reviewer_slot")
    return str(slot) if slot else None


def lane_has_raw_dir(protected_root: Path, reviewer_slot: str) -> bool:
    return (lane_root_for(protected_root, reviewer_slot) / "raw").is_dir()


def raw_files_in_lane(protected_root: Path, reviewer_slot: str) -> set[str]:
    """Sample IDs that have a raw-response artifact in this lane."""
    raw_dir = lane_root_for(protected_root, reviewer_slot) / "raw"
    if not raw_dir.is_dir():
        return set()
    return {path.stem for path in raw_dir.glob("*.resp")}


def orphan_raw_files(
    records: Mapping[str, ModelReviewRecord],
    *,
    protected_root: Path,
    reviewer_slot: str,
) -> set[str]:
    """Raw files with no accepted record (crash orphans).

    Orphans are tolerated (resume-safe) but NEVER count as completed reviews:
    only accepted records do. They must never be silently overwritten.
    """
    return raw_files_in_lane(protected_root, reviewer_slot) - set(records)


def validate_lane_provenance_with_raw(
    lane: LaneFreeze,
    records: Mapping[str, ModelReviewRecord],
    *,
    campaign_id: str,
    sampling: object,
    source_packet_digest: str,
    protected_root: Path,
) -> None:
    """Full lane provenance PLUS raw-evidence verification (FIX-R2-4).

    Wraps ``validate_lane_provenance`` (identity, membership, frozen record
    digests) and additionally proves every accepted record's raw response
    bytes exist and hash to the claimed digest (or are correctly absent for
    provider_error). This is the authority path at freeze, load, and final
    ledger verification.
    """
    from evals.calibration.lane_binding import validate_lane_provenance

    validate_lane_provenance(
        lane,
        records,
        campaign_id=campaign_id,
        sampling=sampling,  # type: ignore[arg-type]
        source_packet_digest=source_packet_digest,
    )
    validate_lane_raw_evidence(
        records,
        protected_root=protected_root,
        reviewer_slot=lane.reviewer.reviewer_slot,
    )

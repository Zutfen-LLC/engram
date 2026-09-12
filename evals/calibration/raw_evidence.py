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
    stored one.

    FIX-R6-3: ALL completed outcome states are re-derived from the exact
    raw bytes — judged, refused, AND malformed. The parser classification
    must map exactly onto the stored parse/outcome statuses, the stored
    judgment/confidence (judged), the stored refusal error code, and the
    canonical malformed error code. A stored ``malformed`` record whose
    bytes actually parse as a judgment or refusal fails closed; ``malformed``
    is never an unchecked catch-all. The record's execution status must
    also agree with the raw-response state (executor_status == completed
    requires response bytes; provider_error requires none).
    """
    path = raw_response_path(lane_root, record.sample_id)
    if record.outcome_status == "provider_error":
        if record.raw_response_digest is not None:
            raise ValueError("provider_error_must_not_claim_raw_response_digest")
        if path.exists():
            raise ValueError("provider_error_must_not_have_raw_response_artifact")
        if record.execution is not None and record.execution.executor_status != "provider_error":
            raise ValueError(f"record_executor_status_disagrees_with_raw_state:{record.sample_id}")
        return
    # A response-carrying outcome requires completed execution evidence.
    if record.execution is not None and record.execution.executor_status != "completed":
        raise ValueError(f"record_executor_status_disagrees_with_raw_state:{record.sample_id}")
    if record.raw_response_digest is None:
        raise ValueError(f"record_raw_evidence_missing_digest:{record.sample_id}")
    if not path.is_file():
        raise ValueError(f"record_raw_evidence_file_missing:{record.sample_id}")
    payload = path.read_bytes()
    actual = hashlib.sha256(payload).hexdigest()
    if actual != record.raw_response_digest:
        raise ValueError(f"record_raw_evidence_digest_mismatch:{record.sample_id}")
    from evals.calibration.provider_metadata import CANONICAL_MALFORMED_ERROR_CODE
    from evals.calibration.reviewer_instructions import parse_model_response

    try:
        parsed = parse_model_response(payload, expected_sample_id=record.sample_id)
    except ValueError:
        raise ValueError(f"record_raw_evidence_sample_mismatch:{record.sample_id}") from None
    if parsed.classification == "judged":
        if record.parse_status != "parsed" or record.outcome_status != "judged":
            raise ValueError(f"record_outcome_not_derived_from_bytes:{record.sample_id}")
        if parsed.judgment is None or parsed.judgment != record.judgment:
            raise ValueError(f"record_judgment_disagrees_with_bytes:{record.sample_id}")
        if record.reviewer_confidence != parsed.judgment.reviewer_confidence:
            raise ValueError(f"record_confidence_disagrees_with_bytes:{record.sample_id}")
        if record.error_code is not None:
            raise ValueError(f"record_judged_must_not_carry_error_code:{record.sample_id}")
    elif parsed.classification == "refused":
        if record.parse_status != "malformed" or record.outcome_status != "refused":
            raise ValueError(f"record_outcome_not_derived_from_bytes:{record.sample_id}")
        if record.judgment is not None:
            raise ValueError(f"record_refusal_must_not_carry_judgment:{record.sample_id}")
        if record.error_code != parsed.error_code:
            raise ValueError(f"record_refusal_error_code_disagrees_with_bytes:{record.sample_id}")
    else:  # malformed
        if record.parse_status != "malformed" or record.outcome_status != "malformed":
            raise ValueError(f"record_outcome_not_derived_from_bytes:{record.sample_id}")
        if record.judgment is not None:
            raise ValueError(f"record_malformed_must_not_carry_judgment:{record.sample_id}")
        if record.error_code != CANONICAL_MALFORMED_ERROR_CODE:
            raise ValueError(f"record_malformed_error_code_not_canonical:{record.sample_id}")


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

    FIX-R5-3: request provenance is folded into this one composed validator:
    the retained request-batch BYTES are re-verified (canonical
    ``verify_request_batch``) and every accepted record's request binding is
    re-derived from them, so deleting/mutating the request evidence after
    lane freeze fails every downstream boundary that uses this path (final
    ``verify_consensus_ledger`` included). FIX-R5-1: only machine-verified
    actual executor identity may back a consensus lane at this boundary.
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
    from evals.calibration.ingestion import (
        require_machine_verified_execution_identity,
        verify_lane_request_bindings,
    )

    verify_lane_request_bindings(
        lane_root_for(protected_root, lane.reviewer.reviewer_slot),
        lane.reviewer,
        records,
        campaign_id=campaign_id,
        neutral_packet_sha256=lane.neutral_packet_sha256,
    )
    from evals.calibration.ingestion import lane_provenance_mode

    mode = lane_provenance_mode(lane_root_for(protected_root, lane.reviewer.reviewer_slot))
    if mode == "operator_attested_subscription_ui":
        # #209 opt-in: the operator-attested subscription gate replaces the
        # machine-verified identity gate at this final boundary too; every
        # other check above is unchanged.
        from evals.calibration.subscription_ui import require_subscription_attested_identity

        require_subscription_attested_identity(
            records,
            lane_root=lane_root_for(protected_root, lane.reviewer.reviewer_slot),
            protected_root=protected_root,
        )
    elif mode == "machine_executor_provenance":
        from evals.calibration.ingestion import require_machine_executor_provenance

        require_machine_executor_provenance(
            records, lane_root=lane_root_for(protected_root, lane.reviewer.reviewer_slot)
        )
    else:
        require_machine_verified_execution_identity(
            records, lane_root=lane_root_for(protected_root, lane.reviewer.reviewer_slot)
        )

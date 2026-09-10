"""Practical provider-agnostic lane execution/ingestion workflow (#206 FIX-6).

The exact model invocation stays an EXTERNAL responsibility (Hermes executes
the frontier reviewers); this module is the complete mechanical handoff:

    model-lane-init      bind one lane to one frozen ReviewerIdentity
    model-lane-request   emit ONLY that lane's neutral cases + frozen
                         labeling instructions/schema (JSONL, resumable)
    (external model execution happens here — no credentials in this module)
    model-lane-ingest    mechanically ingest structured per-case or batch
                         (JSONL) responses into validated ModelReviewRecords
    model-lane-status    completion / missing / failure counts
    freeze-model-lane    freeze only after exact full membership (402)

Guarantees:

- one lane is bound to exactly one frozen ``ReviewerIdentity``; the binding
  file is exclusive-create and immutable;
- requests contain only the lane's neutral case input plus the frozen
  labeling instructions and response schema — no other lane's evidence;
- raw model response bytes are preserved in protected storage and their
  sha256 digest is recorded on the record (when a response exists);
- refusal / malformed / provider_error stay distinct orthogonal states
  (``absent`` parse status for missing responses);
- ingestion is append-only and resume-safe: previously accepted records are
  never replaced, duplicates are refused, and requests resume from the next
  missing sample;
- every accepted record is bound to the lane authority at ingest time
  (FIX-1), so another lane's output cannot be ingested here.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Self

from pydantic import model_validator

from evals.admission.schema import Record
from evals.calibration.consensus import (
    CONSENSUS_PROTOCOL_VERSION,
    CRITICAL_FIELDS,
    LaneFreeze,
    ModelJudgment,
    ModelReviewRecord,
    ReviewerIdentity,
)
from evals.calibration.freeze import LABEL_GUIDE_VERSION, SamplingManifest
from evals.calibration.model_lanes import (
    NeutralModelPacket,
    append_review_record,
    freeze_lane,
    load_lane_records,
)
from evals.calibration.review import write_protected_file

LANE_AUTHORITY_SCHEMA: Literal["engram-calibration-model-lane-authority-206-v1"] = (
    "engram-calibration-model-lane-authority-206-v1"
)
LANE_REQUEST_SCHEMA: Literal["engram-calibration-model-lane-request-206-v1"] = (
    "engram-calibration-model-lane-request-206-v1"
)
RESPONSE_OUTCOMES: tuple[str, ...] = ("judged", "refused", "malformed", "provider_error")

# The frozen labeling instruction block emitted with every request. The
# schema mirrors ``ModelJudgment``: exactly the five critical fields plus
# optional diagnostic fields and the reviewer's own confidence.
LABELING_INSTRUCTIONS: dict[str, Any] = {
    "task": (
        "For each case, judge the memory item on the five critical fields "
        "(expected_kind, retention_value, epistemic_state, consequence, "
        "acceptable_abstention) using the frozen label guide semantics."
    ),
    "label_guide_version": LABEL_GUIDE_VERSION,
    "critical_fields": list(CRITICAL_FIELDS),
    "required_response_shape": {
        "sample_id": "string (echo the request sample_id)",
        "outcome": "judged | refused | malformed | provider_error",
        "judgment": {
            "fields": "object: exactly the five critical fields (+ optional diagnostics)",
            "reviewer_confidence": "low | medium | high",
        },
        "raw_response": "string: the model's own full output text (omit when absent)",
        "error_code": "short token when outcome != judged",
    },
}


class LaneAuthority(Record):
    """The immutable lane binding written by ``model-lane-init``."""

    authority_schema: Literal["engram-calibration-model-lane-authority-206-v1"] = (
        LANE_AUTHORITY_SCHEMA
    )
    protocol_version: str
    campaign_id: str
    reviewer: ReviewerIdentity
    sampling_manifest_digest: str
    source_packet_digest: str

    @model_validator(mode="after")
    def authority_contract(self) -> Self:
        if self.protocol_version != CONSENSUS_PROTOCOL_VERSION:
            raise ValueError("protocol_version_mismatch")
        return self


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


class LaneSession:
    """One reviewer lane bound to its frozen authority.

    Directory layout mirrors ``model_lanes``: the campaign protected root
    contains ``lanes/<slot>/`` record files; the authority file lives at
    ``lanes/<slot>/lane.json``. All operations derive the reviewer identity
    from the immutable authority — a record can never be persisted under a
    different identity through this session.
    """

    def __init__(self, protected_root: Path, reviewer_slot: str):
        self.protected_root = protected_root
        self.reviewer_slot = reviewer_slot
        authority_path = self.lane_root / "lane.json"
        if not authority_path.exists():
            raise ValueError("lane_not_initialized")
        self.authority = LaneAuthority.model_validate(json.loads(authority_path.read_text()))
        if self.authority.reviewer.reviewer_slot != reviewer_slot:
            raise ValueError("lane_authority_slot_mismatch")
        self.reviewer = self.authority.reviewer
        self.campaign_id = self.authority.campaign_id
        self.sampling_manifest_digest = self.authority.sampling_manifest_digest
        self.source_packet_digest = self.authority.source_packet_digest

    @property
    def lane_root(self) -> Path:
        return self.protected_root / "lanes" / self.reviewer_slot

    @classmethod
    def init(
        cls,
        protected_root: Path,
        *,
        reviewer: ReviewerIdentity,
        campaign_id: str,
        sampling: SamplingManifest,
        source_packet_digest: str,
    ) -> LaneSession:
        """Bind one lane to one frozen reviewer identity (exclusive-create)."""
        authority = LaneAuthority(
            protocol_version=CONSENSUS_PROTOCOL_VERSION,
            campaign_id=campaign_id,
            reviewer=reviewer,
            sampling_manifest_digest=sampling.manifest_digest(),
            source_packet_digest=source_packet_digest,
        )
        payload = (json.dumps(authority.model_dump(mode="json"), sort_keys=True) + "\n").encode()
        lane_path = protected_root / "lanes" / reviewer.reviewer_slot / "lane.json"
        write_protected_file(lane_path, payload)
        return cls(protected_root, reviewer.reviewer_slot)

    # -- request emission ---------------------------------------------------

    def _check_packet(self, packet: NeutralModelPacket, sampling: SamplingManifest) -> None:
        if sampling.manifest_digest() != self.sampling_manifest_digest:
            raise ValueError("sampling_manifest_mismatch")
        if packet.sampling_manifest_digest != self.sampling_manifest_digest:
            raise ValueError("lane_packet_sampling_manifest_mismatch")
        if str(getattr(packet, "source_packet_digest", "")) != self.source_packet_digest:
            raise ValueError("lane_packet_source_packet_mismatch")
        if packet.guide_version != LABEL_GUIDE_VERSION:
            raise ValueError("lane_packet_guide_version_mismatch")

    def emit_requests(
        self,
        packet: NeutralModelPacket,
        *,
        sampling: SamplingManifest,
        out_path: Path | None = None,
    ) -> Path:
        """Emit ONLY this lane's neutral case requests (JSONL, resumable).

        One line per case still missing an accepted record. Each line is
        self-describing: lane identity + case + frozen labeling instructions
        + response schema. The external executor answers each line with one
        response object of the documented shape. No other lane's evidence is
        reachable through this emission.
        """
        self._check_packet(packet, sampling)
        accepted = load_lane_records(self.protected_root, self.reviewer.reviewer_slot)
        lines: list[str] = []
        for index, case in enumerate(packet.cases):
            sample_id = case["sample_id"]
            if sample_id in accepted:
                continue  # resume: never re-request accepted evidence
            request = {
                "lane_request_schema": LANE_REQUEST_SCHEMA,
                "protocol_version": CONSENSUS_PROTOCOL_VERSION,
                "campaign_id": self.campaign_id,
                "sampling_manifest_digest": self.sampling_manifest_digest,
                "source_packet_digest": self.source_packet_digest,
                "reviewer_slot": self.reviewer.reviewer_slot,
                "reviewer_family": self.reviewer.reviewer_family,
                "provider_model_identifier": self.reviewer.provider_model_identifier,
                "reviewer_config_digest": self.reviewer.reviewer_config_digest,
                "prompt_digest": self.reviewer.prompt_digest,
                "label_guide_version": LABEL_GUIDE_VERSION,
                "case_index": index,
                "sample_id": sample_id,
                "case": case,
                "labeling_instructions": LABELING_INSTRUCTIONS,
            }
            lines.append(json.dumps(request, sort_keys=True))
        out_path = out_path or (self.lane_root / f"lane-requests-{self.reviewer_slot}.jsonl")
        write_protected_file(out_path, ("\n".join(lines) + "\n").encode() if lines else b"")
        return out_path

    # -- record construction (single path) -----------------------------------

    def build_record(
        self,
        response: Mapping[str, Any],
        *,
        sampling: SamplingManifest,
    ) -> ModelReviewRecord:
        """Construct a ``ModelReviewRecord`` from one structured response.

        The reviewer identity on the record comes from the LANE AUTHORITY,
        never from the response — another lane's output cannot be ingested
        through this path even if its payload names a different slot/family.
        """
        if sampling.manifest_digest() != self.sampling_manifest_digest:
            raise ValueError("sampling_manifest_mismatch")
        sample_id = str(response.get("sample_id", ""))
        if not sample_id:
            raise ValueError("response_requires_sample_id")
        if sample_id not in set(sampling.sample_ids):
            raise ValueError("record_sample_not_in_sampling_manifest")
        outcome = str(response.get("outcome", ""))
        if outcome not in RESPONSE_OUTCOMES:
            raise ValueError("response_outcome_out_of_vocabulary")
        raw_response = response.get("raw_response")
        if raw_response is not None and not isinstance(raw_response, str):
            raise ValueError("raw_response_must_be_string")
        captured_at = response.get("captured_at") or datetime.now(UTC).isoformat()
        common: dict[str, Any] = {
            "protocol_version": CONSENSUS_PROTOCOL_VERSION,
            "campaign_id": self.campaign_id,
            "sampling_manifest_digest": self.sampling_manifest_digest,
            "source_packet_digest": self.source_packet_digest,
            "sample_id": sample_id,
            "reviewer_slot": self.reviewer.reviewer_slot,
            "reviewer_family": self.reviewer.reviewer_family,
            "provider_model_identifier": self.reviewer.provider_model_identifier,
            "reviewer_config_digest": self.reviewer.reviewer_config_digest,
            "prompt_digest": self.reviewer.prompt_digest,
            "label_guide_version": LABEL_GUIDE_VERSION,
            "captured_at": captured_at,
        }
        if outcome == "judged":
            judgment_payload = response.get("judgment")
            if not isinstance(judgment_payload, dict):
                raise ValueError("judged_response_requires_judgment")
            judgment = ModelJudgment.model_validate(judgment_payload)
            if not raw_response:
                raise ValueError("judged_response_requires_raw_response")
            return ModelReviewRecord(
                **common,
                parse_status="parsed",
                outcome_status="judged",
                reviewer_confidence=judgment.reviewer_confidence,
                judgment=judgment,
                raw_response_digest=_sha256_text(raw_response),
                error_code=None,
            )
        if outcome in ("refused", "malformed"):
            if not raw_response:
                raise ValueError("response_received_requires_raw_response")
            error_code = response.get("error_code")
            if not error_code:
                raise ValueError("failed_review_requires_error_code")
            return ModelReviewRecord(
                **common,
                parse_status="malformed",
                outcome_status=outcome,  # type: ignore[arg-type]
                reviewer_confidence="unknown",
                judgment=None,
                raw_response_digest=_sha256_text(raw_response),
                error_code=str(error_code),
            )
        # provider_error: no response bytes exist
        if raw_response:
            raise ValueError("provider_error_without_response_must_not_carry_raw_response")
        error_code = response.get("error_code")
        if not error_code:
            raise ValueError("failed_review_requires_error_code")
        return ModelReviewRecord(
            **common,
            parse_status="absent",
            outcome_status="provider_error",
            reviewer_confidence="unknown",
            judgment=None,
            raw_response_digest=None,
            error_code=str(error_code),
        )

    # -- ingestion ------------------------------------------------------------

    def ingest_response(
        self,
        response: Mapping[str, Any],
        *,
        sampling: SamplingManifest,
    ) -> ModelReviewRecord:
        """Validate + append ONE structured response (fail closed, FIX-1 bound).

        FIX-R2-4 interruption-safe ordering: the raw response bytes are
        written EXCLUSIVELY FIRST (never overwriting existing evidence), the
        record is constructed/validated against that exact digest, and only
        then is the accepted record published. A crash can therefore leave at
        most an orphan raw file without an accepted record — never an
        accepted record lacking its bound raw bytes. Re-ingesting the same
        sample after such a crash reuses the identical raw bytes
        (byte-identical rewrite is refused, not silently overwritten).
        """
        record = self.build_record(response, sampling=sampling)
        raw_response = response.get("raw_response")
        if raw_response:
            raw_path = self.lane_root / "raw" / f"{record.sample_id}.resp"
            payload = raw_response.encode()
            if raw_path.exists():
                # Never silently overwrite raw model evidence: the orphan
                # must hash identically to what this response claims.
                if hashlib.sha256(raw_path.read_bytes()).hexdigest() != (
                    record.raw_response_digest
                ):
                    raise ValueError("raw_response_orphan_digest_conflict")
            else:
                write_protected_file(raw_path, payload)
        append_review_record(
            record,
            self.protected_root,
            reviewer=self.reviewer,
            campaign_id=self.campaign_id,
            sampling=sampling,
            source_packet_digest=self.source_packet_digest,
        )
        return record

    def ingest_jsonl(
        self,
        jsonl_path: Path,
        *,
        sampling: SamplingManifest,
    ) -> dict[str, Any]:
        """Ingest a batch JSONL of response objects (one per line).

        Returns per-outcome accepted counts, refused duplicates, and errors
        with line numbers. Accepted lines persist exclusively; a failed line
        never rolls back previously accepted evidence (resume-safe).
        """
        if sampling.manifest_digest() != self.sampling_manifest_digest:
            raise ValueError("sampling_manifest_mismatch")
        accepted = {"judged": 0, "refused": 0, "malformed": 0, "provider_error": 0}
        duplicates = 0
        errors: list[dict[str, Any]] = []
        for line_number, line in enumerate(jsonl_path.read_text().splitlines(), start=1):
            if not line.strip():
                continue
            try:
                payload = json.loads(line)
                if not isinstance(payload, dict):
                    raise ValueError("response_line_must_be_object")
                record = self.ingest_response(payload, sampling=sampling)
            except ValueError as exc:
                message = str(exc)
                if "review_record_already_accepted" in message:
                    duplicates += 1
                else:
                    errors.append({"line": line_number, "error": message})
                continue
            accepted[record.outcome_status] += 1
        return {
            "accepted": accepted,
            "accepted_total": sum(accepted.values()),
            "duplicates_refused": duplicates,
            "errors": errors,
        }

    # -- status / freeze -------------------------------------------------------

    def status(self, sampling: SamplingManifest) -> dict[str, Any]:
        """Completion / missing / failure counts for this lane."""
        if sampling.manifest_digest() != self.sampling_manifest_digest:
            raise ValueError("sampling_manifest_mismatch")
        records = load_lane_records(self.protected_root, self.reviewer.reviewer_slot)
        expected = list(sampling.sample_ids)
        missing = [sid for sid in expected if sid not in records]
        counts = {"judged": 0, "refused": 0, "malformed": 0, "provider_error": 0}
        for record in records.values():
            counts[record.outcome_status] += 1
        return {
            "reviewer_slot": self.reviewer.reviewer_slot,
            "expected": len(expected),
            "accepted": len(records),
            "missing": len(missing),
            "next_missing": missing[:50],
            "outcome_counts": counts,
            "complete": not missing,
        }

    def freeze(self, sampling: SamplingManifest) -> LaneFreeze:
        """Freeze the lane; requires exact full frozen membership."""
        return freeze_lane(
            protected_root=self.protected_root,
            reviewer=self.reviewer,
            campaign_id=self.campaign_id,
            sampling=sampling,
            source_packet_digest=self.source_packet_digest,
        )

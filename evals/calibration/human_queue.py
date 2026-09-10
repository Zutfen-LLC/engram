"""Minimal protected human-adjudication queue workflow for #206 escalated cases.

Scope is deliberately narrow (per the issue: no general Engram product UI):

- the queue contains ONLY escalated/audit-selected cases, never the full 402;
- the human sees the original blind case evidence first (via the existing
  frozen #202 packets — nothing new is shown);
- the human's independent initial judgment is captured and frozen BEFORE the
  three model votes are revealed for disagreement adjudication, and both are
  preserved separately;
- judgments validate against the same frozen critical vocabulary;
- state is append-only per sample and resumable (autosave = one protected
  file per sample, exclusive-create);
- export is mechanically ingestible protected evidence.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Self

from pydantic import AwareDatetime, Field, model_validator

from evals.admission.schema import Record, Token
from evals.calibration.consensus import (
    CONSENSUS_PROTOCOL_VERSION,
    CRITICAL_FIELDS,
    REVIEWER_SLOTS,
    CriticalFieldVocabulary,
    ModelReviewRecord,
    classify_case,
    select_audit_sample,
)
from evals.calibration.freeze import SamplingManifest
from evals.calibration.review import write_protected_file

QUEUE_SCHEMA: Literal["engram-calibration-human-queue-206-v1"] = (
    "engram-calibration-human-queue-206-v1"
)
QUEUE_JUDGMENT_SCHEMA: Literal["engram-calibration-human-queue-judgment-206-v1"] = (
    "engram-calibration-human-queue-judgment-206-v1"
)


def _validate_critical(critical: dict[str, Any]) -> None:
    if set(critical) != set(CRITICAL_FIELDS):
        raise ValueError("queue_judgment_requires_exactly_critical_fields")
    for name, vocabulary in CriticalFieldVocabulary.items():
        if critical[name] not in vocabulary:
            raise ValueError(f"critical_field_out_of_vocabulary:{name}")


class QueueEntry(Record):
    sample_id: Token
    reasons: tuple[str, ...]
    audit_only: bool = False


class HumanQueueManifest(Record):
    queue_schema: Literal["engram-calibration-human-queue-206-v1"] = (
        "engram-calibration-human-queue-206-v1"
    )
    protocol_version: str
    campaign_id: str
    sampling_manifest_digest: str
    source_packet_digest: str
    entries: tuple[QueueEntry, ...]

    @model_validator(mode="after")
    def manifest_contract(self) -> Self:
        if self.protocol_version != CONSENSUS_PROTOCOL_VERSION:
            raise ValueError("protocol_version_mismatch")
        ids = [entry.sample_id for entry in self.entries]
        if len(set(ids)) != len(ids):
            raise ValueError("duplicate_queue_sample_id")
        for entry in self.entries:
            if entry.audit_only and "audit_selected" not in entry.reasons:
                raise ValueError("audit_only_entry_requires_audit_reason")
            if not entry.audit_only and not entry.reasons:
                raise ValueError("escalated_entry_requires_reasons")
        return self


class HumanQueueJudgment(Record):
    """One human's initial judgment plus (later) the final resolution."""

    judgment_schema: Literal["engram-calibration-human-queue-judgment-206-v1"] = (
        "engram-calibration-human-queue-judgment-206-v1"
    )
    protocol_version: str
    campaign_id: str
    sampling_manifest_digest: str
    source_packet_digest: str
    sample_id: Token
    adjudicator_ref: Token
    queue_reasons: tuple[str, ...]
    audit_selected: bool = False
    # Independent initial judgment — captured before model votes were shown.
    initial_critical: dict[str, Any]
    initial_confidence: Literal["low", "medium", "high"] = Field(default="medium")
    initial_captured_at: AwareDatetime
    # Votes are revealed only after the initial judgment exists.
    model_votes_revealed_at: AwareDatetime | None = None
    # Final resolution after adjudication (may differ from initial).
    final_critical: dict[str, Any] | None = None
    final_confidence: Literal["low", "medium", "high", "unknown"] = "unknown"
    final_adjudicated_at: AwareDatetime | None = None
    final_note: str | None = None

    @model_validator(mode="after")
    def judgment_contract(self) -> Self:
        if self.protocol_version != CONSENSUS_PROTOCOL_VERSION:
            raise ValueError("protocol_version_mismatch")
        _validate_critical(self.initial_critical)
        if self.final_critical is not None:
            _validate_critical(self.final_critical)
            if self.final_adjudicated_at is None:
                raise ValueError("final_resolution_requires_timestamp")
            if self.model_votes_revealed_at is None:
                raise ValueError("final_resolution_requires_revealed_votes")
        if self.model_votes_revealed_at is not None and self.final_critical is None:
            # votes may be revealed for adjudication before the final call
            pass
        return self


def build_queue(
    *,
    campaign_id: str,
    sampling: SamplingManifest,
    source_packet_digest: str,
    records_by_lane: dict[str, dict[str, ModelReviewRecord]],
) -> HumanQueueManifest:
    """Build the mandatory human queue from classified lanes (pre-audit)."""
    classifications = {
        sample_id: classify_case(
            {slot: records_by_lane[slot][sample_id] for slot in REVIEWER_SLOTS}
        )
        for sample_id in sampling.sample_ids
    }
    consensus_ids = [sid for sid, c in classifications.items() if c["consensus"]]
    audit_ids = set(select_audit_sample(consensus_ids))
    entries: list[QueueEntry] = []
    for sample_id in sampling.sample_ids:
        classification = classifications[sample_id]
        reasons = list(classification["escalation_reasons"])
        if sample_id in audit_ids:
            reasons.append("audit_selected")
        if not reasons:
            continue
        entries.append(
            QueueEntry(
                sample_id=sample_id,
                reasons=tuple(sorted(reasons)),
                audit_only=not classification["escalation_reasons"],
            )
        )
    return HumanQueueManifest(
        protocol_version=CONSENSUS_PROTOCOL_VERSION,
        campaign_id=campaign_id,
        sampling_manifest_digest=sampling.manifest_digest(),
        source_packet_digest=source_packet_digest,
        entries=tuple(entries),
    )


def write_queue(queue: HumanQueueManifest, protected_dir: Path) -> Path:
    path = protected_dir / "human-queue.json"
    write_protected_file(
        path, (json.dumps(queue.model_dump(mode="json"), sort_keys=True) + "\n").encode()
    )
    return path


def queue_judgment_path(protected_dir: Path, sample_id: str) -> Path:
    return protected_dir / "judgments" / f"{sample_id}.json"


def final_resolution_path(protected_dir: Path, sample_id: str) -> Path:
    return protected_dir / "judgments" / f"{sample_id}.final.json"


def save_initial_judgment(judgment: HumanQueueJudgment, protected_dir: Path) -> Path:
    """Persist the initial judgment; refuse to overwrite (independence proof)."""
    path = queue_judgment_path(protected_dir, judgment.sample_id)
    if path.exists():
        raise ValueError("initial_judgment_already_recorded")
    write_protected_file(
        path, (json.dumps(judgment.model_dump(mode="json"), sort_keys=True) + "\n").encode()
    )
    return path


def revealed_judgment_path(protected_dir: Path, sample_id: str) -> Path:
    return queue_judgment_path(protected_dir, sample_id).with_suffix(".revealed.json")


def reveal_model_votes(protected_dir: Path, sample_id: str) -> HumanQueueJudgment:
    """Persist the vote-reveal event (only after the initial judgment exists)."""
    path = queue_judgment_path(protected_dir, sample_id)
    if not path.exists():
        raise ValueError("initial_judgment_required_before_reveal")
    revealed_path = revealed_judgment_path(protected_dir, sample_id)
    if revealed_path.exists():
        raise ValueError("model_votes_already_revealed")
    judgment = HumanQueueJudgment.model_validate(json.loads(path.read_text()))
    revealed = judgment.model_copy(update={"model_votes_revealed_at": datetime.now(UTC)})
    write_protected_file(
        revealed_path,
        (json.dumps(revealed.model_dump(mode="json"), sort_keys=True) + "\n").encode(),
    )
    return revealed


def record_final_resolution(
    protected_dir: Path,
    sample_id: str,
    *,
    final_critical: dict[str, Any],
    final_confidence: Literal["low", "medium", "high", "unknown"],
    note: str | None = None,
) -> HumanQueueJudgment:
    """Attach the final resolution to an existing initial judgment."""
    path = queue_judgment_path(protected_dir, sample_id)
    if not path.exists():
        raise ValueError("initial_judgment_required_before_resolution")
    judgment = HumanQueueJudgment.model_validate(json.loads(path.read_text()))
    if judgment.final_critical is not None:
        raise ValueError("final_resolution_already_recorded")
    revealed_path = queue_judgment_path(protected_dir, sample_id).with_suffix(".revealed.json")
    revealed_at = None
    if revealed_path.exists():
        revealed_judgment = HumanQueueJudgment.model_validate(json.loads(revealed_path.read_text()))
        revealed_at = revealed_judgment.model_votes_revealed_at
    if revealed_at is None:
        raise ValueError("model_votes_must_be_revealed_before_final_resolution")
    updated = judgment.model_copy(
        update={
            "model_votes_revealed_at": revealed_at,
            "final_critical": dict(final_critical),
            "final_confidence": final_confidence,
            "final_adjudicated_at": datetime.now(UTC),
            "final_note": note,
        }
    )
    # The initial judgment file is immutable evidence; the resolution is a new
    # append-only artifact beside it (no-replace semantics preserved).
    resolution_path = final_resolution_path(protected_dir, sample_id)
    if resolution_path.exists():
        raise ValueError("final_resolution_already_recorded")
    write_protected_file(
        resolution_path,
        (json.dumps(updated.model_dump(mode="json"), sort_keys=True) + "\n").encode(),
    )
    return updated


def export_queue_evidence(protected_dir: Path) -> dict[str, Any]:
    """Export mechanically ingestible protected queue evidence."""
    queue = HumanQueueManifest.model_validate(
        json.loads((protected_dir / "human-queue.json").read_text())
    )
    judgments_dir = protected_dir / "judgments"
    judgments = []
    if judgments_dir.exists():
        for path in sorted(judgments_dir.glob("*.json")):
            if path.name.endswith(".final.json"):
                continue
            judgments.append(
                HumanQueueJudgment.model_validate(json.loads(path.read_text())).model_dump(
                    mode="json"
                )
            )
    by_id = {j["sample_id"]: j for j in judgments}
    missing_initial = [entry.sample_id for entry in queue.entries if entry.sample_id not in by_id]
    return {
        "queue": queue.model_dump(mode="json"),
        "judgments": judgments,
        "counts": {
            "queue_size": len(queue.entries),
            "judgments_recorded": len(judgments),
            "missing_initial_judgments": len(missing_initial),
            "resolved": sum(1 for j in judgments if j["final_critical"] is not None),
        },
    }

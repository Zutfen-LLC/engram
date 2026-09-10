"""Protected human-adjudication queue workflow for #206 escalated cases.

Scope is deliberately narrow (per the issue: no general Engram product UI):

- the queue contains ONLY escalated/audit-selected/escalation-expanded
  cases, never the full 402 (unless full audit escalation fires);
- the human sees the original blind case evidence first (via the existing
  frozen #202 packets — nothing new is shown);
- the human's independent initial judgment is captured and frozen BEFORE the
  three model votes are revealed for disagreement adjudication, and both are
  preserved separately;
- judgments validate against the same frozen critical vocabulary;
- state is append-only per sample and resumable (one protected file per
  sample state, exclusive-create);
- export is mechanically ingestible protected evidence with ONE coherent
  case state per queued sample (initial_judgment / reveal_event /
  final_resolution kept distinct), never duplicated initial/revealed rows.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Sequence
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
    select_audit_sample_with_coverage,
)
from evals.calibration.freeze import FrameRow, SamplingManifest
from evals.calibration.review import write_protected_file

QUEUE_SCHEMA: Literal["engram-calibration-human-queue-206-v1"] = (
    "engram-calibration-human-queue-206-v1"
)
QUEUE_JUDGMENT_SCHEMA: Literal["engram-calibration-human-queue-judgment-206-v1"] = (
    "engram-calibration-human-queue-judgment-206-v1"
)
REVEAL_EVENT_SCHEMA: Literal["engram-calibration-human-queue-reveal-206-v1"] = (
    "engram-calibration-human-queue-reveal-206-v1"
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
        return self


class VoteRevealEvent(Record):
    """Protected evidence of exactly WHICH model votes were shown (#206 FIX-5A).

    Binds the reveal to the case, campaign/protocol/sampling/packet identity,
    the EXACT three first-pass model record digests in frozen slot order, the
    three frozen lane digests, and the reveal timestamp. A later audit can
    reconstruct precisely what evidence the human saw between the initial
    judgment and the final resolution.
    """

    reveal_schema: Literal["engram-calibration-human-queue-reveal-206-v1"] = REVEAL_EVENT_SCHEMA
    protocol_version: str
    campaign_id: str
    sampling_manifest_digest: str
    source_packet_digest: str
    sample_id: Token
    revealed_record_digests: tuple[str, ...]  # exactly three, slot order
    lane_digests: tuple[str, ...]  # exactly three, slot order
    revealed_at: AwareDatetime

    @model_validator(mode="after")
    def reveal_contract(self) -> Self:
        if self.protocol_version != CONSENSUS_PROTOCOL_VERSION:
            raise ValueError("protocol_version_mismatch")
        if len(self.revealed_record_digests) != len(REVIEWER_SLOTS):
            raise ValueError("reveal_requires_all_three_record_digests")
        if len(self.lane_digests) != len(REVIEWER_SLOTS):
            raise ValueError("reveal_requires_all_three_lane_digests")
        return self


def build_queue(
    *,
    campaign_id: str,
    sampling: SamplingManifest,
    source_packet_digest: str,
    records_by_lane: dict[str, dict[str, ModelReviewRecord]],
    frame_rows: dict[str, FrameRow] | None = None,
) -> HumanQueueManifest:
    """Build the mandatory human queue from classified lanes (pre-audit).

    With ``frame_rows`` (the real campaign path) audit selection uses the
    frozen marginal-coverage algorithm (FIX-2).
    """
    classifications = {
        sample_id: classify_case(
            {slot: records_by_lane[slot][sample_id] for slot in REVIEWER_SLOTS}
        )
        for sample_id in sampling.sample_ids
    }
    consensus_ids = [sid for sid, c in classifications.items() if c["consensus"]]
    if frame_rows is not None:
        audit_ids = set(select_audit_sample_with_coverage(consensus_ids, frame_rows).selected)
    else:
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


def reveal_event_path(protected_dir: Path, sample_id: str) -> Path:
    return protected_dir / "judgments" / f"{sample_id}.revealed.json"


def save_initial_judgment(judgment: HumanQueueJudgment, protected_dir: Path) -> Path:
    """Persist the initial judgment; refuse to overwrite (independence proof)."""
    path = queue_judgment_path(protected_dir, judgment.sample_id)
    if path.exists():
        raise ValueError("initial_judgment_already_recorded")
    write_protected_file(
        path, (json.dumps(judgment.model_dump(mode="json"), sort_keys=True) + "\n").encode()
    )
    return path


def reveal_model_votes(
    protected_dir: Path,
    sample_id: str,
    *,
    current_records_by_slot: dict[str, ModelReviewRecord],
    lane_digests: tuple[str, ...],
    campaign_id: str,
    sampling_manifest_digest: str,
    source_packet_digest: str,
) -> VoteRevealEvent:
    """Persist the vote-reveal event binding the EXACT votes shown (FIX-5A).

    Fails closed unless:

    - the independent initial judgment exists (reveal before initial fails);
    - records for all three frozen slots exist for this exact sample;
    - the reveal binds the CURRENT record digests in frozen slot order;
    - the case/campaign/sampling/packet identity matches the caller-supplied
      frozen identity.

    The initial judgment file is never mutated.
    """
    if set(current_records_by_slot) != set(REVIEWER_SLOTS):
        raise ValueError("reveal_requires_all_three_lanes")
    if len(lane_digests) != len(REVIEWER_SLOTS):
        raise ValueError("reveal_requires_all_three_lane_digests")
    path = queue_judgment_path(protected_dir, sample_id)
    if not path.exists():
        raise ValueError("initial_judgment_required_before_reveal")
    judgment = HumanQueueJudgment.model_validate(json.loads(path.read_text()))
    if judgment.campaign_id != campaign_id:
        raise ValueError("reveal_campaign_mismatch")
    if judgment.sampling_manifest_digest != sampling_manifest_digest:
        raise ValueError("reveal_sampling_manifest_mismatch")
    if judgment.source_packet_digest != source_packet_digest:
        raise ValueError("reveal_source_packet_mismatch")
    record_digests = tuple(current_records_by_slot[slot].record_digest() for slot in REVIEWER_SLOTS)
    for slot in REVIEWER_SLOTS:
        if current_records_by_slot[slot].sample_id != sample_id:
            raise ValueError("reveal_record_sample_mismatch")
    revealed_path = reveal_event_path(protected_dir, sample_id)
    if revealed_path.exists():
        raise ValueError("model_votes_already_revealed")
    event = VoteRevealEvent(
        protocol_version=CONSENSUS_PROTOCOL_VERSION,
        campaign_id=campaign_id,
        sampling_manifest_digest=sampling_manifest_digest,
        source_packet_digest=source_packet_digest,
        sample_id=sample_id,
        revealed_record_digests=record_digests,
        lane_digests=tuple(lane_digests),
        revealed_at=datetime.now(UTC),
    )
    write_protected_file(
        revealed_path,
        (json.dumps(event.model_dump(mode="json"), sort_keys=True) + "\n").encode(),
    )
    return event


def load_reveal_event(protected_dir: Path, sample_id: str) -> VoteRevealEvent | None:
    path = reveal_event_path(protected_dir, sample_id)
    if not path.exists():
        return None
    return VoteRevealEvent.model_validate(json.loads(path.read_text()))


def validate_reveal_binding(
    event: VoteRevealEvent,
    *,
    current_records_by_slot: dict[str, ModelReviewRecord],
    lane_digests: tuple[str, ...],
    protocol_version: str,
    campaign_id: str,
    sampling_manifest_digest: str,
    source_packet_digest: str,
    sample_id: str,
) -> None:
    """Canonical reveal-event validator (FIX-R2-3).

    Proves the reveal event is bound to the EXACT current frozen evidence:

    - protocol version, campaign, sampling manifest, source packet, sample;
    - the exact three CURRENT record digests in frozen slot order;
    - the exact three frozen lane digests in slot order.

    Used both by ``record_final_resolution`` (mandatory strong verification)
    and by ``verify_consensus_ledger`` (the final ledger must never trust the
    writer to have done the right thing).
    """
    if event.protocol_version != protocol_version:
        raise ValueError("reveal_protocol_version_mismatch")
    if event.campaign_id != campaign_id:
        raise ValueError("reveal_campaign_mismatch")
    if event.sampling_manifest_digest != sampling_manifest_digest:
        raise ValueError("reveal_sampling_manifest_mismatch")
    if not hmac.compare_digest(event.source_packet_digest, source_packet_digest):
        raise ValueError("reveal_source_packet_mismatch")
    if event.sample_id != sample_id:
        raise ValueError("reveal_sample_mismatch")
    if set(current_records_by_slot) != set(REVIEWER_SLOTS):
        raise ValueError("reveal_requires_all_three_lanes")
    if len(lane_digests) != len(REVIEWER_SLOTS):
        raise ValueError("reveal_requires_all_three_lane_digests")
    current_digests = tuple(
        current_records_by_slot[slot].record_digest() for slot in REVIEWER_SLOTS
    )
    if tuple(event.revealed_record_digests) != current_digests:
        raise ValueError("reveal_record_digests_do_not_match_current_lane_evidence")
    if not hmac.compare_digest(
        hashlib.sha256(json.dumps(tuple(event.lane_digests)).encode()).hexdigest(),
        hashlib.sha256(json.dumps(tuple(lane_digests)).encode()).hexdigest(),
    ):
        raise ValueError("reveal_lane_digests_do_not_match_frozen_lanes")


def record_final_resolution(
    protected_dir: Path,
    sample_id: str,
    *,
    final_critical: dict[str, Any],
    final_confidence: Literal["low", "medium", "high", "unknown"],
    current_records_by_slot: dict[str, ModelReviewRecord],
    lane_digests: tuple[str, ...],
    campaign_id: str,
    sampling_manifest_digest: str,
    source_packet_digest: str,
    note: str | None = None,
) -> HumanQueueJudgment:
    """Attach the final resolution to an existing initial judgment.

    FIX-5A + FIX-R2-3: the recorded reveal event is verified against the
    CURRENT lane evidence through the canonical ``validate_reveal_binding``
    validator — identity (protocol/campaign/sampling/packet/sample), the
    exact three current record digests, and the exact three frozen lane
    digests must all match. Strong verification is MANDATORY; there is no
    bypass path (the former optional ``current_records_by_slot=None`` weak
    path was removed).
    """
    path = queue_judgment_path(protected_dir, sample_id)
    if not path.exists():
        raise ValueError("initial_judgment_required_before_resolution")
    judgment = HumanQueueJudgment.model_validate(json.loads(path.read_text()))
    if judgment.final_critical is not None:
        raise ValueError("final_resolution_already_recorded")
    if judgment.campaign_id != campaign_id:
        raise ValueError("resolution_campaign_mismatch")
    if judgment.sampling_manifest_digest != sampling_manifest_digest:
        raise ValueError("resolution_sampling_manifest_mismatch")
    if judgment.source_packet_digest != source_packet_digest:
        raise ValueError("resolution_source_packet_mismatch")
    event = load_reveal_event(protected_dir, sample_id)
    if event is None:
        raise ValueError("model_votes_must_be_revealed_before_final_resolution")
    if event.sample_id != sample_id:
        raise ValueError("reveal_event_sample_mismatch")
    validate_reveal_binding(
        event,
        current_records_by_slot=current_records_by_slot,
        lane_digests=lane_digests,
        protocol_version=CONSENSUS_PROTOCOL_VERSION,
        campaign_id=campaign_id,
        sampling_manifest_digest=sampling_manifest_digest,
        source_packet_digest=source_packet_digest,
        sample_id=sample_id,
    )
    updated = judgment.model_copy(
        update={
            "model_votes_revealed_at": event.revealed_at,
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


def load_final_resolution(protected_dir: Path, sample_id: str) -> HumanQueueJudgment | None:
    path = final_resolution_path(protected_dir, sample_id)
    if not path.exists():
        return None
    return HumanQueueJudgment.model_validate(json.loads(path.read_text()))


def load_initial_judgment(protected_dir: Path, sample_id: str) -> HumanQueueJudgment | None:
    path = queue_judgment_path(protected_dir, sample_id)
    if not path.exists():
        return None
    return HumanQueueJudgment.model_validate(json.loads(path.read_text()))


def export_queue_evidence(protected_dir: Path) -> dict[str, Any]:
    """Export ONE coherent protected case state per queued sample (FIX-5B).

    For each queue entry, expose separately:

        initial_judgment   (authoritative independent initial state)
        reveal_event       (the VoteRevealEvent binding the exact votes shown)
        final_resolution   (authoritative final state, when resolved)

    The underlying artifacts stay distinct immutable files; the export never
    emits initial/revealed files as separate human cases. Counts distinguish
    queue size, initial judgments complete, votes revealed, final resolutions
    complete, and unresolved cases.

    FIX-R3-9: if an audit-escalation overlay exists (written mechanically by
    ``materialize_audit_escalation``), its entries are merged into the
    exported queue so the ledger verifier sees the full required population.
    """
    queue = HumanQueueManifest.model_validate(
        json.loads((protected_dir / "human-queue.json").read_text())
    )
    entries = list(queue.entries)
    overlay_path = protected_dir / "human-queue-escalation.json"
    if overlay_path.exists():
        overlay = HumanQueueManifest.model_validate(json.loads(overlay_path.read_text()))
        _validate_overlay_binding(queue, overlay)
        existing = {entry.sample_id for entry in entries}
        for entry in overlay.entries:
            if entry.sample_id in existing:
                raise ValueError("queue_overlay_duplicate_sample_id")
            entries.append(entry)
    merged = queue.model_copy(update={"entries": tuple(entries)})
    case_states: list[dict[str, Any]] = []
    initial_complete = 0
    votes_revealed = 0
    final_complete = 0
    for entry in merged.entries:
        sample_id = entry.sample_id
        initial = load_initial_judgment(protected_dir, sample_id)
        reveal = load_reveal_event(protected_dir, sample_id)
        final = load_final_resolution(protected_dir, sample_id)
        if initial is not None:
            initial_complete += 1
        if reveal is not None:
            votes_revealed += 1
        if final is not None and final.final_critical is not None:
            final_complete += 1
        case_states.append(
            {
                "sample_id": sample_id,
                "queue_reasons": list(entry.reasons),
                "audit_only": entry.audit_only,
                "initial_judgment": (initial.model_dump(mode="json") if initial else None),
                "reveal_event": reveal.model_dump(mode="json") if reveal else None,
                "final_resolution": final.model_dump(mode="json") if final else None,
            }
        )
    unresolved = [
        entry.sample_id
        for entry in merged.entries
        if load_final_resolution(protected_dir, entry.sample_id) is None
    ]
    return {
        "queue": merged.model_dump(mode="json"),
        "case_states": case_states,
        "counts": {
            "queue_size": len(merged.entries),
            "initial_judgments_complete": initial_complete,
            "votes_revealed": votes_revealed,
            "final_resolutions_complete": final_complete,
            "unresolved": len(unresolved),
            "unresolved_sample_ids": unresolved,
        },
    }


def require_queued_sample(
    protected_dir: Path,
    sample_id: str,
    *,
    campaign_id: str,
    sampling_manifest_digest: str,
    source_packet_digest: str,
) -> QueueEntry:
    """Return one base/overlay queue entry or fail before any operator action."""
    exported = export_queue_evidence(protected_dir)
    queue = HumanQueueManifest.model_validate(exported["queue"])
    if queue.campaign_id != campaign_id:
        raise ValueError("queue_manifest_campaign_mismatch")
    if queue.sampling_manifest_digest != sampling_manifest_digest:
        raise ValueError("queue_manifest_sampling_mismatch")
    if not hmac.compare_digest(queue.source_packet_digest, source_packet_digest):
        raise ValueError("queue_manifest_source_packet_mismatch")
    for entry in queue.entries:
        if entry.sample_id == sample_id:
            return entry
    raise ValueError("sample_not_in_human_queue")


def _validate_overlay_binding(base: HumanQueueManifest, overlay: HumanQueueManifest) -> None:
    if overlay.protocol_version != base.protocol_version:
        raise ValueError("queue_overlay_protocol_mismatch")
    if overlay.campaign_id != base.campaign_id:
        raise ValueError("queue_overlay_campaign_mismatch")
    if overlay.sampling_manifest_digest != base.sampling_manifest_digest:
        raise ValueError("queue_overlay_sampling_mismatch")
    if overlay.source_packet_digest != base.source_packet_digest:
        raise ValueError("queue_overlay_source_packet_mismatch")


def materialize_audit_escalation(
    protected_dir: Path,
    *,
    records_by_lane: dict[str, dict[str, ModelReviewRecord]],
    consensus_ids: Sequence[str],
    suggested_kind_by_sample: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Mechanically materialize the expanded full-human queue on escalation.

    FIX-R3-9: when the frozen audit outcome escalates, the expanded
    population must be materialized for review WITHOUT hand-authoring final
    resolution files. This derives the audit outcome from the queue evidence
    (audit-selected final resolutions) plus the frozen lane records, and —
    if escalation fires — writes the append-only escalation overlay
    (``human-queue-escalation.json``) containing every consensus case not
    already queued. Idempotent: an existing overlay is returned as-is when
    it matches the derived expansion, and refused when it does not.
    """
    export = export_queue_evidence_without_overlay(protected_dir)
    base = HumanQueueManifest.model_validate(export["queue"])
    audit_entries = [entry for entry in base.entries if "audit_selected" in entry.reasons]
    final_resolutions: dict[str, dict[str, Any]] = {}
    for entry in audit_entries:
        final = load_final_resolution(protected_dir, entry.sample_id)
        if final is None or final.final_critical is None:
            raise ValueError("escalation_requires_completed_audit_resolutions")
        final_resolutions[entry.sample_id] = dict(final.final_critical)
    from evals.calibration.consensus import evaluate_audit_outcome

    outcome = evaluate_audit_outcome(
        audit_results={
            sample_id: {"human_final_critical": critical}
            for sample_id, critical in final_resolutions.items()
        },
        records_by_lane={
            sample_id: {
                slot: records_by_lane[slot][sample_id]
                for slot in REVIEWER_SLOTS
                if sample_id in records_by_lane.get(slot, {})
            }
            for sample_id in final_resolutions
        },
        suggested_kind_by_sample=suggested_kind_by_sample,
    )
    overlay_path = protected_dir / "human-queue-escalation.json"
    if not outcome["escalate_full_human_review"]:
        return {"escalated": False, "outcome": outcome, "overlay": None}
    queued = {entry.sample_id for entry in base.entries}
    expanded = sorted(set(consensus_ids) - queued)
    if overlay_path.exists():
        existing = HumanQueueManifest.model_validate(json.loads(overlay_path.read_text()))
        _validate_overlay_binding(base, existing)
        if {e.sample_id for e in existing.entries} != set(expanded):
            raise ValueError("queue_escalation_overlay_mismatch")
        return {"escalated": True, "outcome": outcome, "overlay": str(overlay_path)}
    overlay = base.model_copy(
        update={
            "entries": tuple(
                QueueEntry(
                    sample_id=sample_id,
                    reasons=("audit_escalation_full_human_review",),
                    audit_only=False,
                )
                for sample_id in expanded
            )
        }
    )
    write_protected_file(
        overlay_path,
        (json.dumps(overlay.model_dump(mode="json"), sort_keys=True) + "\n").encode(),
    )
    return {"escalated": True, "outcome": outcome, "overlay": str(overlay_path)}


def export_queue_evidence_without_overlay(protected_dir: Path) -> dict[str, Any]:
    """Queue export limited to the base manifest (no escalation overlay)."""
    queue = HumanQueueManifest.model_validate(
        json.loads((protected_dir / "human-queue.json").read_text())
    )
    case_states: list[dict[str, Any]] = []
    for entry in queue.entries:
        initial = load_initial_judgment(protected_dir, entry.sample_id)
        reveal = load_reveal_event(protected_dir, entry.sample_id)
        final = load_final_resolution(protected_dir, entry.sample_id)
        case_states.append(
            {
                "sample_id": entry.sample_id,
                "queue_reasons": list(entry.reasons),
                "audit_only": entry.audit_only,
                "initial_judgment": (initial.model_dump(mode="json") if initial else None),
                "reveal_event": reveal.model_dump(mode="json") if reveal else None,
                "final_resolution": final.model_dump(mode="json") if final else None,
            }
        )
    unresolved = [
        entry.sample_id
        for entry in queue.entries
        if load_final_resolution(protected_dir, entry.sample_id) is None
    ]
    return {
        "queue": queue.model_dump(mode="json"),
        "case_states": case_states,
        "counts": {
            "queue_size": len(queue.entries),
            "unresolved": len(unresolved),
            "unresolved_sample_ids": unresolved,
        },
    }

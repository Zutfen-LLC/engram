"""Verified consensus ledger: the sole downstream authority for #206 (FIX-4).

``ReferenceLabel`` lists prove NOTHING about provenance — any caller can
construct them. The ONLY normal path into floors/fitting is a
``VerifiedConsensusLedger`` produced by ``verify_consensus_ledger`` from
PROTECTED source evidence:

- the three frozen reviewer lanes, each validated through the FULL canonical
  provenance path (FIX-R2-1): lane identity, campaign/sampling/source packet,
  exact frozen membership/order, and current record digests equal to
  ``LaneFreeze.record_digests`` — plus raw-response evidence verification
  (FIX-R2-4), which is MANDATORY at this boundary (FIX-R3-2);
- the initial classifications (``classify_case`` over the frozen records);
- the deterministic audit selection (frozen marginal-coverage algorithm);
- the completed human queue evidence (initial judgments, vote-reveal events,
  final resolutions) with every reveal event re-verified against the exact
  current frozen model evidence (FIX-R2-3);
- the audit outcome MECHANICALLY DERIVED from that protected evidence
  (FIX-R2-2) — a caller-supplied ``AuditOutcomeRecord`` is never authority;
  if one is supplied (audit provenance), it must compare field-exactly
  against the independently derived result.

FIX-R3-1: ``VerifiedConsensusLedger`` is a PRIVATE-CAPABILITY type. It is a
plain ``Record`` subclass, but its constructor requires an opaque
``_VerificationCapability`` token that is ONLY created inside this module by
``verify_consensus_ledger`` after every binding has been re-derived from
protected evidence. A schema-valid-looking object constructed by an outside
caller cannot carry the token, so it can never be manufactured through the
public schema API and passed into ``consensus_reference_observations``,
floor evaluation, or fitting. The preferred full-evidence design is also
implemented: ``load_verified_ledger`` loads a frozen ledger file plus
independently retained SHA-256 and re-verifies ALL underlying evidence
(lanes loaded from the protected root with raw bytes, queue evidence, frame)
before returning the capability-bearing object.

For each sample the verifier RE-DERIVES the final state and rejects any
wrapper that disagrees:

- ``cross_model_consensus``: exactly three records, digests bound to the
  frozen lanes, ``classify_case`` returns consensus, the sample was neither
  human-required nor audit-selected, no full-human escalation made it
  human-required, and ``final_dimensions`` EXACTLY equal the mechanically
  derived unanimous consensus critical fields;
- ``human_audited_consensus``: deterministic-audit-selected, independent
  human initial judgment exists, reveal event bound to the exact current
  frozen evidence, human final resolution exists AND CONFIRMS the unanimous
  model consensus on every critical field (FIX-R3-5), final dimensions
  EXACTLY equal the authoritative human final resolution, and the audit
  outcome permits this provenance state;
- ``human_adjudicated``: the case was in the final required human queue
  (initial queue, audit selection the human overrode, or audit-escalation
  expansion), initial judgment exists, reveal event bound to the exact
  current frozen evidence, final resolution exists, final dimensions EXACTLY
  equal the human final resolution.

If full audit escalation occurred, NO remaining automatically accepted
``cross_model_consensus`` row may survive. No ledger is accepted while any
required human case is unresolved.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path
from typing import Any

from evals.admission.schema import Record
from evals.calibration.consensus import (
    AUDIT_SAMPLE_RATE,
    AUDIT_SELECTION_SEED,
    CONSENSUS_PROTOCOL_VERSION,
    REVIEWER_SLOTS,
    AuditOutcomeRecord,
    ConsensusLedger,
    ConsensusProvenanceWrapper,
    LaneFreeze,
    ModelReviewRecord,
    audit_outcome_record_from_evidence,
    classify_case,
    select_audit_sample_with_coverage,
    unanimous_consensus_critical,
)
from evals.calibration.freeze import FrameRow, SamplingManifest
from evals.calibration.human_queue import (
    export_queue_evidence,
    load_final_resolution,
    load_initial_judgment,
    load_reveal_event,
    validate_reveal_binding,
)
from evals.calibration.lane_binding import (
    audit_campaign_binding,
)
from evals.calibration.raw_evidence import (
    validate_lane_provenance_with_raw,
)
from evals.calibration.review import write_protected_file

LEDGER_FILE_SCHEMA = "engram-calibration-consensus-ledger-file-206-v1"


class _CapabilityToken:
    """Module-private issuance sentinel (never exported)."""

    __slots__ = ()


_CAPABILITY_SECRET = _CapabilityToken()


class _VerificationCapability:
    """Opaque verifier-issued seal bound to one exact ledger serialization."""

    __slots__ = ("ledger_digest",)

    def __init__(self, secret: object | None = None, ledger_digest: str = "") -> None:
        if secret is not _CAPABILITY_SECRET:
            raise RuntimeError(
                "VerifiedConsensusLedger authority cannot be constructed outside "
                "verify_consensus_ledger"
            )
        self.ledger_digest = ledger_digest


def _ledger_digest(ledger: ConsensusLedger) -> str:
    """Stable capability binding; changing any ledger field invalidates authority."""
    return _sha256_bytes(
        json.dumps(ledger.model_dump(mode="json"), sort_keys=True, separators=(",", ":")).encode()
    )


def require_verification_capability(verified: VerifiedConsensusLedger) -> None:
    """Reject Pydantic construction/copy bypasses before consuming final labels.

    ``model_construct`` bypasses ``__init__`` and ``model_copy`` preserves
    private fields, so type identity alone is never authority. The seal must
    both exist and bind the exact immutable ledger payload issued by the
    canonical re-verifier.
    """
    capability = getattr(verified, "_verification_capability", None)
    if not isinstance(capability, _VerificationCapability) or not hmac.compare_digest(
        capability.ledger_digest, _ledger_digest(verified.ledger)
    ):
        raise ValueError("verified_consensus_ledger_capability_invalid")


class QueueEvidenceBindings(Record):
    """Protected human-queue evidence digests bound into the ledger."""

    queue_manifest_sha256: str
    audit_selected_ids: tuple[str, ...]
    initial_queue_ids: tuple[str, ...]
    escalated_expansion_ids: tuple[str, ...]


class VerifiedConsensusLedger(Record):
    """The authoritative result of verifying a frozen consensus ledger file.

    Carries the verified ledger plus the evidence it was derived from, so
    downstream consumers (``consensus_reference_observations`` and friends)
    receive provenance, not a bare label list.

    FIX-R3-1: construction requires ``_capability`` — an opaque object only
    this module can create. Pydantic validates it as an arbitrary object;
    a fabricated payload cannot supply a genuine capability instance, and
    copying/serializing one never transfers authority across processes.
    """

    ledger: ConsensusLedger
    queue_evidence_sha256: str
    records_by_lane: dict[str, dict[str, ModelReviewRecord]]
    lanes: tuple[LaneFreeze, ...]

    def __init__(self, **data: Any) -> None:
        capability = data.pop("verification_capability", None)
        if not isinstance(capability, _VerificationCapability):
            raise ValueError(
                "VerifiedConsensusLedger cannot be constructed directly: authority "
                "is issued only by verify_consensus_ledger from protected evidence"
            )
        super().__init__(**data)
        object.__setattr__(self, "_verification_capability", capability)

    def reference_rows(self) -> list[dict[str, Any]]:
        """One authoritative final row per sample (origin + critical)."""
        require_verification_capability(self)
        return [
            {
                "sample_id": wrapper.sample_id,
                "final_label_origin": wrapper.final_label_origin,
                "critical": dict(wrapper.final_dimensions),
            }
            for wrapper in self.ledger.wrappers
        ]


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def verify_consensus_ledger(
    *,
    campaign_id: str,
    sampling: SamplingManifest,
    source_packet_digest: str,
    lanes: tuple[LaneFreeze, ...],
    records_by_lane: dict[str, dict[str, ModelReviewRecord]],
    queue_dir: Path,
    frame_rows: dict[str, FrameRow],
    protected_root: Path,
    suggested_kind_by_sample: dict[str, str] | None = None,
    audit_outcome_record: AuditOutcomeRecord | None = None,
) -> VerifiedConsensusLedger:
    """Re-derive every final state from protected evidence. Fail closed.

    This is the FIX-4 boundary: nothing downstream may accept wrappers or
    labels that this function has not re-derived from the frozen lanes, the
    deterministic audit selection, and the protected human queue evidence.

    FIX-R2-1: every lane is validated through the FULL canonical provenance
    path inside this verifier — including that the CURRENT record digests
    equal the frozen ``LaneFreeze.record_digests`` — never relying on the
    caller having taken the lane-loading path first.

    FIX-R2-2: the audit outcome is DERIVED here from the deterministic audit
    selection, the frozen three model judgments, and the protected human
    final resolutions. A supplied ``audit_outcome_record`` (stored audit
    provenance) must match the derived result FIELD-EXACTLY; it is never
    authority by itself.

    FIX-R2-3: every human-resolved row's ``VoteRevealEvent`` is re-verified
    against the exact current frozen model evidence (identity, three record
    digests, three lane digests). The initial/final human artifacts are also
    validated against the same campaign/sample identities.

    FIX-R3-2: ``protected_root`` is MANDATORY. Every accepted record's raw
    response bytes are verified to exist and hash to the claimed digest
    (provider_error records must claim none) through the full canonical
    ``validate_lane_provenance_with_raw`` verifier. There is no bypass.
    """
    if not isinstance(protected_root, Path):
        raise ValueError("ledger_requires_protected_root")
    if campaign_id != sampling.campaign_id:
        raise ValueError("ledger_campaign_mismatch")
    # --- lanes: FULL canonical provenance path (FIX-R2-1 + FIX-R3-2) ---
    if len(lanes) != len(REVIEWER_SLOTS):
        raise ValueError("lane_count_mismatch")
    audit_campaign_binding(
        campaign_id=campaign_id,
        sampling=sampling,
        source_packet_digest=source_packet_digest,
        lanes=lanes,
    )
    if set(records_by_lane) != set(REVIEWER_SLOTS):
        raise ValueError("records_lane_slot_mapping_ambiguous")
    from evals.calibration.ingestion import verify_reviewer_prompt_binding

    for lane in lanes:
        verify_reviewer_prompt_binding(lane.reviewer)
        slot_records = records_by_lane.get(lane.reviewer.reviewer_slot)
        if slot_records is None:
            raise ValueError("records_lane_slot_mapping_ambiguous")
        # FIX-R3-2: one full canonical verifier inside the ledger boundary —
        # identity + membership + frozen digests PLUS raw-response evidence.
        validate_lane_provenance_with_raw(
            lane,
            slot_records,
            campaign_id=campaign_id,
            sampling=sampling,
            source_packet_digest=source_packet_digest,
            protected_root=protected_root,
        )
    lane_digests_by_slot: dict[str, str] = {
        lane.reviewer.reviewer_slot: lane.lane_digest() for lane in lanes
    }
    canonical_lane_digests = tuple(lane_digests_by_slot[slot] for slot in REVIEWER_SLOTS)
    # --- classifications + audit selection ---
    classifications = {
        sample_id: classify_case(
            {slot: records_by_lane[slot][sample_id] for slot in REVIEWER_SLOTS}
        )
        for sample_id in sampling.sample_ids
    }
    consensus_ids = [sid for sid, c in classifications.items() if c["consensus"]]
    selection = select_audit_sample_with_coverage(consensus_ids, frame_rows)
    audit_ids = set(selection.selected)
    # --- queue evidence ---
    queue_export = export_queue_evidence(queue_dir)
    queue_payload = json.dumps(queue_export["queue"], sort_keys=True).encode()
    queue_sha = _sha256_bytes(queue_payload)
    queue_ids = {entry["sample_id"] for entry in queue_export["queue"]["entries"]}
    _queue_manifest_identities(queue_export, campaign_id, sampling, source_packet_digest)
    # --- human final resolutions for audit-selected cases ---
    final_by_sample: dict[str, dict[str, Any]] = {}
    for sample_id in audit_ids:
        final = load_final_resolution(queue_dir, sample_id)
        if final is None or final.final_critical is None:
            raise ValueError("ledger_requires_audit_final_resolution")
        final_by_sample[sample_id] = dict(final.final_critical)
    # --- FIX-R2-2: audit outcome is a DERIVED fact ---
    derived_outcome = audit_outcome_record_from_evidence(
        audit_selected_ids=selection.selected,
        records_by_lane=_records_by_sample_from_lanes(records_by_lane),
        final_resolutions=final_by_sample,
        suggested_kind_by_sample=suggested_kind_by_sample,
    )
    if audit_outcome_record is not None and (
        audit_outcome_record.model_dump(mode="json") != derived_outcome.model_dump(mode="json")
    ):
        raise ValueError("audit_outcome_record_does_not_match_derived_evidence")
    escalated = derived_outcome.escalate_full_human_review
    # Required human population: initial queue + audit selection (+ escalation).
    required = set(queue_ids)
    if escalated:
        required.update(consensus_ids)
    # Every required case must be resolved (final resolution file present).
    unresolved = sorted(sid for sid in required if load_final_resolution(queue_dir, sid) is None)
    if unresolved:
        raise ValueError("ledger_requires_all_required_human_resolutions")
    # --- construct wrappers, re-deriving every final state ---
    wrappers: list[ConsensusProvenanceWrapper] = []
    for sample_id in sampling.sample_ids:
        records = {slot: records_by_lane[slot][sample_id] for slot in REVIEWER_SLOTS}
        record_digests = tuple(records[slot].record_digest() for slot in REVIEWER_SLOTS)
        classification = classifications[sample_id]
        consensus_reached = bool(classification["consensus"])
        escalation_reasons = tuple(classification["escalation_reasons"])
        in_queue = sample_id in queue_ids
        audit_selected = sample_id in audit_ids
        human_required = in_queue or escalated_and_consensus(escalated, consensus_reached)
        final = load_final_resolution(queue_dir, sample_id)
        initial = load_initial_judgment(queue_dir, sample_id)
        reveal = load_reveal_event(queue_dir, sample_id)
        if not human_required:
            # Automatic consensus acceptance.
            if not consensus_reached:
                raise ValueError("auto_consensus_row_requires_consensus_classification")
            if audit_selected:
                raise ValueError("audit_selected_case_must_be_human_audited")
            derived = _unanimous_from_records(records)
            if derived is None:
                raise ValueError("auto_consensus_row_requires_unanimous_derivation")
            wrappers.append(
                ConsensusProvenanceWrapper(
                    protocol_version=CONSENSUS_PROTOCOL_VERSION,
                    campaign_id=campaign_id,
                    sampling_manifest_digest=sampling.manifest_digest(),
                    source_packet_digest=source_packet_digest,
                    sample_id=sample_id,
                    first_pass_record_digests=record_digests,
                    consensus_reached=True,
                    entered_human_queue=False,
                    queue_reasons=(),
                    human_initial_judgment_digest=None,
                    final_label_origin="cross_model_consensus",
                    final_dimensions=derived,
                    audit_selected=False,
                )
            )
            continue
        # Human-required rows: evidence chain must be complete and re-verified.
        if initial is None:
            raise ValueError("human_row_requires_initial_judgment")
        if reveal is None:
            raise ValueError("human_row_requires_reveal_event")
        if final is None or final.final_critical is None:
            raise ValueError("human_row_requires_final_resolution")
        _validate_human_identity(initial, sample_id, campaign_id, sampling, source_packet_digest)
        _validate_human_identity(final, sample_id, campaign_id, sampling, source_packet_digest)
        # FIX-R2-3: re-verify the reveal event against CURRENT frozen evidence.
        validate_reveal_binding(
            reveal,
            current_records_by_slot=records,
            lane_digests=canonical_lane_digests,
            protocol_version=CONSENSUS_PROTOCOL_VERSION,
            campaign_id=campaign_id,
            sampling_manifest_digest=sampling.manifest_digest(),
            source_packet_digest=source_packet_digest,
            sample_id=sample_id,
        )
        initial_digest = hashlib.sha256(
            json.dumps(initial.model_dump(mode="json"), sort_keys=True).encode()
        ).hexdigest()
        if audit_selected and consensus_reached:
            # FIX-R3-5: only a CONFIRMED audited consensus is
            # human_audited_consensus. If the human final resolution differs
            # from the unanimous model consensus on ANY critical field, the
            # row is human_adjudicated — even when the audit as a whole does
            # not escalate. The final dimensions remain the human resolution.
            derived = _unanimous_from_records(records)
            if derived is None:
                raise ValueError("audited_consensus_row_requires_unanimous_derivation")
            human_final = dict(final.final_critical)
            confirmed = all(
                derived.get(field) == human_final.get(field)
                for field in (
                    "expected_kind",
                    "retention_value",
                    "epistemic_state",
                    "consequence",
                    "acceptable_abstention",
                )
            )
            origin = "human_audited_consensus" if confirmed else "human_adjudicated"
        else:
            origin = "human_adjudicated"
        wrappers.append(
            ConsensusProvenanceWrapper(
                protocol_version=CONSENSUS_PROTOCOL_VERSION,
                campaign_id=campaign_id,
                sampling_manifest_digest=sampling.manifest_digest(),
                source_packet_digest=source_packet_digest,
                sample_id=sample_id,
                first_pass_record_digests=record_digests,
                consensus_reached=consensus_reached,
                entered_human_queue=True,
                queue_reasons=_queue_reasons(escalation_reasons, audit_selected, escalated),
                human_initial_judgment_digest=initial_digest,
                final_label_origin=origin,  # type: ignore[arg-type]
                final_dimensions=dict(final.final_critical),
                audit_selected=audit_selected,
            )
        )
    # Escalation invariant: no automatic consensus row survives escalation.
    if escalated:
        leftover = [
            w.sample_id for w in wrappers if w.final_label_origin == "cross_model_consensus"
        ]
        if leftover:
            raise ValueError("escalation_forbids_remaining_auto_consensus_rows")
    # Audit outcome must permit human_audited_consensus rows to exist at all.
    audited_origins = {
        w.final_label_origin for w in wrappers if w.final_label_origin == "human_audited_consensus"
    }
    if audited_origins and derived_outcome.audited_count != len(audit_ids):
        raise ValueError("audit_outcome_count_mismatch")
    ledger = ConsensusLedger(
        protocol_version=CONSENSUS_PROTOCOL_VERSION,
        campaign_id=campaign_id,
        sampling_manifest_digest=sampling.manifest_digest(),
        source_packet_digest=source_packet_digest,
        # FIX-R3 (lane order): canonical REVIEWER_SLOTS order, never the
        # caller's input tuple order.
        lane_digests=canonical_lane_digests,
        queue_evidence_sha256=queue_sha,
        audit_outcome=derived_outcome,
        audit_selection=selection,
        audit_seed=AUDIT_SELECTION_SEED,
        audit_rate=AUDIT_SAMPLE_RATE,
        wrappers=tuple(wrappers),
    )
    if {w.sample_id for w in ledger.wrappers} != set(sampling.sample_ids):
        raise ValueError("ledger_sample_membership_mismatch")
    return VerifiedConsensusLedger(
        ledger=ledger,
        queue_evidence_sha256=queue_sha,
        records_by_lane=records_by_lane,
        lanes=tuple(sorted(lanes, key=lambda lane: lane.reviewer.reviewer_slot)),
        verification_capability=_VerificationCapability(_CAPABILITY_SECRET, _ledger_digest(ledger)),
    )


def _queue_manifest_identities(
    queue_export: dict[str, Any],
    campaign_id: str,
    sampling: SamplingManifest,
    source_packet_digest: str,
) -> None:
    """Validate the stored queue manifest against campaign identities."""
    manifest = queue_export["queue"]
    if manifest.get("campaign_id") != campaign_id:
        raise ValueError("queue_manifest_campaign_mismatch")
    if manifest.get("sampling_manifest_digest") != sampling.manifest_digest():
        raise ValueError("queue_manifest_sampling_mismatch")
    # FIX-R3 (queue source packet): exact source-packet equality is required;
    # a queue manifest from a different packet is never acceptable evidence.
    if not hmac.compare_digest(str(manifest.get("source_packet_digest", "")), source_packet_digest):
        raise ValueError("queue_manifest_source_packet_mismatch")


def _validate_human_identity(
    artifact: Any,
    sample_id: str,
    campaign_id: str,
    sampling: SamplingManifest,
    source_packet_digest: str,
) -> None:
    if artifact.sample_id != sample_id:
        raise ValueError("human_artifact_sample_mismatch")
    if artifact.campaign_id != campaign_id:
        raise ValueError("human_artifact_campaign_mismatch")
    if artifact.sampling_manifest_digest != sampling.manifest_digest():
        raise ValueError("human_artifact_sampling_mismatch")
    if not hmac.compare_digest(artifact.source_packet_digest, source_packet_digest):
        raise ValueError("human_artifact_source_packet_mismatch")


def _records_by_sample_from_lanes(
    records_by_lane: dict[str, dict[str, ModelReviewRecord]],
) -> dict[str, dict[str, ModelReviewRecord]]:
    """Transpose slot -> sample mapping into sample -> slot mapping."""
    by_sample: dict[str, dict[str, ModelReviewRecord]] = {}
    for slot, records in records_by_lane.items():
        for sample_id, record in records.items():
            by_sample.setdefault(sample_id, {})[slot] = record
    return by_sample


def escalated_and_consensus(escalated: bool, consensus_reached: bool) -> bool:
    """Full-human escalation makes every consensus case human-required."""
    return escalated and consensus_reached


def _queue_reasons(
    escalation_reasons: tuple[str, ...], audit_selected: bool, escalated: bool
) -> tuple[str, ...]:
    reasons = set(escalation_reasons)
    if audit_selected:
        reasons.add("audit_selected")
    if escalated:
        reasons.add("audit_escalation_full_human_review")
    return tuple(sorted(reasons))


def _unanimous_from_records(records: dict[str, ModelReviewRecord]) -> dict[str, Any] | None:
    judgments = [r.judgment for r in (records[slot] for slot in REVIEWER_SLOTS)]
    if any(j is None for j in judgments):
        return None
    return unanimous_consensus_critical(judgments)  # type: ignore[arg-type]


def freeze_verified_ledger(
    verified: VerifiedConsensusLedger,
    *,
    protected_dir: Path,
) -> dict[str, str]:
    """Persist the verified ledger as a protected artifact; return digests."""
    payload = json.dumps(
        {
            "ledger_file_schema": LEDGER_FILE_SCHEMA,
            "ledger": verified.ledger.model_dump(mode="json"),
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    path = protected_dir / "consensus-ledger.json"
    write_protected_file(path, payload)
    return {"path": str(path), "sha256": _sha256_bytes(payload)}


def load_verified_ledger(
    path: Path,
    expected_sha256: str,
    *,
    campaign_id: str,
    sampling: SamplingManifest,
    source_packet_digest: str,
    queue_dir: Path,
    frame_rows: dict[str, FrameRow],
    protected_root: Path,
    suggested_kind_by_sample: dict[str, str] | None = None,
    reviewers: dict[str, Any] | None = None,
) -> VerifiedConsensusLedger:
    """Load a frozen ledger file and RE-VERIFY all underlying evidence.

    FIX-R3-1 preferred design: the frozen ledger FILE is only a serialization
    convenience plus an independently retained SHA-256; authority ALWAYS
    comes from re-derivation. This function loads the three frozen lanes
    (with their bound raw model evidence) and the human queue evidence from
    the protected root, re-runs the full verifier, and requires the
    re-derived ledger to equal the stored one field-exactly. The stored
    audit outcome is passed through as audit PROVENANCE and must match the
    independently derived outcome (FIX-R2-2).

    ``reviewers`` is optional only when the caller has already loaded and
    validated the lanes; when omitted, the lanes are loaded from
    ``protected_root`` through ``load_frozen_lanes``-equivalent validation.
    """
    from evals.calibration.model_lanes import load_frozen_lanes, records_by_lane_from_files

    payload = path.read_bytes()
    if not hmac.compare_digest(_sha256_bytes(payload), expected_sha256):
        raise ValueError("consensus_ledger_digest_mismatch")
    envelope = json.loads(payload)
    if envelope.get("ledger_file_schema") != LEDGER_FILE_SCHEMA:
        raise ValueError("consensus_ledger_schema_mismatch")
    stored = ConsensusLedger.model_validate(envelope["ledger"])
    if reviewers is not None:
        lanes = load_frozen_lanes(
            protected_root,
            campaign_id=campaign_id,
            sampling=sampling,
            source_packet_digest=source_packet_digest,
            reviewers=reviewers,
        )
    else:
        lanes = _load_lanes_from_authorities(
            protected_root, campaign_id=campaign_id, sampling=sampling
        )
    records_by_lane = records_by_lane_from_files(protected_root)
    reverified = verify_consensus_ledger(
        campaign_id=campaign_id,
        sampling=sampling,
        source_packet_digest=source_packet_digest,
        lanes=lanes,
        records_by_lane=records_by_lane,
        queue_dir=queue_dir,
        frame_rows=frame_rows,
        suggested_kind_by_sample=suggested_kind_by_sample,
        audit_outcome_record=stored.audit_outcome,
        protected_root=protected_root,
    )
    if reverified.ledger.model_dump(mode="json") != stored.model_dump(mode="json"):
        raise ValueError("consensus_ledger_reverification_mismatch")
    return reverified


def _load_lanes_from_authorities(
    protected_root: Path,
    *,
    campaign_id: str,
    sampling: SamplingManifest,
) -> tuple[LaneFreeze, ...]:
    """Load and fully validate the three frozen lanes from lane authorities."""
    from evals.calibration.consensus import ReviewerIdentity
    from evals.calibration.model_lanes import load_frozen_lanes

    reviewers: dict[str, ReviewerIdentity] = {}
    digests: set[str] = set()
    for slot in REVIEWER_SLOTS:
        authority_path = protected_root / "lanes" / slot / "lane.json"
        if not authority_path.is_file():
            raise ValueError("lane_not_initialized")
        authority = json.loads(authority_path.read_text())
        reviewers[slot] = ReviewerIdentity.model_validate(authority["reviewer"])
        digests.add(str(authority.get("source_packet_digest", "")))
    if len(digests) != 1:
        raise ValueError("lane_authority_source_packet_inconsistent")
    source_packet_digest = digests.pop()
    if not source_packet_digest:
        raise ValueError("lane_authority_source_packet_inconsistent")
    return load_frozen_lanes(
        protected_root,
        campaign_id=campaign_id,
        sampling=sampling,
        source_packet_digest=source_packet_digest,
        reviewers=reviewers,
    )

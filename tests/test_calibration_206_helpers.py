"""Shared builders for #206 tests: frame rows, receipts, split, identity."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime

from engram.assessment_schema import AssessmentContract
from evals.admission.schema import digest
from evals.calibration.fit import AssessmentExecutionReceipt
from evals.calibration.freeze import FrameRow, SplitManifest, TargetIdentity

NOW = datetime(2026, 9, 10, tzinfo=UTC)


def build_identity() -> TargetIdentity:
    return TargetIdentity(
        campaign_id="campaign",
        campaign_tooling_repo_sha="a" * 40,
        assessment_schema_version="engram.assessment.v1",
        assessment_code_version="assessment-engine-v1",
        prompt_version="engram.assess.1",
        provider_adapter="openai",
        provider_model="model",
        provider_config_digest="sha256:" + "9" * 64,
        provider_params={"temperature": 0},
        assessment_policy_version="assessment-selection-v1",
        calibration_artifact_schema_version="engram.calibration-profiles-v1",
        calibration_dataset_version="dataset-v2",
        label_guide_version="engram-calibration-guide-157-v1",
        canonicalization_version="assessment-evidence-manifest-v1",
        dimensions=("taxonomy", "retention", "epistemic"),
    )


def build_frame_rows(ids: tuple[str, ...]) -> list[FrameRow]:
    return [
        FrameRow(
            item_uuid=f"00000000-0000-0000-0000-{index:012d}",
            sample_id=sample_id,
            content_hash=digest(sample_id),
            content_norm_hash=digest(["norm", sample_id]),
            kind="fact",
            source_type="manual",
            review_status="active",
            assertion_mode="unknown",
            origin="unknown",
            risk="unknown",
            age_bucket="week",
            evidence_state="unknown",
            content_bytes=10,
            input_size_bucket="small",
        )
        for index, sample_id in enumerate(ids)
    ]


def build_split(
    ids: tuple[str, ...], *, dev: tuple[str, ...], holdout: tuple[str, ...]
) -> SplitManifest:
    return SplitManifest(
        campaign_id="campaign",
        sampling_manifest_digest="e" * 64,
        sampling_membership_digest=digest(sorted(ids)),
        split_seed="split",
        dev_fraction=0.6,
        grouping=("content_hash",),
        dev_ids=dev,
        holdout_ids=holdout,
        leakage_checks={},
    )


def build_receipts(
    ids: tuple[str, ...],
    frame: list[FrameRow],
    identity: TargetIdentity,
    contract: AssessmentContract,
) -> list[AssessmentExecutionReceipt]:
    by_id = {row.sample_id: row for row in frame}
    receipts: list[AssessmentExecutionReceipt] = []
    for sample_id in ids:
        base = {
            "sample_id": sample_id,
            "input_content_hash": by_id[sample_id].content_hash,
            "execution_id": f"execution-{sample_id}",
            "captured_at": NOW.isoformat(),
            "assessment": {
                "taxonomy": {"raw_value": 0.55},
                "retention": {"raw_value": 0.55},
                "epistemic": {"raw_value": 0.55},
                "suggested_kind": "fact",
            },
        }
        base["provider_request_digest"] = digest(
            {
                "sample_id": sample_id,
                "input_content_hash": by_id[sample_id].content_hash,
                "target_identity_digest": identity.identity_digest(),
                "assessment_contract_digest": digest(contract.model_dump(mode="json")),
            }
        )
        base["provider_response_digest"] = digest(["response", sample_id])
        placeholder = AssessmentExecutionReceipt.model_validate(
            {**base, "receipt_digest": "0" * 64}
        )
        receipts.append(
            placeholder.model_copy(update={"receipt_digest": placeholder.verified_payload_digest()})
        )
    return receipts


def build_verified_ledger(
    ids: tuple[str, ...],
    critical_by_id: dict[str, dict],
    origin: str = "cross_model_consensus",
):
    """Genuine VerifiedConsensusLedger built through the REAL verifier.

    FIX-R3-1: the capability-guarded constructor makes direct fabrication
    impossible, so this helper materializes a minimal synthetic campaign
    (lanes, raw evidence, queue evidence) and runs
    ``verify_consensus_ledger``. ``origin="human_adjudicated"`` forces every
    case into the human queue (one lane disagrees) and resolves each with
    the caller's critical fields as the human final resolution.
    """
    import tempfile
    from pathlib import Path

    from evals.admission.schema import digest as _digest
    from evals.calibration.consensus import (
        CONSENSUS_PROTOCOL_VERSION,
        REVIEWER_FAMILIES,
        REVIEWER_SLOTS,
        ModelJudgment,
        ModelReviewRecord,
        ReviewerIdentity,
        classify_case,
        select_audit_sample_with_coverage,
    )
    from evals.calibration.freeze import SamplingManifest
    from evals.calibration.human_queue import (
        HumanQueueJudgment,
        HumanQueueManifest,
        QueueEntry,
        record_final_resolution,
        reveal_model_votes,
        save_initial_judgment,
        write_queue,
    )
    from evals.calibration.ingestion import labeling_instructions_digest
    from evals.calibration.model_lanes import append_review_record, freeze_lane
    from evals.calibration.review import write_protected_file

    prompt_digest = labeling_instructions_digest()
    now = NOW
    sampling = SamplingManifest(
        campaign_id="campaign",
        target_identity_digest="1" * 64,
        frame_digest="2" * 64,
        snapshot_sha256="3" * 64,
        snapshot_as_of=now,
        sampling_seed="seed",
        inclusion_rules=("rule",),
        exclusion_rules=(),
        source_row_counts={"eligible_frame": len(ids)},
        stratum_counts={"all": len(ids)},
        coverage_dimensions={},
        sample_ids=ids,
        sample_hashes=tuple(_digest(sid) for sid in ids),
    )
    frame_rows = {str(row.sample_id): row for row in build_frame_rows(ids)}
    adjudicate = origin != "cross_model_consensus"
    family_by_slot = dict(zip(REVIEWER_SLOTS, REVIEWER_FAMILIES, strict=True))
    reviewers = {
        slot: ReviewerIdentity(
            reviewer_slot=slot,  # type: ignore[arg-type]
            reviewer_family=family_by_slot[slot],
            provider_model_identifier=f"{family_by_slot[slot]}-exact-2026-09",
            reviewer_config_digest="a" * 64,
            prompt_digest=prompt_digest,
        )
        for slot in REVIEWER_SLOTS
    }
    records_by_lane: dict[str, dict[str, ModelReviewRecord]] = {slot: {} for slot in REVIEWER_SLOTS}
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        for slot in REVIEWER_SLOTS:
            for sample_id in ids:
                fields = dict(critical_by_id[sample_id])
                if adjudicate and slot == "model_b":
                    fields = dict(fields)
                    fields["expected_kind"] = "decision"  # guaranteed disagreement
                judgment = ModelJudgment(fields=fields, reviewer_confidence="medium")
                raw = f"raw-model-output:{slot}:{sample_id}".encode()
                record = ModelReviewRecord(
                    protocol_version=CONSENSUS_PROTOCOL_VERSION,
                    campaign_id="campaign",
                    sampling_manifest_digest=sampling.manifest_digest(),
                    source_packet_digest="f" * 64,
                    sample_id=sample_id,
                    reviewer_slot=slot,  # type: ignore[arg-type]
                    reviewer_family=family_by_slot[slot],
                    provider_model_identifier=reviewers[slot].provider_model_identifier,
                    reviewer_config_digest="a" * 64,
                    prompt_digest=prompt_digest,
                    label_guide_version="engram-calibration-guide-157-v1",
                    captured_at=now,
                    parse_status="parsed",
                    outcome_status="judged",
                    reviewer_confidence="medium",
                    judgment=judgment,
                    raw_response_digest=hashlib.sha256(raw).hexdigest(),
                    error_code=None,
                )
                records_by_lane[slot][sample_id] = record
                write_protected_file(tmp_path / "lanes" / slot / "raw" / f"{sample_id}.resp", raw)
                append_review_record(record, tmp_path)
        classifications = {
            sid: classify_case({slot: records_by_lane[slot][sid] for slot in REVIEWER_SLOTS})
            for sid in ids
        }
        consensus_ids = [sid for sid, c in classifications.items() if c["consensus"]]
        selection = select_audit_sample_with_coverage(consensus_ids, frame_rows)
        audit_ids = set(selection.selected)
        entries = []
        for sid in ids:
            reasons = list(classifications[sid]["escalation_reasons"])
            if sid in audit_ids:
                reasons.append("audit_selected")
            if reasons:
                entries.append(
                    QueueEntry(
                        sample_id=sid,
                        reasons=tuple(sorted(reasons)),
                        audit_only=not classifications[sid]["escalation_reasons"],
                    )
                )
        queue_dir = tmp_path / "queue"
        write_queue(
            HumanQueueManifest(
                protocol_version=CONSENSUS_PROTOCOL_VERSION,
                campaign_id="campaign",
                sampling_manifest_digest=sampling.manifest_digest(),
                source_packet_digest="f" * 64,
                entries=tuple(entries),
            ),
            queue_dir,
        )
        lanes = tuple(
            freeze_lane(
                protected_root=tmp_path,
                reviewer=reviewers[slot],
                campaign_id="campaign",
                sampling=sampling,
                source_packet_digest="f" * 64,
                neutral_packet_sha256="8" * 64,
            )
            for slot in REVIEWER_SLOTS
        )
        lane_digests = tuple(lane.lane_digest() for lane in lanes)
        for entry in entries:
            sid = entry.sample_id
            save_initial_judgment(
                HumanQueueJudgment.model_validate(
                    {
                        "protocol_version": CONSENSUS_PROTOCOL_VERSION,
                        "campaign_id": "campaign",
                        "sampling_manifest_digest": sampling.manifest_digest(),
                        "source_packet_digest": "f" * 64,
                        "sample_id": sid,
                        "adjudicator_ref": "human-1",
                        "queue_reasons": entry.reasons,
                        "audit_selected": "audit_selected" in entry.reasons,
                        "initial_critical": dict(critical_by_id[sid]),
                        "initial_confidence": "medium",
                        "initial_captured_at": now.isoformat(),
                    }
                ),
                queue_dir,
            )
            reveal_model_votes(
                queue_dir,
                sid,
                current_records_by_slot={
                    slot: records_by_lane[slot][sid] for slot in REVIEWER_SLOTS
                },
                lane_digests=lane_digests,
                campaign_id="campaign",
                sampling_manifest_digest=sampling.manifest_digest(),
                source_packet_digest="f" * 64,
            )
            record_final_resolution(
                queue_dir,
                sid,
                final_critical=dict(critical_by_id[sid]),
                final_confidence="high",
                current_records_by_slot={
                    slot: records_by_lane[slot][sid] for slot in REVIEWER_SLOTS
                },
                lane_digests=lane_digests,
                campaign_id="campaign",
                sampling_manifest_digest=sampling.manifest_digest(),
                source_packet_digest="f" * 64,
            )
        from evals.calibration.ledger import verify_consensus_ledger

        return verify_consensus_ledger(
            campaign_id="campaign",
            sampling=sampling,
            source_packet_digest="f" * 64,
            lanes=lanes,
            records_by_lane=records_by_lane,
            queue_dir=queue_dir,
            frame_rows=frame_rows,
            protected_root=tmp_path,
        )

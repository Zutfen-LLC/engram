"""Shared builders for #206 tests: frame rows, receipts, split, identity."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

from engram.assessment_schema import AssessmentContract
from evals.admission.schema import digest
from evals.calibration.fit import AssessmentExecutionReceipt
from evals.calibration.freeze import FrameRow, SamplingManifest, SplitManifest, TargetIdentity

NOW = datetime(2026, 9, 10, tzinfo=UTC)


def provider_metadata_for(
    model: str,
    *,
    request_id: str = "req-206-0001",
    response_id: str = "resp-206-0001",
):
    """FIX-R6-2 test fixtures: a digest-bound provider metadata artifact
    that mechanically derives the given observed identity (the synthetic
    provider honestly reports whatever model/IDs the fixture observed)."""
    from evals.calibration.provider_metadata import ProviderMetadataArtifact

    return ProviderMetadataArtifact.capture(
        provider="synthetic-provider-206",
        raw_metadata={
            "model": model,
            "request_id": request_id,
            "response_id": response_id,
        },
    )


def build_raw_response(
    sample_id: str,
    fields: dict,
    *,
    reviewer_confidence: str = "medium",
) -> bytes:
    """FIX-R4-2 test fixtures: a model response whose bytes carry the judgment."""
    return json.dumps(
        {
            "sample_id": sample_id,
            "outcome": "judged",
            "judgment": {
                "fields": dict(fields),
                "reviewer_confidence": reviewer_confidence,
            },
        }
    ).encode()


def build_raw_refusal(sample_id: str, error_code: str = "refused_by_model") -> bytes:
    """FIX-R4-2 test fixtures: an explicit refusal in response bytes."""
    return json.dumps(
        {"sample_id": sample_id, "outcome": "refused", "error_code": error_code}
    ).encode()


def execution_receipt_for(
    lane_root: Path,
    reviewer: object,
    sample_id: str,
    *,
    request_generation: int,
    campaign_id: str = "campaign",
    executor_status: str = "completed",
    identity_source: str = "provider_metadata",
    executor_identity: str = "synthetic-executor-206",
    provider_request_id: str | None = "req-206-0001",
    provider_response_id: str | None = "resp-206-0001",
    provider_metadata_raw: dict | None = None,
    **identity_overrides: str,
) -> object:
    """Build a truthful execution receipt from OBSERVED executor metadata.

    FIX-R5-1 / FIX-R6-2 test fixtures: the actual identity is supplied by
    the (synthetic) executor as OBSERVED values — ``reviewer`` is only
    consulted for the DEFAULT observed values (the synthetic executor was
    configured to run exactly this lane's model), and every adversarial
    test overrides one observed field to a genuinely different value. The
    receipt can never be built by copying the expected identity silently:
    overrides are explicit executor observations.

    FIX-R6-2: for ``identity_source == "provider_metadata"`` the receipt is
    backed by a digest-bound provider metadata artifact whose bytes derive
    the observed model/request/response IDs. The default artifact carries
    the OBSERVED identity (post-override); supply ``provider_metadata_raw``
    to forge a metadata/observed disagreement.
    """
    from evals.calibration.ingestion import observe_execution
    from evals.calibration.provider_metadata import ProviderMetadataArtifact

    observed = {
        "actual_reviewer_slot": reviewer.reviewer_slot,  # type: ignore[attr-defined]
        "actual_reviewer_family": reviewer.reviewer_family,  # type: ignore[attr-defined]
        "actual_provider_model_identifier": reviewer.provider_model_identifier,  # type: ignore[attr-defined]
        "actual_configuration_digest": reviewer.reviewer_config_digest,  # type: ignore[attr-defined]
        "actual_prompt_digest": reviewer.prompt_digest,  # type: ignore[attr-defined]
    }
    observed.update(identity_overrides)
    artifact: object = None
    if identity_source == "provider_metadata":
        raw = dict(provider_metadata_raw or {})
        raw.setdefault("model", observed["actual_provider_model_identifier"])
        raw.setdefault("request_id", provider_request_id or "req-206-0001")
        raw.setdefault("response_id", provider_response_id or "resp-206-0001")
        artifact = ProviderMetadataArtifact.capture(
            provider="synthetic-provider-206", raw_metadata=raw
        )
    return observe_execution(
        lane_root,
        campaign_id=campaign_id,
        sample_id=sample_id,
        request_generation=request_generation,
        executor_status=executor_status,  # type: ignore[arg-type]
        identity_source=identity_source,  # type: ignore[arg-type]
        executor_identity=executor_identity,
        provider_metadata_artifact=artifact,  # type: ignore[arg-type]
        executed_at=NOW,
        **observed,  # type: ignore[arg-type]
    )


def build_identity() -> TargetIdentity:
    return TargetIdentity(
        campaign_id="campaign",
        campaign_tooling_repo_sha="a" * 40,
        assessment_schema_version="engram.assessment.v1",
        assessment_code_version="assessment-engine-v1",
        prompt_version="engram.assess.2",
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


def write_assessment_evidence(
    tmp_path: Path,
    ids: tuple[str, ...],
    frame: list[FrameRow],
    identity: TargetIdentity,
    contract: AssessmentContract,
    sampling: SamplingManifest,
) -> tuple[Path, str]:
    """FIX-R4-5 test fixtures: write a protected assessment-evidence artifact.

    Builds the same ``engram-calibration-assessment-evidence-v1`` envelope
    the legacy #202 path consumes (target/contract/sampling/frame bound,
    one execution per sampled item, self-consistent receipt digests) and
    returns ``(path, sha256)`` for the consensus front door.
    """
    import hashlib
    import json as _json

    from evals.calibration.freeze import SamplingManifest  # noqa: F401 (re-assurance)

    receipts = build_receipts(ids, frame, identity, contract)
    envelope = {
        "evidence_schema": "engram-calibration-assessment-evidence-v1",
        "target_identity_digest": identity.identity_digest(),
        "sampling_manifest_digest": sampling.manifest_digest(),
        "assessment_contract_digest": digest(contract.model_dump(mode="json")),
        "frame_digest": sampling.frame_digest,
        "executions": [r.model_dump(mode="json") for r in receipts],
    }
    payload = _json.dumps(envelope, sort_keys=True, separators=(",", ":")).encode()
    path = tmp_path / "assessment-evidence.json"
    path.write_bytes(payload)
    return path, hashlib.sha256(payload).hexdigest()


def build_verified_ledger(
    ids: tuple[str, ...],
    critical_by_id: dict[str, dict],
    origin: str = "cross_model_consensus",
    *,
    sampling: SamplingManifest | None = None,
    split: SplitManifest | None = None,
    expected_split_digest: str | None = None,
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
    from typing import cast

    from evals.admission.schema import digest as _digest
    from evals.calibration.consensus import (
        CONSENSUS_PROTOCOL_VERSION,
        REVIEWER_FAMILIES,
        REVIEWER_SLOTS,
        ExecutionReceipt,
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
    from evals.calibration.freeze import protected_frame_digest

    frame_rows_list = build_frame_rows(ids)
    if sampling is None:
        sampling = SamplingManifest(
            campaign_id="campaign",
            target_identity_digest="1" * 64,
            # FIX-R4-3: derived from the actual frame rows so the canonical
            # frozen-frame validator can verify them at the ledger boundary.
            frame_digest=protected_frame_digest(frame_rows_list),
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
    retained_split = split
    retained_expected_split = expected_split_digest
    frame_rows = {str(row.sample_id): row for row in frame_rows_list}
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
        # FIX-R4-1: emit immutable request batches first so every record can
        # bind to an ACTUAL emitted request item (freeze enforces this).
        from evals.calibration.ingestion import LaneSession
        from evals.calibration.model_lanes import NeutralModelPacket
        from evals.calibration.review import _packet_file_payload

        cases = [
            {
                "sample_id": sid,
                "content": f"content-{sid}",
                "governed_kind": "fact",
                "source_type": "manual",
                "review_status": "active",
                "assertion_mode": "unknown",
                "origin": "unknown",
                "risk": "unknown",
                "evidence_state": "unknown",
                "age_days": 5,
                "age_bucket": "lt_7d",
                "input_size_bucket": "small",
            }
            for sid in ids
        ]
        packet = NeutralModelPacket(
            packet_id="campaign-blind-v2",
            sampling_manifest_digest=sampling.manifest_digest(),
            guide_version="engram-calibration-guide-157-v1",
            reviewer_hint="neutral_model_review",
            cases=cases,
            source_packet_digest="f" * 64,
        )
        packet_dir = tmp_path / "packet"
        packet_dir.mkdir(parents=True, exist_ok=True)
        packet_payload = _packet_file_payload(packet)
        packet_path = packet_dir / "campaign-blind-v2.neutral.json"
        write_protected_file(packet_path, packet_payload)
        packet_manifest = packet_dir / "neutral-packet-manifest.json"
        write_protected_file(
            packet_manifest,
            (
                json.dumps(
                    {packet_path.name: hashlib.sha256(packet_payload).hexdigest()},
                    sort_keys=True,
                    indent=2,
                )
                + "\n"
            ).encode(),
        )
        for slot in REVIEWER_SLOTS:
            session = LaneSession.init(
                tmp_path,
                reviewer=reviewers[slot],
                campaign_id="campaign",
                sampling=sampling,
                source_packet_digest="f" * 64,
                neutral_packet_path=packet_path,
                neutral_packet_manifest=packet_manifest,
            )
            session.emit_requests(packet_path, sampling=sampling, manifest_path=packet_manifest)
        for slot in REVIEWER_SLOTS:
            for sample_id in ids:
                fields = dict(critical_by_id[sample_id])
                if adjudicate and slot == "model_b":
                    fields = dict(fields)
                    fields["expected_kind"] = "decision"  # guaranteed disagreement
                judgment = ModelJudgment(fields=fields, reviewer_confidence="medium")
                import json as _json

                from evals.calibration.reviewer_instructions import RESPONSE_PARSER_VERSION

                raw = _json.dumps(
                    {
                        "sample_id": sample_id,
                        "outcome": "judged",
                        "judgment": {"fields": dict(fields), "reviewer_confidence": "medium"},
                    }
                ).encode()
                from tests.test_calibration_206_helpers import execution_receipt_for

                execution = cast(
                    "ExecutionReceipt",
                    execution_receipt_for(
                        tmp_path / "lanes" / slot,
                        reviewers[slot],
                        sample_id,
                        request_generation=1,
                        campaign_id="campaign",
                        executor_status="completed",
                    ),
                )
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
                    execution=execution,
                    request_generation=execution.request_generation,
                    request_item_digest=execution.request_item_digest,
                    parser_version=RESPONSE_PARSER_VERSION,
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
                neutral_packet_sha256=hashlib.sha256(packet_payload).hexdigest(),
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
            split=retained_split,
            expected_split_digest=retained_expected_split,
        )

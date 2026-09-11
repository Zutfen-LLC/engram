"""Pre-review campaign CLI for #202 (ENG-CALIBRATION-001F).

Available subcommands:
  freeze-target   write the target identity and frozen evidence floors
  sample          write protected exact sampling/split artifacts and a public summary
  packets         emit blind reviewer packets from protected sample membership
  model-packet    project the frozen blind packet into a neutral model packet (#206)
  model-lane-init    bind one lane to one frozen reviewer identity (#206 FIX-6)
  model-lane-request emit only this lane's pending neutral case requests (#206 FIX-6)
  model-lane-ingest  mechanically ingest structured model responses (#206 FIX-6)
  model-lane-status  completion/missing/failure counts for one lane (#206 FIX-6)
  sub-lane-init     initialize one lane in operator-attested subscription-UI mode (#209)
  sub-batches       export deterministic paste-ready logical review batches (#209)
  sub-batch-show    print one batch's paste-ready prompt (#209)
  sub-import        ingest one verbatim subscription-UI batch response (#209)
  freeze-model-lane  freeze one completed frontier-model reviewer lane (#206)
  model-report    public-safe correlation report after all three lanes freeze (#206)
  human-queue     build the mandatory human queue from frozen lanes (#206)

Reviewer ingestion, ledger freezing, fitting, gating, and reporting remain
library-only stop-point operations until real human labels exist.
"""

from __future__ import annotations

import argparse
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from evals.admission.schema import digest
from evals.calibration.consensus import (
    CONSENSUS_PROTOCOL_VERSION,
    REVIEWER_SLOTS,
    ModelReviewRecord,
    ReviewerIdentity,
    build_correlation_report,
)
from evals.calibration.freeze import (
    EXCLUSION_RULES,
    INCLUSION_RULES,
    EvidenceFloors,
    FrameRow,
    SamplingManifest,
    SplitManifest,
    TargetIdentity,
    assign_splits,
    build_frame,
    load_frozen_frame_rows,
    protected_frame_digest,
    public_sampling_summary,
    sample_id_for,
    stratified_sample,
    validate_split_membership,
)
from evals.calibration.human_queue import build_queue, write_queue
from evals.calibration.ingestion import (
    LaneSession,
    init_subscription_lane,
    load_neutral_packet_verified,
)
from evals.calibration.model_lanes import (
    NeutralModelPacket,
    freeze_lane,
    load_frozen_lanes,
    records_by_lane_from_files,
    write_neutral_packet,
)
from evals.calibration.review import (
    BlindPacket,
    build_packets,
    write_packets,
    write_protected_file,
)

CAMPAIGN_ID = "eng-calibration-001f"
DATASET_VERSION = "calibration-157-dogfood-v2"
SAMPLING_SEED = "202-sample-v2"
SPLIT_SEED = "202-split-v2"
DEV_FRACTION = 0.6
COVERAGE_MIN = 8
ALLOCATION_FRACTION = 0.6


def _write_public(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, sort_keys=True, indent=2) + "\n")


def _resolve_provider_config_digest(args: argparse.Namespace) -> str:
    """Return the campaign config identity in the exact production representation.

    Either derive it from the deployed runtime through the very same helper
    ``current_contract()`` uses, or accept an explicitly captured value -- but
    only in production form. A bare 64-hex digest is rejected outright: it is a
    different identity that production calibration would later refuse.
    """
    if getattr(args, "derive_provider_config_digest", False):
        from engram.assessments import assessment_config_version
        from engram.provider_clients import resolve_classification_provider

        return assessment_config_version(resolve_classification_provider())
    supplied = str(args.provider_config_digest)
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", supplied):
        raise SystemExit(
            "provider config digest must be the exact production "
            "AssessmentContract.config_version representation (sha256:<64hex>); "
            f"refusing {supplied!r}"
        )
    return supplied


def cmd_freeze_target(args: argparse.Namespace) -> int:
    identity = TargetIdentity(
        campaign_id=CAMPAIGN_ID,
        campaign_tooling_repo_sha=args.campaign_tooling_repo_sha,
        assessment_schema_version="engram.assessment.v1",
        assessment_code_version="assessment-engine-v1",
        prompt_version="engram.assess.1",
        provider_adapter="openai",
        provider_model="deepseek-ai/DeepSeek-V4-Flash",
        provider_config_digest=_resolve_provider_config_digest(args),
        provider_params={"temperature": 0, "max_tokens": 1024, "input_limit": 16000},
        assessment_policy_version="assessment-selection-v1",
        calibration_artifact_schema_version="engram.calibration-profiles-v1",
        calibration_dataset_version=DATASET_VERSION,
        label_guide_version="engram-calibration-guide-157-v1",
        canonicalization_version="assessment-evidence-manifest-v1",
        dimensions=("taxonomy", "retention", "epistemic"),
    )
    floors = EvidenceFloors(
        campaign_id=CAMPAIGN_ID,
        total_reviewed_min=300,
        per_dimension_labeled_min=150,
        per_dimension_non_unknown_fraction_min=0.50,
        holdout_min=100,
        holdout_per_profile_min=10,
        holdout_calibrated_brier_max=0.25,
        holdout_calibrated_ece_max=0.15,
        high_consequence_reviewed_min=20,
        per_bin_support_min=50,
        per_stratum_min=10,
    )
    payload = {
        "target_identity": identity.model_dump(mode="json"),
        "target_identity_digest": identity.identity_digest(),
        "floors": floors.model_dump(mode="json"),
    }
    write_protected_file(
        args.output,
        (json.dumps(payload, sort_keys=True, indent=2) + "\n").encode(),
    )
    return 0


def cmd_sample(args: argparse.Namespace) -> int:
    frame_data = json.loads(Path(args.frame).read_text())
    rows = frame_data["rows"]
    content_by_uuid = frame_data["content_by_uuid"]
    snapshot_as_of = datetime.fromisoformat(frame_data["snapshot_as_of"].replace("Z", "+00:00"))
    frame, excluded = build_frame(
        rows, content_by_uuid=content_by_uuid, snapshot_as_of=snapshot_as_of
    )
    sample_ids, stratum_counts, coverage = stratified_sample(
        frame,
        campaign_id=CAMPAIGN_ID,
        sampling_seed=SAMPLING_SEED,
        coverage_min=COVERAGE_MIN,
        allocation_fraction=ALLOCATION_FRACTION,
    )
    hash_by_id = {sample_id_for(row.item_uuid): row.content_hash for row in frame}
    sampling = SamplingManifest(
        campaign_id=CAMPAIGN_ID,
        target_identity_digest=frame_data["target_identity_digest"],
        frame_digest=protected_frame_digest(frame),
        snapshot_sha256=frame_data["snapshot_sha256"],
        snapshot_as_of=snapshot_as_of,
        sampling_seed=SAMPLING_SEED,
        inclusion_rules=INCLUSION_RULES,
        exclusion_rules=EXCLUSION_RULES,
        source_row_counts={
            "eligible_frame": len(frame),
            "excluded_total": sum(excluded.values()),
            **{f"excluded_{key}": value for key, value in excluded.items()},
        },
        stratum_counts=stratum_counts,
        coverage_dimensions=coverage,
        sample_ids=tuple(sample_ids),
        sample_hashes=tuple(hash_by_id[sid] for sid in sample_ids),
    )
    dev_ids, holdout_ids, checks, groups = assign_splits(
        frame,
        sample_ids=sample_ids,
        campaign_id=CAMPAIGN_ID,
        split_seed=SPLIT_SEED,
        dev_fraction=DEV_FRACTION,
    )
    split = SplitManifest(
        campaign_id=CAMPAIGN_ID,
        sampling_manifest_digest=sampling.manifest_digest(),
        sampling_membership_digest=digest(sorted(sample_ids)),
        split_seed=SPLIT_SEED,
        dev_fraction=DEV_FRACTION,
        grouping=("content_hash", "normalized_text", "source_ref", "root_ref", "session_ref"),
        dev_ids=tuple(dev_ids),
        holdout_ids=tuple(holdout_ids),
        leakage_checks=checks,
    )
    validate_split_membership(sampling, split)

    protected = Path(args.protected_dir)
    frame_by_id = {sample_id_for(row.item_uuid): row for row in frame}
    sample_payload = {
        "samples": [
            {
                "sample_id": sid,
                "content": content_by_uuid[frame_by_id[sid].item_uuid],
                **frame_by_id[sid].model_dump(mode="json", exclude={"item_uuid"}),
            }
            for sid in sample_ids
        ]
    }
    artifacts = {
        "frame.json": [row.model_dump(mode="json") for row in frame],
        "sampling-manifest.json": sampling.model_dump(mode="json"),
        "split-manifest.json": split.model_dump(mode="json"),
        "duplicate-groups.json": groups,
        "samples.json": sample_payload,
    }
    for name, payload in artifacts.items():
        write_protected_file(
            protected / name,
            (json.dumps(payload, sort_keys=True, indent=2) + "\n").encode(),
        )
    _write_public(args.manifest, public_sampling_summary(sampling, split))
    return 0


def cmd_packets(args: argparse.Namespace) -> int:
    sampling = SamplingManifest.model_validate(json.loads(Path(args.sampling_manifest).read_text()))
    samples = json.loads(Path(args.samples).read_text())["samples"]
    packets = build_packets(
        sampling=sampling,
        samples=samples,
        packet_id=f"{CAMPAIGN_ID}-blind-v2",
    )
    manifest = write_packets(packets, Path(args.protected_dir))
    print(json.dumps({"packets": manifest}, sort_keys=True))
    return 0


def cmd_model_packet(args: argparse.Namespace) -> int:
    """Project the frozen blind packet into the neutral model-review packet."""
    blind = BlindPacket.model_validate(json.loads(Path(args.blind_packet).read_text()))
    neutral = NeutralModelPacket.from_blind(blind, protocol_version=CONSENSUS_PROTOCOL_VERSION)
    if neutral.sampling_manifest_digest != blind.sampling_manifest_digest:
        raise SystemExit("neutral_packet_sampling_manifest_mismatch")
    manifest = write_neutral_packet(neutral, Path(args.protected_dir))
    print(json.dumps({"neutral_packet": manifest}, sort_keys=True))
    return 0


def cmd_freeze_model_lane(args: argparse.Namespace) -> int:
    """Freeze one completed reviewer lane after exact membership is proven."""
    sampling = SamplingManifest.model_validate(json.loads(Path(args.sampling_manifest).read_text()))
    reviewer = ReviewerIdentity.model_validate(json.loads(Path(args.reviewer_identity).read_text()))
    lane = freeze_lane(
        protected_root=Path(args.protected_dir),
        reviewer=reviewer,
        campaign_id=CAMPAIGN_ID,
        sampling=sampling,
        source_packet_digest=args.source_packet_digest,
    )
    print(json.dumps({"lane_digest": lane.lane_digest(), "cases": len(lane.sample_ids)}))
    return 0


def cmd_model_report(args: argparse.Namespace) -> int:
    """Build the public-safe correlation report after all three lanes freeze.

    FIX-R3-4: the frozen marginal-coverage audit is MANDATORY on this path —
    ``--frame`` is required, frame membership must be exact, and the report
    uses the same ``select_audit_sample_with_coverage`` the human queue
    uses. The global-HMAC fallback is unreachable from campaign commands.
    """
    sampling = SamplingManifest.model_validate(json.loads(Path(args.sampling_manifest).read_text()))
    reviewers = {
        slot: ReviewerIdentity.model_validate(json.loads(path.read_text()))
        for slot, path in zip(REVIEWER_SLOTS, args.reviewer_identities, strict=True)
    }
    lanes = load_frozen_lanes(
        Path(args.protected_dir),
        campaign_id=CAMPAIGN_ID,
        sampling=sampling,
        source_packet_digest=args.source_packet_digest,
        reviewers=reviewers,
    )
    records_by_lane = records_by_lane_from_files(Path(args.protected_dir))
    frame_rows = _load_frame_rows(args.frame, sampling)
    report = build_correlation_report(
        campaign_id=CAMPAIGN_ID,
        lanes=lanes,
        records_by_lane=records_by_lane,
        sampling=sampling,
        source_packet_digest=args.source_packet_digest,
        frame_rows=frame_rows,
    )
    if not report.aggregate_by_axis:
        raise SystemExit("model_report_requires_frame_aggregates")
    _write_public(Path(args.report), report.model_dump(mode="json"))
    print(
        json.dumps(
            {
                "consensus_count": report.consensus_count,
                "human_queue_count_before_audit": report.human_queue_count_before_audit,
                "audit_count": report.audit_count,
                "audit_algorithm": report.audit.get(
                    "audit_selection_algorithm", "marginal-coverage-greedy-hmac-v1"
                ),
                "total_human_workload": report.total_human_workload,
            }
        )
    )
    return 0


def cmd_human_queue(args: argparse.Namespace) -> int:
    """Build the mandatory human queue from three frozen lanes (pre-audit).

    FIX-R3-4: ``--frame`` is required; audit selection is the frozen
    marginal-coverage algorithm, identical to ``model-report``.
    """
    sampling = SamplingManifest.model_validate(json.loads(Path(args.sampling_manifest).read_text()))
    reviewers = {
        slot: ReviewerIdentity.model_validate(json.loads(path.read_text()))
        for slot, path in zip(REVIEWER_SLOTS, args.reviewer_identities, strict=True)
    }
    load_frozen_lanes(
        Path(args.protected_dir),
        campaign_id=CAMPAIGN_ID,
        sampling=sampling,
        source_packet_digest=args.source_packet_digest,
        reviewers=reviewers,
    )
    records_by_lane = records_by_lane_from_files(Path(args.protected_dir))
    frame_rows = _load_frame_rows(args.frame, sampling)
    queue = build_queue(
        campaign_id=CAMPAIGN_ID,
        sampling=sampling,
        source_packet_digest=args.source_packet_digest,
        records_by_lane=records_by_lane,
        frame_rows=frame_rows,
    )
    path = write_queue(queue, Path(args.queue_dir))
    print(json.dumps({"queue_path": str(path), "queue_size": len(queue.entries)}))
    return 0


def _queue_context(
    args: argparse.Namespace,
) -> tuple[
    SamplingManifest,
    dict[str, str],
    dict[str, dict[str, ModelReviewRecord]],
]:
    """Shared loader for the queue-operate commands (FIX-R3-9)."""
    sampling = SamplingManifest.model_validate(json.loads(Path(args.sampling_manifest).read_text()))
    reviewers = {
        slot: ReviewerIdentity.model_validate(json.loads(path.read_text()))
        for slot, path in zip(REVIEWER_SLOTS, args.reviewer_identities, strict=True)
    }
    lanes = load_frozen_lanes(
        Path(args.protected_dir),
        campaign_id=CAMPAIGN_ID,
        sampling=sampling,
        source_packet_digest=args.source_packet_digest,
        reviewers=reviewers,
    )
    lane_digests: dict[str, str] = {
        lane.reviewer.reviewer_slot: lane.lane_digest() for lane in lanes
    }
    records_by_lane = records_by_lane_from_files(Path(args.protected_dir))
    return sampling, lane_digests, records_by_lane


def cmd_queue_status(args: argparse.Namespace) -> int:
    """FIX-R3-9: queue progress overview (counts only, no protected IDs)."""
    from evals.calibration.human_queue import export_queue_evidence

    export = export_queue_evidence(Path(args.queue_dir))
    counts = dict(export["counts"])
    counts.pop("unresolved_sample_ids", None)
    print(json.dumps(counts, sort_keys=True))
    return 0


def cmd_queue_case(args: argparse.Namespace) -> int:
    """FIX-R3-9: materialize one queued case for the operator.

    Stage 1 (blind): only the original case evidence and queue reasons are
    shown — never the model votes. The exact neutral packet case view is
    reproduced from the byte-verified packet (which the lane authority
    binds), so the operator sees the same evidence the reviewers saw.
    """
    from evals.calibration.human_queue import (
        load_final_resolution,
        load_initial_judgment,
        load_reveal_event,
        require_queued_sample,
    )

    sampling, _, _ = _queue_context(args)
    require_queued_sample(
        Path(args.queue_dir),
        args.sample_id,
        campaign_id=CAMPAIGN_ID,
        sampling_manifest_digest=sampling.manifest_digest(),
        source_packet_digest=args.source_packet_digest,
    )
    manifest_path = (
        Path(args.neutral_packet_manifest)
        if args.neutral_packet_manifest
        else Path(args.neutral_packet).parent / "neutral-packet-manifest.json"
    )
    packet, _ = load_neutral_packet_verified(Path(args.neutral_packet), manifest_path=manifest_path)
    case = next((c for c in packet.cases if c["sample_id"] == args.sample_id), None)
    if case is None:
        raise SystemExit(f"sample {args.sample_id} not in neutral packet")
    initial = load_initial_judgment(Path(args.queue_dir), args.sample_id)
    reveal = load_reveal_event(Path(args.queue_dir), args.sample_id)
    final = load_final_resolution(Path(args.queue_dir), args.sample_id)
    payload: dict[str, object] = {"sample_id": args.sample_id, "case": case}
    if initial is not None:
        payload["initial_recorded"] = True
    if reveal is not None and args.show_votes:
        # votes are only ever shown after the initial judgment exists
        payload["model_record_digests"] = list(reveal.revealed_record_digests)
    if final is not None:
        payload["final_recorded"] = True
    print(json.dumps(payload, sort_keys=True))
    return 0


def cmd_queue_initial(args: argparse.Namespace) -> int:
    """FIX-R3-9 stage 1: record the independent initial judgment (blind)."""
    from evals.calibration.human_queue import (
        HumanQueueJudgment,
        require_queued_sample,
        save_initial_judgment,
    )

    sampling, _, _ = _queue_context(args)
    entry = require_queued_sample(
        Path(args.queue_dir),
        args.sample_id,
        campaign_id=CAMPAIGN_ID,
        sampling_manifest_digest=sampling.manifest_digest(),
        source_packet_digest=args.source_packet_digest,
    )
    critical = json.loads(Path(args.critical).read_text())
    judgment = {
        "protocol_version": CONSENSUS_PROTOCOL_VERSION,
        "campaign_id": CAMPAIGN_ID,
        "sampling_manifest_digest": sampling.manifest_digest(),
        "source_packet_digest": args.source_packet_digest,
        "sample_id": args.sample_id,
        "adjudicator_ref": args.adjudicator,
        "queue_reasons": list(entry.reasons),
        "audit_selected": "audit_selected" in entry.reasons,
        "initial_critical": critical,
        "initial_confidence": args.confidence,
        "initial_captured_at": datetime.now(UTC).isoformat(),
    }
    path = save_initial_judgment(HumanQueueJudgment.model_validate(judgment), Path(args.queue_dir))
    print(json.dumps({"initial_judgment": str(path)}))
    return 0


def cmd_queue_reveal(args: argparse.Namespace) -> int:
    """FIX-R3-9 stage 2: reveal the exact frozen model votes for one case."""
    from evals.calibration.human_queue import require_queued_sample, reveal_model_votes

    sampling, lane_digests, records_by_lane = _queue_context(args)
    require_queued_sample(
        Path(args.queue_dir),
        args.sample_id,
        campaign_id=CAMPAIGN_ID,
        sampling_manifest_digest=sampling.manifest_digest(),
        source_packet_digest=args.source_packet_digest,
    )
    event = reveal_model_votes(
        Path(args.queue_dir),
        args.sample_id,
        current_records_by_slot={
            slot: records_by_lane[slot][args.sample_id] for slot in REVIEWER_SLOTS
        },
        lane_digests=tuple(lane_digests[slot] for slot in REVIEWER_SLOTS),
        campaign_id=CAMPAIGN_ID,
        sampling_manifest_digest=sampling.manifest_digest(),
        source_packet_digest=args.source_packet_digest,
    )
    # The operator sees the three parsed judgments that were revealed.
    votes: dict[str, Any] = {}
    for slot in REVIEWER_SLOTS:
        record = records_by_lane[slot][args.sample_id]
        if record.judgment is not None:
            votes[slot] = record.judgment.model_dump(mode="json")
        else:
            votes[slot] = {"outcome": record.outcome_status}
    print(
        json.dumps(
            {"revealed_record_digests": list(event.revealed_record_digests), "votes": votes},
            sort_keys=True,
        )
    )
    return 0


def cmd_queue_resolve(args: argparse.Namespace) -> int:
    """FIX-R3-9 stage 3: record the final resolution after adjudication."""
    from evals.calibration.human_queue import record_final_resolution, require_queued_sample

    sampling, lane_digests, records_by_lane = _queue_context(args)
    require_queued_sample(
        Path(args.queue_dir),
        args.sample_id,
        campaign_id=CAMPAIGN_ID,
        sampling_manifest_digest=sampling.manifest_digest(),
        source_packet_digest=args.source_packet_digest,
    )
    critical = json.loads(Path(args.critical).read_text())
    record_final_resolution(
        Path(args.queue_dir),
        args.sample_id,
        final_critical=critical,
        final_confidence=args.confidence,
        current_records_by_slot={
            slot: records_by_lane[slot][args.sample_id] for slot in REVIEWER_SLOTS
        },
        lane_digests=tuple(lane_digests[slot] for slot in REVIEWER_SLOTS),
        campaign_id=CAMPAIGN_ID,
        sampling_manifest_digest=sampling.manifest_digest(),
        source_packet_digest=args.source_packet_digest,
        note=args.note,
    )
    print(json.dumps({"resolved": args.sample_id}))
    return 0


def cmd_queue_escalate(args: argparse.Namespace) -> int:
    """FIX-R3-9: mechanically materialize the audit-escalation expansion."""
    from evals.calibration.consensus import classify_case
    from evals.calibration.human_queue import materialize_audit_escalation

    sampling, _, records_by_lane = _queue_context(args)
    classifications = {
        sample_id: classify_case(
            {slot: records_by_lane[slot][sample_id] for slot in REVIEWER_SLOTS}
        )
        for sample_id in sampling.sample_ids
    }
    consensus_ids = [sid for sid, c in classifications.items() if c["consensus"]]
    result = materialize_audit_escalation(
        Path(args.queue_dir),
        records_by_lane=records_by_lane,
        consensus_ids=consensus_ids,
    )
    print(json.dumps(result, sort_keys=True, default=str))
    return 0


def _load_frame_rows(frame_path: str | None, sampling: SamplingManifest) -> dict[str, FrameRow]:
    """Load protected frame rows for marginal-coverage audit selection.

    FIX-R3-4: fails closed. The frame is mandatory on campaign paths and must
    contain EVERY frozen sampled ID — a partial mapping is rejected instead
    of silently degrading the audit selection.

    FIX-R4-3: delegates to the ONE canonical frozen-frame validator
    (``load_frozen_frame_rows`` -> ``verify_frozen_frame``) shared with the
    ledger authority boundary, so the CLI and the verifier can never diverge
    on what counts as the frozen frame.
    """
    if not frame_path:
        raise SystemExit("--frame is required for the frozen campaign audit path")
    try:
        return load_frozen_frame_rows(Path(frame_path), sampling)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc


def _load_lane_session(args: argparse.Namespace) -> tuple[LaneSession, SamplingManifest]:
    sampling = SamplingManifest.model_validate(json.loads(Path(args.sampling_manifest).read_text()))
    session = LaneSession(Path(args.protected_dir), args.reviewer_slot)
    if session.source_packet_digest != args.source_packet_digest:
        raise SystemExit("lane_source_packet_digest_mismatch")
    return session, sampling


def cmd_model_lane_init(args: argparse.Namespace) -> int:
    """Bind one lane to one frozen ReviewerIdentity (FIX-6, FIX-R3-3)."""
    sampling = SamplingManifest.model_validate(json.loads(Path(args.sampling_manifest).read_text()))
    reviewer = ReviewerIdentity.model_validate(json.loads(Path(args.reviewer_identity).read_text()))
    LaneSession.init(
        Path(args.protected_dir),
        reviewer=reviewer,
        campaign_id=CAMPAIGN_ID,
        sampling=sampling,
        source_packet_digest=args.source_packet_digest,
        neutral_packet_path=Path(args.neutral_packet),
        neutral_packet_manifest=Path(args.neutral_packet_manifest),
    )
    print(
        json.dumps(
            {
                "reviewer_slot": reviewer.reviewer_slot,
                "lane_identity_digest": reviewer.lane_identity_digest(),
            }
        )
    )
    return 0


def cmd_model_lane_request(args: argparse.Namespace) -> int:
    """Emit only this lane's pending neutral case requests (JSONL, FIX-R3-6)."""
    session, sampling = _load_lane_session(args)
    path = session.emit_requests(
        Path(args.neutral_packet),
        sampling=sampling,
        manifest_path=Path(args.neutral_packet_manifest) if args.neutral_packet_manifest else None,
    )
    status = session.status(sampling)
    print(
        json.dumps(
            {
                "requests_path": str(path),
                "pending": status["expected"] - status["accepted"],
                "accepted": status["accepted"],
                "expected": status["expected"],
            }
        )
    )
    return 0


def cmd_model_lane_ingest(args: argparse.Namespace) -> int:
    """Mechanically ingest structured model responses (JSONL) into the lane."""
    session, sampling = _load_lane_session(args)
    result = session.ingest_jsonl(Path(args.responses), sampling=sampling)
    print(json.dumps(result, sort_keys=True))
    return 0


def cmd_model_lane_status(args: argparse.Namespace) -> int:
    """Report completion / missing / failure counts for one lane."""
    session, sampling = _load_lane_session(args)
    status = session.status(sampling)
    status.pop("next_missing")  # protected sample IDs: never print
    print(json.dumps(status, sort_keys=True))
    return 0


def cmd_sub_lane_init(args: argparse.Namespace) -> int:
    """Initialize one lane in #209 subscription-UI provenance mode.

    FIX-1: requires and freezes the exact user-visible selected model
    name/version and the operator reference BEFORE any output exists.
    """
    from evals.calibration.subscription_ui import subscription_reviewer_identity

    sampling = SamplingManifest.model_validate(json.loads(Path(args.sampling_manifest).read_text()))
    reviewer = subscription_reviewer_identity(
        args.reviewer_slot,
        reviewer_config_digest=args.reviewer_config_digest,
        prompt_digest=_labeling_prompt_digest(),
    )
    session = init_subscription_lane(
        Path(args.protected_dir),
        reviewer=reviewer,
        campaign_id=CAMPAIGN_ID,
        sampling=sampling,
        source_packet_digest=args.source_packet_digest,
        neutral_packet_path=Path(args.neutral_packet),
        neutral_packet_manifest=Path(args.neutral_packet_manifest),
        user_visible_model_name=args.visible_model_name,
        operator_reference=args.operator,
    )
    from evals.calibration.subscription_ui import load_subscription_lane_authority

    authority = load_subscription_lane_authority(session.lane_root)
    print(
        json.dumps(
            {
                "reviewer_slot": reviewer.reviewer_slot,
                "provenance_mode": "operator_attested_subscription_ui",
                "service": authority.service,
                "reviewer_family": authority.reviewer_family,
                "frozen_user_visible_model_name": authority.user_visible_model_name,
                "lane_identity_digest": reviewer.lane_identity_digest(),
                "subscription_authority_digest": authority.authority_digest,
            }
        )
    )
    return 0


def _labeling_prompt_digest() -> str:
    from evals.calibration.ingestion import labeling_instructions_digest

    return labeling_instructions_digest()


def _run_sub_prepare(args: argparse.Namespace) -> int:
    """Campaign preparation (#209 FIX-4): verify the three frozen lanes,
    emit canonical per-lane #206 request evidence (idempotently), export
    the shared deterministic logical paste batches, bind both sides."""
    from evals.calibration import subscription_ui

    sampling = SamplingManifest.model_validate(json.loads(Path(args.sampling_manifest).read_text()))
    result = subscription_ui.prepare_subscription_campaign(
        Path(args.protected_dir),
        campaign_id=CAMPAIGN_ID,
        sampling=sampling,
        source_packet_digest=args.source_packet_digest,
        neutral_packet_path=Path(args.neutral_packet),
        neutral_packet_manifest=Path(args.neutral_packet_manifest),
    )
    print(json.dumps(result, sort_keys=True))
    return 0


def cmd_sub_prepare(args: argparse.Namespace) -> int:
    return _run_sub_prepare(args)


def cmd_sub_batches(args: argparse.Namespace) -> int:
    """Export the deterministic paste-ready logical review batches (#209).

    FIX-4: performs/verifies the same campaign preparation as
    ``sub-prepare`` — the workflow is self-contained either way.
    """
    return _run_sub_prepare(args)


def cmd_sub_batch_show(args: argparse.Namespace) -> int:
    """Print one batch's paste-ready prompt (maintainer handoff, #209)."""
    from evals.calibration import subscription_ui

    sampling = SamplingManifest.model_validate(json.loads(Path(args.sampling_manifest).read_text()))
    subscription_ui.verify_review_batches(
        Path(args.protected_dir), sampling=sampling, source_packet_digest=args.source_packet_digest
    )
    path = subscription_ui.batch_prompt_path(Path(args.protected_dir), args.batch_id)
    if not path.is_file():
        raise SystemExit("subscription_batch_not_in_canonical_manifest")
    print(path.read_text(), end="")
    return 0


def cmd_sub_import(args: argparse.Namespace) -> int:
    """Ingest one verbatim subscription-UI batch response (#209).

    FIX-1: no service/model claims are accepted at import time — the
    attestation is built exclusively from the lane's frozen visible-model
    authority. FIX-4: ``--source-packet-digest`` is used in the
    authoritative import verification.
    """
    from evals.calibration import subscription_ui

    sampling = SamplingManifest.model_validate(json.loads(Path(args.sampling_manifest).read_text()))
    raw_response = Path(args.raw_response).read_text()
    result = subscription_ui.import_batch_response(
        Path(args.protected_dir),
        reviewer_slot=args.reviewer_slot,
        batch_id=args.batch_id,
        raw_response=raw_response,
        sampling=sampling,
        retry_mechanical_failure=args.retry_mechanical_failure,
        source_packet_digest=args.source_packet_digest,
        conversation_reference=args.conversation_reference,
    )
    print(json.dumps(result, sort_keys=True))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    command = sub.add_parser("freeze-target")
    command.add_argument(
        "--campaign-tooling-repo-sha",
        required=True,
        help="tooling revision that generates this freeze (campaign provenance only; "
        "NOT a constraint on the revision later deployed at serving time)",
    )
    config_source = command.add_mutually_exclusive_group(required=True)
    config_source.add_argument(
        "--provider-config-digest",
        help="exact production AssessmentContract.config_version (sha256:<64hex>)",
    )
    config_source.add_argument(
        "--derive-provider-config-digest",
        action="store_true",
        help="derive config identity from the deployed runtime via the production helper",
    )
    command.add_argument("--output", type=Path, required=True)
    command.set_defaults(func=cmd_freeze_target)

    command = sub.add_parser("sample")
    command.add_argument("--frame", required=True)
    command.add_argument("--manifest", type=Path, required=True, help="public aggregate summary")
    command.add_argument("--protected-dir", required=True)
    command.set_defaults(func=cmd_sample)

    command = sub.add_parser("packets")
    command.add_argument("--sampling-manifest", required=True)
    command.add_argument("--samples", required=True)
    command.add_argument("--protected-dir", required=True)
    command.set_defaults(func=cmd_packets)

    command = sub.add_parser(
        "model-packet", help="project the frozen blind packet for model review (#206)"
    )
    command.add_argument("--blind-packet", required=True)
    command.add_argument("--protected-dir", required=True)
    command.set_defaults(func=cmd_model_packet)

    command = sub.add_parser("freeze-model-lane", help="freeze one completed reviewer lane (#206)")
    command.add_argument("--sampling-manifest", required=True)
    command.add_argument("--reviewer-identity", required=True)
    command.add_argument("--source-packet-digest", required=True)
    command.add_argument("--protected-dir", required=True)
    command.set_defaults(func=cmd_freeze_model_lane)

    command = sub.add_parser(
        "model-report", help="public-safe correlation report after three frozen lanes (#206)"
    )
    command.add_argument("--sampling-manifest", required=True)
    command.add_argument(
        "--reviewer-identities", nargs=3, required=True, metavar=("MODEL_A", "MODEL_B", "MODEL_C")
    )
    command.add_argument("--source-packet-digest", required=True)
    command.add_argument("--protected-dir", required=True)
    command.add_argument(
        "--frame",
        required=True,
        help="protected frame.json (MANDATORY: frozen marginal-coverage audit selection)",
    )
    command.add_argument("--report", type=Path, required=True, help="public aggregate report")
    command.set_defaults(func=cmd_model_report)

    command = sub.add_parser(
        "human-queue", help="build the mandatory human queue from frozen lanes (#206)"
    )
    command.add_argument("--sampling-manifest", required=True)
    command.add_argument(
        "--reviewer-identities", nargs=3, required=True, metavar=("MODEL_A", "MODEL_B", "MODEL_C")
    )
    command.add_argument("--source-packet-digest", required=True)
    command.add_argument("--protected-dir", required=True)
    command.add_argument("--queue-dir", required=True)
    command.add_argument(
        "--frame",
        required=True,
        help="protected frame.json (MANDATORY: frozen marginal-coverage audit selection)",
    )
    command.set_defaults(func=cmd_human_queue)

    command = sub.add_parser(
        "model-lane-init", help="bind one lane to one frozen reviewer identity (#206 FIX-6)"
    )
    command.add_argument("--sampling-manifest", required=True)
    command.add_argument("--reviewer-identity", required=True)
    command.add_argument("--reviewer-slot", required=True, choices=list(REVIEWER_SLOTS))
    command.add_argument("--source-packet-digest", required=True)
    command.add_argument(
        "--neutral-packet",
        required=True,
        help="frozen neutral packet file (byte-verified against its manifest at init)",
    )
    command.add_argument(
        "--neutral-packet-manifest",
        required=True,
        help="independently retained neutral-packet-manifest.json",
    )
    command.add_argument("--protected-dir", required=True)
    command.set_defaults(func=cmd_model_lane_init)

    command = sub.add_parser(
        "model-lane-request", help="emit this lane's pending neutral case requests (#206 FIX-6)"
    )
    command.add_argument("--sampling-manifest", required=True)
    command.add_argument("--reviewer-slot", required=True, choices=list(REVIEWER_SLOTS))
    command.add_argument("--neutral-packet", required=True)
    command.add_argument(
        "--neutral-packet-manifest",
        help="independently retained manifest (default: beside the packet)",
    )
    command.add_argument("--source-packet-digest", required=True)
    command.add_argument("--protected-dir", required=True)
    command.set_defaults(func=cmd_model_lane_request)

    command = sub.add_parser(
        "model-lane-ingest", help="ingest structured model responses as JSONL (#206 FIX-6)"
    )
    command.add_argument("--sampling-manifest", required=True)
    command.add_argument("--reviewer-slot", required=True, choices=list(REVIEWER_SLOTS))
    command.add_argument("--responses", required=True, help="JSONL of response objects")
    command.add_argument("--source-packet-digest", required=True)
    command.add_argument("--protected-dir", required=True)
    command.set_defaults(func=cmd_model_lane_ingest)

    command = sub.add_parser(
        "model-lane-status", help="completion/missing/failure counts for one lane (#206 FIX-6)"
    )
    command.add_argument("--sampling-manifest", required=True)
    command.add_argument("--reviewer-slot", required=True, choices=list(REVIEWER_SLOTS))
    command.add_argument("--source-packet-digest", required=True)
    command.add_argument("--protected-dir", required=True)
    command.set_defaults(func=cmd_model_lane_status)

    # -- #209 subscription-UI lanes ---------------------------------------------

    command = sub.add_parser(
        "sub-lane-init",
        help="initialize one lane in operator-attested subscription-UI mode (#209)",
    )
    command.add_argument("--sampling-manifest", required=True)
    command.add_argument("--reviewer-slot", required=True, choices=list(REVIEWER_SLOTS))
    command.add_argument(
        "--reviewer-config-digest",
        required=True,
        help="frozen reviewer configuration digest (sha256:<64hex>)",
    )
    command.add_argument("--source-packet-digest", required=True)
    command.add_argument("--neutral-packet", required=True)
    command.add_argument("--neutral-packet-manifest", required=True)
    command.add_argument(
        "--visible-model-name",
        required=True,
        help="exact visible model display name/version, frozen at init (#209 FIX-1)",
    )
    command.add_argument(
        "--operator",
        required=True,
        help="operator reference attesting the execution and the frozen model selection",
    )
    command.add_argument("--protected-dir", required=True)
    command.set_defaults(func=cmd_sub_lane_init)

    command = sub.add_parser(
        "sub-prepare",
        help="prepare the subscription campaign: verify lanes, emit canonical requests, "
        "export logical paste batches (#209 FIX-4)",
    )
    command.add_argument("--sampling-manifest", required=True)
    command.add_argument("--neutral-packet", required=True)
    command.add_argument("--neutral-packet-manifest", required=True)
    command.add_argument("--source-packet-digest", required=True)
    command.add_argument("--protected-dir", required=True)
    command.set_defaults(func=cmd_sub_prepare)

    command = sub.add_parser(
        "sub-batches",
        help="prepare + export deterministic paste-ready logical review batches (#209)",
    )
    command.add_argument("--sampling-manifest", required=True)
    command.add_argument("--neutral-packet", required=True)
    command.add_argument("--neutral-packet-manifest", required=True)
    command.add_argument("--source-packet-digest", required=True)
    command.add_argument("--protected-dir", required=True)
    command.set_defaults(func=cmd_sub_batches)

    command = sub.add_parser(
        "sub-batch-show",
        help="print one batch's paste-ready prompt for maintainer handoff (#209)",
    )
    command.add_argument("--sampling-manifest", required=True)
    command.add_argument("--source-packet-digest", required=True)
    command.add_argument("--protected-dir", required=True)
    command.add_argument("--batch-id", required=True)
    command.set_defaults(func=cmd_sub_batch_show)

    command = sub.add_parser(
        "sub-import",
        help="ingest one verbatim subscription-UI batch response (#209)",
    )
    command.add_argument("--sampling-manifest", required=True)
    command.add_argument("--reviewer-slot", required=True, choices=list(REVIEWER_SLOTS))
    command.add_argument("--batch-id", required=True)
    command.add_argument(
        "--raw-response", required=True, help="file containing the exact copied raw response"
    )
    command.add_argument("--conversation-reference", default=None)
    command.add_argument(
        "--retry-mechanical-failure",
        action="store_true",
        help="explicit mechanical-failure retry (failed attempt evidence retained)",
    )
    command.add_argument(
        "--source-packet-digest",
        required=True,
        help="verified against the frozen campaign authority during import (#209 FIX-4)",
    )
    command.add_argument("--protected-dir", required=True)
    command.set_defaults(func=cmd_sub_import)

    # -- human queue operation (FIX-R3-9) -------------------------------------

    def _add_queue_common(parser: argparse.ArgumentParser) -> None:
        parser.add_argument("--sampling-manifest", required=True)
        parser.add_argument(
            "--reviewer-identities",
            nargs=3,
            required=True,
            metavar=("MODEL_A", "MODEL_B", "MODEL_C"),
        )
        parser.add_argument("--source-packet-digest", required=True)
        parser.add_argument("--protected-dir", required=True)
        parser.add_argument("--queue-dir", required=True)

    command = sub.add_parser("queue-status", help="human queue progress counts (#206 FIX-R3-9)")
    command.add_argument("--queue-dir", required=True)
    command.set_defaults(func=cmd_queue_status)

    command = sub.add_parser(
        "queue-case", help="show one queued case's blind evidence (#206 FIX-R3-9)"
    )
    _add_queue_common(command)
    command.add_argument("--sample-id", required=True)
    command.add_argument("--neutral-packet", required=True)
    command.add_argument("--neutral-packet-manifest")
    command.add_argument("--show-votes", action="store_true")
    command.set_defaults(func=cmd_queue_case)

    command = sub.add_parser(
        "queue-initial", help="record the independent initial judgment (#206 FIX-R3-9)"
    )
    _add_queue_common(command)
    command.add_argument("--sample-id", required=True)
    command.add_argument("--critical", required=True, help="JSON file of five critical fields")
    command.add_argument("--adjudicator", required=True)
    command.add_argument("--confidence", choices=["low", "medium", "high"], default="medium")
    command.set_defaults(func=cmd_queue_initial)

    command = sub.add_parser(
        "queue-reveal", help="reveal the frozen model votes for one case (#206 FIX-R3-9)"
    )
    _add_queue_common(command)
    command.add_argument("--sample-id", required=True)
    command.set_defaults(func=cmd_queue_reveal)

    command = sub.add_parser("queue-resolve", help="record the final resolution (#206 FIX-R3-9)")
    _add_queue_common(command)
    command.add_argument("--sample-id", required=True)
    command.add_argument("--critical", required=True, help="JSON file of five critical fields")
    command.add_argument(
        "--confidence", choices=["low", "medium", "high", "unknown"], default="high"
    )
    command.add_argument("--note")
    command.set_defaults(func=cmd_queue_resolve)

    command = sub.add_parser(
        "queue-escalate",
        help="materialize the audit-escalation full-human expansion (#206 FIX-R3-9)",
    )
    _add_queue_common(command)
    command.set_defaults(func=cmd_queue_escalate)

    args = parser.parse_args()
    return int(args.func(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main())

"""Pre-review campaign CLI for #202 (ENG-CALIBRATION-001F).

Available subcommands:
  freeze-target   write the target identity and frozen evidence floors
  sample          write protected exact sampling/split artifacts and a public summary
  packets         emit blind reviewer packets from protected sample membership
  model-packet    project the frozen blind packet into a neutral model packet (#206)
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
from datetime import datetime
from pathlib import Path

from evals.admission.schema import digest
from evals.calibration.consensus import (
    CONSENSUS_PROTOCOL_VERSION,
    REVIEWER_SLOTS,
    ReviewerIdentity,
    build_correlation_report,
)
from evals.calibration.freeze import (
    EXCLUSION_RULES,
    INCLUSION_RULES,
    EvidenceFloors,
    SamplingManifest,
    SplitManifest,
    TargetIdentity,
    assign_splits,
    build_frame,
    protected_frame_digest,
    public_sampling_summary,
    sample_id_for,
    stratified_sample,
    validate_split_membership,
)
from evals.calibration.human_queue import build_queue, write_queue
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
    """Build the public-safe correlation report after all three lanes freeze."""
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
    report = build_correlation_report(
        campaign_id=CAMPAIGN_ID,
        lanes=lanes,
        records_by_lane=records_by_lane,
        sampling=sampling,
        source_packet_digest=args.source_packet_digest,
    )
    _write_public(Path(args.report), report.model_dump(mode="json"))
    print(
        json.dumps(
            {
                "consensus_count": report.consensus_count,
                "human_queue_count_before_audit": report.human_queue_count_before_audit,
                "audit_count": report.audit_count,
                "total_human_workload": report.total_human_workload,
            }
        )
    )
    return 0


def cmd_human_queue(args: argparse.Namespace) -> int:
    """Build the mandatory human queue from three frozen lanes (pre-audit)."""
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
    queue = build_queue(
        campaign_id=CAMPAIGN_ID,
        sampling=sampling,
        source_packet_digest=args.source_packet_digest,
        records_by_lane=records_by_lane,
    )
    path = write_queue(queue, Path(args.queue_dir))
    print(json.dumps({"queue_path": str(path), "queue_size": len(queue.entries)}))
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
    command.set_defaults(func=cmd_human_queue)

    args = parser.parse_args()
    return int(args.func(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main())

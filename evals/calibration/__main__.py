"""Campaign CLI for #202 (ENG-CALIBRATION-001F).

Subcommands:
  freeze-target   — write the frozen TargetIdentity + EvidenceFloors (public)
  sample          — build the sampling + split manifests from a protected frame
  packets         — emit blind reviewer packets into a protected directory
  ingest          — validate reviewer label files against a packet (fail closed)
  fit             — fit profiles from DEV labels + captured scores, evaluate holdout
  gate            — run the selection-enable gate against an artifact bundle
  report          — write the public-safe calibration report

Protected inputs/outputs live outside the repository (0700/0600). Public
artifacts contain digests and aggregates only.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from evals.calibration.freeze import (
    EXCLUSION_RULES,
    INCLUSION_RULES,
    EvidenceFloors,
    SamplingManifest,
    SplitManifest,
    TargetIdentity,
    assign_splits,
    build_frame,
    stratified_sample,
)
from evals.calibration.review import (
    build_packets,
    write_packets,
)

# Frozen campaign constants (issue #202).
CAMPAIGN_ID = "eng-calibration-001f"
DATASET_VERSION = "calibration-157-dogfood-v1"
SAMPLING_SEED = "202-sample-v1"
SPLIT_SEED = "202-split-v1"
DEV_FRACTION = 0.6
COVERAGE_MIN = 8
ALLOCATION_FRACTION = 0.6
SNAPSHOT_SHA256 = "REPLACED_AT_CAPTURE"  # bound at capture time


def _write_public(path: Path, payload: dict[str, object]) -> None:
    path.write_text(json.dumps(payload, sort_keys=True, indent=2) + "\n")


def cmd_freeze_target(args: argparse.Namespace) -> int:
    identity = TargetIdentity(
        campaign_id=CAMPAIGN_ID,
        repo_sha=args.repo_sha,
        assessment_schema_version="engram.assessment.v1",
        assessment_code_version="assessment-engine-v1",
        prompt_version="engram.assess.1",
        provider_adapter="openai",
        provider_model="deepseek-ai/DeepSeek-V4-Flash",
        provider_config_digest=args.provider_config_digest,
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
        holdout_min=100,
        high_consequence_reviewed_min=20,
        per_bin_support_min=50,
        per_stratum_min=10,
    )
    _write_public(
        args.output,
        {
            "target_identity": json.loads(identity.model_dump_json()),
            "target_identity_digest": identity.identity_digest(),
            "floors": json.loads(floors.model_dump_json()),
        },
    )
    return 0


def cmd_sample(args: argparse.Namespace) -> int:
    from datetime import datetime

    from evals.calibration.freeze import sample_id_for

    frame_data = json.loads(Path(args.frame).read_text())
    rows = frame_data["rows"]
    content_by_uuid = frame_data["content_by_uuid"]
    snapshot_as_of = datetime.fromisoformat(frame_data["snapshot_as_of"])
    frame, excluded = build_frame(
        rows, content_by_uuid=content_by_uuid, snapshot_as_of=snapshot_as_of
    )
    sample_ids, stratum_counts = stratified_sample(
        frame,
        campaign_id=CAMPAIGN_ID,
        sampling_seed=SAMPLING_SEED,
        coverage_min=COVERAGE_MIN, allocation_fraction=ALLOCATION_FRACTION,
    )
    dev_ids, holdout_ids, checks, groups = assign_splits(
        frame,
        campaign_id=CAMPAIGN_ID,
        split_seed=SPLIT_SEED,
        dev_fraction=DEV_FRACTION,
    )
    hash_by_id = {sample_id_for(r.item_uuid): r.content_hash for r in frame}
    sampling = SamplingManifest(
        campaign_id=CAMPAIGN_ID,
        target_identity_digest=frame_data["target_identity_digest"],
        snapshot_sha256=frame_data["snapshot_sha256"],
        snapshot_as_of=snapshot_as_of,
        sampling_seed=SAMPLING_SEED,
        inclusion_rules=INCLUSION_RULES,
        exclusion_rules=EXCLUSION_RULES,
        source_row_counts={
            "frame": len(frame),
            "excluded_total": sum(excluded.values()),
            **{f"excluded_{k}": v for k, v in excluded.items()},
        },
        stratum_counts=stratum_counts,
        sample_ids=tuple(sample_ids),
        sample_hashes=tuple(hash_by_id[sid] for sid in sample_ids),
    )
    split = SplitManifest(
        campaign_id=CAMPAIGN_ID,
        sampling_manifest_digest=sampling.manifest_digest(),
        split_seed=SPLIT_SEED,
        dev_fraction=DEV_FRACTION,
        grouping=("content_hash", "normalized_text"),
        dev_ids=tuple(dev_ids),
        holdout_ids=tuple(holdout_ids),
        leakage_checks=checks,
    )
    protected = Path(args.protected_dir)
    protected.mkdir(parents=True, exist_ok=True)
    (protected / "frame.json").write_text(
        json.dumps([json.loads(r.model_dump_json()) for r in frame], sort_keys=True)
    )
    _write_public(
        args.manifest,
        {
            "sampling_manifest": json.loads(sampling.model_dump_json()),
            "sampling_manifest_digest": sampling.manifest_digest(),
            "split_manifest": json.loads(split.model_dump_json()),
            "split_manifest_digest": split.split_digest(),
            "duplicate_groups": {k: v for k, v in groups.items()},
        },
    )
    return 0


def cmd_packets(args: argparse.Namespace) -> int:
    manifest_data = json.loads(Path(args.manifest).read_text())
    sampling = SamplingManifest.model_validate(manifest_data["sampling_manifest"])
    samples = json.loads(Path(args.samples).read_text())["samples"]
    packets = build_packets(
        sampling=sampling, samples=samples, packet_id=f"{CAMPAIGN_ID}-blind-v1"
    )
    manifest = write_packets(packets, Path(args.protected_dir))
    print(json.dumps({"packets": manifest}, sort_keys=True))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("freeze-target")
    p.add_argument("--repo-sha", required=True)
    p.add_argument("--provider-config-digest", required=True)
    p.add_argument("--output", type=Path, required=True)
    p.set_defaults(func=cmd_freeze_target)

    p = sub.add_parser("sample")
    p.add_argument("--frame", required=True)
    p.add_argument("--manifest", type=Path, required=True)
    p.add_argument("--protected-dir", required=True)
    p.set_defaults(func=cmd_sample)

    p = sub.add_parser("packets")
    p.add_argument("--manifest", required=True)
    p.add_argument("--samples", required=True)
    p.add_argument("--protected-dir", required=True)
    p.set_defaults(func=cmd_packets)

    args = parser.parse_args()
    return int(args.func(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main())

"""Regression proofs for the PR #203 correction campaign."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from engram.assessment_calibration import (
    CalibrationBin,
    CalibrationProfile,
    calibration_profiles_digest,
)
from engram.assessment_schema import AssessmentContract
from engram.canonicalize import canonicalize, content_hash
from evals.admission.schema import HumanJudgment, LabelRecord, digest
from evals.calibration.fit import (
    AssessmentExecutionReceipt,
    EvidenceFloorResult,
    LabeledObservation,
    evaluate_holdout,
    fit_profiles,
)
from evals.calibration.fit import (
    check_floors as _production_check_floors,
)
from evals.calibration.freeze import (
    EvidenceFloors,
    FrameRow,
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
from evals.calibration.gate import REQUIRED_FLOOR_CHECKS, gate_checks, prove_mismatch_uncalibrated
from evals.calibration.review import (
    build_packets,
    freeze_ledger,
    verify_ledger,
    write_packets,
    write_protected_file,
)

NOW = datetime(2026, 9, 9, tzinfo=UTC)


def _identity() -> TargetIdentity:
    return TargetIdentity(
        campaign_id="campaign",
        repo_sha="a" * 40,
        assessment_schema_version="engram.assessment.v1",
        assessment_code_version="assessment-engine-v1",
        prompt_version="engram.assess.1",
        provider_adapter="openai",
        provider_model="model",
        provider_config_digest="b" * 64,
        provider_params={"temperature": 0},
        assessment_policy_version="assessment-selection-v1",
        calibration_artifact_schema_version="engram.calibration-profiles-v1",
        calibration_dataset_version="dataset-v2",
        label_guide_version="engram-calibration-guide-157-v1",
        canonicalization_version="assessment-evidence-manifest-v1",
        dimensions=("taxonomy", "retention", "epistemic"),
    )


def _frame(n: int = 40, *, duplicate: bool = False) -> list[Any]:
    rows: list[dict[str, Any]] = []
    content: dict[str, str] = {}
    for index in range(n):
        item_uuid = f"00000000-0000-0000-0000-{index:012d}"
        text = "duplicate text" if duplicate and index < 2 else f"memory {index} " + ("x" * index)
        rows.append(
            {
                "item_uuid": item_uuid,
                "content_hash": content_hash(canonicalize(text)),
                "kind": "fact",
                "source_type": "manual",
                "review_status": "active",
                "created_at": NOW - timedelta(days=index * 4),
                "valid_to": None,
            }
        )
        content[item_uuid] = text
    return build_frame(rows, content_by_uuid=content, snapshot_as_of=NOW)[0]


def _sampling(
    frame: list[Any], sample_ids: list[str], strata: dict[str, int], coverage: dict[str, Any]
) -> SamplingManifest:
    hashes = {sample_id_for(row.item_uuid): row.content_hash for row in frame}
    return SamplingManifest(
        campaign_id="campaign",
        target_identity_digest="c" * 64,
        frame_digest=protected_frame_digest(frame),
        snapshot_sha256="d" * 64,
        snapshot_as_of=NOW,
        sampling_seed="seed",
        inclusion_rules=("rule",),
        exclusion_rules=(),
        source_row_counts={"frame": len(frame)},
        stratum_counts=strata,
        coverage_dimensions=coverage,
        sample_ids=tuple(sample_ids),
        sample_hashes=tuple(hashes[sid] for sid in sample_ids),
    )


def test_frame_freezes_canonical_content_hash_from_protected_content() -> None:
    item_uuid = "00000000-0000-0000-0000-000000000001"
    text = "  Mixed   CASE content  "
    frame, _ = build_frame(
        [
            {
                "item_uuid": item_uuid,
                "content_hash": "legacy-or-malformed-source-hash",
                "kind": "fact",
                "source_type": "manual",
                "review_status": "active",
                "created_at": NOW,
                "valid_to": None,
            }
        ],
        content_by_uuid={item_uuid: text},
        snapshot_as_of=NOW,
    )
    assert frame[0].content_hash == content_hash(canonicalize(text))


def test_strict_subset_split_exactly_partitions_sample_membership() -> None:
    frame = _frame()
    sample_ids, strata, coverage = stratified_sample(
        frame,
        campaign_id="campaign",
        sampling_seed="seed",
        coverage_min=1,
        allocation_fraction=0.25,
    )
    assert len(sample_ids) < len(frame)
    dev, holdout, checks, groups = assign_splits(
        frame,
        sample_ids=sample_ids,
        campaign_id="campaign",
        split_seed="split",
        dev_fraction=0.6,
    )
    sampling = _sampling(frame, sample_ids, strata, coverage)
    split = SplitManifest(
        campaign_id="campaign",
        sampling_manifest_digest=sampling.manifest_digest(),
        sampling_membership_digest=digest(sorted(sample_ids)),
        split_seed="split",
        dev_fraction=0.6,
        grouping=("content_hash", "normalized_text"),
        dev_ids=tuple(dev),
        holdout_ids=tuple(holdout),
        leakage_checks=checks,
    )
    validate_split_membership(sampling, split)
    assert set(dev).isdisjoint(holdout)
    assert set(dev) | set(holdout) == set(sample_ids)
    assert not ({sample_id_for(row.item_uuid) for row in frame} - set(sample_ids)) & (
        set(dev) | set(holdout)
    )
    assert len(dev) + len(holdout) == len(sample_ids)
    assert all(
        len({"dev" if sid in dev else "holdout" for sid in members}) == 1
        for members in groups.values()
    )


def test_known_source_root_and_session_groups_never_cross_split() -> None:
    frame = _frame(12)
    frame[0] = frame[0].model_copy(update={"source_ref": "shared-source"})
    frame[1] = frame[1].model_copy(update={"source_ref": "shared-source"})
    frame[2] = frame[2].model_copy(update={"root_ref": "shared-root"})
    frame[3] = frame[3].model_copy(update={"root_ref": "shared-root"})
    frame[4] = frame[4].model_copy(update={"session_ref": "shared-session"})
    frame[5] = frame[5].model_copy(update={"session_ref": "shared-session"})
    sample_ids = [sample_id_for(row.item_uuid) for row in frame]
    dev, holdout, _, _ = assign_splits(
        frame,
        sample_ids=sample_ids,
        campaign_id="campaign",
        split_seed="split",
        dev_fraction=0.6,
    )
    side = {sample_id: "dev" if sample_id in dev else "holdout" for sample_id in sample_ids}
    for left, right in ((0, 1), (2, 3), (4, 5)):
        assert side[sample_ids[left]] == side[sample_ids[right]]
    assert dev and holdout


def test_split_manifest_rejects_missing_or_unsampled_members() -> None:
    frame = _frame(20)
    sample_ids, strata, coverage = stratified_sample(
        frame,
        campaign_id="campaign",
        sampling_seed="seed",
        coverage_min=1,
        allocation_fraction=0.25,
    )
    sampling = _sampling(frame, sample_ids, strata, coverage)
    dev, holdout, checks, _ = assign_splits(
        frame, sample_ids=sample_ids, campaign_id="campaign", split_seed="split", dev_fraction=0.6
    )
    base = dict(
        campaign_id="campaign",
        sampling_manifest_digest=sampling.manifest_digest(),
        sampling_membership_digest=digest(sorted(sample_ids)),
        split_seed="split",
        dev_fraction=0.6,
        grouping=("content_hash",),
        leakage_checks=checks,
    )
    missing = SplitManifest(dev_ids=tuple(dev[:-1]), holdout_ids=tuple(holdout), **base)
    with pytest.raises(ValueError, match="split_sample_membership_mismatch"):
        validate_split_membership(sampling, missing)
    foreign = SplitManifest(dev_ids=tuple(dev) + ("sforeign",), holdout_ids=tuple(holdout), **base)
    with pytest.raises(ValueError, match="split_sample_membership_mismatch"):
        validate_split_membership(sampling, foreign)


def test_duplicate_grouping_uses_only_sampled_rows() -> None:
    frame = _frame(20, duplicate=True)
    duplicate_ids = [sample_id_for(frame[0].item_uuid), sample_id_for(frame[1].item_uuid)]
    sample_ids = duplicate_ids + [sample_id_for(row.item_uuid) for row in frame[2:8]]
    dev, holdout, _, groups = assign_splits(
        frame, sample_ids=sample_ids, campaign_id="campaign", split_seed="split", dev_fraction=0.6
    )
    assert set(dev) | set(holdout) == set(sample_ids)
    assert any(set(members) == set(duplicate_ids) for members in groups.values())
    assert (duplicate_ids[0] in dev) == (duplicate_ids[1] in dev)


def test_coverage_is_deterministic_and_ignores_provider_or_reviewer_fields() -> None:
    base_rows: list[dict[str, Any]] = []
    content: dict[str, str] = {}
    for index in range(24):
        item_uuid = f"10000000-0000-0000-0000-{index:012d}"
        text = f"case {index}"
        row = {
            "item_uuid": item_uuid,
            "content_hash": "sha256:" + digest(text),
            "kind": "fact" if index % 2 else "decision",
            "source_type": "manual" if index % 3 else "extraction",
            "review_status": "active" if index % 4 else "proposed",
            "created_at": NOW - timedelta(days=index * 5),
            "valid_to": None,
        }
        base_rows.append(row)
        content[item_uuid] = text
    noisy = [
        {**row, "provider_output": {"score": index / 24}, "reviewer_label": "positive"}
        for index, row in enumerate(base_rows)
    ]
    frame_a, _ = build_frame(base_rows, content_by_uuid=content, snapshot_as_of=NOW)
    frame_b, _ = build_frame(list(reversed(noisy)), content_by_uuid=content, snapshot_as_of=NOW)
    result_a = stratified_sample(
        frame_a,
        campaign_id="campaign",
        sampling_seed="seed",
        coverage_min=2,
        allocation_fraction=0.25,
    )
    result_b = stratified_sample(
        frame_b,
        campaign_id="campaign",
        sampling_seed="seed",
        coverage_min=2,
        allocation_fraction=0.25,
    )
    assert result_a == result_b
    coverage = result_a[2]
    assert coverage["age_bucket"]["status"] == "sampled"
    assert coverage["source_type"]["status"] == "sampled"
    assert coverage["assertion_mode"]["status"] == "unavailable"
    assert coverage["risk"]["status"] == "unavailable"
    assert coverage["retention_disposition"]["status"] == "post_review_only"
    assert coverage["provider_condition"]["status"] == "post_selection_only"
    assert coverage["difficulty"]["status"] == "unavailable"


def test_missing_and_explicit_unknown_are_distinct() -> None:
    rows = [
        {
            "item_uuid": "1",
            "content_hash": "sha256:" + "1" * 64,
            "kind": "fact",
            "source_type": "manual",
            "review_status": "active",
            "created_at": NOW,
            "valid_to": None,
        },
        {
            "item_uuid": "2",
            "content_hash": "sha256:" + "2" * 64,
            "kind": "fact",
            "source_type": "manual",
            "review_status": "active",
            "assertion_mode": "unknown",
            "origin": "unknown",
            "risk": "unknown",
            "evidence_state": "unknown",
            "created_at": NOW,
            "valid_to": None,
        },
    ]
    frame, _ = build_frame(rows, content_by_uuid={"1": "one", "2": "two"}, snapshot_as_of=NOW)
    assert frame[0].assertion_mode == "unavailable"
    assert frame[1].assertion_mode == "unknown"


def test_public_aggregate_reconciles_without_private_membership() -> None:
    frame = _frame(20)
    ids, strata, coverage = stratified_sample(
        frame,
        campaign_id="campaign",
        sampling_seed="seed",
        coverage_min=1,
        allocation_fraction=0.25,
    )
    sampling = _sampling(frame, ids, strata, coverage)
    dev, holdout, checks, _ = assign_splits(
        frame, sample_ids=ids, campaign_id="campaign", split_seed="split", dev_fraction=0.6
    )
    split = SplitManifest(
        campaign_id="campaign",
        sampling_manifest_digest=sampling.manifest_digest(),
        sampling_membership_digest=digest(sorted(ids)),
        split_seed="split",
        dev_fraction=0.6,
        grouping=("content_hash",),
        dev_ids=tuple(dev),
        holdout_ids=tuple(holdout),
        leakage_checks=checks,
    )
    public = public_sampling_summary(sampling, split)
    assert public["sample_count"] == public["dev_count"] + public["holdout_count"]
    blob = json.dumps(public)
    assert "sample_ids" not in blob and "sample_hashes" not in blob
    assert not any(sample_id in blob for sample_id in ids)


def test_protected_writer_forces_permissions_and_exclusive_create(tmp_path: Path) -> None:
    old_umask = os.umask(0)
    try:
        path = tmp_path / "protected" / "artifact.json"
        write_protected_file(path, b"secret")
    finally:
        os.umask(old_umask)
    assert path.parent.stat().st_mode & 0o777 == 0o700
    assert path.stat().st_mode & 0o777 == 0o600
    with pytest.raises(FileExistsError):
        write_protected_file(path, b"replacement")


def test_protected_writer_secures_nested_parents_and_rejects_symlinks(tmp_path: Path) -> None:
    old_umask = os.umask(0)
    try:
        nested = tmp_path / "outer" / "inner" / "artifact.json"
        write_protected_file(nested, b"complete")
    finally:
        os.umask(old_umask)
    assert nested.parent.parent.stat().st_mode & 0o777 == 0o700
    assert nested.parent.stat().st_mode & 0o777 == 0o700
    assert nested.read_bytes() == b"complete"

    real = tmp_path / "real"
    real.mkdir()
    redirected = tmp_path / "redirected"
    redirected.symlink_to(real, target_is_directory=True)
    with pytest.raises(OSError):
        write_protected_file(redirected / "secret.json", b"secret")
    assert not (real / "secret.json").exists()


def test_packet_content_is_bound_to_frozen_sample_hash() -> None:
    frame = _frame(1)
    sample_id = sample_id_for(frame[0].item_uuid)
    sampling = _sampling(frame, [sample_id], {"fact/manual/active": 1}, {})
    sample = {
        "sample_id": sample_id,
        "content": "memory 0 ",
        "content_hash": frame[0].content_hash,
        "kind": "fact",
        "source_type": "manual",
        "review_status": "active",
        "assertion_mode": "unavailable",
        "origin": "unavailable",
        "risk": "unavailable",
        "evidence_state": "unavailable",
        "age_days": 0,
    }
    assert build_packets(sampling=sampling, samples=[sample], packet_id="packet")
    with pytest.raises(ValueError, match="packet_content_hash_mismatch"):
        build_packets(
            sampling=sampling,
            samples=[{**sample, "content": "substituted tenant content"}],
            packet_id="packet",
        )


def test_freeze_ledger_enforces_full_independent_dual_review(tmp_path: Path) -> None:
    frame = _frame(2)
    sample_ids = [sample_id_for(row.item_uuid) for row in frame]
    sampling = _sampling(frame, sample_ids, {"fact/manual/active": 2}, {})
    samples = [
        {
            "sample_id": sample_id,
            "content": "memory 0 " if index == 0 else "memory 1 x",
            "content_hash": row.content_hash,
            "kind": "fact",
            "source_type": "manual",
            "review_status": "active",
            "assertion_mode": "unavailable",
            "origin": "unavailable",
            "risk": "unavailable",
            "evidence_state": "unavailable",
            "age_days": index * 4,
        }
        for index, (sample_id, row) in enumerate(zip(sample_ids, frame, strict=True))
    ]
    packet_dir = tmp_path / "packets"
    packet_manifest = write_packets(
        build_packets(sampling=sampling, samples=samples, packet_id="packet"), packet_dir
    )
    reviewer_a_path = packet_dir / "packet.reviewer_a.json"
    reviewer_b_path = packet_dir / "packet.reviewer_b.json"
    records = [
        _record(index).model_copy(
            update={
                "sample_id": sample_id,
                "content_hash": row.content_hash,
                "reviewer_b": _judgment(f"b{index}"),
                "review_stage": "complete",
            }
        )
        for index, (sample_id, row) in enumerate(zip(sample_ids, frame, strict=True))
    ]
    result = freeze_ledger(
        campaign_id="campaign",
        records=records,
        sampling=sampling,
        protected_dir=tmp_path / "ledger",
        reviewer_a_packet_path=reviewer_a_path,
        reviewer_a_packet_sha256=packet_manifest[reviewer_a_path.name],
        reviewer_b_packet_path=reviewer_b_path,
        reviewer_b_packet_sha256=packet_manifest[reviewer_b_path.name],
        expected_dataset_id="campaign",
        expected_dataset_version="dataset-v2",
    )
    assert result["records"] == 2
    ledger_path = Path(str(result["path"]))
    verified = verify_ledger(
        ledger_path,
        str(result["sha256"]),
        sampling=sampling,
        reviewer_a_packet_sha256=packet_manifest[reviewer_a_path.name],
        reviewer_b_packet_sha256=packet_manifest[reviewer_b_path.name],
        expected_dataset_id="campaign",
        expected_dataset_version="dataset-v2",
    )
    assert verified.full_population_dual_review is True
    assert len(verified.records) == 2
    with pytest.raises(ValueError, match="full_dual_review_required"):
        freeze_ledger(
            campaign_id="campaign",
            records=[records[0].model_copy(update={"reviewer_b": None}), records[1]],
            sampling=sampling,
            protected_dir=tmp_path / "bad-ledger",
            reviewer_a_packet_path=reviewer_a_path,
            reviewer_a_packet_sha256=packet_manifest[reviewer_a_path.name],
            reviewer_b_packet_path=reviewer_b_path,
            reviewer_b_packet_sha256=packet_manifest[reviewer_b_path.name],
            expected_dataset_id="campaign",
            expected_dataset_version="dataset-v2",
        )


def test_freeze_target_is_protected_and_immutable(tmp_path: Path) -> None:
    from argparse import Namespace

    from evals.calibration.__main__ import cmd_freeze_target

    output = tmp_path / "protected" / "identity-frozen.json"
    args = Namespace(
        repo_sha="1" * 40,
        provider_config_digest="2" * 64,
        output=output,
    )
    old_umask = os.umask(0)
    try:
        assert cmd_freeze_target(args) == 0
    finally:
        os.umask(old_umask)
    assert output.parent.stat().st_mode & 0o777 == 0o700
    assert output.stat().st_mode & 0o777 == 0o600
    with pytest.raises(FileExistsError):
        cmd_freeze_target(args)


def test_cli_sample_writes_exact_protected_artifacts_and_safe_public_summary(
    tmp_path: Path,
) -> None:
    from argparse import Namespace

    from evals.calibration.__main__ import cmd_sample

    frame = _frame(30)
    raw = {
        "rows": [
            {
                "item_uuid": row.item_uuid,
                "content_hash": row.content_hash,
                "kind": row.kind,
                "source_type": row.source_type,
                "review_status": row.review_status,
                "created_at": (NOW - timedelta(days=index * 4)).isoformat(),
                "valid_to": None,
            }
            for index, row in enumerate(frame)
        ],
        "content_by_uuid": {
            row.item_uuid: f"memory {index} " + ("x" * index) for index, row in enumerate(frame)
        },
        "snapshot_as_of": NOW.isoformat(),
        "snapshot_sha256": "d" * 64,
        "target_identity_digest": "c" * 64,
    }
    frame_path = tmp_path / "frame-input.json"
    frame_path.write_text(json.dumps(raw))
    protected = tmp_path / "protected"
    public_path = tmp_path / "public.json"
    assert (
        cmd_sample(Namespace(frame=frame_path, manifest=public_path, protected_dir=protected)) == 0
    )
    public = json.loads(public_path.read_text())
    sampling = SamplingManifest.model_validate(
        json.loads((protected / "sampling-manifest.json").read_text())
    )
    split = SplitManifest.model_validate(
        json.loads((protected / "split-manifest.json").read_text())
    )
    validate_split_membership(sampling, split)
    assert public["sample_count"] == public["dev_count"] + public["holdout_count"]
    assert protected.stat().st_mode & 0o777 == 0o700
    assert all(path.stat().st_mode & 0o777 == 0o600 for path in protected.iterdir())
    public_blob = public_path.read_text()
    assert "sample_ids" not in public_blob
    assert "memory 0" not in public_blob
    assert "00000000-" not in public_blob
    assert str(protected) not in public_blob


def test_committed_public_campaign_artifact_reconciles_and_is_content_free() -> None:
    path = Path("evals/calibration/campaigns/202/campaign-manifest-public.json")
    manifest = json.loads(path.read_text())
    sampling = manifest["sampling"]
    assert sampling["sample_count"] == sampling["dev_count"] + sampling["holdout_count"]
    blob = path.read_text()
    assert (
        re.search(
            r"\b[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\b",
            blob,
            re.IGNORECASE,
        )
        is None
    )
    for forbidden in (
        '"sample_ids"',
        '"sample_hashes"',
        '"content"',
        '"reviewer_notes"',
        '"credentials"',
        "/home/",
        ".local/share",
        "raw-items-snapshot.json",
    ):
        assert forbidden not in blob
    assert manifest["serving_invariants"] == {
        "assessment_selection_enabled": False,
        "certified_serving_profiles": ["legacy"],
        "mcp_recall_authority": "legacy",
        "ordinary_recall_authority": "legacy",
    }


def test_cli_help_advertises_only_implemented_pre_review_commands() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "evals.calibration", "--help"],
        check=True,
        capture_output=True,
        text=True,
    )
    assert "freeze-target" in result.stdout
    assert "sample" in result.stdout
    assert "packets" in result.stdout
    for unavailable in ("ingest", "fit", "gate", "report"):
        assert f"  {unavailable} " not in result.stdout


def test_serving_and_selection_invariants_remain_exact() -> None:
    from engram.config import settings
    from engram.recall_profiles import CERTIFIED_SERVING_PROFILES

    assert frozenset({"legacy"}) == CERTIFIED_SERVING_PROFILES
    assert settings.assessment_selection_enabled is False


def _contract() -> AssessmentContract:
    return AssessmentContract(
        provider="openai", model="model", config_version="b" * 64, calibration_version="dataset-v2"
    )


def _profiles() -> list[CalibrationProfile]:
    contract = _contract()
    bins = [
        CalibrationBin(lower=i / 10, upper=(i + 1) / 10, value=0.5, count=50 if i == 5 else 0)
        for i in range(10)
    ]
    return [
        CalibrationProfile(
            version="dataset-v2",
            contract=contract,
            dataset_version="dataset-v2",
            dimension=dimension,
            source_type="manual",
            assertion_mode="unknown",
            kind="fact",
            risk="unknown",
            bins=bins,
        )
        for dimension in ("taxonomy", "retention", "epistemic")
    ]


def _judgment(ref: str, consequence: str = "low") -> HumanJudgment:
    return HumanJudgment.model_validate(
        {
            "adjudicator_ref": ref,
            "adjudicated_at": NOW,
            "adjudicator_confidence": "medium",
            "reason_code": "campaign-review",
            "dimensions": {
                "atomic": "yes",
                "proposition_count": "one",
                "attribution": "unknown",
                "source_span": "unknown",
                "evidence_span": "unknown",
                "assertion_origin": "unknown",
                "expected_kind": "fact",
                "expected_subject_or_domain": "unknown",
                "expected_scope": "unknown",
                "retention_value": "retain",
                "epistemic_state": "adequately_supported",
                "factual_outcome": None,
                "consequence": consequence,
                "expected_storage_disposition": "retain",
                "expected_startup_eligibility": "unknown",
                "expected_governed_semantic_eligibility": "unknown",
                "human_review_required": "unknown",
                "acceptable_abstention": "unknown",
                "conflict_expected": "unknown",
                "dispute_expected": "unknown",
                "supersession_expected": "unknown",
                "temporal_validity_issue": "unknown",
                "scope_visibility_concern": "unknown",
                "evidence_independence": "unknown",
                "expected_blockers": None,
                "expected_next_action": "unknown",
            },
        }
    )


def _record(index: int, consequence: str = "low", *, dual: bool = True) -> LabelRecord:
    a = _judgment(f"a{index}", consequence)
    b = _judgment(f"b{index}", consequence) if consequence == "high" and dual else None
    return LabelRecord.model_validate(
        {
            "sample_id": f"s{index}",
            "label_schema_version": "engram-admission-label-v1",
            "dataset_id": "campaign",
            "dataset_version": "dataset-v2",
            "fixture_role": "ordinary_claim",
            "label_origin": "human_adjudicated",
            "reviewer_a": a,
            "reviewer_b": b,
            "resolution": None,
            "disagreement": "none",
            "review_stage": "complete" if dual else "reviewer_b_pending",
        }
    )


def _floor_evidence(
    *,
    profile_bin_count: int = 50,
    dev_count: int = 50,
    holdout_count: int = 10,
    dimensions: tuple[str, ...] = ("taxonomy", "retention", "epistemic"),
    high_count: int = 0,
) -> tuple[list[LabelRecord], list[LabeledObservation], list[CalibrationProfile]]:
    records = [
        _record(i, "high" if i < high_count else "low") for i in range(dev_count + holdout_count)
    ]
    observations = [
        LabeledObservation(
            sample_id=f"s{i}",
            split="dev" if i < dev_count else "holdout",
            dimension=dimension,
            outcome="positive",
            raw_value=0.55,
            source_type="manual",
            assertion_mode="unknown",
            kind="fact",
            risk="unknown",
            consequence="high" if i < high_count else "low",
        )
        for i in range(dev_count + holdout_count)
        for dimension in dimensions
    ]
    profiles = _profiles()
    if profile_bin_count != 50:
        profiles = [
            p.model_copy(
                update={
                    "bins": [
                        b.model_copy(update={"count": profile_bin_count if j == 5 else 0})
                        for j, b in enumerate(p.bins)
                    ]
                }
            )
            for p in profiles
        ]
    return records, observations, profiles


def _bound_manifests(
    observations: list[LabeledObservation],
    records: list[LabelRecord] | None = None,
    *,
    target_identity_digest: str = "c" * 64,
) -> tuple[SamplingManifest, SplitManifest]:
    by_id = {obs.sample_id: obs.split for obs in observations}
    sample_ids = sorted(by_id)
    record_hashes = {
        record.sample_id: str(record.content_hash)
        for record in records or []
        if record.content_hash
    }
    sample_hashes = {
        sample_id: record_hashes.get(sample_id, "sha256:" + digest(sample_id))
        for sample_id in sample_ids
    }
    frame = [
        FrameRow(
            item_uuid=f"protected-{sample_id}",
            sample_id=sample_id,
            content_hash=sample_hashes[sample_id],
            content_norm_hash=digest(["norm", sample_id]),
            kind=next(obs.kind for obs in observations if obs.sample_id == sample_id),
            source_type=next(obs.source_type for obs in observations if obs.sample_id == sample_id),
            review_status="active",
            assertion_mode=next(
                obs.assertion_mode for obs in observations if obs.sample_id == sample_id
            ),
            origin="unknown",
            risk=next(obs.risk for obs in observations if obs.sample_id == sample_id),
            age_bucket="unknown",
            evidence_state="unknown",
            content_bytes=1,
            input_size_bucket="small",
        )
        for sample_id in sample_ids
    ]
    sampling = SamplingManifest(
        campaign_id="campaign",
        target_identity_digest=target_identity_digest,
        frame_digest=protected_frame_digest(frame),
        snapshot_sha256="d" * 64,
        snapshot_as_of=NOW,
        sampling_seed="seed",
        inclusion_rules=("rule",),
        exclusion_rules=(),
        source_row_counts={"eligible_frame": len(sample_ids)},
        stratum_counts={"all": len(sample_ids)},
        coverage_dimensions={},
        sample_ids=tuple(sample_ids),
        sample_hashes=tuple(sample_hashes[sample_id] for sample_id in sample_ids),
    )
    split = SplitManifest(
        campaign_id="campaign",
        sampling_manifest_digest=sampling.manifest_digest(),
        sampling_membership_digest=digest(sorted(sample_ids)),
        split_seed="split",
        dev_fraction=0.6,
        grouping=("content_hash",),
        dev_ids=tuple(sample_id for sample_id in sample_ids if by_id[sample_id] == "dev"),
        holdout_ids=tuple(sample_id for sample_id in sample_ids if by_id[sample_id] == "holdout"),
        leakage_checks={},
    )
    validate_split_membership(sampling, split)
    return sampling, split


def check_floors(
    *,
    floors: EvidenceFloors,
    reviewed_records: list[LabelRecord],
    observations: list[LabeledObservation],
    profiles: list[CalibrationProfile],
    synthesize_full_dual_review: bool = True,
    target_identity_override: TargetIdentity | None = None,
    mutate_frame_after_freeze: bool = False,
    drop_last_execution: bool = False,
) -> EvidenceFloorResult:
    target_identity = target_identity_override or _identity()
    sampling, split = _bound_manifests(
        observations,
        reviewed_records,
        target_identity_digest=target_identity.identity_digest(),
    )
    expected_hashes = dict(zip(sampling.sample_ids, sampling.sample_hashes, strict=True))
    reviewed_records = [
        record.model_copy(
            update={
                "content_hash": expected_hashes[record.sample_id],
                "reviewer_b": (
                    record.reviewer_b
                    or (
                        record.reviewer_a.model_copy(
                            update={"adjudicator_ref": f"reviewer-b-{record.sample_id}"}
                        )
                        if synthesize_full_dual_review
                        else None
                    )
                ),
            }
        )
        for record in reviewed_records
    ]
    packet_a_sha = "b" * 64
    packet_b_sha = "c" * 64
    records_by_id = {record.sample_id: record for record in reviewed_records}
    bound_observations = [
        observation.model_copy(
            update={
                "suggested_kind": (
                    observation.suggested_kind
                    or (
                        records_by_id[observation.sample_id].final_dimensions()
                        or records_by_id[observation.sample_id].reviewer_a.dimensions
                    ).expected_kind
                    if observation.dimension == "taxonomy"
                    else None
                )
            }
        )
        for observation in observations
    ]
    frame = [
        FrameRow(
            item_uuid=f"protected-{sample_id}",
            sample_id=sample_id,
            content_hash=expected_hashes[sample_id],
            content_norm_hash=digest(["norm", sample_id]),
            kind=next(obs.kind for obs in bound_observations if obs.sample_id == sample_id),
            source_type=next(
                obs.source_type for obs in bound_observations if obs.sample_id == sample_id
            ),
            review_status="active",
            assertion_mode=next(
                obs.assertion_mode for obs in bound_observations if obs.sample_id == sample_id
            ),
            origin="unknown",
            risk=next(obs.risk for obs in bound_observations if obs.sample_id == sample_id),
            age_bucket="unknown",
            evidence_state="unknown",
            content_bytes=1,
            input_size_bucket="small",
        )
        for sample_id in sampling.sample_ids
    ]
    if mutate_frame_after_freeze:
        frame[0] = frame[0].model_copy(update={"source_type": "substituted-source"})
    contract = _contract()
    executions = []
    for sample_id in sampling.sample_ids:
        by_dimension = {
            obs.dimension: obs for obs in bound_observations if obs.sample_id == sample_id
        }
        receipt = {
            "sample_id": sample_id,
            "input_content_hash": expected_hashes[sample_id],
            "execution_id": f"execution-{sample_id}",
            "captured_at": NOW.isoformat(),
            "provider_request_digest": digest(
                {
                    "sample_id": sample_id,
                    "input_content_hash": expected_hashes[sample_id],
                    "target_identity_digest": target_identity.identity_digest(),
                    "assessment_contract_digest": digest(contract.model_dump(mode="json")),
                }
            ),
            "provider_response_digest": digest(["response", sample_id]),
            "assessment": {
                "taxonomy": {
                    "raw_value": (
                        by_dimension["taxonomy"].raw_value if "taxonomy" in by_dimension else None
                    )
                },
                "suggested_kind": (
                    by_dimension["taxonomy"].suggested_kind if "taxonomy" in by_dimension else None
                ),
                "retention": {
                    "raw_value": (
                        by_dimension["retention"].raw_value if "retention" in by_dimension else None
                    )
                },
                "epistemic": {
                    "raw_value": (
                        by_dimension["epistemic"].raw_value if "epistemic" in by_dimension else None
                    )
                },
            },
        }
        receipt["receipt_digest"] = "0" * 64
        validated_receipt = AssessmentExecutionReceipt.model_validate(receipt)
        receipt = validated_receipt.model_dump(mode="json")
        receipt["receipt_digest"] = validated_receipt.verified_payload_digest()
        executions.append(receipt)
    if drop_last_execution:
        executions.pop()
    assessment_envelope = {
        "evidence_schema": "engram-calibration-assessment-evidence-v1",
        "target_identity_digest": target_identity.identity_digest(),
        "sampling_manifest_digest": sampling.manifest_digest(),
        "assessment_contract_digest": digest(contract.model_dump(mode="json")),
        "frame_digest": sampling.frame_digest,
        "executions": executions,
    }
    assessment_payload = json.dumps(
        assessment_envelope, sort_keys=True, separators=(",", ":")
    ).encode()
    envelope = {
        "ledger_schema": "engram-calibration-label-ledger-v2",
        "campaign_id": sampling.campaign_id,
        "sampling_manifest_digest": sampling.manifest_digest(),
        "reviewer_a_packet_sha256": packet_a_sha,
        "reviewer_b_packet_sha256": packet_b_sha,
        "full_population_dual_review": True,
        "records": [record.model_dump(mode="json") for record in reviewed_records],
    }
    payload = json.dumps(envelope, sort_keys=True, separators=(",", ":")).encode()
    fd, raw_path = tempfile.mkstemp()
    ledger_path = Path(raw_path)
    with os.fdopen(fd, "wb") as stream:
        stream.write(payload)
    evidence_fd, evidence_raw_path = tempfile.mkstemp()
    evidence_path = Path(evidence_raw_path)
    with os.fdopen(evidence_fd, "wb") as stream:
        stream.write(assessment_payload)
    try:
        return _production_check_floors(
            floors=floors,
            ledger_path=ledger_path,
            expected_ledger_sha256=hashlib.sha256(payload).hexdigest(),
            reviewer_a_packet_sha256=packet_a_sha,
            reviewer_b_packet_sha256=packet_b_sha,
            expected_dataset_id=reviewed_records[0].dataset_id,
            expected_dataset_version=reviewed_records[0].dataset_version,
            assessment_evidence_path=evidence_path,
            expected_assessment_evidence_sha256=hashlib.sha256(assessment_payload).hexdigest(),
            target_identity=target_identity,
            assessment_contract=contract,
            frame=frame,
            profiles=profiles,
            sampling=sampling,
            split=split,
        )
    finally:
        ledger_path.unlink(missing_ok=True)
        evidence_path.unlink(missing_ok=True)


def _floors(**updates: int | float) -> EvidenceFloors:
    values = dict(
        campaign_id="campaign",
        total_reviewed_min=50,
        per_dimension_labeled_min=50,
        per_dimension_non_unknown_fraction_min=0.80,
        holdout_min=10,
        holdout_per_profile_min=10,
        holdout_calibrated_brier_max=0.25,
        holdout_calibrated_ece_max=0.15,
        high_consequence_reviewed_min=0,
        per_bin_support_min=50,
        per_stratum_min=50,
    )
    values.update(updates)
    return EvidenceFloors(**values)


def test_fit_and_floors_reject_duplicate_or_wrong_split_observations() -> None:
    records, observations, _ = _floor_evidence()
    sampling, split = _bound_manifests(observations)
    with pytest.raises(ValueError, match="duplicate_observation"):
        fit_profiles(
            observations=[observations[0], observations[0]],
            identity=_identity(),
            contract=_contract(),
            split=split,
        )
    wrong_split = [
        *observations[:-1],
        observations[-1].model_copy(update={"split": "dev"}),
    ]
    with pytest.raises(ValueError, match="observation_split_mismatch"):
        fit_profiles(
            observations=wrong_split,
            identity=_identity(),
            contract=_contract(),
            split=split,
        )


def test_holdout_metrics_use_calibrated_profile_outputs() -> None:
    _, observations, profiles = _floor_evidence()
    _, split = _bound_manifests(observations)
    bad_profile = profiles[0].model_copy(
        update={"bins": tuple(bin_.model_copy(update={"value": 0.0}) for bin_ in profiles[0].bins)}
    )
    metrics = evaluate_holdout(observations, profiles=[bad_profile], split=split)
    taxonomy = next(metric for metric in metrics if metric.dimension == "taxonomy")
    assert taxonomy.brier == 1.0
    assert taxonomy.raw_brier is not None
    assert taxonomy.brier is not None
    assert taxonomy.raw_brier < taxonomy.brier
    with pytest.raises(ValueError, match="duplicate_calibration_profile_key"):
        evaluate_holdout(observations, profiles=[profiles[0], profiles[0]], split=split)
    unsupported_profile = profiles[0].model_copy(
        update={
            "bins": tuple(
                bin_.model_copy(update={"count": 0}) if index == 5 else bin_
                for index, bin_ in enumerate(profiles[0].bins)
            )
        }
    )
    unsupported = evaluate_holdout(observations, profiles=[unsupported_profile], split=split)
    unsupported_taxonomy = next(metric for metric in unsupported if metric.dimension == "taxonomy")
    assert unsupported_taxonomy.population_n == 10
    assert unsupported_taxonomy.n == 0
    assert unsupported_taxonomy.coverage == 0.0


def test_assessment_evidence_binds_target_contract_frame_and_full_population() -> None:
    records, observations, profiles = _floor_evidence()
    mismatched_target = _identity().model_copy(update={"provider_model": "different-model"})
    with pytest.raises(ValueError, match="assessment_contract_target_mismatch"):
        check_floors(
            floors=_floors(),
            reviewed_records=records,
            observations=observations,
            profiles=profiles,
            target_identity_override=mismatched_target,
        )
    with pytest.raises(ValueError, match="assessment_frame_digest_mismatch"):
        check_floors(
            floors=_floors(),
            reviewed_records=records,
            observations=observations,
            profiles=profiles,
            mutate_frame_after_freeze=True,
        )
    with pytest.raises(ValueError, match="assessment_evidence_membership_mismatch"):
        check_floors(
            floors=_floors(),
            reviewed_records=records,
            observations=observations,
            profiles=profiles,
            drop_last_execution=True,
        )


def test_floor_evidence_derives_outcomes_and_consequence_from_frozen_review() -> None:
    records, observations, profiles = _floor_evidence()
    baseline = check_floors(
        floors=_floors(),
        reviewed_records=records,
        observations=observations,
        profiles=profiles,
    )
    forged = [
        obs.model_copy(update={"outcome": "negative", "consequence": "high"})
        for obs in observations
    ]
    result = check_floors(
        floors=_floors(),
        reviewed_records=records,
        observations=forged,
        profiles=profiles,
    )
    assert result.checks == baseline.checks
    assert result.dimension_support == baseline.dimension_support


def test_every_frozen_floor_is_evidence_backed() -> None:
    records, observations, profiles = _floor_evidence()
    passing = check_floors(
        floors=_floors(), reviewed_records=records, observations=observations, profiles=profiles
    )
    assert passing.passed and all(passing.checks.values())

    cases = [
        ("total_reviewed", _floors(total_reviewed_min=61), records, observations, profiles),
        (
            "per_dimension_labeled",
            _floors(per_dimension_labeled_min=61),
            records,
            observations,
            profiles,
        ),
        ("holdout_size", _floors(holdout_min=11), records, observations, profiles),
        ("bin_support", _floors(), *_floor_evidence(profile_bin_count=49)),
        ("per_stratum_support", _floors(per_stratum_min=51), records, observations, profiles),
    ]
    for failed, floor, case_records, case_observations, case_profiles in cases:
        result = check_floors(
            floors=floor,
            reviewed_records=case_records,
            observations=case_observations,
            profiles=case_profiles,
        )
        assert result.checks[failed] is False
        assert result.passed is False


def test_high_consequence_and_dual_review_floors_fail_independently() -> None:
    records, observations, profiles = _floor_evidence(high_count=2)
    assert check_floors(
        floors=_floors(high_consequence_reviewed_min=2, per_stratum_min=1),
        reviewed_records=records,
        observations=observations,
        profiles=profiles,
    ).passed
    too_few = check_floors(
        floors=_floors(high_consequence_reviewed_min=3, per_stratum_min=1),
        reviewed_records=records,
        observations=observations,
        profiles=profiles,
    )
    assert too_few.checks["high_consequence"] is False
    pending = list(records)
    pending[0] = _record(0, "high", dual=False)
    with pytest.raises(ValueError, match="dual_review_required"):
        check_floors(
            floors=_floors(high_consequence_reviewed_min=2),
            reviewed_records=pending,
            observations=observations,
            profiles=profiles,
            synthesize_full_dual_review=False,
        )


def _records_with_unknown_dimensions(
    records: list[LabelRecord], sample_ids: set[str]
) -> list[LabelRecord]:
    result = []
    for record in records:
        if record.sample_id not in sample_ids:
            result.append(record)
            continue
        dimensions = record.reviewer_a.dimensions.model_copy(
            update={
                "expected_kind": "unknown",
                "retention_value": "uncertain",
                "epistemic_state": "unknown",
            }
        )
        reviewer_a = record.reviewer_a.model_copy(update={"dimensions": dimensions})
        reviewer_b = (
            None
            if record.reviewer_b is None
            else record.reviewer_b.model_copy(update={"dimensions": dimensions})
        )
        result.append(
            record.model_copy(update={"reviewer_a": reviewer_a, "reviewer_b": reviewer_b})
        )
    return result


def test_unknown_holdout_and_high_consequence_evidence_fail_closed() -> None:
    records, observations, profiles = _floor_evidence(high_count=2)
    unknown_holdout = [
        obs.model_copy(update={"outcome": "unknown", "raw_value": None})
        if obs.split == "holdout"
        else obs
        for obs in observations
    ]
    holdout_ids = {obs.sample_id for obs in unknown_holdout if obs.split == "holdout"}
    holdout_result = check_floors(
        floors=_floors(),
        reviewed_records=_records_with_unknown_dimensions(records, holdout_ids),
        observations=unknown_holdout,
        profiles=profiles,
    )
    assert holdout_result.checks["holdout_labeled_support"] is False
    assert not holdout_result.passed

    high_ids = {records[0].sample_id, records[1].sample_id}
    unknown_high = [
        obs.model_copy(update={"outcome": "unknown", "raw_value": None})
        if obs.sample_id in high_ids
        else obs
        for obs in observations
    ]
    high_result = check_floors(
        floors=_floors(high_consequence_reviewed_min=2),
        reviewed_records=_records_with_unknown_dimensions(records, high_ids),
        observations=unknown_high,
        profiles=profiles,
    )
    assert high_result.checks["high_consequence_labeled_support"] is False
    assert not high_result.passed


def test_non_unknown_fraction_and_omitted_strata_are_explicit() -> None:
    records, observations, profiles = _floor_evidence()
    unknown_ids = {record.sample_id for record in records[:20]}
    partly_unknown = [
        obs.model_copy(update={"outcome": "unknown", "raw_value": None})
        if obs.sample_id in unknown_ids
        else obs
        for obs in observations
    ]
    fraction_result = check_floors(
        floors=_floors(
            per_dimension_labeled_min=40,
            per_dimension_non_unknown_fraction_min=0.80,
        ),
        reviewed_records=_records_with_unknown_dimensions(records, unknown_ids),
        observations=partly_unknown,
        profiles=profiles,
    )
    assert fraction_result.checks["per_dimension_non_unknown_fraction"] is False

    thin_observations = [
        obs.model_copy(update={"source_type": "thin-unprofiled-source"})
        if obs.sample_id == observations[0].sample_id
        else obs
        for obs in observations
    ]
    explicit_result = check_floors(
        floors=_floors(),
        reviewed_records=records,
        observations=thin_observations,
        profiles=profiles,
    )
    thin = explicit_result.stratum_support["taxonomy/thin-unprofiled-source/unknown/fact/unknown"]
    assert thin["claimed_supported"] is False
    assert thin["supported"] is False


def test_partial_dimension_support_remains_explicit() -> None:
    records, observations, profiles = _floor_evidence(dimensions=("retention",))
    result = check_floors(
        floors=_floors(), reviewed_records=records, observations=observations, profiles=profiles
    )
    assert result.dimension_support["retention"]["supported"] is True
    assert result.dimension_support["taxonomy"]["supported"] is False
    assert result.dimension_support["epistemic"]["supported"] is False
    assert not result.passed


def test_mismatch_proof_uses_production_calibrate_and_is_nonvacuous() -> None:
    profiles = _profiles()
    profile_digest = calibration_profiles_digest(profiles)
    assert profile_digest is not None
    deployed = _contract().model_copy(update={"calibration_digest": profile_digest})
    proof = prove_mismatch_uncalibrated(
        profiles, deployed, target_identity_digest="c" * 64, artifact_digest="d" * 64
    )
    assert proof.baseline_calibrates
    assert all(proof.checks.values())
    for key in (
        "provider",
        "model",
        "prompt_version",
        "schema_version",
        "code_version",
        "config_version",
        "calibration_version",
        "calibration_digest",
        "dimension",
        "source_type",
        "assertion_mode",
        "kind",
        "risk",
    ):
        assert f"mismatch_{key}_stays_uncalibrated" in proof.checks
    with pytest.raises(ValueError, match="mismatch_proof_requires_profiles"):
        prove_mismatch_uncalibrated(
            [], deployed, target_identity_digest="c" * 64, artifact_digest="d" * 64
        )


def test_gate_requires_bound_authoritative_recall_and_mismatch_proofs(tmp_path: Path) -> None:
    from evals.calibration.fit import CalibrationArtifactBundle

    identity = _identity()
    profiles = _profiles()
    profile_payload = [p.model_dump(mode="json") for p in profiles]
    profile_digest = calibration_profiles_digest(profiles)
    assert profile_digest is not None
    deployed = _contract().model_copy(update={"calibration_digest": profile_digest})
    floor_result = EvidenceFloorResult(
        sampling_manifest_digest="e" * 64,
        split_manifest_digest="f" * 64,
        ledger_sha256="a" * 64,
        reviewer_a_packet_sha256="b" * 64,
        reviewer_b_packet_sha256="c" * 64,
        assessment_evidence_sha256="d" * 64,
        assessment_contract_digest="e" * 64,
        full_population_dual_review=True,
        checks={key: True for key in REQUIRED_FLOOR_CHECKS},
        dimension_support={},
        stratum_support={},
        bin_support={},
        failures=(),
        passed=True,
    )
    proof_path = tmp_path / "authoritative-recall-proof.json"
    proof_payload = {
        "proof_schema": "engram-authoritative-recall-proof-v1",
        "campaign_id": identity.campaign_id,
        "target_identity_digest": identity.identity_digest(),
        "profile_set_digest": profile_digest,
        "deployed_repo_sha": identity.repo_sha,
        "deployed_contract_digest": digest(deployed.model_dump(mode="json")),
        "assessment_policy_version": identity.assessment_policy_version,
        "captured_at": datetime.now(UTC).isoformat(),
        "observations": {
            "ordinary_http": {"status_code": 200, "effective_profile": "legacy"},
            "mcp": {"ok": True, "effective_profile": "legacy"},
            "assessment_selection_enabled": False,
            "certified_serving_profiles": ["legacy"],
            "governed_serving_authorized": False,
            "exploratory_serving_authorized": False,
        },
    }
    proof_path.write_text(json.dumps(proof_payload), encoding="utf-8")
    proof_digest = hashlib.sha256(proof_path.read_bytes()).hexdigest()
    bundle = CalibrationArtifactBundle(
        calibration_version="dataset-v2",
        target=identity.model_dump(mode="json"),
        target_identity_digest=identity.identity_digest(),
        sampling_manifest_digest="e" * 64,
        split_manifest_digest="f" * 64,
        floors=_floors().model_dump(mode="json"),
        fitting_method="exact-stratum-reliability-bins-v1",
        profiles=profile_payload,
        holdout_metrics=[
            {
                "dimension": dimension,
                "stratum": "manual/unknown/fact/unknown",
                "n": 10,
                "brier": 0.1,
                "ece": 0.05,
                "covered_stratum": True,
            }
            for dimension in ("taxonomy", "retention", "epistemic")
        ],
        unsupported_strata=[],
        floor_results=floor_result.model_dump(mode="json"),
        floors_satisfied=True,
        authoritative_recall_evidence_digest=proof_digest,
    )
    artifact_path = tmp_path / "profiles.json"
    artifact_path.write_bytes(bundle.to_loader_payload())
    mismatch = prove_mismatch_uncalibrated(
        profiles,
        deployed,
        target_identity_digest=identity.identity_digest(),
        artifact_digest=bundle.artifact_digest(),
    )
    kwargs = dict(
        artifact_path=artifact_path,
        bundle=bundle,
        deployed_contract=deployed,
        deployed_repo_sha=identity.repo_sha,
        deployed_assessment_policy_version=identity.assessment_policy_version,
        certified_serving_profiles={"legacy"},
        selection_currently_enabled=False,
        floor_result=floor_result,
        mismatch_proof=mismatch,
    )
    absent = gate_checks(**kwargs, authoritative_recall_evidence_path=None)
    assert absent["recommendation"] == "KEEP_DISABLED"
    passed = gate_checks(**kwargs, authoritative_recall_evidence_path=proof_path)
    assert passed["recommendation"] == "ENABLE_DOGFOOD_SHADOW_SELECTION", [
        key for key, value in passed["checks"].items() if value is not True
    ]
    assert passed["checks"]["holdout_calibrated_performance"] is True
    partial_floor = floor_result.model_copy(update={"checks": {"holdout_size": True}})
    partial_floor_gate = gate_checks(
        **{**kwargs, "floor_result": partial_floor},
        authoritative_recall_evidence_path=proof_path,
    )
    assert partial_floor_gate["recommendation"] == "KEEP_DISABLED"
    partial_mismatch = mismatch.model_copy(update={"checks": {next(iter(mismatch.checks)): True}})
    partial_mismatch_gate = gate_checks(
        **{**kwargs, "mismatch_proof": partial_mismatch},
        authoritative_recall_evidence_path=proof_path,
    )
    assert partial_mismatch_gate["recommendation"] == "KEEP_DISABLED"
    invalid_metric_sets = [
        [{**metric, "brier": 1.0} for metric in bundle.holdout_metrics],
        [{**metric, "brier": float("nan")} for metric in bundle.holdout_metrics],
        [{**metric, "n": 9} for metric in bundle.holdout_metrics],
        [*bundle.holdout_metrics, bundle.holdout_metrics[0]],
    ]
    for invalid_metrics in invalid_metric_sets:
        bad_holdout_bundle = bundle.model_copy(update={"holdout_metrics": invalid_metrics})
        bad_holdout = gate_checks(
            **{**kwargs, "bundle": bad_holdout_bundle},
            authoritative_recall_evidence_path=proof_path,
        )
        assert bad_holdout["checks"]["holdout_calibrated_performance"] is False
    duplicate_profile_bundle = bundle.model_copy(
        update={"profiles": [*bundle.profiles, bundle.profiles[0]]}
    )
    duplicate_profile_gate = gate_checks(
        **{**kwargs, "bundle": duplicate_profile_bundle},
        authoritative_recall_evidence_path=proof_path,
    )
    assert duplicate_profile_gate["checks"]["holdout_calibrated_performance"] is False
    failed_mismatch = mismatch.model_copy(
        update={"checks": {**mismatch.checks, "mismatch_provider_stays_uncalibrated": False}}
    )
    assert (
        gate_checks(
            **{**kwargs, "mismatch_proof": failed_mismatch},
            authoritative_recall_evidence_path=proof_path,
        )["recommendation"]
        == "KEEP_DISABLED"
    )
    proof_payload["artifact_digest"] = "0" * 64
    proof_path.write_text(json.dumps(proof_payload), encoding="utf-8")
    assert (
        gate_checks(**kwargs, authoritative_recall_evidence_path=proof_path)["recommendation"]
        == "KEEP_DISABLED"
    )
    proof_payload["artifact_digest"] = bundle.artifact_digest()
    proof_payload["observations"]["ordinary_http"]["status_code"] = 503
    proof_path.write_text(json.dumps(proof_payload), encoding="utf-8")
    assert (
        gate_checks(**kwargs, authoritative_recall_evidence_path=proof_path)["recommendation"]
        == "KEEP_DISABLED"
    )
    proof_payload["observations"]["ordinary_http"]["status_code"] = 200
    proof_payload["captured_at"] = (datetime.now(UTC) - timedelta(days=2)).isoformat()
    proof_path.write_text(json.dumps(proof_payload), encoding="utf-8")
    assert (
        gate_checks(**kwargs, authoritative_recall_evidence_path=proof_path)["recommendation"]
        == "KEEP_DISABLED"
    )
    proof_payload["captured_at"] = datetime.now(UTC).isoformat()
    proof_path.write_text(json.dumps(proof_payload), encoding="utf-8")
    assert (
        gate_checks(
            **{**kwargs, "certified_serving_profiles": {"legacy", "governed"}},
            authoritative_recall_evidence_path=proof_path,
        )["recommendation"]
        == "KEEP_DISABLED"
    )

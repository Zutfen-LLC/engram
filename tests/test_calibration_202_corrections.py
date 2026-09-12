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
from evals.calibration.gate import (
    REQUIRED_FLOOR_CHECKS,
    _load_authoritative_recall_proof,
    gate_checks,
    prove_mismatch_uncalibrated,
)
from evals.calibration.review import (
    build_packets,
    freeze_ledger,
    verify_ledger,
    write_packets,
    write_protected_file,
)

NOW = datetime(2026, 9, 9, tzinfo=UTC)


def _production_config_version(model: str = "model") -> str:
    """Config identity produced by the exact helper production current_contract() uses."""
    from engram.assessments import assessment_config_version
    from engram.provider_clients import ClassificationProviderConfig

    return assessment_config_version(
        ClassificationProviderConfig(
            provider_adapter="openai",
            api_key=None,
            base_url="https://calibration.provider.test/v1",
            model=model,
            sanitized_provider_host="calibration.provider.test",
        )
    )


def _identity() -> TargetIdentity:
    return TargetIdentity(
        campaign_id="campaign",
        campaign_tooling_repo_sha="a" * 40,
        assessment_schema_version="engram.assessment.v1",
        assessment_code_version="assessment-engine-v1",
        prompt_version="engram.assess.2",
        provider_adapter="openai",
        provider_model="model",
        provider_config_digest=_production_config_version(),
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
        campaign_tooling_repo_sha="1" * 40,
        derive_provider_config_digest=False,
        provider_config_digest=_production_config_version(),
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
    # Round-2 re-freeze complete: the manifest now records the real frozen
    # identity, and no pending_refreeze markers remain anywhere.
    assert manifest["identity_digest"] == (
        "57fc03918d5335c2925e2e6402fcadc4a29f5ef138fba6e6d839e9d8ec292ef9"
    )
    assert "pending_refreeze" not in manifest["identity"]
    assert "pending_refreeze" not in manifest["sampling"]
    assert manifest["identity"]["campaign_tooling_repo_sha"] == (
        "25256f7615c27be683e09e604df1a3bb553ff061"
    )
    assert manifest["sampling"]["sampling_manifest_digest"] == (
        "ed2e0c80bfe0c30c39d5ad5bc5656b007617320484efae14c87fa51027d66b3d"
    )
    assert manifest["sampling"]["split_manifest_digest"] == (
        "a2a27ed4c0152bf2d9b6c318bbfcfd6e5e20944184a0cc9df18cb2b4e3fbb72b"
    )
    assert manifest["packet_digests"] == {
        "eng-calibration-001f-blind-v2.reviewer_a.json": (
            "07f9fdbfdaae080fd860dc08f22cd56432826e115a017085004e92a0c9a7d5d4"
        ),
        "eng-calibration-001f-blind-v2.reviewer_b.json": (
            "dbd952815222c232dd8a011c6972cb712963da77c1fedba04e712e57a2f82618"
        ),
    }
    assert manifest["status"] == "STOPPED_FOR_HUMAN_ADJUDICATION"
    assert "pending_refreeze" not in blob
    # The invalidation trail is preserved, and no human labels preceded it.
    assert manifest["correction"]["status"] == "INVALIDATED_AND_SUPERSEDED_PRE_REVIEW"
    assert manifest["correction"]["human_labels_accepted_before_invalidation"] is False
    assert manifest["review_plan"]["human_adjudication_complete"] is False
    superseded = manifest["correction"]["superseded_freezes"]
    round2 = next(
        entry
        for entry in superseded
        if entry.get("campaign_tooling_repo_sha") == "143ff3ffa23cf3ab5884ce11d19c12621ade5aca"
    )
    assert round2["identity_digest"] == (
        "b81348f7b6f14cf4eaf2ca735ae266babc410bb79299c247150bda054ab01614"
    )
    assert round2["sampling_manifest_digest"] and round2["split_manifest_digest"]
    # The corrected campaign stores config identity in production representation only.
    schema = manifest["identity"]["schema"]
    assert "sha256:<64hex>" in schema["provider_config_digest"]
    assert "tooling revision" in schema["campaign_tooling_repo_sha"]


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
    # #206 FIX-6 lane workflow commands are implemented
    for available in (
        "model-lane-init",
        "model-lane-request",
        "model-lane-ingest",
        "model-lane-status",
    ):
        assert available in result.stdout
    # unimplemented downstream commands must stay unadvertised
    for unavailable in ("ledger-freeze", "fit", "gate", "final-report"):
        assert f"  {unavailable} " not in result.stdout


def test_serving_and_selection_invariants_remain_exact() -> None:
    from engram.config import settings
    from engram.recall_profiles import CERTIFIED_SERVING_PROFILES

    assert frozenset({"legacy"}) == CERTIFIED_SERVING_PROFILES
    assert settings.assessment_selection_enabled is False


def _contract() -> AssessmentContract:
    return AssessmentContract(
        provider="openai",
        model="model",
        config_version=_production_config_version(),
        calibration_version="dataset-v2",
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
    # The campaign was frozen at tooling SHA `identity.campaign_tooling_repo_sha`;
    # the runtime under test runs a later, different revision. The gate must not
    # equate the two (FIX-R2-2).
    deployed_repo_sha = "b" * 40
    assert deployed_repo_sha != identity.campaign_tooling_repo_sha
    proof_path = tmp_path / "authoritative-recall-proof.json"
    proof_payload: dict[str, Any] = {
        "proof_schema": "engram-authoritative-recall-proof-v1",
        "campaign_id": identity.campaign_id,
        "target_identity_digest": identity.identity_digest(),
        "profile_set_digest": profile_digest,
        "deployed_repo_sha": deployed_repo_sha,
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
        deployed_repo_sha=deployed_repo_sha,
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
    # --- Non-vacuous semantic negatives (FIX-R2-3) --------------------------
    # Mutating proof bytes invalidates the frozen proof-file SHA. Unless that
    # digest is rebound (and with it the artifact digest and mismatch proof),
    # the gate rejects at the digest check and the intended semantic guard is
    # never reached. Every semantic case below therefore rebinds first.

    def _rebind(payload: dict[str, Any]) -> dict[str, Any]:
        proof_path.write_text(json.dumps(payload), encoding="utf-8")
        rebound_digest = hashlib.sha256(proof_path.read_bytes()).hexdigest()
        rebound_bundle = bundle.model_copy(
            update={"authoritative_recall_evidence_digest": rebound_digest}
        )
        # A digest mismatch and a semantic mismatch both surface as the single
        # `authoritative_recall_unchanged` check being false. Proving the proof
        # parses under its rebound digest is what makes the negatives below
        # attributable to the mutated field rather than to stale bytes.
        assert _load_authoritative_recall_proof(proof_path, rebound_digest) is not None
        rebound_mismatch = prove_mismatch_uncalibrated(
            profiles,
            deployed,
            target_identity_digest=identity.identity_digest(),
            artifact_digest=rebound_bundle.artifact_digest(),
        )
        return {**kwargs, "bundle": rebound_bundle, "mismatch_proof": rebound_mismatch}

    def _only_failure(result: dict[str, Any]) -> list[str]:
        return sorted(key for key, value in result["checks"].items() if value is False)

    def _rebound_gate(payload: dict[str, Any]) -> dict[str, Any]:
        return gate_checks(**_rebind(payload), authoritative_recall_evidence_path=proof_path)

    # Control: rebinding alone must leave the gate passing, so any failure below
    # is attributable to the mutated field and not to the rebinding itself.
    control = _rebound_gate(dict(proof_payload))
    assert control["recommendation"] == "ENABLE_DOGFOOD_SHADOW_SELECTION", _only_failure(control)

    # Retained digest-integrity case: proof bytes changed WITHOUT rebinding.
    tampered_payload = {**proof_payload, "campaign_id": "tampered-campaign"}
    proof_path.write_text(json.dumps(tampered_payload), encoding="utf-8")
    # Here the proof genuinely fails to load: the frozen digest no longer matches.
    assert _load_authoritative_recall_proof(proof_path, proof_digest) is None
    unbound = gate_checks(**kwargs, authoritative_recall_evidence_path=proof_path)
    assert _only_failure(unbound) == ["authoritative_recall_unchanged"]
    assert unbound["recommendation"] == "KEEP_DISABLED"

    # Ordinary /v1/recall did not answer 200.
    http_failed = _rebound_gate(
        {
            **proof_payload,
            "observations": {
                **proof_payload["observations"],
                "ordinary_http": {"status_code": 503, "effective_profile": "legacy"},
            },
        }
    )
    assert _only_failure(http_failed) == ["authoritative_recall_unchanged"]
    assert http_failed["recommendation"] == "KEEP_DISABLED"

    # Ordinary recall answered from a non-legacy profile.
    drifted_recall = _rebound_gate(
        {
            **proof_payload,
            "observations": {
                **proof_payload["observations"],
                "ordinary_http": {"status_code": 200, "effective_profile": "governed"},
            },
        }
    )
    assert _only_failure(drifted_recall) == ["authoritative_recall_unchanged"]

    # MCP recall lost legacy authority.
    drifted_mcp = _rebound_gate(
        {
            **proof_payload,
            "observations": {
                **proof_payload["observations"],
                "mcp": {"ok": True, "effective_profile": "governed"},
            },
        }
    )
    assert _only_failure(drifted_mcp) == ["authoritative_recall_unchanged"]

    # Proof is older than the 24h freshness bound; everything else is valid.
    stale = _rebound_gate(
        {**proof_payload, "captured_at": (datetime.now(UTC) - timedelta(days=2)).isoformat()}
    )
    assert _only_failure(stale) == ["authoritative_recall_unchanged"]
    assert stale["recommendation"] == "KEEP_DISABLED"

    # Certified serving profiles drifted on the probed runtime.
    proof_serving_drift = _rebound_gate(
        {
            **proof_payload,
            "observations": {
                **proof_payload["observations"],
                "certified_serving_profiles": ["legacy", "governed"],
            },
        }
    )
    assert _only_failure(proof_serving_drift) == ["authoritative_recall_unchanged"]

    # Selection was observed enabled on the probed runtime.
    proof_selection_enabled = _rebound_gate(
        {
            **proof_payload,
            "observations": {**proof_payload["observations"], "assessment_selection_enabled": True},
        }
    )
    assert _only_failure(proof_selection_enabled) == ["authoritative_recall_unchanged"]

    # Governed serving authorized on the probed runtime.
    proof_governed = _rebound_gate(
        {
            **proof_payload,
            "observations": {**proof_payload["observations"], "governed_serving_authorized": True},
        }
    )
    assert _only_failure(proof_governed) == ["authoritative_recall_unchanged"]

    # The proof is bound to a runtime other than the one being gated.
    wrong_sha_binding = _rebound_gate({**proof_payload, "deployed_repo_sha": "9" * 40})
    assert _only_failure(wrong_sha_binding) == ["authoritative_recall_unchanged"]

    # The proof observed a different deployed contract than the gated one.
    wrong_contract_binding = _rebound_gate({**proof_payload, "deployed_contract_digest": "0" * 64})
    assert _only_failure(wrong_contract_binding) == ["authoritative_recall_unchanged"]

    # The proof is bound to a different campaign identity.
    wrong_identity_binding = _rebound_gate({**proof_payload, "target_identity_digest": "0" * 64})
    assert _only_failure(wrong_identity_binding) == ["authoritative_recall_unchanged"]

    # --- Deployment-side negatives ------------------------------------------
    # Restore a valid, rebound proof and vary the deployment arguments instead.
    valid_kwargs = _rebind(dict(proof_payload))

    serving_drift = gate_checks(
        **{**valid_kwargs, "certified_serving_profiles": {"legacy", "governed"}},
        authoritative_recall_evidence_path=proof_path,
    )
    assert _only_failure(serving_drift) == ["certified_serving_profiles_exact"]
    assert serving_drift["recommendation"] == "KEEP_DISABLED"

    already_enabled = gate_checks(
        **{**valid_kwargs, "selection_currently_enabled": True},
        authoritative_recall_evidence_path=proof_path,
    )
    assert _only_failure(already_enabled) == ["selection_currently_disabled"]
    assert already_enabled["recommendation"] == "KEEP_DISABLED"

    # A deployed contract that differs from the calibrated one fails on identity,
    # calibration binding, mismatch binding, and the proof's contract binding.
    wrong_contract = deployed.model_copy(update={"model": "other-model"})
    wrong_deployment = gate_checks(
        **{**valid_kwargs, "deployed_contract": wrong_contract},
        authoritative_recall_evidence_path=proof_path,
    )
    assert wrong_deployment["checks"]["identity_matches_deployment"] is False
    assert wrong_deployment["checks"]["authoritative_recall_unchanged"] is False
    assert wrong_deployment["recommendation"] == "KEEP_DISABLED"

    # A deployed config identity in bare-hex form is a different identity and
    # must never be accepted as equivalent to the production representation.
    bare_hex_contract = deployed.model_copy(
        update={"config_version": deployed.config_version.removeprefix("sha256:")}
    )
    bare_hex_deployment = gate_checks(
        **{**valid_kwargs, "deployed_contract": bare_hex_contract},
        authoritative_recall_evidence_path=proof_path,
    )
    assert bare_hex_deployment["checks"]["identity_matches_deployment"] is False
    assert bare_hex_deployment["recommendation"] == "KEEP_DISABLED"

    # The gated runtime is not the one the proof probed.
    wrong_runtime = gate_checks(
        **{**valid_kwargs, "deployed_repo_sha": "7" * 40},
        authoritative_recall_evidence_path=proof_path,
    )
    assert _only_failure(wrong_runtime) == ["authoritative_recall_unchanged"]


# --- FIX-R2-1: one canonical config identity, end to end ---------------------


def test_production_current_contract_emits_prefixed_config_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The production contract's config identity is `sha256:<64hex>`, not bare hex."""
    from engram.assessments import assessment_config_version, current_contract
    from engram.provider_clients import ClassificationProviderConfig

    provider = ClassificationProviderConfig(
        provider_adapter="openai",
        api_key=None,
        base_url="https://calibration.provider.test/v1",
        model="model",
        sanitized_provider_host="calibration.provider.test",
    )
    monkeypatch.setattr(
        "engram.provider_clients.resolve_classification_provider", lambda *a, **k: provider
    )
    monkeypatch.setattr("engram.assessments.resolve_classification_provider", lambda *a: provider)
    contract = current_contract()
    assert contract.config_version == assessment_config_version(provider)
    assert re.fullmatch(r"sha256:[0-9a-f]{64}", contract.config_version)
    # The bare-hex form is a different identity and must never stand in for it.
    assert contract.config_version != contract.config_version.removeprefix("sha256:")


def test_frozen_target_requires_exact_production_config_representation() -> None:
    """TargetIdentity stores the production representation verbatim."""
    identity = _identity()
    assert identity.provider_config_digest == _production_config_version()
    assert re.fullmatch(r"sha256:[0-9a-f]{64}", identity.provider_config_digest)
    # A bare 64-hex digest cannot be frozen as campaign config identity at all.
    with pytest.raises(ValueError):
        _identity().model_copy(
            update={"provider_config_digest": _production_config_version().removeprefix("sha256:")}
        ).model_validate(
            {
                **_identity().model_dump(mode="json"),
                "provider_config_digest": _production_config_version().removeprefix("sha256:"),
            }
        )


def test_freeze_target_cli_rejects_bare_hex_and_derives_from_production(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`freeze-target` refuses non-production form and can derive the real value."""
    from argparse import Namespace

    from engram.provider_clients import ClassificationProviderConfig
    from evals.calibration.__main__ import cmd_freeze_target

    bare = Namespace(
        campaign_tooling_repo_sha="1" * 40,
        derive_provider_config_digest=False,
        provider_config_digest=_production_config_version().removeprefix("sha256:"),
        output=tmp_path / "bare" / "identity.json",
    )
    with pytest.raises(SystemExit):
        cmd_freeze_target(bare)
    assert not (tmp_path / "bare" / "identity.json").exists()

    provider = ClassificationProviderConfig(
        provider_adapter="openai",
        api_key=None,
        base_url="https://calibration.provider.test/v1",
        model="model",
        sanitized_provider_host="calibration.provider.test",
    )
    monkeypatch.setattr(
        "engram.provider_clients.resolve_classification_provider", lambda *a, **k: provider
    )
    derived_output = tmp_path / "derived" / "identity.json"
    derived = Namespace(
        campaign_tooling_repo_sha="1" * 40,
        derive_provider_config_digest=True,
        provider_config_digest=None,
        output=derived_output,
    )
    assert cmd_freeze_target(derived) == 0
    frozen = json.loads(derived_output.read_text())
    # Derived through the exact helper production current_contract() uses.
    assert frozen["target_identity"]["provider_config_digest"] == _production_config_version()


def test_production_calibrate_accepts_frozen_identity_and_rejects_config_drift() -> None:
    """The frozen identity survives all the way into production calibration."""
    from engram.assessment_calibration import calibrate

    identity = _identity()
    contract = _contract()
    # Campaign identity and the deployed contract are the same exact string.
    assert contract.config_version == identity.provider_config_digest

    profile = _profiles()[0]
    assert profile.contract.config_version == identity.provider_config_digest
    supported = next(b for b in profile.bins if b.count >= 50)
    raw = (supported.lower + supported.upper) / 2

    calibrated = calibrate(
        raw,
        profile=profile,
        contract=contract,
        dimension=profile.dimension,
        source_type=profile.source_type,
        assertion_mode=profile.assertion_mode,
        kind=profile.kind,
        risk=profile.risk,
    )
    assert calibrated.status == "calibrated"

    # Bare-hex config identity is a genuinely different contract: fail closed.
    bare_hex = contract.model_copy(
        update={"config_version": contract.config_version.removeprefix("sha256:")}
    )
    assert (
        calibrate(
            raw,
            profile=profile,
            contract=bare_hex,
            dimension=profile.dimension,
            source_type=profile.source_type,
            assertion_mode=profile.assertion_mode,
            kind=profile.kind,
            risk=profile.risk,
        ).status
        == "uncalibrated"
    )
    # Any other config identity change is equally fail-closed.
    assert (
        calibrate(
            raw,
            profile=profile,
            contract=contract.model_copy(update={"config_version": "sha256:" + "0" * 64}),
            dimension=profile.dimension,
            source_type=profile.source_type,
            assertion_mode=profile.assertion_mode,
            kind=profile.kind,
            risk=profile.risk,
        ).status
        == "uncalibrated"
    )


def test_no_calibration_fixture_uses_bare_hex_config_identity() -> None:
    """Guard against a fixture silently reintroducing the non-production form."""
    # Structural: every fixture identity/contract carries the production form.
    assert re.fullmatch(r"sha256:[0-9a-f]{64}", _identity().provider_config_digest)
    assert re.fullmatch(r"sha256:[0-9a-f]{64}", _contract().config_version)
    for profile in _profiles():
        assert re.fullmatch(r"sha256:[0-9a-f]{64}", profile.contract.config_version)

    # Textual: no hand-authored bare-hex config identity anywhere in the suites.
    # The pattern is written so it cannot match its own source line.
    bare_hex_literal = re.compile(
        r"(?:provider_)?config_" + r"(?:version|digest)" + r'\s*=\s*"[0-9a-f]"\s*\*\s*64'
    )
    for path in (
        Path("tests/test_calibration_202.py"),
        Path("tests/test_calibration_202_corrections.py"),
    ):
        offenders = bare_hex_literal.findall(path.read_text())
        assert not offenders, (path, offenders)


# --- FIX-R2-2: campaign tooling provenance vs deployed runtime identity ------


def _gate_fixture(tmp_path: Path, *, deployed_repo_sha: str) -> dict[str, Any]:
    """A fully valid gate invocation, parameterized by the deployed runtime SHA."""
    from evals.calibration.fit import CalibrationArtifactBundle

    identity = _identity()
    profiles = _profiles()
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
    proof_path = tmp_path / "proof.json"
    proof_path.write_text(
        json.dumps(
            {
                "proof_schema": "engram-authoritative-recall-proof-v1",
                "campaign_id": identity.campaign_id,
                "target_identity_digest": identity.identity_digest(),
                "profile_set_digest": profile_digest,
                "deployed_repo_sha": deployed_repo_sha,
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
        ),
        encoding="utf-8",
    )
    bundle = CalibrationArtifactBundle(
        calibration_version="dataset-v2",
        target=identity.model_dump(mode="json"),
        target_identity_digest=identity.identity_digest(),
        sampling_manifest_digest="e" * 64,
        split_manifest_digest="f" * 64,
        floors=_floors().model_dump(mode="json"),
        fitting_method="exact-stratum-reliability-bins-v1",
        profiles=[p.model_dump(mode="json") for p in profiles],
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
        authoritative_recall_evidence_digest=hashlib.sha256(proof_path.read_bytes()).hexdigest(),
    )
    artifact_path = tmp_path / "profiles.json"
    artifact_path.write_bytes(bundle.to_loader_payload())
    return {
        "identity": identity,
        "proof_path": proof_path,
        "kwargs": dict(
            artifact_path=artifact_path,
            bundle=bundle,
            deployed_contract=deployed,
            deployed_repo_sha=deployed_repo_sha,
            deployed_assessment_policy_version=identity.assessment_policy_version,
            certified_serving_profiles={"legacy"},
            selection_currently_enabled=False,
            floor_result=floor_result,
            mismatch_proof=prove_mismatch_uncalibrated(
                profiles,
                deployed,
                target_identity_digest=identity.identity_digest(),
                artifact_digest=bundle.artifact_digest(),
            ),
        ),
    }


def test_gate_passes_when_deployed_runtime_sha_differs_from_campaign_tooling_sha(
    tmp_path: Path,
) -> None:
    """A merge commit changes the runtime SHA; it must not invalidate the campaign.

    The campaign is frozen at tooling SHA `A`. The runtime later runs SHA `B`.
    With the assessment contract and every serving invariant unchanged, the gate
    must still pass: `A != B` is expected once the campaign branch merges, and
    re-freezing on every merge is not the #202 contract.
    """
    deployed_repo_sha = "b" * 40
    fixture = _gate_fixture(tmp_path, deployed_repo_sha=deployed_repo_sha)
    identity = fixture["identity"]
    campaign_tooling_repo_sha = identity.campaign_tooling_repo_sha

    assert campaign_tooling_repo_sha != deployed_repo_sha
    # The campaign keeps its tooling provenance in the frozen target.
    assert re.fullmatch(r"[0-9a-f]{40}", campaign_tooling_repo_sha)

    result = gate_checks(
        **fixture["kwargs"], authoritative_recall_evidence_path=fixture["proof_path"]
    )
    assert result["recommendation"] == "ENABLE_DOGFOOD_SHADOW_SELECTION", [
        key for key, value in result["checks"].items() if value is False
    ]
    assert result["checks"]["identity_matches_deployment"] is True
    assert result["checks"]["authoritative_recall_unchanged"] is True
    # The scope never widens beyond dogfood shadow selection.
    assert result["scope"] == "dogfood_shadow_selection_only"


def test_gate_still_passes_when_tooling_and_runtime_sha_coincide(tmp_path: Path) -> None:
    """Divergence is tolerated, not required."""
    identity_sha = _identity().campaign_tooling_repo_sha
    fixture = _gate_fixture(tmp_path, deployed_repo_sha=identity_sha)
    result = gate_checks(
        **fixture["kwargs"], authoritative_recall_evidence_path=fixture["proof_path"]
    )
    assert result["recommendation"] == "ENABLE_DOGFOOD_SHADOW_SELECTION"


def test_campaign_tooling_sha_is_not_a_runtime_trust_signal(tmp_path: Path) -> None:
    """A newer runtime SHA is never automatically trusted when the assessment contract differs."""
    fixture = _gate_fixture(tmp_path, deployed_repo_sha="b" * 40)
    kwargs = fixture["kwargs"]
    drifted = kwargs["deployed_contract"].model_copy(update={"prompt_version": "engram.assess.1"})
    # The gate fixture uses the current corrected identity (engram.assess.2),
    # so drifting to the superseded engram.assess.1 is real contract drift and
    # must fail closed, exactly like any other identity divergence.
    assert (
        gate_checks(
            **{**kwargs, "deployed_contract": drifted},
            authoritative_recall_evidence_path=fixture["proof_path"],
        )["recommendation"]
        == "KEEP_DISABLED"
    )
    # Any real contract drift on that newer SHA fails closed.
    for update in (
        {"provider": "other"},
        {"model": "other"},
        {"config_version": "sha256:" + "0" * 64},
        {"calibration_version": "other"},
        {"calibration_digest": "sha256:" + "0" * 64},
    ):
        contract_drift = kwargs["deployed_contract"].model_copy(update=update)
        drifted_gate = gate_checks(
            **{**kwargs, "deployed_contract": contract_drift},
            authoritative_recall_evidence_path=fixture["proof_path"],
        )
        assert drifted_gate["recommendation"] == "KEEP_DISABLED", update


def test_campaign_identity_change_does_not_move_sample_or_split_membership() -> None:
    """Re-freezing for an identity correction must not disturb the corpus.

    Sampling and splitting consume the snapshot, campaign id, and seeds -- never
    the target identity digest. So correcting the config representation or the
    tooling-SHA field name changes the recorded manifest digests but leaves
    eligible/sampled/dev/holdout membership byte-identical.
    """
    frame = _frame(40)
    sample_ids, stratum_counts, _ = stratified_sample(
        frame, campaign_id="campaign", sampling_seed="seed", coverage_min=2
    )
    dev_ids, holdout_ids, _, _ = assign_splits(
        frame,
        sample_ids=sample_ids,
        campaign_id="campaign",
        split_seed="split",
        dev_fraction=0.6,
    )

    old_identity = _identity()
    # The corrected identity: different config representation and tooling SHA.
    new_identity = _identity().model_copy(
        update={
            "campaign_tooling_repo_sha": "c" * 40,
            "provider_config_digest": "sha256:" + "1" * 64,
        }
    )
    assert old_identity.identity_digest() != new_identity.identity_digest()

    resampled_ids, resampled_counts, _ = stratified_sample(
        frame, campaign_id="campaign", sampling_seed="seed", coverage_min=2
    )
    redev_ids, reholdout_ids, _, _ = assign_splits(
        frame,
        sample_ids=resampled_ids,
        campaign_id="campaign",
        split_seed="split",
        dev_fraction=0.6,
    )
    assert resampled_ids == sample_ids
    assert resampled_counts == stratum_counts
    assert (redev_ids, reholdout_ids) == (dev_ids, holdout_ids)
    assert set(dev_ids) | set(holdout_ids) == set(sample_ids)
    assert not set(dev_ids) & set(holdout_ids)

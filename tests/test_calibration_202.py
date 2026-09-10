"""Focused #202 campaign contract tests.

Proves (pre-review, mechanically): deterministic manifests, split leakage
rejection, blind-packet discipline, fail-closed label ingestion, deterministic
fitting, loader reproduction, mismatch fail-closed, undersampled strata staying
uncalibrated, and privacy of public outputs. Human-review-dependent checks
(high-consequence dual review on real data, holdout metrics on real labels)
are exercised through the same code paths on synthetic fixtures.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from engram.assessment_calibration import load_profiles
from engram.assessment_schema import AssessmentContract
from engram.canonicalize import canonicalize, content_hash
from evals.admission.schema import LabelRecord, digest
from evals.calibration.fit import (
    EvidenceFloorResult,
    LabeledObservation,
    build_artifact,
    evaluate_holdout,
    fit_profiles,
)
from evals.calibration.freeze import (
    EvidenceFloors,
    SamplingManifest,
    SplitManifest,
    TargetIdentity,
    assign_splits,
    build_frame,
    normalized_text_hash,
    sample_id_for,
    stratified_sample,
)
from evals.calibration.review import (
    build_packets,
    ingest_reviewer_labels,
    packet_file_digest,
    reviewer_agreement,
    write_packets,
)

NOW = datetime(2026, 9, 9, tzinfo=UTC)


def _identity() -> TargetIdentity:
    return TargetIdentity(
        campaign_id="test-campaign",
        repo_sha="a" * 40,
        assessment_schema_version="engram.assessment.v1",
        assessment_code_version="assessment-engine-v1",
        prompt_version="engram.assess.1",
        provider_adapter="openai",
        provider_model="test-model",
        provider_config_digest="b" * 64,
        provider_params={"temperature": 0},
        assessment_policy_version="assessment-selection-v1",
        calibration_artifact_schema_version="engram.calibration-profiles-v1",
        calibration_dataset_version="cal-test-v1",
        label_guide_version="engram-calibration-guide-157-v1",
        canonicalization_version="assessment-evidence-manifest-v1",
        dimensions=("taxonomy", "retention", "epistemic"),
    )


def _contract(identity: TargetIdentity) -> AssessmentContract:
    return AssessmentContract(
        provider=identity.provider_adapter,
        model=identity.provider_model,
        config_version="fixture",
        calibration_version=identity.calibration_dataset_version,
    )


def _floor_result(sampling_digest: str, split_digest: str) -> EvidenceFloorResult:
    return EvidenceFloorResult(
        sampling_manifest_digest=sampling_digest,
        split_manifest_digest=split_digest,
        ledger_sha256="a" * 64,
        reviewer_a_packet_sha256="b" * 64,
        reviewer_b_packet_sha256="c" * 64,
        assessment_evidence_sha256="d" * 64,
        assessment_contract_digest="e" * 64,
        full_population_dual_review=True,
        checks={"holdout_size": True},
        dimension_support={},
        stratum_support={},
        bin_support={},
        failures=(),
        passed=True,
    )


def _split_for_observations(observations: list[LabeledObservation]) -> SplitManifest:
    by_id = {observation.sample_id: observation.split for observation in observations}
    return SplitManifest(
        campaign_id="c",
        sampling_manifest_digest="d" * 64,
        sampling_membership_digest=digest(sorted(by_id)),
        split_seed="s",
        dev_fraction=0.6,
        grouping=("content_hash",),
        dev_ids=tuple(sorted(sample_id for sample_id, split in by_id.items() if split == "dev")),
        holdout_ids=tuple(
            sorted(sample_id for sample_id, split in by_id.items() if split == "holdout")
        ),
        leakage_checks={},
    )


def _artifact_split() -> SplitManifest:
    return SplitManifest(
        campaign_id="c",
        sampling_manifest_digest="d" * 64,
        sampling_membership_digest=digest(["s1", "s2"]),
        split_seed="s",
        dev_fraction=0.6,
        grouping=("content_hash",),
        dev_ids=("s1",),
        holdout_ids=("s2",),
        leakage_checks={},
    )


def _frame(n: int = 40):
    rows = []
    content_by_uuid = {}
    for i in range(n):
        uuid = f"00000000-0000-0000-0000-{i:012d}"
        content = f"Memory fact number {i} about host{i}.example.com."
        rows.append(
            {
                "item_uuid": uuid,
                "content_hash": f"sha256:{digest(content)}",
                "kind": "fact" if i % 3 else "decision",
                "source_type": ["manual", "sync_turn", "extraction"][i % 3],
                "review_status": "active" if i % 2 else "proposed",
                "assertion_mode": "unknown",
                "origin": "unknown",
                "risk": "unknown",
                "created_at": NOW,
                "valid_to": None,
            }
        )
        content_by_uuid[uuid] = content
    return build_frame(rows, content_by_uuid=content_by_uuid, snapshot_as_of=NOW)


class TestDeterministicManifests:
    def test_sampling_deterministic(self):
        frame, _ = _frame()
        ids1, strata1, coverage1 = stratified_sample(
            frame, campaign_id="c", sampling_seed="s", coverage_min=1, allocation_fraction=0.5
        )
        ids2, strata2, coverage2 = stratified_sample(
            frame, campaign_id="c", sampling_seed="s", coverage_min=1, allocation_fraction=0.5
        )
        assert ids1 == ids2 and strata1 == strata2 and coverage1 == coverage2
        # different seed changes membership (deterministic, not constant)
        ids3, _, _ = stratified_sample(
            frame, campaign_id="c", sampling_seed="t", coverage_min=1, allocation_fraction=0.5
        )
        assert ids1 != ids3

    def test_manifest_digest_binds_membership(self):
        frame, _ = _frame()
        ids, strata, coverage = stratified_sample(
            frame, campaign_id="c", sampling_seed="s", coverage_min=1, allocation_fraction=0.5
        )
        m1 = SamplingManifest(
            campaign_id="c",
            target_identity_digest="d" * 64,
            frame_digest="f" * 64,
            snapshot_sha256="e" * 64,
            snapshot_as_of=NOW,
            sampling_seed="s",
            inclusion_rules=("r1",),
            exclusion_rules=(),
            source_row_counts={"frame": len(frame)},
            stratum_counts=strata,
            coverage_dimensions=coverage,
            sample_ids=tuple(ids),
            sample_hashes=("f" * 64,) * len(ids),
        )
        m2 = m1.model_copy(update={"sample_ids": tuple(ids[:-1]), "stratum_counts": {}})
        with pytest.raises((ValueError, ValidationError)):
            m2.model_validate(m2.model_dump())  # stratum count now inconsistent
        assert m1.manifest_digest() == m1.model_copy().manifest_digest()
        assert m1.manifest_digest() != m2.manifest_digest()


class TestSplitLeakage:
    def test_duplicates_grouped_into_one_split(self):
        frame, _ = _frame()
        # force a duplicate: same normalized content, different uuid
        dup = frame[0].model_copy(update={"item_uuid": "00000000-0000-0000-0001-000000000001"})
        frame.append(dup)
        dev, holdout, checks, groups = assign_splits(
            frame,
            sample_ids=[sample_id_for(row.item_uuid) for row in frame],
            campaign_id="c",
            split_seed="s",
            dev_fraction=0.6,
        )
        assert checks["duplicate_groups"] >= 1
        assert not (set(dev) & set(holdout))
        for members in groups.values():
            owners = {("dev" if m in dev else "holdout") for m in members}
            assert len(owners) == 1, "duplicate group leaked across splits"

    def test_paraphrase_normalized_hash_groups(self):
        a = normalized_text_hash("The  quick   brown fox!")
        b = normalized_text_hash("the quick brown fox")
        assert a == b

    def test_split_manifest_rejects_overlap(self):
        with pytest.raises((ValueError, ValidationError)):
            SplitManifest(
                campaign_id="c",
                sampling_manifest_digest="d" * 64,
                sampling_membership_digest=digest(["s1", "s2"]),
                split_seed="s",
                dev_fraction=0.6,
                grouping=("content_hash",),
                dev_ids=("s1", "s2"),
                holdout_ids=("s2", "s3"),  # overlap must fail
                leakage_checks={},
            )


class TestBlindPackets:
    def test_packets_exclude_scores_and_policy(self, tmp_path: Path):
        frame, _ = _frame()
        ids, _, _ = stratified_sample(
            frame, campaign_id="c", sampling_seed="s", coverage_min=3, allocation_fraction=1.0
        )
        samples = [
            {
                "sample_id": sid,
                "content": f"content for {sid}",
                "content_hash": content_hash(canonicalize(f"content for {sid}")),
                "kind": "fact",
                "source_type": "manual",
                "review_status": "active",
                "assertion_mode": "unknown",
                "origin": "unknown",
                "age_days": 5,
            }
            for sid in ids
        ]
        sampling = SamplingManifest(
            campaign_id="c",
            target_identity_digest="d" * 64,
            frame_digest="f" * 64,
            snapshot_sha256="e" * 64,
            snapshot_as_of=NOW,
            sampling_seed="s",
            inclusion_rules=("r",),
            exclusion_rules=(),
            source_row_counts={"frame": len(samples)},
            stratum_counts={"fact/manual/active": len(samples)},
            coverage_dimensions={},
            sample_ids=tuple(ids),
            sample_hashes=tuple(sample["content_hash"] for sample in samples),
        )
        packets = build_packets(sampling=sampling, samples=samples, packet_id="p1")
        assert len(packets) == 2
        blob = json.dumps([json.loads(p.model_dump_json()) for p in packets])
        for banned in ("raw_value", "taxonomy_value", "retention_value", "score", "policy"):
            assert banned not in blob, f"packet leaks {banned}"
        # both reviewers see identical case views
        assert packets[0].cases == packets[1].cases
        manifest = write_packets(packets, tmp_path / "protected")
        assert manifest and all(len(v) == 64 for v in manifest.values())
        assert (tmp_path / "protected").stat().st_mode & 0o777 == 0o700

    def test_ingestion_fails_closed(self, tmp_path: Path):
        frame, _ = _frame()
        ids, _, _ = stratified_sample(
            frame, campaign_id="c", sampling_seed="s", coverage_min=3, allocation_fraction=1.0
        )
        samples = [
            {
                "sample_id": sid,
                "content": "x",
                "content_hash": content_hash(canonicalize("x")),
                "kind": "fact",
                "source_type": "manual",
                "review_status": "active",
                "assertion_mode": "unknown",
                "origin": "unknown",
                "age_days": 1,
            }
            for sid in ids
        ]
        sampling = SamplingManifest(
            campaign_id="c",
            target_identity_digest="d" * 64,
            frame_digest="f" * 64,
            snapshot_sha256="e" * 64,
            snapshot_as_of=NOW,
            sampling_seed="s",
            inclusion_rules=("r",),
            exclusion_rules=(),
            source_row_counts={"frame": len(samples)},
            stratum_counts={"a": len(samples)},
            coverage_dimensions={},
            sample_ids=tuple(ids),
            sample_hashes=tuple(sample["content_hash"] for sample in samples),
        )
        packet = build_packets(sampling=sampling, samples=samples, packet_id="p1")[0]
        labels = [
            LabelRecord.model_validate(
                {
                    "sample_id": sid,
                    "label_schema_version": "engram-admission-label-v1",
                    "dataset_id": "ds",
                    "dataset_version": "v1",
                    "fixture_role": "ordinary_claim",
                    "label_origin": "human_adjudicated",
                    "reviewer_a": _judgment("ra"),
                    "reviewer_b": None,
                    "resolution": None,
                    "disagreement": "none",
                }
            )
            for sid in ids
        ]
        # valid ingestion passes
        ingest_reviewer_labels(
            packet=packet,
            labels=labels,
            dataset_id="ds",
            dataset_version="v1",
            expected_packet_digest=packet_file_digest(packet),
            expected_reviewer_hint="reviewer_a",
        )
        with pytest.raises(ValueError, match="packet_digest_mismatch"):
            ingest_reviewer_labels(
                packet=packet,
                labels=labels,
                dataset_id="ds",
                dataset_version="v1",
                expected_packet_digest="0" * 64,
                expected_reviewer_hint="reviewer_a",
            )
        # wrong count fails
        with pytest.raises((ValueError, ValidationError)):
            ingest_reviewer_labels(
                packet=packet,
                labels=labels[:-1],
                dataset_id="ds",
                dataset_version="v1",
                expected_packet_digest=packet_file_digest(packet),
                expected_reviewer_hint="reviewer_a",
            )
        # wrong dataset identity fails
        with pytest.raises((ValueError, ValidationError)):
            ingest_reviewer_labels(
                packet=packet,
                labels=labels,
                dataset_id="other",
                dataset_version="v1",
                expected_packet_digest=packet_file_digest(packet),
                expected_reviewer_hint="reviewer_a",
            )

    def test_high_consequence_requires_dual_review(self):
        base = _judgment("ra")
        dims = base.dimensions.model_dump()
        dims["consequence"] = "high"
        with pytest.raises((ValueError, ValidationError)):
            LabelRecord.model_validate(
                {
                    "sample_id": "s1",
                    "label_schema_version": "engram-admission-label-v1",
                    "dataset_id": "ds",
                    "dataset_version": "v1",
                    "fixture_role": "ordinary_claim",
                    "label_origin": "human_adjudicated",
                    "reviewer_a": {**base.model_dump(), "dimensions": dims},
                    "reviewer_b": None,
                    "resolution": None,
                    "disagreement": "none",
                }
            )


def _judgment(ref: str, consequence: str = "low"):
    from evals.admission.schema import HumanJudgment

    dimensions = {
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
        "epistemic_state": "unknown",
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
    }
    return HumanJudgment.model_validate(
        {
            "adjudicator_ref": ref,
            "adjudicated_at": NOW,
            "adjudicator_confidence": "medium",
            "reason_code": "calibration-review",
            "dimensions": dimensions,
        }
    )


class TestFitting:
    def _observations(self, n_per_bin: int = 60, split_of="dev"):
        obs = []
        for i in range(n_per_bin * 10):
            raw = (i % 10) / 10 + 0.05
            obs.append(
                LabeledObservation(
                    sample_id=f"s{i}",
                    split=split_of,
                    dimension="retention",
                    outcome="positive" if raw >= 0.5 else "negative",
                    raw_value=min(raw, 1.0),
                    source_type="manual",
                    assertion_mode="unknown",
                    kind="fact",
                    risk="unknown",
                )
            )
        return obs

    def test_fit_deterministic_and_drops_thin_strata(self):
        identity = _identity()
        contract = _contract(identity)
        dev = self._observations()
        p1 = fit_profiles(
            dev, identity=identity, contract=contract, split=_split_for_observations(dev)
        )
        p2 = fit_profiles(
            dev, identity=identity, contract=contract, split=_split_for_observations(dev)
        )
        assert [p.model_dump() for p in p1] == [p.model_dump() for p in p2]
        assert len(p1) == 1
        bins = p1[0].bins
        # bins partition [0,1]
        assert bins[0].lower == 0.0 and bins[-1].upper == 1.0
        # thin stratum: single observation cannot produce a profile
        thin = self._observations(n_per_bin=1)
        assert (
            fit_profiles(
                thin,
                identity=identity,
                contract=contract,
                split=_split_for_observations(thin),
            )
            == []
        )

    def test_holdout_never_fitted(self):
        identity = _identity()
        contract = _contract(identity)
        dev = self._observations(split_of="dev")
        holdout = [
            observation.model_copy(update={"sample_id": f"h{observation.sample_id}"})
            for observation in self._observations(split_of="holdout")
        ]
        combined = dev + holdout
        split = _split_for_observations(combined)
        profiles = fit_profiles(combined, identity=identity, contract=contract, split=split)
        # fitting on dev+holdout must equal fitting on dev alone
        assert [p.model_dump() for p in profiles] == [
            p.model_dump()
            for p in fit_profiles(
                dev,
                identity=identity,
                contract=contract,
                split=_split_for_observations(dev),
            )
        ]
        metrics = evaluate_holdout(combined, profiles=profiles, split=split)
        assert metrics and all(m.n > 0 for m in metrics)

    def test_loader_reproduces_calibration_state(self, tmp_path: Path):
        identity = _identity()
        contract = _contract(identity)
        observations = self._observations()
        profiles = fit_profiles(
            observations,
            identity=identity,
            contract=contract,
            split=_split_for_observations(observations),
        )
        bundle = build_artifact(
            identity=identity,
            contract=contract,
            sampling_digest="d" * 64,
            split=_artifact_split(),
            floors=EvidenceFloors(
                campaign_id="c",
                total_reviewed_min=10,
                per_dimension_labeled_min=5,
                per_dimension_non_unknown_fraction_min=0.5,
                holdout_min=5,
                holdout_per_profile_min=1,
                holdout_calibrated_brier_max=0.25,
                holdout_calibrated_ece_max=0.15,
                high_consequence_reviewed_min=0,
                per_bin_support_min=50,
                per_stratum_min=1,
            ),
            profiles=profiles,
            holdout_metrics=[],
            floor_result=_floor_result("d" * 64, _artifact_split().split_digest()),
            authoritative_recall_evidence_digest="a" * 64,
        )
        path = tmp_path / "artifact.json"
        path.write_bytes(bundle.to_loader_payload())
        loaded = load_profiles(str(path))
        assert [p.model_dump(mode="json") for p in loaded] == bundle.profiles
        assert len(json.dumps(bundle.profiles)) <= 65536

    def test_mismatch_fails_closed(self, tmp_path: Path):
        from engram.assessment_calibration import calibrate

        identity = _identity()
        contract = _contract(identity)
        observations = self._observations()
        profiles = fit_profiles(
            observations,
            identity=identity,
            contract=contract,
            split=_split_for_observations(observations),
        )
        for wrong in (
            contract.model_copy(update={"model": "other"}),
            contract.model_copy(update={"provider": "other"}),
            contract.model_copy(update={"prompt_version": "other"}),
        ):
            score = calibrate(
                0.55,
                profile=profiles[0],
                contract=wrong,
                dimension=profiles[0].dimension,
                source_type=profiles[0].source_type,
                assertion_mode=profiles[0].assertion_mode,
                kind=profiles[0].kind,
                risk=profiles[0].risk,
            )
            assert score.status == "uncalibrated"

    def test_public_outputs_contain_no_raw_content(self):
        identity = _identity()
        contract = _contract(identity)
        observations = self._observations()
        profiles = fit_profiles(
            observations,
            identity=identity,
            contract=contract,
            split=_split_for_observations(observations),
        )
        bundle = build_artifact(
            identity=identity,
            contract=contract,
            sampling_digest="d" * 64,
            split=_artifact_split(),
            floors=EvidenceFloors(
                campaign_id="c",
                total_reviewed_min=10,
                per_dimension_labeled_min=5,
                per_dimension_non_unknown_fraction_min=0.5,
                holdout_min=5,
                holdout_per_profile_min=1,
                holdout_calibrated_brier_max=0.25,
                holdout_calibrated_ece_max=0.15,
                high_consequence_reviewed_min=0,
                per_bin_support_min=50,
                per_stratum_min=1,
            ),
            profiles=profiles,
            holdout_metrics=[],
            floor_result=_floor_result("d" * 64, _artifact_split().split_digest()),
            authoritative_recall_evidence_digest="a" * 64,
        )
        blob = json.dumps([bundle.target, bundle.floors, bundle.holdout_metrics])
        assert "content" not in blob.lower().replace("content_hash", "")
        assert sample_id_for("00000000-0000-0000-0000-000000000001").startswith("s")
        assert "00000000" not in json.dumps(bundle.target)


class TestAgreement:
    def test_agreement_report(self):
        frame, _ = _frame()
        ids, _, _ = stratified_sample(
            frame, campaign_id="c", sampling_seed="s", coverage_min=2, allocation_fraction=1.0
        )
        labels_a = [
            LabelRecord.model_validate(
                {
                    "sample_id": sid,
                    "label_schema_version": "engram-admission-label-v1",
                    "dataset_id": "ds",
                    "dataset_version": "v1",
                    "fixture_role": "ordinary_claim",
                    "label_origin": "human_adjudicated",
                    "reviewer_a": _judgment("ra"),
                    "reviewer_b": None,
                    "resolution": None,
                    "disagreement": "none",
                }
            )
            for sid in ids
        ]
        report = reviewer_agreement(labels_a, None)
        assert report["reviewer_b_present"] is False
        report2 = reviewer_agreement(labels_a, labels_a)
        assert report2["reviewer_b_present"] is True
        assert report2["per_dimension"]["expected_kind"]["rate"] == 1.0

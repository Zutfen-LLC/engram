"""Regression proofs for the #216 campaign (ENG-CALIBRATION-001K) — round 2.

Covers the FIX-217 correction round:

- FIX-217-1: committed CLI availability + 001k subscription opt-in
  (001f unchanged, non-opted campaigns fail closed);
- FIX-217-2: DEV-only reviewer membership (exactly 102, zero holdout);
- FIX-217-3: stage sampling authorities bind the NEW 001k target digest,
  never the 001f one; prior provenance stays separately bound;
- FIX-217-4: provider evidence is identity-verified before any value can
  become an observation (prompt/model/target/population mismatches fail);
- FIX-217-5: fresh-label membership is judged by exact frozen stage
  membership, never by count;
- FIX-217-6: holdout reviewer export is mechanically blocked before
  artifact freeze.

Synthetic fixtures only; real protected evidence is exercised on-host by the
clean-checkout reproduction gate.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from evals.admission.schema import digest
from evals.calibration import campaign_001k as c216
from evals.calibration.campaign_001k_fit import (
    ProviderEvidence216,
    _frame_stratum,
    dev_fit_observations,
    holdout_evaluate_observations,
)
from evals.calibration.campaigns import (
    EXPECTED_FRAME_DIGEST,
    EXPECTED_SAMPLING_MANIFEST_DIGEST,
)
from evals.calibration.freeze import FrameRow, SamplingManifest


def _sid(i: int) -> str:
    return f"s{i:024x}"


def _frame_row(i: int, *, kind: str = "fact", source_type: str = "migration") -> FrameRow:
    return FrameRow(
        item_uuid=f"uuid-{i}",
        sample_id=_sid(i),
        content_hash=digest(["content", i]),
        content_norm_hash=digest(["norm", i]),
        kind=kind,
        source_type=source_type,
        review_status="active",
        assertion_mode="unavailable",
        origin="unavailable",
        risk="unavailable",
        age_bucket="30_89d",
        evidence_state="unavailable",
        content_bytes=64,
        input_size_bucket="le_256b",
    )


def _sampling(ids: list[str], *, target_digest: str = "a" * 64) -> SamplingManifest:
    return SamplingManifest(
        campaign_id=c216.CAMPAIGN_ID_001K,
        target_identity_digest=target_digest,
        frame_digest="b" * 64,
        snapshot_sha256="c" * 64,
        snapshot_as_of=datetime(2026, 9, 11, tzinfo=UTC),
        sampling_seed="seed",
        inclusion_rules=("rule",),
        exclusion_rules=("rule",),
        source_row_counts={"eligible_frame": len(ids)},
        stratum_counts={"fact/migration/active": len(ids)},
        coverage_dimensions={},
        sample_ids=tuple(ids),  # type: ignore[arg-type]
        sample_hashes=tuple(digest(["h", sid]) for sid in ids),
    )


def _split(dev: list[str], holdout: list[str]) -> Any:
    from evals.calibration.freeze import SplitManifest

    return SplitManifest(
        campaign_id=c216.CAMPAIGN_ID_001K,
        sampling_manifest_digest="d" * 64,
        sampling_membership_digest=digest(sorted(dev + holdout)),
        split_seed="216-split-v1",
        dev_fraction=len(dev) / (len(dev) + len(holdout)),
        grouping=("content_hash", "normalized_text", "source_ref", "root_ref", "session_ref"),
        dev_ids=tuple(sorted(dev)),  # type: ignore[arg-type]
        holdout_ids=tuple(sorted(holdout)),  # type: ignore[arg-type]
        leakage_checks={
            "cross_split_shared_hash_groups": 0,
            "duplicate_groups": 0,
            "sample_membership_count": len(dev) + len(holdout),
        },
    )


def _reuse(holdout: list[str], forced: list[str], dev_fresh: list[str]) -> c216.ReuseManifest:
    executed = [_sid(i) for i in range(c216.EXECUTED_PREFIX)]
    return c216.ReuseManifest(
        manifest_schema=c216.REUSE_MANIFEST_SCHEMA,
        campaign_id=c216.CAMPAIGN_ID_001K,
        prior_campaign_id=c216.PRIOR_CAMPAIGN_ID,
        prior_sampling_manifest_digest=EXPECTED_SAMPLING_MANIFEST_DIGEST,
        prior_frame_digest=EXPECTED_FRAME_DIGEST,
        executed=c216.ExecutedEvidenceFacts(
            executed_case_count=c216.EXECUTED_PREFIX,
            executed_ids_digest=digest(sorted(executed)),
            executed_batch_ids=c216.EXECUTED_BATCH_IDS,
            unexecuted_batch_ids=c216.UNEXECUTED_BATCH_IDS,
            synthesis_sha256=c216.SYNTHESIS_SHA256,
            synthesis_case_count=c216.EXECUTED_PREFIX,
            replay3_sha256=c216.REPLAY3_SHA256,
            replay3_prompt_version="engram.assess.3",
            replay3_model="deepseek-ai/DeepSeek-V4-Flash",
        ),
        freshness=c216.FreshnessProof(
            candidate_count=202,
            corpora_scanned=("corpus",),
            spanning_group_forced_dev=tuple(sorted(forced)),
            duplicate_groups_total=1,
            fresh_internal_group_count=0,
            leakage_safe_pool=len(dev_fresh) + len(holdout),
        ),
        reuse_rules=c216.REUSE_RULES,
        holdout_ids=tuple(sorted(holdout)),  # type: ignore[arg-type]
        forced_dev_fresh_ids=tuple(sorted(forced)),  # type: ignore[arg-type]
        dev_fresh_ids=tuple(sorted(dev_fresh)),  # type: ignore[arg-type]
    )


_POP = {
    "executed": [_sid(i) for i in range(200)],
    "forced": [_sid(i) for i in range(200, 225)],  # 25
    "dev_fresh": [_sid(i) for i in range(225, 302)],  # 77
    "holdout": [_sid(i) for i in range(302, 402)],  # 100
}
_NEW_TARGET = "e" * 64
_OLD_TARGET = "a" * 64


def _real_shaped_reuse() -> c216.ReuseManifest:
    return _reuse(_POP["holdout"], _POP["forced"], _POP["dev_fresh"])


class TestReusePartition:
    def test_first_200_never_enter_holdout(self) -> None:
        reuse = _real_shaped_reuse()
        assert not set(reuse.holdout_ids) & set(_POP["executed"])
        dev = reuse.dev_ids(tuple(_POP["executed"]))  # type: ignore[arg-type]
        assert set(_POP["executed"]) <= set(dev)
        assert len(reuse.holdout_ids) == 100
        assert len(_POP["forced"]) + len(_POP["dev_fresh"]) == 102

    def test_executed_overlap_fails_closed(self) -> None:
        fresh = [_sid(i) for i in range(200, 402)]
        corpora = {"some-corpus": set(fresh[:5])}
        with pytest.raises(ValueError, match="fresh_candidates_appear_in_executed_evidence"):
            c216.build_freshness_proof(
                fresh=tuple(fresh),  # type: ignore[arg-type]
                corpora=corpora,
                spanning=(),
                duplicate_groups_total=0,
                fresh_internal_group_count=0,
            )

    def test_unexecuted_export_overlap_is_not_contamination(self) -> None:
        fresh = [_sid(i) for i in range(200, 402)]
        corpora = {"unexecuted-export:batch": set(fresh[:5])}
        proof = c216.build_freshness_proof(
            fresh=tuple(fresh),  # type: ignore[arg-type]
            corpora=corpora,
            spanning=(),
            duplicate_groups_total=0,
            fresh_internal_group_count=0,
        )
        assert proof.executed_overlap == ()
        assert len(proof.unexecuted_export_overlap) == 5

    def test_spanning_group_members_forced_dev(self) -> None:
        executed = _POP["executed"]
        spanning_fresh = [_sid(300), _sid(301)]
        duplicates = {"g1": [executed[0], *spanning_fresh]}
        forced = c216.spanning_duplicate_members(duplicates, tuple(executed))  # type: ignore[arg-type]
        assert set(forced) == set(spanning_fresh)

    def test_holdout_floor_enforced(self) -> None:
        fresh = [_sid(i) for i in range(200, 250)]
        with pytest.raises(ValueError, match="reuse_manifest_holdout_below_floor"):
            _reuse(holdout=fresh[:10], forced=[], dev_fresh=fresh[10:])


class TestDeterministicHoldout:
    def test_deterministic_and_group_constrained(self) -> None:
        pool = tuple(_sid(i) for i in range(200, 377))
        internal = [[_sid(350), _sid(351)], [_sid(360), _sid(361), _sid(362)]]
        h1 = c216.deterministic_holdout(pool, internal)
        h2 = c216.deterministic_holdout(pool, internal)
        assert h1 == h2
        assert len(h1) >= c216.HOLDOUT_MIN_216
        hold = set(h1)
        for group in internal:
            assert not (set(group) & hold) or set(group) <= hold

    def test_label_blind_ranking(self) -> None:
        pool = tuple(_sid(i) for i in range(200, 377))
        assert c216.deterministic_holdout(pool, []) == c216.deterministic_holdout(pool, [])


class TestSplit001k:
    def test_split_partitions_population_and_zero_cross_leakage(self) -> None:
        reuse = _real_shaped_reuse()
        sampling = _sampling(
            _POP["executed"] + _POP["holdout"] + _POP["forced"] + _POP["dev_fresh"]
        )
        duplicates = {"g1": [_POP["executed"][0], _POP["forced"][0]]}
        split = c216.build_split_001k(reuse, sampling, duplicates)
        assert set(split.dev_ids) | set(split.holdout_ids) == set(sampling.sample_ids)
        assert not set(split.dev_ids) & set(split.holdout_ids)
        assert split.leakage_checks["cross_split_shared_hash_groups"] == 0
        assert _POP["forced"][0] in split.dev_ids

    def test_cross_split_group_fails_closed(self) -> None:
        reuse = _real_shaped_reuse()
        sampling = _sampling(
            _POP["executed"] + _POP["holdout"] + _POP["forced"] + _POP["dev_fresh"]
        )
        duplicates = {"g1": [_POP["holdout"][50], _POP["dev_fresh"][10]]}
        with pytest.raises(ValueError, match="split_001k_cross_split_duplicate_groups"):
            c216.build_split_001k(reuse, sampling, duplicates)


class TestStageSamplingAuthorities:
    """FIX-217-2 / FIX-217-3: DEV/HOLDOUT reviewer authorities."""

    def _counts(self, ids: list[str]) -> dict[str, int]:
        return {"fact/migration/active": len(ids)}

    def test_dev_authority_exactly_102_zero_holdout(self) -> None:
        reuse = _real_shaped_reuse()
        sampling = _sampling(
            _POP["executed"] + _POP["holdout"] + _POP["forced"] + _POP["dev_fresh"]
        )
        manifest = c216.build_dev_sampling_manifest(
            sampling,
            reuse,
            stratum_counts=self._counts(_POP["forced"] + _POP["dev_fresh"]),
            target_identity_digest=_NEW_TARGET,
        )
        assert len(manifest.sample_ids) == 102
        assert set(manifest.sample_ids) == set(_POP["forced"]) | set(_POP["dev_fresh"])
        assert not set(manifest.sample_ids) & set(_POP["holdout"])
        assert manifest.sampling_seed == "216-dev-v1"

    def test_holdout_authority_exactly_100(self) -> None:
        reuse = _real_shaped_reuse()
        sampling = _sampling(
            _POP["executed"] + _POP["holdout"] + _POP["forced"] + _POP["dev_fresh"]
        )
        manifest = c216.build_holdout_sampling_manifest(
            sampling,
            reuse,
            stratum_counts=self._counts(_POP["holdout"]),
            target_identity_digest=_NEW_TARGET,
        )
        assert len(manifest.sample_ids) == 100
        assert set(manifest.sample_ids) == set(_POP["holdout"])

    def test_stage_authority_binds_new_target_digest_not_001f(self) -> None:
        reuse = _real_shaped_reuse()
        sampling = _sampling(
            _POP["executed"] + _POP["holdout"] + _POP["forced"] + _POP["dev_fresh"],
            target_digest=_OLD_TARGET,
        )
        manifest = c216.build_dev_sampling_manifest(
            sampling,
            reuse,
            stratum_counts=self._counts(_POP["forced"] + _POP["dev_fresh"]),
            target_identity_digest=_NEW_TARGET,
        )
        assert manifest.target_identity_digest == _NEW_TARGET
        assert manifest.target_identity_digest != sampling.target_identity_digest
        # prior provenance stays separately recorded
        assert manifest.frame_digest == sampling.frame_digest
        assert manifest.snapshot_sha256 == sampling.snapshot_sha256

    def test_stage_authority_rejects_inherited_old_target(self) -> None:
        reuse = _real_shaped_reuse()
        sampling = _sampling(
            _POP["executed"] + _POP["holdout"] + _POP["forced"] + _POP["dev_fresh"],
            target_digest=_OLD_TARGET,
        )
        with pytest.raises(
            ValueError, match="stage_authority_must_bind_new_target_identity|target_identity"
        ):
            c216.build_dev_sampling_manifest(
                sampling,
                reuse,
                stratum_counts=self._counts(_POP["forced"] + _POP["dev_fresh"]),
                target_identity_digest=_OLD_TARGET,
            )

    def test_dev_and_holdout_authorities_share_exact_target(self) -> None:
        reuse = _real_shaped_reuse()
        sampling = _sampling(
            _POP["executed"] + _POP["holdout"] + _POP["forced"] + _POP["dev_fresh"]
        )
        dev = c216.build_dev_sampling_manifest(
            sampling,
            reuse,
            stratum_counts=self._counts(_POP["forced"] + _POP["dev_fresh"]),
            target_identity_digest=_NEW_TARGET,
        )
        hold = c216.build_holdout_sampling_manifest(
            sampling,
            reuse,
            stratum_counts=self._counts(_POP["holdout"]),
            target_identity_digest=_NEW_TARGET,
        )
        assert dev.target_identity_digest == hold.target_identity_digest == _NEW_TARGET
        assert set(dev.sample_ids) & set(hold.sample_ids) == set()

    def test_malformed_target_digest_rejected(self) -> None:
        reuse = _real_shaped_reuse()
        sampling = _sampling(
            _POP["executed"] + _POP["holdout"] + _POP["forced"] + _POP["dev_fresh"]
        )
        with pytest.raises(ValueError, match="malformed|target"):
            c216.build_dev_sampling_manifest(
                sampling,
                reuse,
                stratum_counts=self._counts(_POP["forced"] + _POP["dev_fresh"]),
                target_identity_digest="not-a-digest",
            )


class TestProviderEvidenceBinding:
    """FIX-217-4: identity-verified provider evidence before fitting."""

    def _payload(self, **overrides: Any) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "run_kind": "issue-216-fresh-202-assess3",
            "prompt_version": "engram.assess.3",
            "model": "deepseek-ai/DeepSeek-V4-Flash",
            "code_git_head": "a" * 40,
            "target_identity_digest": self._target().identity_digest(),
            "cases": [
                {
                    "sample_id": _sid(1),
                    "status": "ok",
                    "values": {"taxonomy_value": 0.9, "retention_value": 0.85},
                },
                {"sample_id": _sid(2), "status": "error", "error_type": "ValidationError"},
            ],
        }
        payload.update(overrides)
        return payload

    def _target(self) -> Any:
        from evals.calibration.freeze import TargetIdentity

        return TargetIdentity(
            campaign_id=c216.CAMPAIGN_ID_001K,
            campaign_tooling_repo_sha="f" * 40,
            assessment_schema_version="engram.assessment.v1",
            assessment_code_version="assessment-engine-v1",
            prompt_version="engram.assess.3",
            provider_adapter="openai",
            provider_model="deepseek-ai/DeepSeek-V4-Flash",
            provider_config_digest="sha256:" + "1" * 64,
            provider_params={"temperature": 0},
            assessment_policy_version="assessment-selection-v1",
            calibration_artifact_schema_version="engram.calibration-profiles-v1",
            calibration_dataset_version="calibration-157-dogfood-v3-216",
            label_guide_version="engram-calibration-guide-157-v1",
            canonicalization_version="assessment-evidence-manifest-v1",
            dimensions=("taxonomy", "retention"),
        )

    def test_rejects_wrong_prompt_version(self) -> None:
        payload = self._payload(prompt_version="engram.assess.2")
        with pytest.raises(ValueError, match="provider_evidence_wrong_prompt_version"):
            ProviderEvidence216.from_payload(payload, artifact_sha256="0" * 64)

    def test_parses_ok_and_error_cases(self) -> None:
        evidence = ProviderEvidence216.from_payload(self._payload(), artifact_sha256="0" * 64)
        assert evidence.ok_count == 1 and evidence.error_count == 1
        assert _sid(2) not in evidence.values_by_sample

    def test_verified_for_fitting_rejects_wrong_model(self) -> None:
        evidence = ProviderEvidence216.from_payload(
            self._payload(model="other-model"), artifact_sha256="0" * 64
        )
        with pytest.raises(ValueError, match="provider_evidence_model_mismatch"):
            evidence.verified_for_fitting(
                target_identity=self._target(),
                expected_population=frozenset({_sid(1), _sid(2)}),
            )

    def test_verified_for_fitting_rejects_wrong_target_digest(self) -> None:
        evidence = ProviderEvidence216.from_payload(
            self._payload(target_identity_digest="9" * 64), artifact_sha256="0" * 64
        )
        with pytest.raises(ValueError, match="provider_evidence_target_identity_mismatch"):
            evidence.verified_for_fitting(
                target_identity=self._target(),
                expected_population=frozenset({_sid(1), _sid(2)}),
            )

    def test_verified_for_fitting_rejects_foreign_population(self) -> None:
        evidence = ProviderEvidence216.from_payload(self._payload(), artifact_sha256="0" * 64)
        with pytest.raises(ValueError, match="provider_evidence_population_mismatch"):
            evidence.verified_for_fitting(
                target_identity=self._target(),
                expected_population=frozenset({_sid(7)}),
            )

    def test_verified_for_fitting_accepts_exact_identity(self) -> None:
        evidence = ProviderEvidence216.from_payload(self._payload(), artifact_sha256="0" * 64)
        values = evidence.verified_for_fitting(
            target_identity=self._target(),
            expected_population=frozenset({_sid(1), _sid(2), _sid(3)}),
        )
        assert set(values) == {_sid(1)}

    def test_load_verified_rejects_digest_mismatch(self, tmp_path: Path) -> None:
        path = tmp_path / "evidence.json"
        path.write_text(json.dumps(self._payload()))
        with pytest.raises(ValueError, match="provider_evidence_artifact_digest_mismatch"):
            ProviderEvidence216.load_verified(path, expected_sha256="0" * 64)


class TestFreshLabelMembership:
    """FIX-217-5: exact membership, never count."""

    def _fixtures(self) -> tuple[Any, Any, dict[str, dict[str, Any]], dict[str, FrameRow]]:
        reuse = _real_shaped_reuse()
        split = _split(_POP["executed"] + _POP["forced"] + _POP["dev_fresh"], _POP["holdout"])
        values = {
            sid: {"taxonomy_value": 0.9, "retention_value": 0.85, "suggested_kind": "fact"}
            for sid in _POP["forced"] + _POP["dev_fresh"] + _POP["holdout"]
        }
        frame_by_id = {row.sample_id: row for row in (_frame_row(i) for i in range(402))}
        # remap frame ids to the synthetic population
        frame_by_id = {
            sid: _frame_row(0) for sid in _POP["forced"] + _POP["dev_fresh"] + _POP["holdout"]
        }
        return reuse, split, values, frame_by_id

    def _labels(self, ids: list[str]) -> dict[str, dict[str, Any]]:
        return {
            sid: {
                "expected_kind": "fact",
                "retention_value": "retain",
                "epistemic_state": "adequately_supported",
                "consequence": "low",
            }
            for sid in ids
        }

    def test_dev_fit_requires_exact_membership(self) -> None:
        reuse, split, values, frame_by_id = self._fixtures()
        dev_ids = _POP["forced"] + _POP["dev_fresh"]
        obs = dev_fit_observations(
            labels_by_sample=self._labels(dev_ids),
            provider_values=values,
            split=split,
            frame_by_id=frame_by_id,
            reuse=reuse,
        )
        assert {o.sample_id for o in obs} == set(dev_ids)

    def test_dev_fit_rejects_missing_labels(self) -> None:
        reuse, split, values, frame_by_id = self._fixtures()
        dev_ids = _POP["forced"] + _POP["dev_fresh"]
        with pytest.raises(ValueError, match="dev_fit_labels_incomplete"):
            dev_fit_observations(
                labels_by_sample=self._labels(dev_ids[:-1]),
                provider_values=values,
                split=split,
                frame_by_id=frame_by_id,
                reuse=reuse,
            )

    def test_dev_fit_rejects_holdout_id(self) -> None:
        reuse, split, values, frame_by_id = self._fixtures()
        dev_ids = _POP["forced"] + _POP["dev_fresh"]
        labels = self._labels(dev_ids)
        labels[_POP["holdout"][0]] = dict(next(iter(labels.values())))
        with pytest.raises(
            ValueError, match="dev_fit_labels_outside_frozen_membership|dev_fit_label_split"
        ):
            dev_fit_observations(
                labels_by_sample=labels,
                provider_values=values,
                split=split,
                frame_by_id=frame_by_id,
                reuse=reuse,
            )

    def test_dev_fit_rejects_exact_count_wrong_membership(self) -> None:
        reuse, split, values, frame_by_id = self._fixtures()
        dev_ids = _POP["forced"] + _POP["dev_fresh"]
        labels = self._labels(dev_ids[:-1])
        # 102 labels, but one is foreign
        labels[_sid(9999)] = dict(next(iter(labels.values())))
        with pytest.raises(ValueError, match="dev_fit_labels_outside_frozen_membership"):
            dev_fit_observations(
                labels_by_sample=labels,
                provider_values=values,
                split=split,
                frame_by_id=frame_by_id,
                reuse=reuse,
            )

    def test_partial_dev_audit_never_fit_complete(self) -> None:
        reuse, split, values, frame_by_id = self._fixtures()
        dev_ids = _POP["forced"] + _POP["dev_fresh"]
        # partial audit input succeeds only with require_complete=False
        obs = dev_fit_observations(
            labels_by_sample=self._labels(dev_ids[:10]),
            provider_values=values,
            split=split,
            frame_by_id=frame_by_id,
            reuse=reuse,
            require_complete=False,
        )
        assert {o.sample_id for o in obs} == set(dev_ids[:10])
        # but the same input is rejected as a fit freeze
        with pytest.raises(ValueError, match="dev_fit_labels_incomplete"):
            dev_fit_observations(
                labels_by_sample=self._labels(dev_ids[:10]),
                provider_values=values,
                split=split,
                frame_by_id=frame_by_id,
                reuse=reuse,
            )

    def test_holdout_rejects_dev_id(self) -> None:
        reuse, split, values, frame_by_id = self._fixtures()
        labels = self._labels(_POP["holdout"])
        labels[_POP["dev_fresh"][0]] = dict(next(iter(labels.values())))
        with pytest.raises(ValueError, match="holdout_evaluate_labels_outside_frozen_membership"):
            holdout_evaluate_observations(
                labels_by_sample=labels,
                provider_values=values,
                split=split,
                frame_by_id=frame_by_id,
                reuse=reuse,
            )

    def test_holdout_requires_all_100(self) -> None:
        reuse, split, values, frame_by_id = self._fixtures()
        with pytest.raises(ValueError, match="holdout_evaluate_labels_incomplete"):
            holdout_evaluate_observations(
                labels_by_sample=self._labels(_POP["holdout"][:-1]),
                provider_values=values,
                split=split,
                frame_by_id=frame_by_id,
                reuse=reuse,
            )

    def test_202_full_collection_rejected_by_dev_fit(self) -> None:
        reuse, split, values, frame_by_id = self._fixtures()
        # the whole fresh 202 (dev 102 + holdout 100) must never pass dev fit
        labels = self._labels(_POP["forced"] + _POP["dev_fresh"] + _POP["holdout"])
        with pytest.raises(
            ValueError, match="dev_fit_labels_outside_frozen_membership|dev_fit_label_split"
        ):
            dev_fit_observations(
                labels_by_sample=labels,
                provider_values=values,
                split=split,
                frame_by_id=frame_by_id,
                reuse=reuse,
            )


class TestSubscriptionOptIn:
    """FIX-217-1: committed 001k opt-in; others fail closed."""

    def test_001k_opted_under_frozen_protocol(self) -> None:
        from evals.calibration.subscription_ui import subscription_mode_permitted

        assert subscription_mode_permitted(
            "eng-calibration-001k", "eng-calibration-consensus-206-v1"
        )

    def test_001f_unchanged(self) -> None:
        from evals.calibration.subscription_ui import subscription_mode_permitted

        assert subscription_mode_permitted(
            "eng-calibration-001f", "eng-calibration-consensus-206-v1"
        )

    def test_unknown_campaign_fails_closed(self) -> None:
        from evals.calibration.subscription_ui import subscription_mode_permitted

        assert not subscription_mode_permitted(
            "eng-calibration-999z", "eng-calibration-consensus-206-v1"
        )

    def test_001k_under_wrong_protocol_fails_closed(self) -> None:
        from evals.calibration.subscription_ui import subscription_mode_permitted

        assert not subscription_mode_permitted(
            "eng-calibration-001k", "eng-calibration-consensus-999-v9"
        )

    def test_committed_cli_exposes_216_commands(self) -> None:
        import subprocess
        import sys

        result = subprocess.run(
            [sys.executable, "-m", "evals.calibration", "--help"],
            capture_output=True,
            text=True,
            cwd=Path(__file__).parents[1],
        )
        assert result.returncode == 0
        for command in ("216-freeze-target", "216-reuse-manifest", "216-dev-packet"):
            assert command in result.stdout
        # campaign override on subscription boundaries (subparser help)
        for sub in ("sub-lane-init", "sub-batches", "sub-import", "sub-prepare"):
            sub_help = subprocess.run(
                [sys.executable, "-m", "evals.calibration", sub, "--help"],
                capture_output=True,
                text=True,
                cwd=Path(__file__).parents[1],
            )
            assert "--campaign-id" in sub_help.stdout, sub

    def test_committed_public_manifest_matches_reality(self) -> None:
        manifest = Path(c216.__file__).parent / "campaigns/216/campaign-reuse-public.json"
        payload = json.loads(manifest.read_text())
        assert payload["campaign_id"] == c216.CAMPAIGN_ID_001K
        assert payload["holdout"] >= c216.HOLDOUT_MIN_216
        assert payload["executed_200"] == c216.EXECUTED_PREFIX
        assert payload["leakage_checks"]["cross_split_shared_hash_groups"] == 0


class TestHoldoutBarrier:
    """FIX-217-6: holdout export blocked before artifact freeze."""

    def test_barrier_blocks_holdout_export_without_artifact_digest(self, tmp_path: Path) -> None:
        from evals.calibration import campaign_001k_holdout_barrier as barrier

        with pytest.raises(ValueError, match="holdout_locked_artifact_not_frozen|holdout_locked"):
            barrier.require_holdout_export_allowed(
                campaign_id=c216.CAMPAIGN_ID_001K,
                frozen_artifact_digest=None,
                dev_fitting_evidence_digest=None,
            )

    def test_barrier_rejects_wrong_campaign(self, tmp_path: Path) -> None:
        from evals.calibration import campaign_001k_holdout_barrier as barrier

        with pytest.raises(ValueError, match="holdout_locked"):
            barrier.require_holdout_export_allowed(
                campaign_id="eng-calibration-999z",
                frozen_artifact_digest="0" * 64,
                dev_fitting_evidence_digest="1" * 64,
            )

    def test_barrier_allows_after_freeze_with_exact_bindings(self) -> None:
        from evals.calibration import campaign_001k_holdout_barrier as barrier

        # permitted only with both digests present and well formed
        barrier.require_holdout_export_allowed(
            campaign_id=c216.CAMPAIGN_ID_001K,
            frozen_artifact_digest="0" * 64,
            dev_fitting_evidence_digest="1" * 64,
        )


class TestFrameStratumVocabulary:
    def test_unavailable_maps_to_unknown(self) -> None:
        row = _frame_row(1)
        stratum = _frame_stratum(row)
        assert stratum["assertion_mode"] == "unknown"
        assert stratum["risk"] == "unknown"
        assert stratum["source_type"] == "migration"


class TestReusedObservations:
    def test_campaign_constants(self) -> None:
        assert c216.DIMENSIONS_001K == ("taxonomy", "retention")
        assert c216.CAMPAIGN_ID_001K == "eng-calibration-001k"
        assert c216.EXECUTED_PREFIX == 200

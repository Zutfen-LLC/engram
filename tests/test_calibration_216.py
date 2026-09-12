"""Regression proofs for the #216 campaign (ENG-CALIBRATION-001K) — round 2.

FIX2-217 correction round. Every test in the new classes below FAILS
against the reviewed NO-GO head ``9e019df`` (verified during development:
the old API — optional-attribute identity checks, naked provider/label
dicts at the fitting boundary, arbitrary-digest holdout unlock — either
cannot express these fixtures or accepts the forged input).

Coverage required by the correction spec §9:

- ProviderEvidence216 fail-closed authority (duplicates, missing/extra
  cases, subset-only populations, missing/wrong target digest, wrong
  model/adapter/config/schema/execution identity, invalid replay-3
  historical authority);
- no naked provider mapping / fresh-label mapping can reach fitting
  (boundary signature no longer accepts dicts);
- fresh labels provenance-bound through a REAL VerifiedConsensusLedger
  (duplicate-before-dict detection, holdout-in-DEV, DEV-in-holdout,
  exact-count-wrong-membership, incomplete stages);
- one and only one 001k target authority (generic legacy freeze-target
  rejected; hash-valid but wrong-contract identity rejected);
- holdout unlock bound to a canonical DEV artifact freeze (forged
  arbitrary digests rejected; export/show/import/direct-lane blocked
  before freeze);
- 001f behavior unchanged.
"""

from __future__ import annotations

import inspect
import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from evals.admission.schema import digest
from evals.calibration import campaign_001k as c216
from evals.calibration.campaign_001k_fit import (
    DIMENSIONS_001K,
    ProviderEvidence216,
    dev_fit_observations,
    holdout_evaluate_observations,
    verify_target_identity_001k,
)
from evals.calibration.campaigns import (
    EXPECTED_FRAME_DIGEST,
    EXPECTED_SAMPLING_MANIFEST_DIGEST,
)
from evals.calibration.freeze import FrameRow, SamplingManifest, TargetIdentity


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
_TOOLING_SHA = "f" * 40


def _real_shaped_reuse() -> c216.ReuseManifest:
    return _reuse(_POP["holdout"], _POP["forced"], _POP["dev_fresh"])


_GOOD_CRITICAL = {
    "expected_kind": "fact",
    "retention_value": "retain",
    "epistemic_state": "adequately_supported",
    "consequence": "low",
    "acceptable_abstention": "no",
}


def _target(**overrides: Any) -> TargetIdentity:
    fields: dict[str, Any] = {
        "campaign_id": c216.CAMPAIGN_ID_001K,
        "campaign_tooling_repo_sha": _TOOLING_SHA,
        "assessment_schema_version": "engram.assessment.v1",
        "assessment_code_version": "assessment-engine-v1",
        "prompt_version": "engram.assess.3",
        "provider_adapter": "openai",
        "provider_model": "deepseek-ai/DeepSeek-V4-Flash",
        "provider_config_digest": "sha256:" + "1" * 64,
        "provider_params": {"temperature": 0, "max_tokens": 1024, "input_limit": 16000},
        "assessment_policy_version": "assessment-selection-v1",
        "calibration_artifact_schema_version": "engram.calibration-profiles-v1",
        "calibration_dataset_version": "calibration-157-dogfood-v3-216",
        "label_guide_version": "engram-calibration-guide-157-v1",
        "canonicalization_version": "assessment-evidence-manifest-v1",
        "dimensions": DIMENSIONS_001K,
    }
    fields.update(overrides)
    return TargetIdentity(**fields)


# ---------------------------------------------------------------------------
# Verified-ledger fixture (FIX2-217-3): the REAL consensus machinery
# ---------------------------------------------------------------------------


def _stage_sampling(ids: list[str]) -> SamplingManifest:
    from evals.calibration.freeze import protected_frame_digest
    from tests.test_calibration_206_helpers import build_frame_rows

    sampling = _sampling(ids)
    # the verifier re-derives the frame from these IDs; make the sampling
    # self-consistent with exactly that derivation (round3 helper pattern)
    return sampling.model_copy(
        update={
            "sampling_seed": "216-dev-v1",
            "frame_digest": protected_frame_digest(build_frame_rows(tuple(ids))),
            "sample_hashes": tuple(digest(sid) for sid in ids),
        }
    )


def _build_dev_ledger(dev_ids: list[str], critical_overrides: dict[str, dict] | None = None):
    """Genuine VerifiedConsensusLedger for the DEV stage via the REAL verifier."""
    from tests.test_calibration_206_helpers import build_verified_ledger

    critical_by_id = {sid: dict(_GOOD_CRITICAL) for sid in dev_ids}
    for sid, fields in (critical_overrides or {}).items():
        critical_by_id[sid].update(fields)
    stage_sampling = _stage_sampling(dev_ids)
    return build_verified_ledger(
        tuple(dev_ids),
        critical_by_id,
        campaign_id=c216.CAMPAIGN_ID_001K,
        sampling=stage_sampling,
    )


def _fresh_payload(population: list[str], **overrides: Any) -> dict[str, Any]:
    cases = [
        {
            "sample_id": sid,
            "status": "ok",
            "values": {"taxonomy_value": 0.9, "retention_value": 0.85},
        }
        for sid in population
    ]
    payload: dict[str, Any] = {
        "run_kind": "issue-216-fresh-202-assess3",
        "prompt_version": "engram.assess.3",
        "model": "deepseek-ai/DeepSeek-V4-Flash",
        "provider_adapter": "openai",
        "schema_version": "engram.assessment.v1",
        "code_version": "assessment-engine-v1",
        "code_git_head": _TOOLING_SHA,
        "target_identity_digest": _target().identity_digest(),
        "provider_config_digest": "sha256:" + "1" * 64,
        "cases": cases,
    }
    payload.update(overrides)
    return payload


_FRESH_202 = _POP["forced"] + _POP["dev_fresh"] + _POP["holdout"]


def _fresh_evidence(**overrides: Any) -> ProviderEvidence216:
    payload = _fresh_payload(_FRESH_202, **overrides)
    cases = payload["cases"]
    if "cases" in overrides:
        cases = overrides["cases"]
    body = dict(payload)
    body["cases"] = cases
    return ProviderEvidence216.from_payload(body, artifact_sha256="9" * 64)


# ---------------------------------------------------------------------------
# FIX2-217-4: one and only one 001k target authority
# ---------------------------------------------------------------------------


class TestTargetAuthority001k:
    def test_exact_contract_verifies(self) -> None:
        identity = verify_target_identity_001k(_target())
        assert identity.campaign_id == "eng-calibration-001k"

    def test_hash_valid_but_wrong_contract_rejected(self) -> None:
        # An internally hash-valid identity with the LEGACY 001f shape
        # (legacy dataset id + epistemic dimension) is not the 001k authority.
        legacy_shaped = _target(
            calibration_dataset_version="calibration-157-dogfood-v2",
            dimensions=("taxonomy", "retention", "epistemic"),
        )
        # its digest is internally consistent — that proves nothing:
        assert legacy_shaped.identity_digest()
        with pytest.raises(ValueError, match="target_identity_not_001k_contract"):
            verify_target_identity_001k(legacy_shaped)

    def test_wrong_dimensions_rejected(self) -> None:
        with pytest.raises(ValueError, match="target_identity_not_001k_contract"):
            verify_target_identity_001k(_target(dimensions=("taxonomy", "retention", "epistemic")))

    def test_wrong_campaign_rejected(self) -> None:
        with pytest.raises(ValueError, match="target_identity_not_001k_contract"):
            verify_target_identity_001k(_target(campaign_id="eng-calibration-001f"))

    def test_wrong_prompt_rejected(self) -> None:
        with pytest.raises(ValueError, match="target_identity_not_001k_contract"):
            verify_target_identity_001k(_target(prompt_version="engram.assess.2"))

    def test_wrong_model_rejected(self) -> None:
        with pytest.raises(ValueError, match="target_identity_not_001k_contract"):
            verify_target_identity_001k(_target(provider_model="other-model"))

    def test_wrong_provider_params_rejected(self) -> None:
        with pytest.raises(ValueError, match="target_identity_not_001k_contract"):
            verify_target_identity_001k(
                _target(
                    provider_params={"temperature": 1, "max_tokens": 1024, "input_limit": 16000}
                )
            )

    def test_generic_freeze_target_rejects_001k(self) -> None:
        """The legacy generic command must not create an 001k authority."""
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "evals.calibration",
                "freeze-target",
                "--campaign-id",
                "eng-calibration-001k",
                "--campaign-tooling-repo-sha",
                _TOOLING_SHA,
                "--provider-config-digest",
                "sha256:" + "1" * 64,
                "--output",
                "/tmp/should-not-exist-001k.json",
            ],
            capture_output=True,
            text=True,
            cwd=Path(__file__).parents[1],
        )
        assert result.returncode != 0
        assert "campaign_requires_216_command" in result.stderr

    def test_generic_001f_freeze_target_still_works(self, tmp_path: Path) -> None:
        """001f behavior on the generic path is unchanged."""
        out = tmp_path / "identity-001f.json"
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "evals.calibration",
                "freeze-target",
                "--campaign-tooling-repo-sha",
                _TOOLING_SHA,
                "--provider-config-digest",
                "sha256:" + "1" * 64,
                "--output",
                str(out),
            ],
            capture_output=True,
            text=True,
            cwd=Path(__file__).parents[1],
        )
        assert result.returncode == 0, result.stderr
        payload = json.loads(out.read_text())
        assert payload["target_identity"]["campaign_id"] == "eng-calibration-001f"
        assert payload["target_identity"]["dimensions"] == ["taxonomy", "retention", "epistemic"]

    def test_216_freeze_target_has_no_campaign_parameter(self) -> None:
        """216-freeze-target is unambiguously 001k — no campaign parameter."""
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "evals.calibration",
                "216-freeze-target",
                "--help",
            ],
            capture_output=True,
            text=True,
            cwd=Path(__file__).parents[1],
        )
        assert result.returncode == 0
        assert "--campaign-id" not in result.stdout

    def test_identity_loader_rejects_hash_valid_wrong_contract(self, tmp_path: Path) -> None:
        legacy_shaped = _target(
            calibration_dataset_version="calibration-157-dogfood-v2",
            dimensions=("taxonomy", "retention", "epistemic"),
        )
        artifact = {
            "target_identity": legacy_shaped.model_dump(mode="json"),
            "target_identity_digest": legacy_shaped.identity_digest(),
        }
        (tmp_path / "identity-frozen.json").write_text(json.dumps(artifact))
        with pytest.raises(ValueError, match="target_identity_not_001k_contract"):
            c216.load_001k_target_identity(tmp_path)


# ---------------------------------------------------------------------------
# FIX2-217-1: provider evidence fail-closed authority
# ---------------------------------------------------------------------------


class TestProviderEvidence216FailClosed:
    def _evidence(self, **overrides: Any) -> ProviderEvidence216:
        return _fresh_evidence(**overrides)

    def _stage(self, **overrides: Any) -> dict[str, Any]:
        return dict(
            target_identity=_target(),
            expected_population=frozenset(_FRESH_202),
            **overrides,
        )

    def test_exact_identity_and_population_pass(self) -> None:
        values = self._evidence().stage_provider_values(**self._stage())
        assert set(values) == set(_FRESH_202)

    def test_duplicate_sample_ids_rejected_before_dict_conversion(self) -> None:
        cases = _fresh_payload(_FRESH_202)["cases"]
        cases.append(dict(cases[0]))  # duplicate of an existing case
        with pytest.raises(ValueError, match="provider_evidence_duplicate_sample_id"):
            self._evidence(cases=cases)

    def test_missing_provider_cases_rejected(self) -> None:
        cases = _fresh_payload(_FRESH_202)["cases"][:-1]  # one case short
        with pytest.raises(ValueError, match="provider_evidence_population_mismatch:1_missing"):
            self._evidence(cases=cases).stage_provider_values(**self._stage())

    def test_extra_provider_cases_rejected(self) -> None:
        cases = _fresh_payload(_FRESH_202)["cases"] + [
            {"sample_id": _sid(9999), "status": "ok", "values": {"taxonomy_value": 0.5}}
        ]
        with pytest.raises(ValueError, match="provider_evidence_population_mismatch"):
            self._evidence(cases=cases).stage_provider_values(**self._stage())

    def test_subset_of_successful_ids_is_not_population_proof(self) -> None:
        # errors/abstentions are part of the population: a run whose cases
        # are all-ok over only part of the population fails.
        cases = [
            {
                "sample_id": sid,
                "status": "ok",
                "values": {"taxonomy_value": 0.9, "retention_value": 0.85},
            }
            for sid in _FRESH_202[:100]
        ]
        with pytest.raises(ValueError, match="provider_evidence_population_mismatch"):
            self._evidence(cases=cases).stage_provider_values(**self._stage())

    def test_missing_target_digest_rejected(self) -> None:
        with pytest.raises(ValueError, match="provider_evidence_target_identity_missing"):
            self._evidence(target_identity_digest=None).stage_provider_values(**self._stage())

    def test_wrong_target_digest_rejected(self) -> None:
        with pytest.raises(ValueError, match="provider_evidence_target_identity_mismatch"):
            self._evidence(target_identity_digest="9" * 64).stage_provider_values(**self._stage())

    def test_wrong_model_rejected(self) -> None:
        with pytest.raises(ValueError, match="provider_evidence_model_mismatch"):
            self._evidence(model="other-model").stage_provider_values(**self._stage())

    def test_wrong_adapter_rejected(self) -> None:
        with pytest.raises(ValueError, match="provider_evidence_adapter_mismatch"):
            self._evidence(provider_adapter="anthropic").stage_provider_values(**self._stage())

    def test_wrong_provider_config_digest_rejected(self) -> None:
        with pytest.raises(ValueError, match="provider_evidence_config_mismatch"):
            self._evidence(provider_config_digest="sha256:" + "2" * 64).stage_provider_values(
                **self._stage()
            )

    def test_missing_provider_config_digest_rejected(self) -> None:
        with pytest.raises(ValueError, match="provider_evidence_config_digest_missing"):
            self._evidence(provider_config_digest=None).stage_provider_values(**self._stage())

    def test_wrong_schema_contract_rejected(self) -> None:
        with pytest.raises(ValueError, match="provider_evidence_schema_mismatch"):
            self._evidence(schema_version="engram.assessment.v2").stage_provider_values(
                **self._stage()
            )

    def test_wrong_code_contract_rejected(self) -> None:
        with pytest.raises(ValueError, match="provider_evidence_code_version_mismatch"):
            self._evidence(code_version="assessment-engine-v2").stage_provider_values(
                **self._stage()
            )

    def test_wrong_execution_identity_rejected(self) -> None:
        with pytest.raises(ValueError, match="provider_evidence_execution_identity_mismatch"):
            self._evidence(code_git_head="0" * 40).stage_provider_values(**self._stage())

    def test_honest_errors_count_toward_population(self) -> None:
        cases = _fresh_payload(_FRESH_202)["cases"]
        cases[5] = {"sample_id": cases[5]["sample_id"], "status": "error", "error_type": "X"}
        evidence = self._evidence(cases=cases)
        values = evidence.stage_provider_values(**self._stage())
        assert cases[5]["sample_id"] not in values
        assert evidence.ok_count() == len(_FRESH_202) - 1
        assert evidence.error_count() == 1
        assert evidence.error_ids() == {cases[5]["sample_id"]}


class TestReplay3HistoricalAuthority:
    def _replay_payload(self, **overrides: Any) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "run_kind": "issue-214-protected-200-case-replay-assess3",
            "prompt_version": "engram.assess.3",
            "model": "deepseek-ai/DeepSeek-V4-Flash",
            "provider_adapter": "openai",
            "schema_version": "engram.assessment.v1",
            "code_version": "assessment-engine-v1",
            "code_git_head": "1dc42fca1f3062a06d0486fb9a53803e77410706",
            "target_identity_digest": None,
            "provider_config_digest": None,
            "cases": [
                {
                    "sample_id": sid,
                    "status": "ok",
                    "values": {"taxonomy_value": 0.9, "retention_value": 0.7},
                }
                for sid in _POP["executed"]
            ],
        }
        payload.update(overrides)
        return payload

    def _evidence(self, **overrides: Any) -> ProviderEvidence216:
        payload = self._replay_payload(**overrides)
        return ProviderEvidence216.from_payload(payload, artifact_sha256=c216.REPLAY3_SHA256)

    def test_valid_historical_authority_binds_reused_200(self) -> None:
        values = self._evidence().reused_provider_values(
            expected_population=frozenset(_POP["executed"])
        )
        assert set(values) == set(_POP["executed"])

    def test_wrong_artifact_sha_rejected(self) -> None:
        evidence = ProviderEvidence216.from_payload(
            self._replay_payload(), artifact_sha256="0" * 64
        )
        with pytest.raises(ValueError, match="replay3_authority_artifact_mismatch"):
            evidence.reused_provider_values(expected_population=frozenset(_POP["executed"]))

    def test_wrong_execution_identity_rejected(self) -> None:
        with pytest.raises(ValueError, match="replay3_authority_execution_identity_mismatch"):
            self._evidence(code_git_head="0" * 40).reused_provider_values(
                expected_population=frozenset(_POP["executed"])
            )

    def test_forged_001k_target_digest_rejected(self) -> None:
        with pytest.raises(ValueError, match="replay3_authority_unexpected_target_digest"):
            self._evidence(
                target_identity_digest=_target().identity_digest()
            ).reused_provider_values(expected_population=frozenset(_POP["executed"]))

    def test_wrong_population_rejected(self) -> None:
        with pytest.raises(
            ValueError,
            match="replay3_authority_population_size_mismatch|provider_evidence_population_mismatch",
        ):
            self._evidence().reused_provider_values(
                expected_population=frozenset(_POP["executed"][:-1])
            )


# ---------------------------------------------------------------------------
# FIX2-217-2/3: no naked provider/label mappings at the fitting boundary
# ---------------------------------------------------------------------------


class TestNoNakedMappingsAtFittingBoundary:
    def test_boundaries_have_no_provider_mapping_parameter(self) -> None:
        for fn in (dev_fit_observations, holdout_evaluate_observations):
            sig = inspect.signature(fn)
            for name in sig.parameters:
                assert "provider_values" not in name, fn.__name__
                assert "labels_by_sample" not in name, fn.__name__
            assert "provider_evidence" in sig.parameters, fn.__name__
            assert "fresh_label_authority" in sig.parameters, fn.__name__

    def test_boundary_rejects_dict_arguments(self) -> None:
        reuse = _real_shaped_reuse()
        split = _split(_POP["executed"] + _POP["forced"] + _POP["dev_fresh"], _POP["holdout"])
        frame_by_id = {sid: _frame_row(0) for sid in _FRESH_202}
        with pytest.raises((ValueError, TypeError, AttributeError)):
            dev_fit_observations(  # type: ignore[arg-type]
                fresh_label_authority={sid: dict(_GOOD_CRITICAL) for sid in _FRESH_202},
                provider_evidence=_fresh_payload(_FRESH_202),
                target_identity=_target(),
                split=split,
                frame_by_id=frame_by_id,
                reuse=reuse,
            )

    def test_fresh_label_authority_cannot_be_fabricated(self) -> None:
        from evals.calibration.campaign_001k_fit import FreshLabelAuthority216

        with pytest.raises(Exception, match="fresh_label_authority|validation"):
            FreshLabelAuthority216.model_construct(
                campaign_id="eng-calibration-001k",
                protocol_version="eng-calibration-consensus-206-v1",
                stage="dev",
                sampling_manifest_digest="1" * 64,
                source_packet_digest="2" * 64,
                lane_digests=("3" * 64, "4" * 64, "5" * 64),
                queue_evidence_sha256="6" * 64,
                expected_membership_digest="7" * 64,
                labels=((_sid(1), dict(_GOOD_CRITICAL), "cross_model_consensus"),),
                retained_unknown_ids=(),
                human_adjudicated_ids=(),
            ).labels_by_sample()


class TestFreshLabelLedgerBinding:
    def _authority(self, dev_ids: list[str]):
        from evals.calibration.campaign_001k_fit import FreshLabelAuthority216

        verified = _build_dev_ledger(dev_ids)
        stage_sampling = _stage_sampling(dev_ids)
        return FreshLabelAuthority216.from_verified_ledger(
            verified,
            stage="dev",
            stage_sampling=stage_sampling,
            expected_membership=frozenset(dev_ids),
            source_packet_digest=verified.ledger.source_packet_digest,
        )

    def test_real_ledger_projects_dev_labels(self) -> None:
        dev_ids = _POP["forced"] + _POP["dev_fresh"]
        authority = self._authority(dev_ids)
        assert authority.stage == "dev"
        assert len(authority.labels) == 102
        labels = authority.labels_by_sample()
        assert set(labels) == set(dev_ids)

    def test_wrong_stage_membership_rejected(self) -> None:
        from evals.calibration.campaign_001k_fit import FreshLabelAuthority216

        dev_ids = _POP["forced"] + _POP["dev_fresh"]
        verified = _build_dev_ledger(dev_ids)
        with pytest.raises(ValueError, match="fresh_label_membership_mismatch"):
            FreshLabelAuthority216.from_verified_ledger(
                verified,
                stage="dev",
                stage_sampling=_stage_sampling(dev_ids),
                expected_membership=frozenset(dev_ids[:-1]),
                source_packet_digest=verified.ledger.source_packet_digest,
            )

    def test_non_001k_ledger_rejected(self) -> None:
        from evals.calibration.campaign_001k_fit import FreshLabelAuthority216
        from tests.test_calibration_206_helpers import build_verified_ledger

        ids = [_sid(i) for i in range(6)]
        verified = build_verified_ledger(
            tuple(ids), {sid: dict(_GOOD_CRITICAL) for sid in ids}, campaign_id="campaign"
        )
        with pytest.raises(ValueError, match="fresh_label_ledger_campaign_mismatch"):
            FreshLabelAuthority216.from_verified_ledger(
                verified,
                stage="dev",
                stage_sampling=_stage_sampling(ids),
                expected_membership=frozenset(ids),
                source_packet_digest=verified.ledger.source_packet_digest,
            )

    def test_forged_ledger_lookalike_rejected(self) -> None:
        from evals.calibration.campaign_001k_fit import FreshLabelAuthority216

        class ForgedLedger:
            ledger = type(
                "L",
                (),
                {
                    "campaign_id": "eng-calibration-001k",
                    "protocol_version": "eng-calibration-consensus-206-v1",
                    "sampling_manifest_digest": "1" * 64,
                    "source_packet_digest": "2" * 64,
                    "lane_digests": ("3" * 64, "4" * 64, "5" * 64),
                    "queue_evidence_sha256": "6" * 64,
                    "wrappers": (),
                },
            )()

        dev_ids = _POP["forced"][:3]
        with pytest.raises((ValueError, TypeError, AttributeError)):
            FreshLabelAuthority216.from_verified_ledger(
                ForgedLedger(),  # type: ignore[arg-type]
                stage="dev",
                stage_sampling=_stage_sampling(dev_ids),
                expected_membership=frozenset(dev_ids),
                source_packet_digest="2" * 64,
            )

    def test_capability_invalidated_by_tampering(self) -> None:
        dev_ids = _POP["forced"] + _POP["dev_fresh"]
        authority = self._authority(dev_ids)
        # mutate the frozen record past validators (simulated tamper)
        extra_row = (_sid(9999), dict(_GOOD_CRITICAL), "cross_model_consensus")
        tampered = authority.model_copy(update={"labels": authority.labels + (extra_row,)})
        with pytest.raises(ValueError, match="capability_invalid|membership"):
            tampered.labels_by_sample()


class TestFreshStageMembership:
    def _fixtures(self, dev_ids: list[str]):
        reuse = _real_shaped_reuse()
        split = _split(_POP["executed"] + _POP["forced"] + _POP["dev_fresh"], _POP["holdout"])
        frame_by_id = {sid: _frame_row(0) for sid in _FRESH_202}
        authority = None
        return reuse, split, frame_by_id, authority

    def _dev_fit(self, dev_ids: list[str], **overrides: Any):
        from evals.calibration.campaign_001k_fit import FreshLabelAuthority216

        reuse, split, frame_by_id, _ = self._fixtures(dev_ids)
        verified = _build_dev_ledger(dev_ids)
        authority = FreshLabelAuthority216.from_verified_ledger(
            verified,
            stage="dev",
            stage_sampling=_stage_sampling(dev_ids),
            expected_membership=frozenset(dev_ids),
            source_packet_digest=verified.ledger.source_packet_digest,
        )
        return dev_fit_observations(
            fresh_label_authority=authority,
            provider_evidence=_fresh_evidence(),
            target_identity=_target(),
            split=split,
            frame_by_id=frame_by_id,
            reuse=reuse,
            **overrides,
        )

    def test_dev_fit_requires_exact_102(self) -> None:
        dev_ids = _POP["forced"] + _POP["dev_fresh"]
        obs = self._dev_fit(dev_ids)
        assert {o.sample_id for o in obs} == set(dev_ids)

    def test_dev_fit_rejects_incomplete_stage(self) -> None:
        dev_ids = (_POP["forced"] + _POP["dev_fresh"])[:-1]
        with pytest.raises(ValueError, match="dev_fit_labels_incomplete"):
            self._dev_fit(dev_ids)

    def test_dev_fit_rejects_exact_count_wrong_membership(self) -> None:
        # 102 IDs, but one is a holdout ID substituted for a dev ID
        dev_ids = (_POP["forced"] + _POP["dev_fresh"])[:-1] + [_POP["holdout"][0]]
        with pytest.raises(ValueError, match="dev_fit_labels_outside_frozen_membership"):
            self._dev_fit(dev_ids)

    def test_holdout_label_in_dev_rejected(self) -> None:
        from evals.calibration.campaign_001k_fit import FreshLabelAuthority216

        dev_ids = _POP["forced"] + _POP["dev_fresh"]
        reuse, split, frame_by_id, _ = self._fixtures(dev_ids)
        verified = _build_dev_ledger(dev_ids + [_POP["holdout"][0]])
        with pytest.raises(ValueError, match="dev_fit_labels_outside_frozen_membership"):
            authority = FreshLabelAuthority216.from_verified_ledger(
                verified,
                stage="dev",
                stage_sampling=_stage_sampling(dev_ids + [_POP["holdout"][0]]),
                expected_membership=frozenset(dev_ids + [_POP["holdout"][0]]),
                source_packet_digest=verified.ledger.source_packet_digest,
            )
            dev_fit_observations(
                fresh_label_authority=authority,
                provider_evidence=_fresh_evidence(),
                target_identity=_target(),
                split=split,
                frame_by_id=frame_by_id,
                reuse=reuse,
            )

    def test_holdout_stage_requires_holdout_authority(self) -> None:
        from evals.calibration.campaign_001k_fit import FreshLabelAuthority216

        reuse = _real_shaped_reuse()
        split = _split(_POP["executed"] + _POP["forced"] + _POP["dev_fresh"], _POP["holdout"])
        frame_by_id = {sid: _frame_row(0) for sid in _FRESH_202}
        dev_ids = _POP["forced"] + _POP["dev_fresh"]
        verified = _build_dev_ledger(dev_ids)
        authority = FreshLabelAuthority216.from_verified_ledger(
            verified,
            stage="dev",
            stage_sampling=_stage_sampling(dev_ids),
            expected_membership=frozenset(dev_ids),
            source_packet_digest=verified.ledger.source_packet_digest,
        )
        with pytest.raises(ValueError, match="holdout_evaluate_requires_holdout_stage"):
            holdout_evaluate_observations(
                fresh_label_authority=authority,
                provider_evidence=_fresh_evidence(),
                target_identity=_target(),
                split=split,
                frame_by_id=frame_by_id,
                reuse=reuse,
            )


# ---------------------------------------------------------------------------
# FIX2-217-7: holdout barrier bound to canonical artifact freeze
# ---------------------------------------------------------------------------


class TestHoldoutBarrierFreezeBound:
    def _freeze_kwargs(self, tmp_path: Path, **overrides: Any) -> dict[str, Any]:
        artifact = tmp_path / "candidate.json"
        artifact.write_text('{"profiles": []}\n')
        kwargs: dict[str, Any] = dict(
            protected_root=tmp_path,
            target_identity=_target(),
            split_digest="1" * 64,
            dev_membership_digest="2" * 64,
            dev_fitting_evidence_digest="3" * 64,
            candidate_artifact_path=artifact,
            frozen_fitting_inputs_digest="4" * 64,
            holdout_membership_digest="5" * 64,
        )
        kwargs.update(overrides)
        return kwargs

    def test_forged_arbitrary_digests_fail(self, tmp_path: Path) -> None:
        from evals.calibration import campaign_001k_holdout_barrier as barrier

        with pytest.raises(ValueError, match="holdout_locked_artifact_not_frozen"):
            barrier.require_holdout_export_allowed(
                campaign_id=c216.CAMPAIGN_ID_001K,
                protected_root=tmp_path,
                frozen_artifact_digest="0" * 64,
                dev_fitting_evidence_digest="1" * 64,
            )

    def test_export_blocked_before_freeze(self, tmp_path: Path) -> None:
        from evals.calibration import campaign_001k_holdout_barrier as barrier

        with pytest.raises(ValueError, match="holdout_locked_artifact_not_frozen"):
            barrier.require_holdout_export_allowed(
                campaign_id=c216.CAMPAIGN_ID_001K,
                protected_root=tmp_path,
            )

    def test_wrong_campaign_rejected(self, tmp_path: Path) -> None:
        from evals.calibration import campaign_001k_holdout_barrier as barrier

        with pytest.raises(ValueError, match="holdout_locked_unknown_campaign"):
            barrier.require_holdout_export_allowed(
                campaign_id="eng-calibration-999z",
                protected_root=tmp_path,
            )

    def test_unlock_requires_canonical_freeze(self, tmp_path: Path) -> None:
        from evals.calibration import campaign_001k_holdout_barrier as barrier

        with pytest.raises(ValueError, match="holdout_locked_artifact_not_frozen"):
            barrier.unlock_holdout(
                protected_root=tmp_path,
                campaign_id=c216.CAMPAIGN_ID_001K,
                holdout_split_digest="1" * 64,
                holdout_membership_digest="5" * 64,
                frozen_artifact_digest="6" * 64,
                target_identity_digest=_target().identity_digest(),
            )

    def test_full_freeze_then_unlock_then_export(self, tmp_path: Path) -> None:
        from evals.calibration import campaign_001k_holdout_barrier as barrier

        kwargs = self._freeze_kwargs(tmp_path)
        artifact_digest = barrier._sha256_file(kwargs["candidate_artifact_path"])
        barrier.record_dev_artifact_freeze(**kwargs)
        barrier.unlock_holdout(
            protected_root=tmp_path,
            campaign_id=c216.CAMPAIGN_ID_001K,
            holdout_split_digest=kwargs["split_digest"],
            holdout_membership_digest=kwargs["holdout_membership_digest"],
            frozen_artifact_digest=artifact_digest,
            target_identity_digest=_target().identity_digest(),
        )
        barrier.require_holdout_export_allowed(
            campaign_id=c216.CAMPAIGN_ID_001K, protected_root=tmp_path
        )
        # binding verification with exact identities succeeds
        barrier.verify_holdout_binding(
            protected_root=tmp_path,
            campaign_id=c216.CAMPAIGN_ID_001K,
            holdout_split_digest=kwargs["split_digest"],
            frozen_artifact_digest=artifact_digest,
            target_identity_digest=_target().identity_digest(),
        )

    def test_unlock_rejects_mismatched_bindings(self, tmp_path: Path) -> None:
        from evals.calibration import campaign_001k_holdout_barrier as barrier

        kwargs = self._freeze_kwargs(tmp_path)
        barrier.record_dev_artifact_freeze(**kwargs)
        with pytest.raises(ValueError, match="holdout_unlock_binding_mismatch"):
            barrier.unlock_holdout(
                protected_root=tmp_path,
                campaign_id=c216.CAMPAIGN_ID_001K,
                holdout_split_digest="f" * 64,  # not the frozen split digest
                holdout_membership_digest=kwargs["holdout_membership_digest"],
                frozen_artifact_digest=barrier._sha256_file(kwargs["candidate_artifact_path"]),
                target_identity_digest=_target().identity_digest(),
            )

    def test_freeze_rejects_wrong_contract_target(self, tmp_path: Path) -> None:
        from evals.calibration import campaign_001k_holdout_barrier as barrier

        kwargs = self._freeze_kwargs(
            tmp_path,
            target_identity=_target(
                calibration_dataset_version="calibration-157-dogfood-v2",
                dimensions=("taxonomy", "retention", "epistemic"),
            ),
        )
        with pytest.raises(ValueError, match="target_identity_not_001k_contract"):
            barrier.record_dev_artifact_freeze(**kwargs)

    def test_tampered_unlock_record_fails(self, tmp_path: Path) -> None:
        from evals.calibration import campaign_001k_holdout_barrier as barrier

        kwargs = self._freeze_kwargs(tmp_path)
        barrier.record_dev_artifact_freeze(**kwargs)
        barrier.unlock_holdout(
            protected_root=tmp_path,
            campaign_id=c216.CAMPAIGN_ID_001K,
            holdout_split_digest=kwargs["split_digest"],
            holdout_membership_digest=kwargs["holdout_membership_digest"],
            frozen_artifact_digest=barrier._sha256_file(kwargs["candidate_artifact_path"]),
            target_identity_digest=_target().identity_digest(),
        )
        # tamper the freeze record AFTER unlock: the unlock must stop validating
        freeze_path = tmp_path / "dev-artifact-freeze-001k.json"
        record = json.loads(freeze_path.read_text())
        record["frozen_artifact_digest"] = "e" * 64
        freeze_path.write_text(json.dumps(record, indent=2))
        with pytest.raises(ValueError, match="holdout_locked_artifact_not_frozen"):
            barrier.require_holdout_export_allowed(
                campaign_id=c216.CAMPAIGN_ID_001K, protected_root=tmp_path
            )


class TestHoldoutBypassNegativePaths:
    """No CLI path exposes/ingests holdout material before freeze."""

    def _holdout_sampling_file(self, tmp_path: Path) -> Path:
        sampling = _sampling(_POP["holdout"])
        sampling = sampling.model_copy(update={"sampling_seed": "216-holdout-v1"})
        path = tmp_path / "holdout-sampling.json"
        path.write_text(json.dumps(sampling.model_dump(mode="json")))
        return path

    def test_sub_prepare_blocked_before_freeze(self, tmp_path: Path) -> None:
        sampling_path = self._holdout_sampling_file(tmp_path)
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "evals.calibration",
                "sub-prepare",
                "--campaign-id",
                "eng-calibration-001k",
                "--sampling-manifest",
                str(sampling_path),
                "--neutral-packet",
                str(tmp_path / "n.json"),
                "--neutral-packet-manifest",
                str(tmp_path / "m.json"),
                "--source-packet-digest",
                "0" * 64,
                "--protected-dir",
                str(tmp_path),
            ],
            capture_output=True,
            text=True,
            cwd=Path(__file__).parents[1],
        )
        assert result.returncode != 0
        assert "holdout_locked" in result.stderr

    def test_sub_batch_show_blocked_before_freeze(self, tmp_path: Path) -> None:
        sampling_path = self._holdout_sampling_file(tmp_path)
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "evals.calibration",
                "sub-batch-show",
                "--campaign-id",
                "eng-calibration-001k",
                "--sampling-manifest",
                str(sampling_path),
                "--source-packet-digest",
                "0" * 64,
                "--protected-dir",
                str(tmp_path),
                "--batch-id",
                "eng-calibration-001k:sub-review-001",
            ],
            capture_output=True,
            text=True,
            cwd=Path(__file__).parents[1],
        )
        assert result.returncode != 0
        assert "holdout_locked" in result.stderr

    def test_sub_import_blocked_before_freeze(self, tmp_path: Path) -> None:
        sampling_path = self._holdout_sampling_file(tmp_path)
        resp = tmp_path / "resp.txt"
        resp.write_text("{}")
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "evals.calibration",
                "sub-import",
                "--campaign-id",
                "eng-calibration-001k",
                "--sampling-manifest",
                str(sampling_path),
                "--reviewer-slot",
                "model_a",
                "--batch-id",
                "eng-calibration-001k:sub-review-001",
                "--raw-response",
                str(resp),
                "--source-packet-digest",
                "0" * 64,
                "--protected-dir",
                str(tmp_path),
            ],
            capture_output=True,
            text=True,
            cwd=Path(__file__).parents[1],
        )
        assert result.returncode != 0
        assert "holdout_locked" in result.stderr

    def test_sub_lane_init_blocked_before_freeze(self, tmp_path: Path) -> None:
        sampling_path = self._holdout_sampling_file(tmp_path)
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "evals.calibration",
                "sub-lane-init",
                "--campaign-id",
                "eng-calibration-001k",
                "--sampling-manifest",
                str(sampling_path),
                "--reviewer-slot",
                "model_a",
                "--reviewer-config-digest",
                "a" * 64,
                "--source-packet-digest",
                "0" * 64,
                "--neutral-packet",
                str(tmp_path / "n.json"),
                "--neutral-packet-manifest",
                str(tmp_path / "m.json"),
                "--visible-model-name",
                "Claude Opus 5",
                "--operator",
                "test-op",
                "--protected-dir",
                str(tmp_path),
            ],
            capture_output=True,
            text=True,
            cwd=Path(__file__).parents[1],
        )
        assert result.returncode != 0
        assert "holdout_locked" in result.stderr

    def test_direct_model_lane_rejects_001k(self, tmp_path: Path) -> None:
        sampling_path = self._holdout_sampling_file(tmp_path)
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "evals.calibration",
                "model-lane-init",
                "--campaign-id",
                "eng-calibration-001k",
                "--sampling-manifest",
                str(sampling_path),
                "--reviewer-identity",
                str(tmp_path / "r.json"),
                "--reviewer-slot",
                "model_a",
                "--source-packet-digest",
                "0" * 64,
                "--neutral-packet",
                str(tmp_path / "n.json"),
                "--neutral-packet-manifest",
                str(tmp_path / "m.json"),
                "--protected-dir",
                str(tmp_path),
            ],
            capture_output=True,
            text=True,
            cwd=Path(__file__).parents[1],
        )
        assert result.returncode != 0
        assert "campaign_001k_rejects_direct_model_lane" in result.stderr


# ---------------------------------------------------------------------------
# 001f unchanged + retained earlier-round coverage
# ---------------------------------------------------------------------------


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

    def test_holdout_floor_enforced(self) -> None:
        fresh = [_sid(i) for i in range(200, 250)]
        with pytest.raises(ValueError, match="reuse_manifest_holdout_below_floor"):
            _reuse(holdout=fresh[:10], forced=[], dev_fresh=fresh[10:])


class TestSubscriptionOptIn:
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

    def test_committed_cli_exposes_216_commands(self) -> None:
        result = subprocess.run(
            [sys.executable, "-m", "evals.calibration", "--help"],
            capture_output=True,
            text=True,
            cwd=Path(__file__).parents[1],
        )
        assert result.returncode == 0
        for command in ("216-freeze-target", "216-reuse-manifest", "216-dev-packet"):
            assert command in result.stdout

    def test_committed_public_manifest_matches_reality(self) -> None:
        manifest = Path(c216.__file__).parent / "campaigns/216/campaign-reuse-public.json"
        payload = json.loads(manifest.read_text())
        assert payload["campaign_id"] == c216.CAMPAIGN_ID_001K
        assert payload["holdout"] >= c216.HOLDOUT_MIN_216
        assert payload["executed_200"] == c216.EXECUTED_PREFIX
        assert payload["leakage_checks"]["cross_split_shared_hash_groups"] == 0


class TestFrameStratumVocabulary:
    def test_unavailable_maps_to_unknown(self) -> None:
        from evals.calibration.campaign_001k_fit import _frame_stratum

        stratum = _frame_stratum(_frame_row(1))
        assert stratum["assertion_mode"] == "unknown"
        assert stratum["risk"] == "unknown"
        assert stratum["source_type"] == "migration"


class TestCampaignConstants:
    def test_constants(self) -> None:
        assert c216.DIMENSIONS_001K == ("taxonomy", "retention")
        assert c216.CAMPAIGN_ID_001K == "eng-calibration-001k"
        assert c216.EXECUTED_PREFIX == 200

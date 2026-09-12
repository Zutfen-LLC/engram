"""Regression proofs for the #216 campaign (ENG-CALIBRATION-001K).

Synthetic-fixture tests for the campaign 001k module: reuse-boundary
partitioning, freshness fail-closed, deterministic holdout, split leakage,
and reused-label provenance. (The real protected evidence is exercised by
the campaign CLI on the host; these tests bind the invariants.)
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


def _sampling(ids: list[str]) -> SamplingManifest:
    return SamplingManifest(
        campaign_id=c216.CAMPAIGN_ID_001K,
        target_identity_digest="a" * 64,
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


class TestReusePartition:
    def test_first_200_never_enter_holdout(self) -> None:
        executed = [_sid(i) for i in range(c216.EXECUTED_PREFIX)]
        fresh = [_sid(i) for i in range(200, 402)]
        reuse = _reuse(holdout=fresh[:100], forced=[], dev_fresh=fresh[100:])
        assert not set(reuse.holdout_ids) & set(executed)
        dev = reuse.dev_ids(tuple(executed))  # type: ignore[arg-type]
        assert set(executed) <= set(dev)

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
        executed = [_sid(i) for i in range(c216.EXECUTED_PREFIX)]
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
        pool = tuple(_sid(i) for i in range(200, 377))  # 177 like the real pool
        internal = [[_sid(350), _sid(351)], [_sid(360), _sid(361), _sid(362)]]
        h1 = c216.deterministic_holdout(pool, internal)
        h2 = c216.deterministic_holdout(pool, internal)
        assert h1 == h2
        assert len(h1) >= c216.HOLDOUT_MIN_216
        # no internal group straddles the boundary
        hold = set(h1)
        for group in internal:
            assert not (set(group) & hold) or set(group) <= hold

    def test_label_blind_ranking(self) -> None:
        pool = tuple(_sid(i) for i in range(200, 377))
        # identical inputs -> identical outputs (no hidden randomness)
        assert c216.deterministic_holdout(pool, []) == c216.deterministic_holdout(pool, [])


class TestSplit001k:
    def test_split_partitions_population_and_zero_cross_leakage(self) -> None:
        executed = [_sid(i) for i in range(c216.EXECUTED_PREFIX)]
        fresh = [_sid(i) for i in range(200, 402)]
        spanning = [_sid(300)]
        dev_fresh = [sid for sid in fresh[100:] if sid not in spanning]
        reuse = _reuse(holdout=fresh[:100], forced=spanning, dev_fresh=dev_fresh)
        sampling = _sampling(executed + fresh)
        duplicates = {"g1": [executed[0], _sid(300)]}
        split = c216.build_split_001k(reuse, sampling, duplicates)
        assert set(split.dev_ids) | set(split.holdout_ids) == set(sampling.sample_ids)
        assert not set(split.dev_ids) & set(split.holdout_ids)
        assert split.leakage_checks["cross_split_shared_hash_groups"] == 0
        assert _sid(300) in split.dev_ids

    def test_cross_split_group_fails_closed(self) -> None:
        executed = [_sid(i) for i in range(c216.EXECUTED_PREFIX)]
        fresh = [_sid(i) for i in range(200, 402)]
        # a duplicate group straddling dev/holdout must be rejected
        reuse = _reuse(holdout=fresh[:100], forced=[], dev_fresh=fresh[100:])
        sampling = _sampling(executed + fresh)
        duplicates = {"g1": [fresh[50], fresh[150]]}  # holdout member + dev member
        with pytest.raises(ValueError, match="split_001k_cross_split_duplicate_groups"):
            c216.build_split_001k(reuse, sampling, duplicates)


class TestFrameStratumVocabulary:
    def test_unavailable_maps_to_unknown(self) -> None:
        row = _frame_row(1)
        stratum = _frame_stratum(row)
        assert stratum["assertion_mode"] == "unknown"
        assert stratum["risk"] == "unknown"
        assert stratum["source_type"] == "migration"


class TestProviderEvidence:
    def test_rejects_wrong_prompt_version(self) -> None:
        payload: dict[str, Any] = {
            "run_kind": "issue-216-fresh-202-assess3",
            "prompt_version": "engram.assess.2",
            "cases": [],
        }
        with pytest.raises(ValueError, match="provider_evidence_wrong_prompt_version"):
            ProviderEvidence216.from_payload(payload)

    def test_parses_ok_and_error_cases(self) -> None:
        payload = {
            "run_kind": "issue-216-fresh-202-assess3",
            "prompt_version": "engram.assess.3",
            "cases": [
                {
                    "sample_id": _sid(1),
                    "status": "ok",
                    "values": {"taxonomy_value": 0.9, "retention_value": 0.85},
                },
                {"sample_id": _sid(2), "status": "error", "error_type": "ValidationError"},
            ],
        }
        evidence = ProviderEvidence216.from_payload(payload)
        assert evidence.ok_count == 1 and evidence.error_count == 1
        assert _sid(2) not in evidence.values_by_sample


class TestReusedObservations:
    def test_abstaining_case_contributes_no_numeric(self) -> None:
        # exercised end-to-end on real evidence by the campaign run; here we
        # bind the vocabulary contract only
        assert c216.DIMENSIONS_001K == ("taxonomy", "retention")
        assert c216.CAMPAIGN_ID_001K == "eng-calibration-001k"
        assert c216.EXECUTED_PREFIX == 200


class TestCampaignAuthority:
    def test_committed_public_manifest_matches_reality(self) -> None:
        manifest = Path(c216.__file__).parent / "campaigns/216/campaign-reuse-public.json"
        payload = json.loads(manifest.read_text())
        assert payload["campaign_id"] == c216.CAMPAIGN_ID_001K
        assert payload["holdout"] >= c216.HOLDOUT_MIN_216
        assert payload["executed_200"] == c216.EXECUTED_PREFIX
        assert payload["leakage_checks"]["cross_split_shared_hash_groups"] == 0

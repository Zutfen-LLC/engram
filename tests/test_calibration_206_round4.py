"""Round-4 adversarial proofs (#206, PR #207): execution provenance and
calibration-evidence chain.

Covers FIX-R4-1 .. FIX-R4-6:

- FIX-R4-1: execution receipts bind actual emitted requests and actual
  executor identity; cross-lane / wrong-model / never-requested / wrong
  generation / other-packet responses all fail.
- FIX-R4-2: parsed judgments are DERIVED from the exact preserved response
  bytes; raw/parsed disagreement is impossible to ingest and re-verified at
  freeze/load.
- FIX-R4-3: the final ledger verifies the frozen frame itself; same-ID
  mutated frames fail before audit selection.
- FIX-R4-4: the frozen split is bound and verified against an INDEPENDENTLY
  retained authority; an alternative internally-valid split over the same
  membership is rejected at every downstream boundary.
- FIX-R4-5: consensus calibration consumes the SAME protected
  assessment-evidence contract as #202; fabricated self-consistent receipt
  lists can no longer be passed at all (the parameter is gone) and tampered
  artifacts fail Stage A.
- FIX-R4-6: the canonical semantic instruction bundle is frozen, complete,
  digest-bound, and drives reviewer identity + request emission.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from evals.admission.schema import digest
from evals.calibration.consensus import (
    REVIEWER_SLOTS,
    ModelReviewRecord,
    ReviewerIdentity,
)
from evals.calibration.freeze import (
    LABEL_GUIDE_VERSION,
    SamplingManifest,
    protected_frame_digest,
)
from evals.calibration.ingestion import (
    LABELING_INSTRUCTIONS,
    LaneSession,
    labeling_instructions_digest,
)
from evals.calibration.review import write_protected_file
from evals.calibration.reviewer_instructions import (
    CANONICAL_SEMANTIC_BUNDLE,
    RESPONSE_PARSER_VERSION,
    parse_model_response,
)
from tests.test_calibration_206_helpers import (
    NOW,
    build_frame_rows,
    build_identity,
    build_split,
    write_assessment_evidence,
)

IDS = ("s1", "s2", "s3")
GOOD_CRITICAL = {
    "expected_kind": "fact",
    "retention_value": "retain",
    "epistemic_state": "adequately_supported",
    "consequence": "low",
    "acceptable_abstention": "no",
}


def _sampling(ids: tuple[str, ...]) -> SamplingManifest:
    return SamplingManifest(
        campaign_id="campaign",
        target_identity_digest="1" * 64,
        frame_digest=protected_frame_digest(build_frame_rows(ids)),
        snapshot_sha256="3" * 64,
        snapshot_as_of=NOW,
        sampling_seed="seed",
        inclusion_rules=("rule",),
        exclusion_rules=(),
        source_row_counts={"eligible_frame": len(ids)},
        stratum_counts={"all": len(ids)},
        coverage_dimensions={},
        sample_ids=ids,
        sample_hashes=tuple(digest(sid) for sid in ids),
    )


def _reviewer(slot: str) -> ReviewerIdentity:
    families = dict(zip(REVIEWER_SLOTS, ("claude-opus", "gpt-astra", "glm-5-3-max"), strict=True))
    family = families[slot]
    return ReviewerIdentity(
        reviewer_slot=slot,  # type: ignore[arg-type]
        reviewer_family=family,
        provider_model_identifier=f"{family}-exact-2026-09",
        reviewer_config_digest="a" * 64,
        prompt_digest=labeling_instructions_digest(),
    )


def _raw_judgment(sample_id: str, fields: dict | None = None) -> str:
    return json.dumps(
        {
            "sample_id": sample_id,
            "outcome": "judged",
            "judgment": {
                "fields": dict(fields or GOOD_CRITICAL),
                "reviewer_confidence": "medium",
            },
        }
    )


def _setup_lane(tmp_path: Path):
    """Init a claude-opus lane + emit generation-1 requests for all samples."""
    sampling = _sampling(IDS)
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
        for sid in IDS
    ]
    from evals.calibration.model_lanes import NeutralModelPacket
    from evals.calibration.review import _packet_file_payload

    packet = NeutralModelPacket(
        packet_id="campaign-blind-v2",
        sampling_manifest_digest=sampling.manifest_digest(),
        guide_version=LABEL_GUIDE_VERSION,
        reviewer_hint="neutral_model_review",
        cases=cases,
        source_packet_digest="f" * 64,
    )
    packet_dir = tmp_path / "packet"
    packet_dir.mkdir(parents=True, exist_ok=True)
    payload = _packet_file_payload(packet)
    packet_path = packet_dir / "campaign-blind-v2.neutral.json"
    write_protected_file(packet_path, payload)
    manifest_path = packet_dir / "neutral-packet-manifest.json"
    write_protected_file(
        manifest_path,
        (
            json.dumps({packet_path.name: hashlib.sha256(payload).hexdigest()}, indent=2) + "\n"
        ).encode(),
    )
    session = LaneSession.init(
        tmp_path,
        reviewer=_reviewer("model_a"),
        campaign_id="campaign",
        sampling=sampling,
        source_packet_digest="f" * 64,
        neutral_packet_path=packet_path,
        neutral_packet_manifest=manifest_path,
    )
    session.emit_requests(packet_path, sampling=sampling, manifest_path=manifest_path)
    return session, sampling, packet_path, manifest_path


def _receipt(session: LaneSession, sample_id: str, **overrides) -> dict:
    """FIX-R5-1: receipts are built from OBSERVED executor metadata.

    Defaults observe the lane's own identity (the synthetic executor was
    configured for this lane); adversarial overrides replace one OBSERVED
    value with a genuinely different executor observation. The item digest
    is always derived from the VERIFIED request-batch registry — never
    caller-asserted.
    """
    from evals.calibration.ingestion import observe_execution

    observed_keys = {
        "reviewer_family": "actual_reviewer_family",
        "reviewer_slot": "actual_reviewer_slot",
        "provider_model_identifier": "actual_provider_model_identifier",
        "reviewer_config_digest": "actual_configuration_digest",
        "prompt_digest": "actual_prompt_digest",
    }
    observed = {
        "actual_reviewer_slot": session.reviewer.reviewer_slot,
        "actual_reviewer_family": session.reviewer.reviewer_family,
        "actual_provider_model_identifier": session.reviewer.provider_model_identifier,
        "actual_configuration_digest": session.reviewer.reviewer_config_digest,
        "actual_prompt_digest": session.reviewer.prompt_digest,
    }
    for key, observed_key in observed_keys.items():
        if key in overrides:
            observed[observed_key] = overrides.pop(key)
    identity_source = overrides.pop("identity_source", "provider_metadata")
    provider_request_id = overrides.pop("provider_request_id", "req-206-0001")
    provider_response_id = overrides.pop("provider_response_id", "resp-206-0001")
    # FIX-R6-2: the synthetic executor captures a digest-bound provider
    # metadata artifact that honestly reports the (possibly overridden)
    # observed identity — the artifact derives the model/request/response IDs.
    artifact = None
    if identity_source == "provider_metadata":
        from evals.calibration.provider_metadata import ProviderMetadataArtifact

        artifact = ProviderMetadataArtifact.capture(
            provider="synthetic-provider-206",
            raw_metadata={
                "model": observed["actual_provider_model_identifier"],
                "request_id": provider_request_id,
                "response_id": provider_response_id,
            },
        )
    receipt = observe_execution(
        session.lane_root,
        campaign_id="campaign",
        sample_id=sample_id,
        request_generation=overrides.pop("request_generation", 1),
        executor_status=overrides.pop("executor_status", "completed"),
        identity_source=identity_source,
        executor_identity=overrides.pop("executor_identity", "synthetic-executor-206"),
        provider_metadata_artifact=artifact,
        executed_at=NOW,
        **observed,
    )
    payload = json.loads(json.dumps(receipt.model_dump(mode="json")))
    assert not overrides, f"unmapped receipt overrides: {sorted(overrides)}"
    return payload


# ---------------------------------------------------------------------------
# FIX-R4-6: canonical frozen semantic instruction bundle
# ---------------------------------------------------------------------------


class TestFixR46SemanticBundle:
    def test_bundle_digest_deterministic(self):
        assert labeling_instructions_digest() == labeling_instructions_digest()

    def test_changing_one_semantic_rule_changes_prompt_digest(self):
        from evals.calibration.consensus import digest_of

        mutated = json.loads(json.dumps(LABELING_INSTRUCTIONS))
        mutated["semantics"]["retention_value"]["rules"] = (
            mutated["semantics"]["retention_value"]["rules"] + " CHANGED."
        )
        assert digest_of(mutated) != labeling_instructions_digest()

    def test_bundle_carries_actual_semantics_not_just_version_name(self):
        semantics = LABELING_INSTRUCTIONS["semantics"]
        assert semantics is CANONICAL_SEMANTIC_BUNDLE
        for field in (
            "expected_kind",
            "retention_value",
            "epistemic_state",
            "consequence",
            "acceptable_abstention",
        ):
            assert field in semantics
            assert semantics[field]["allowed_vocabulary"]
            rules = semantics[field]["rules"]
            assert len(str(rules)) > 80, f"{field} must carry decision semantics"
        # expected_kind: judge independently of governed kind + per-kind rules
        assert "governed" in str(semantics["expected_kind"]["judge_independently_of_governed_kind"])
        for kind in (
            "fact",
            "observation",
            "decision",
            "procedure",
            "summary",
            "doctrine",
            "invariant",
        ):
            assert kind in semantics["expected_kind"]["rules"]
        # epistemic definitions at decision time
        assert "DECISION TIME" in semantics["epistemic_state"]["rules"]
        # consequence = erroneous silent admission
        assert "ERRONEOUS SILENT ADMISSION" in semantics["consequence"]["rules"]
        # retention = durable usefulness, not truth
        assert "NEVER truth" in semantics["retention_value"]["rules"]

    def test_stale_prompt_digest_cannot_initialize_lane(self, tmp_path: Path):
        stale = _reviewer("model_a").model_copy(update={"prompt_digest": "0" * 64})
        with pytest.raises(
            ValueError, match="reviewer_prompt_digest_does_not_match_frozen_instructions"
        ):
            # any init attempt with the stale identity fails before writing
            from evals.calibration.ingestion import verify_reviewer_prompt_binding

            verify_reviewer_prompt_binding(stale)

    def test_emitted_request_contains_complete_frozen_bundle(self, tmp_path: Path):
        session, sampling, packet_path, manifest_path = _setup_lane(tmp_path)
        # re-emit for the still-pending samples and inspect the line
        path = session.emit_requests(packet_path, sampling=sampling, manifest_path=manifest_path)
        line = json.loads(path.read_text().splitlines()[0])
        assert line["labeling_instructions"] == LABELING_INSTRUCTIONS
        assert line["prompt_digest"] == labeling_instructions_digest()
        assert line["labeling_instructions"]["semantics"]["expected_kind"]["rules"]["fact"]

    def test_lane_cannot_initialize_against_another_instruction_version(self, tmp_path: Path):
        # A reviewer identity bound to a DIFFERENT (older) prompt digest cannot
        # even pass module-level verification, and LaneSession.init re-checks.
        stale = _reviewer("model_b").model_copy(update={"prompt_digest": "9" * 64})
        with pytest.raises(ValueError, match="prompt_digest"):
            from evals.calibration.ingestion import verify_reviewer_prompt_binding

            verify_reviewer_prompt_binding(stale)


# ---------------------------------------------------------------------------
# FIX-R4-1: execution receipts + request binding
# ---------------------------------------------------------------------------


class TestFixR41ExecutionReceipts:
    def test_correct_receipt_and_binding_passes(self, tmp_path: Path):
        session, sampling, _, _ = _setup_lane(tmp_path)
        record = session.ingest_response(
            {
                "sample_id": "s1",
                "raw_response": _raw_judgment("s1"),
                "execution": _receipt(session, "s1"),
            },
            sampling=sampling,
        )
        assert record.parse_status == "parsed"
        assert record.request_generation == 1
        assert record.execution is not None
        assert record.execution.provider_model_identifier.startswith("claude-opus")

    def test_gpt_receipt_submitted_to_claude_lane_fails(self, tmp_path: Path):
        session, sampling, _, _ = _setup_lane(tmp_path)
        # FIX-R5-1: the OBSERVED execution metadata names GPT for the Claude
        # lane — the receipt is built from the observation and then compared
        # EXACTLY against the frozen expected identity, so ingestion fails.
        wrong = _receipt(
            session,
            "s1",
            reviewer_family="gpt-astra",
            reviewer_slot="model_b",
            provider_model_identifier="gpt-astra-impostor",
        )
        with pytest.raises(ValueError, match="execution_receipt_identity_mismatch"):
            session.ingest_response(
                {
                    "sample_id": "s1",
                    "raw_response": _raw_judgment("s1"),
                    "execution": wrong,
                },
                sampling=sampling,
            )

    def test_same_family_wrong_exact_model_version_fails(self, tmp_path: Path):
        session, sampling, _, _ = _setup_lane(tmp_path)
        wrong = _receipt(session, "s1", provider_model_identifier="claude-opus-OLD")
        with pytest.raises(ValueError, match="execution_receipt_identity_mismatch"):
            session.ingest_response(
                {
                    "sample_id": "s1",
                    "raw_response": _raw_judgment("s1"),
                    "execution": wrong,
                },
                sampling=sampling,
            )

    def test_wrong_config_digest_fails(self, tmp_path: Path):
        session, sampling, _, _ = _setup_lane(tmp_path)
        wrong = _receipt(session, "s1", reviewer_config_digest="b" * 64)
        with pytest.raises(ValueError, match="execution_receipt_identity_mismatch"):
            session.ingest_response(
                {
                    "sample_id": "s1",
                    "raw_response": _raw_judgment("s1"),
                    "execution": wrong,
                },
                sampling=sampling,
            )

    def test_response_without_receipt_fails(self, tmp_path: Path):
        session, sampling, _, _ = _setup_lane(tmp_path)
        with pytest.raises(ValueError, match="response_requires_execution_receipt"):
            session.ingest_response(
                {
                    "sample_id": "s1",
                    "raw_response": _raw_judgment("s1"),
                },
                sampling=sampling,
            )

    def test_never_requested_sample_fails(self, tmp_path: Path):
        """A frozen-manifest sample with NO emitted request cannot be ingested."""
        session, sampling, _, _ = _setup_lane(tmp_path)
        # FIX-R5-1: a generation that was never emitted cannot even produce a
        # receipt (the observation helper refuses to bind one), and a forged
        # payload naming it is rejected at ingest.
        from evals.calibration.ingestion import observe_execution

        with pytest.raises(ValueError, match="response_request_not_emitted"):
            observe_execution(
                session.lane_root,
                campaign_id="campaign",
                actual_reviewer_slot="model_a",
                actual_reviewer_family="claude-opus",
                actual_provider_model_identifier=session.reviewer.provider_model_identifier,
                actual_configuration_digest=session.reviewer.reviewer_config_digest,
                actual_prompt_digest=session.reviewer.prompt_digest,
                sample_id="s1",
                request_generation=99,
                executor_identity="synthetic-executor-206",
                executor_status="completed",
                identity_source="provider_metadata",
            )

    def test_other_generation_binding_fails_after_partial_ingest(self, tmp_path: Path):
        session, sampling, packet_path, manifest_path = _setup_lane(tmp_path)
        assert sampling is not None
        session.ingest_response(
            {
                "sample_id": "s1",
                "raw_response": _raw_judgment("s1"),
                "execution": _receipt(session, "s1"),
            },
            sampling=sampling,
        )
        # generation 2 exists for s2/s3 only; claiming s1 was answered in
        # generation 2 must fail (s1 has no generation-2 entry).
        session.emit_requests(packet_path, sampling=sampling, manifest_path=manifest_path)
        gen1 = _receipt(session, "s2")
        forged = dict(gen1)
        forged["request_generation"] = 2  # s1 was never emitted in generation 2
        with pytest.raises(
            ValueError,
            match=(
                "response_request_not_emitted|response_request_item_digest_mismatch"
                "|execution_receipt_not_derived_from_its_evidence"
            ),
        ):
            session.ingest_response(
                {
                    "sample_id": "s1",
                    "raw_response": _raw_judgment("s1"),
                    "execution": forged,
                },
                sampling=sampling,
            )

    def test_request_item_from_other_lane_fails(self, tmp_path: Path):
        session, sampling, _, _ = _setup_lane(tmp_path)
        # FIX-R5-2: a fabricated manifest alone can no longer register a
        # request — there is no batch FILE behind it, so the canonical byte
        # verifier rejects the whole manifest before any response can bind.
        other = _reviewer("model_b")
        other_manifest = {
            "lane_request_batch_schema": "engram-calibration-model-lane-request-batch-206-v2",
            "generation": 2,
            "reviewer_identity_digest": other.lane_identity_digest(),
            "reviewer_prompt_digest": session.reviewer.prompt_digest,
            "neutral_packet_sha256": session.neutral_packet_sha256,
            "accepted_record_digests": {},
            "pending_sample_ids": ["s2"],
            "request_items": {
                "s2": {"request_item_digest": "e" * 64, "case_index": 1},
            },
            "request_sha256": "0" * 64,
        }
        write_protected_file(
            session.lane_root / "lane-requests-000002.manifest.json",
            (json.dumps(other_manifest, sort_keys=True) + "\n").encode(),
        )
        # FIX-R5-2: a fabricated manifest alone can no longer register a
        # request — the registry is built from VERIFIED batch FILES, so a
        # manifest with no batch behind it is invisible and the response has
        # no emitted request to bind to.
        with pytest.raises(ValueError, match="response_request_not_emitted"):
            session.ingest_response(
                {
                    "sample_id": "s2",
                    "raw_response": _raw_judgment("s2"),
                    "execution": _receipt(session, "s2", request_generation=2),
                },
                sampling=sampling,
            )

    def test_request_item_from_other_neutral_packet_fails(self, tmp_path: Path):
        session, sampling, _, _ = _setup_lane(tmp_path)
        # FIX-R5-2: same shape — a manifest claiming a different neutral
        # packet has no batch bytes and fails the canonical verifier.
        other_packet_manifest = {
            "lane_request_batch_schema": "engram-calibration-model-lane-request-batch-206-v2",
            "generation": 2,
            "reviewer_identity_digest": session.reviewer.lane_identity_digest(),
            "reviewer_prompt_digest": session.reviewer.prompt_digest,
            "neutral_packet_sha256": "f" * 64,  # DIFFERENT packet
            "accepted_record_digests": {},
            "pending_sample_ids": ["s2"],
            "request_items": {
                "s2": {"request_item_digest": "e" * 64, "case_index": 1},
            },
            "request_sha256": "0" * 64,
        }
        write_protected_file(
            session.lane_root / "lane-requests-000002.manifest.json",
            (json.dumps(other_packet_manifest, sort_keys=True) + "\n").encode(),
        )
        # FIX-R5-2: same shape — a manifest claiming a different neutral
        # packet has no batch bytes, is invisible to the registry, and the
        # response has no emitted request to bind to.
        with pytest.raises(ValueError, match="response_request_not_emitted"):
            session.ingest_response(
                {
                    "sample_id": "s2",
                    "raw_response": _raw_judgment("s2"),
                    "execution": _receipt(session, "s2", request_generation=2),
                },
                sampling=sampling,
            )

    def test_provider_error_receipt_identifies_attempted_request(self, tmp_path: Path):
        session, sampling, _, _ = _setup_lane(tmp_path)
        record = session.ingest_response(
            {
                "sample_id": "s1",
                "error_code": "http-503",
                "execution": _receipt(session, "s1", executor_status="provider_error"),
            },
            sampling=sampling,
        )
        assert record.parse_status == "absent"
        assert record.request_item_digest is not None  # attempted request identified
        # FIX-R5-4: a completed execution REQUIRES raw response bytes — an
        # envelope with none fails deterministically (no caller-selected
        # outcome can rescue it).
        with pytest.raises(ValueError, match="completed_execution_requires_raw_response_bytes"):
            session.ingest_response(
                {
                    "sample_id": "s2",
                    "error_code": "http-503",
                    "execution": _receipt(session, "s2", executor_status="completed"),
                },
                sampling=sampling,
            )


# ---------------------------------------------------------------------------
# FIX-R4-2: raw bytes -> deterministic parser -> judgment
# ---------------------------------------------------------------------------


class TestFixR42DerivedJudgment:
    def test_parser_version_frozen_and_recorded(self):
        assert RESPONSE_PARSER_VERSION == "model-response-parser-206-v1"

    def test_deterministic_identical_parse(self):
        raw = _raw_judgment("s1").encode()
        first = parse_model_response(raw, expected_sample_id="s1")
        second = parse_model_response(raw, expected_sample_id="s1")
        assert first == second
        assert first.classification == "judged"
        assert first.judgment is not None
        assert first.judgment.fields["expected_kind"] == "fact"

    def test_raw_says_fact_wrapper_claims_decision_impossible(self, tmp_path: Path):
        """The wrapper's claimed judgment is not even a parameter anymore:
        the judgment is derived from the bytes, so a wrapper claiming
        expected_kind=decision against fact-bytes cannot be expressed."""
        session, sampling, _, _ = _setup_lane(tmp_path)
        # bytes say fact; there is NO way to supply decision — the response
        # shape has no judgment field, and derivation reads the bytes only.
        record = session.ingest_response(
            {
                "sample_id": "s1",
                "raw_response": _raw_judgment("s1", {**GOOD_CRITICAL, "expected_kind": "decision"}),
                "execution": _receipt(session, "s1"),
            },
            sampling=sampling,
        )
        assert record.judgment is not None
        assert record.judgment.fields["expected_kind"] == "decision"  # derived from bytes
        # FIX-R5-4: unparseable bytes on a COMPLETED execution become the
        # frozen failure state `malformed` — deterministically derived, not
        # caller-selected.
        malformed = session.ingest_response(
            {
                "sample_id": "s2",
                "raw_response": "not json at all",
                "execution": _receipt(session, "s2"),
            },
            sampling=sampling,
        )
        assert malformed.outcome_status == "malformed"
        assert malformed.parse_status == "malformed"

    def test_mutated_judgment_without_changing_bytes_fails_at_freeze(self, tmp_path: Path):
        """A record whose stored judgment disagrees with its own preserved
        bytes is caught by the canonical raw-evidence verifier."""
        session, sampling, _, _ = _setup_lane(tmp_path)
        record = session.ingest_response(
            {
                "sample_id": "s1",
                "raw_response": _raw_judgment("s1"),
                "execution": _receipt(session, "s1"),
            },
            sampling=sampling,
        )
        # forge: same identity, judgment mutated, bytes untouched
        forged = ModelReviewRecord.model_validate(
            {
                **record.model_dump(mode="json"),
                "judgment": {
                    "fields": {**record.judgment.fields, "expected_kind": "doctrine"},
                    "reviewer_confidence": record.judgment.reviewer_confidence,
                },
            }
        )
        from evals.calibration.raw_evidence import validate_record_raw_evidence

        with pytest.raises(ValueError, match="record_judgment_disagrees_with_bytes"):
            validate_record_raw_evidence(forged, lane_root=session.lane_root)

    def test_mutated_bytes_without_reparsing_fails(self, tmp_path: Path):
        session, sampling, _, _ = _setup_lane(tmp_path)
        record = session.ingest_response(
            {
                "sample_id": "s1",
                "raw_response": _raw_judgment("s1"),
                "execution": _receipt(session, "s1"),
            },
            sampling=sampling,
        )
        # mutate the preserved bytes on disk
        raw_path = session.lane_root / "raw" / "s1.resp"
        raw_path.write_bytes(
            _raw_judgment("s1", {**GOOD_CRITICAL, "expected_kind": "doctrine"}).encode()
        )
        from evals.calibration.raw_evidence import validate_record_raw_evidence

        with pytest.raises(ValueError, match="record_raw_evidence_digest_mismatch"):
            validate_record_raw_evidence(record, lane_root=session.lane_root)

    def test_parser_rejects_wrong_sample_bytes(self):
        with pytest.raises(ValueError, match="response_sample_id_mismatch"):
            parse_model_response(_raw_judgment("s2").encode(), expected_sample_id="s1")

    def test_parser_degrades_bad_vocab_to_malformed(self):
        raw = json.dumps(
            {
                "sample_id": "s1",
                "outcome": "judged",
                "judgment": {
                    "fields": {**GOOD_CRITICAL, "expected_kind": "not-a-kind"},
                    "reviewer_confidence": "medium",
                },
            }
        ).encode()
        parsed = parse_model_response(raw, expected_sample_id="s1")
        assert parsed.classification == "malformed"


# ---------------------------------------------------------------------------
# FIX-R4-3: frozen-frame verification at the ledger boundary
# ---------------------------------------------------------------------------


class TestFixR43FrozenFrameVerification:
    def _verified_campaign(self, tmp_path: Path):
        from tests.test_calibration_206_helpers import build_verified_ledger

        critical = {sid: dict(GOOD_CRITICAL) for sid in IDS}
        return build_verified_ledger(IDS, critical)

    def test_same_ids_mutated_source_type_fails(self, tmp_path: Path):
        from evals.calibration.freeze import verify_frozen_frame

        sampling = _sampling(IDS)
        rows = {row.sample_id: row for row in build_frame_rows(IDS)}
        mutated = dict(rows)
        mutated["s1"] = rows["s1"].model_copy(update={"source_type": "substituted"})
        with pytest.raises(ValueError, match="frame_digest_mismatch"):
            verify_frozen_frame(mutated, sampling)

    def test_same_ids_mutated_kind_fails(self):
        from evals.calibration.freeze import verify_frozen_frame

        sampling = _sampling(IDS)
        rows = {row.sample_id: row for row in build_frame_rows(IDS)}
        mutated = dict(rows)
        mutated["s2"] = rows["s2"].model_copy(update={"kind": "doctrine"})
        with pytest.raises(ValueError, match="frame_digest_mismatch"):
            verify_frozen_frame(mutated, sampling)

    def test_same_ids_mutated_review_status_fails(self):
        from evals.calibration.freeze import verify_frozen_frame

        sampling = _sampling(IDS)
        rows = {row.sample_id: row for row in build_frame_rows(IDS)}
        mutated = dict(rows)
        mutated["s3"] = rows["s3"].model_copy(update={"review_status": "rejected"})
        with pytest.raises(ValueError, match="frame_digest_mismatch"):
            verify_frozen_frame(mutated, sampling)

    def test_same_ids_mutated_age_bucket_fails(self):
        from evals.calibration.freeze import verify_frozen_frame

        sampling = _sampling(IDS)
        rows = {row.sample_id: row for row in build_frame_rows(IDS)}
        mutated = dict(rows)
        mutated["s1"] = rows["s1"].model_copy(update={"age_bucket": "ge_90d"})
        with pytest.raises(ValueError, match="frame_digest_mismatch"):
            verify_frozen_frame(mutated, sampling)

    def test_same_ids_mutated_content_hash_fails(self):
        from evals.calibration.freeze import verify_frozen_frame

        sampling = _sampling(IDS)
        rows = {row.sample_id: row for row in build_frame_rows(IDS)}
        mutated = dict(rows)
        mutated["s2"] = rows["s2"].model_copy(update={"content_hash": "0" * 64})
        with pytest.raises(ValueError, match="frame_digest_mismatch"):
            verify_frozen_frame(mutated, sampling)

    def test_frame_verified_inside_final_ledger_verifier(self):
        """verify_consensus_ledger runs the canonical frame check itself —
        proven by the R2/R3 suites; here we assert the import surface the
        verifier uses is the one canonical validator."""
        import inspect

        from evals.calibration import ledger as ledger_module

        source = inspect.getsource(ledger_module.verify_consensus_ledger)
        assert "verify_frozen_frame" in source


# ---------------------------------------------------------------------------
# FIX-R4-4: frozen split binding
# ---------------------------------------------------------------------------


class TestFixR44FrozenSplitBinding:
    def test_alternate_valid_split_over_same_membership_rejected(self, tmp_path: Path):
        """An internally valid re-partition of the same 402-style membership
        cannot substitute the frozen split at any downstream boundary."""
        from tests.test_calibration_206_helpers import build_verified_ledger

        critical = {sid: dict(GOOD_CRITICAL) for sid in IDS}
        frozen_split = build_split(IDS, dev=("s1", "s2"), holdout=("s3",)).model_copy(
            update={"sampling_manifest_digest": _sampling(IDS).manifest_digest()}
        )
        verified = build_verified_ledger(
            IDS, critical, split=frozen_split, expected_split_digest=frozen_split.split_digest()
        )
        # alternative internally-valid partition: swap memberships
        alternate = build_split(IDS, dev=("s3",), holdout=("s1", "s2")).model_copy(
            update={"sampling_manifest_digest": _sampling(IDS).manifest_digest()}
        )
        from evals.calibration.fit import verify_fitting_campaign_binding

        with pytest.raises(
            ValueError,
            match="fitting_split_digest_mismatch|fitting_split_disagrees_with_ledger_split_binding",
        ):
            verify_fitting_campaign_binding(
                verified_ledger=verified,
                sampling=_sampling(IDS),
                split=alternate,
                target_identity=build_identity(),
                expected_split_digest=frozen_split.split_digest(),
            )

    def test_cannot_self_bind_expected_digest(self):
        """The tautological helper is gone: deriving the expected digest from
        the split under test is no longer possible through the API."""
        import evals.calibration.fit as fit_module

        assert not hasattr(fit_module, "_frozen_split_digest_binding")

    def test_real_campaign_authority_constants_match_public_manifest(self):
        from evals.calibration import campaigns

        values = campaigns.public_manifest_values()
        assert values["split_manifest_digest"] == campaigns.EXPECTED_SPLIT_MANIFEST_DIGEST
        assert campaigns.EXPECTED_SPLIT_MANIFEST_DIGEST == (
            "a2a27ed4c0152bf2d9b6c318bbfcfd6e5e20944184a0cc9df18cb2b4e3fbb72b"
        )
        assert campaigns.EXPECTED_SAMPLING_MANIFEST_DIGEST == (
            "ed2e0c80bfe0c30c39d5ad5bc5656b007617320484efae14c87fa51027d66b3d"
        )
        assert campaigns.EXPECTED_TARGET_IDENTITY_DIGEST == (
            "57fc03918d5335c2925e2e6402fcadc4a29f5ef138fba6e6d839e9d8ec292ef9"
        )


# ---------------------------------------------------------------------------
# FIX-R4-5: protected assessment-evidence integration
# ---------------------------------------------------------------------------


class TestFixR45ProtectedAssessmentEvidence:
    def test_receipt_list_parameter_no_longer_exists(self):
        import inspect

        from evals.calibration.fit import (
            check_consensus_floors,
            consensus_reference_observations,
        )

        for func in (consensus_reference_observations, check_consensus_floors):
            signature = inspect.signature(func)
            assert "receipts" not in signature.parameters
            assert "assessment_evidence_path" in signature.parameters
            assert "expected_assessment_evidence_sha256" in signature.parameters

    def test_fabricated_self_consistent_receipt_list_cannot_feed_calibration(self):
        """A fabricated, perfectly self-consistent receipt list has NO path
        into consensus calibration: the parameter is gone entirely."""
        from evals.calibration.fit import consensus_reference_observations

        with pytest.raises(TypeError):
            consensus_reference_observations(
                receipts=[],  # type: ignore[call-arg]
                verified_ledger=None,  # type: ignore[arg-type]
            )

    def _evidence_context(self, tmp_path: Path):
        from tests.test_calibration_206_helpers import build_verified_ledger

        identity = build_identity()
        sampling = _sampling(IDS).model_copy(
            update={"target_identity_digest": identity.identity_digest()}
        )
        split = build_split(IDS, dev=("s1", "s2"), holdout=("s3",)).model_copy(
            update={"sampling_manifest_digest": sampling.manifest_digest()}
        )
        frame = build_frame_rows(IDS)
        contract_kwargs = dict(
            provider="openai",
            model="model",
            config_version="sha256:" + "9" * 64,
            calibration_version="dataset-v2",
        )
        from engram.assessment_schema import AssessmentContract

        contract = AssessmentContract(**contract_kwargs)
        path, sha = write_assessment_evidence(tmp_path, IDS, frame, identity, contract, sampling)
        verified = build_verified_ledger(
            IDS,
            {sid: dict(GOOD_CRITICAL) for sid in IDS},
            sampling=sampling,
            split=split,
            expected_split_digest=split.split_digest(),
        )
        return {
            "identity": identity,
            "sampling": sampling,
            "split": split,
            "frame": frame,
            "contract": contract,
            "path": path,
            "sha": sha,
            "verified": verified,
        }

    def test_valid_protected_evidence_passes(self, tmp_path: Path):
        from evals.calibration.fit import consensus_reference_observations

        ctx = self._evidence_context(tmp_path)
        observations = consensus_reference_observations(
            assessment_evidence_path=ctx["path"],
            expected_assessment_evidence_sha256=ctx["sha"],
            verified_ledger=ctx["verified"],
            sampling=ctx["sampling"],
            target_identity=ctx["identity"],
            contract=ctx["contract"],
            split=ctx["split"],
            frame=ctx["frame"],
            expected_split_digest=ctx["split"].split_digest(),
        )
        assert len(observations) == 9  # 3 samples x 3 dimensions

    def test_wrong_artifact_sha_fails(self, tmp_path: Path):
        from evals.calibration.fit import consensus_reference_observations

        ctx = self._evidence_context(tmp_path)
        with pytest.raises(ValueError, match="assessment_evidence_digest_mismatch"):
            consensus_reference_observations(
                assessment_evidence_path=ctx["path"],
                expected_assessment_evidence_sha256="0" * 64,
                verified_ledger=ctx["verified"],
                sampling=ctx["sampling"],
                target_identity=ctx["identity"],
                contract=ctx["contract"],
                split=ctx["split"],
                frame=ctx["frame"],
                expected_split_digest=ctx["split"].split_digest(),
            )

    def test_altered_raw_provider_score_fails(self, tmp_path: Path):
        from evals.calibration.fit import consensus_reference_observations

        ctx = self._evidence_context(tmp_path)
        envelope = json.loads(ctx["path"].read_text())
        envelope["executions"][0]["assessment"]["taxonomy"]["raw_value"] = 0.99
        payload = json.dumps(envelope, sort_keys=True, separators=(",", ":")).encode()
        ctx["path"].write_bytes(payload)
        sha = hashlib.sha256(payload).hexdigest()
        with pytest.raises(ValueError, match="assessment_execution_receipt_digest_mismatch"):
            consensus_reference_observations(
                assessment_evidence_path=ctx["path"],
                expected_assessment_evidence_sha256=sha,
                verified_ledger=ctx["verified"],
                sampling=ctx["sampling"],
                target_identity=ctx["identity"],
                contract=ctx["contract"],
                split=ctx["split"],
                frame=ctx["frame"],
                expected_split_digest=ctx["split"].split_digest(),
            )

    def test_different_provider_model_fails(self, tmp_path: Path):
        from evals.calibration.fit import consensus_reference_observations

        ctx = self._evidence_context(tmp_path)
        from engram.assessment_schema import AssessmentContract

        other_contract = AssessmentContract(
            provider="anthropic",
            model="other-model",
            config_version="sha256:" + "9" * 64,
            calibration_version="dataset-v2",
        )
        with pytest.raises(ValueError, match="assessment_contract_target_mismatch"):
            consensus_reference_observations(
                assessment_evidence_path=ctx["path"],
                expected_assessment_evidence_sha256=ctx["sha"],
                verified_ledger=ctx["verified"],
                sampling=ctx["sampling"],
                target_identity=ctx["identity"],
                contract=other_contract,
                split=ctx["split"],
                frame=ctx["frame"],
                expected_split_digest=ctx["split"].split_digest(),
            )

    def test_missing_execution_fails(self, tmp_path: Path):
        from evals.calibration.fit import consensus_reference_observations

        ctx = self._evidence_context(tmp_path)
        envelope = json.loads(ctx["path"].read_text())
        envelope["executions"] = envelope["executions"][:-1]
        payload = json.dumps(envelope, sort_keys=True, separators=(",", ":")).encode()
        ctx["path"].write_bytes(payload)
        sha = hashlib.sha256(payload).hexdigest()
        with pytest.raises(ValueError, match="assessment_evidence_membership_mismatch"):
            consensus_reference_observations(
                assessment_evidence_path=ctx["path"],
                expected_assessment_evidence_sha256=sha,
                verified_ledger=ctx["verified"],
                sampling=ctx["sampling"],
                target_identity=ctx["identity"],
                contract=ctx["contract"],
                split=ctx["split"],
                frame=ctx["frame"],
                expected_split_digest=ctx["split"].split_digest(),
            )

    def test_duplicate_execution_fails(self, tmp_path: Path):
        from evals.calibration.fit import consensus_reference_observations

        ctx = self._evidence_context(tmp_path)
        envelope = json.loads(ctx["path"].read_text())
        envelope["executions"] = [envelope["executions"][0]] * len(IDS)
        payload = json.dumps(envelope, sort_keys=True, separators=(",", ":")).encode()
        ctx["path"].write_bytes(payload)
        sha = hashlib.sha256(payload).hexdigest()
        with pytest.raises(ValueError, match="assessment_evidence_membership_mismatch"):
            consensus_reference_observations(
                assessment_evidence_path=ctx["path"],
                expected_assessment_evidence_sha256=sha,
                verified_ledger=ctx["verified"],
                sampling=ctx["sampling"],
                target_identity=ctx["identity"],
                contract=ctx["contract"],
                split=ctx["split"],
                frame=ctx["frame"],
                expected_split_digest=ctx["split"].split_digest(),
            )

    def test_extra_execution_fails(self, tmp_path: Path):
        from evals.calibration.fit import consensus_reference_observations

        ctx = self._evidence_context(tmp_path)
        envelope = json.loads(ctx["path"].read_text())
        extra = json.loads(json.dumps(envelope["executions"][0]))
        extra["execution_id"] = "execution-extra"
        envelope["executions"].append(extra)
        payload = json.dumps(envelope, sort_keys=True, separators=(",", ":")).encode()
        ctx["path"].write_bytes(payload)
        sha = hashlib.sha256(payload).hexdigest()
        with pytest.raises(ValueError, match="assessment_evidence_membership_mismatch"):
            consensus_reference_observations(
                assessment_evidence_path=ctx["path"],
                expected_assessment_evidence_sha256=sha,
                verified_ledger=ctx["verified"],
                sampling=ctx["sampling"],
                target_identity=ctx["identity"],
                contract=ctx["contract"],
                split=ctx["split"],
                frame=ctx["frame"],
                expected_split_digest=ctx["split"].split_digest(),
            )

    def test_altered_frame_fails_stage_a(self, tmp_path: Path):
        from evals.calibration.fit import consensus_reference_observations

        ctx = self._evidence_context(tmp_path)
        mutated_frame = list(ctx["frame"])
        mutated_frame[0] = mutated_frame[0].model_copy(update={"kind": "doctrine"})
        with pytest.raises(ValueError, match="assessment_frame_digest_mismatch"):
            consensus_reference_observations(
                assessment_evidence_path=ctx["path"],
                expected_assessment_evidence_sha256=ctx["sha"],
                verified_ledger=ctx["verified"],
                sampling=ctx["sampling"],
                target_identity=ctx["identity"],
                contract=ctx["contract"],
                split=ctx["split"],
                frame=mutated_frame,
                expected_split_digest=ctx["split"].split_digest(),
            )


# ---------------------------------------------------------------------------
# Serving invariants (preserved)
# ---------------------------------------------------------------------------


class TestServingInvariantsPreserved:
    def test_certified_serving_profiles_unchanged(self):
        from engram.recall_profiles import CERTIFIED_SERVING_PROFILES

        assert {"legacy"} == CERTIFIED_SERVING_PROFILES

    def test_assessment_selection_disabled(self):
        # the public manifest records the frozen serving posture
        # (runtime enforcement lives in engram.config.settings)
        import json as _json

        from evals.calibration import campaigns

        payload = _json.loads(
            (Path(campaigns.__file__).parent / "202" / "campaign-manifest-public.json").read_text()
        )
        assert payload["serving_invariants"]["assessment_selection_enabled"] is False
        assert payload["serving_invariants"]["certified_serving_profiles"] == ["legacy"]

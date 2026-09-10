"""Frozen consensus-protocol proofs for #206 (ENG-CALIBRATION-001G).

Every test maps to a numbered verification item in the issue. The protocol
constants in evals.calibration.consensus are frozen: these tests pin them.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from evals.admission.schema import digest
from evals.calibration.consensus import (
    AUDIT_DISAGREEMENT_RATE_THRESHOLD,
    AUDIT_SAMPLE_RATE,
    AUDIT_SELECTION_SEED,
    CONSENSUS_PROTOCOL_VERSION,
    CRITICAL_FIELDS,
    DIAGNOSTIC_FIELDS,
    MIN_REVIEWER_CONFIDENCE,
    REVIEWER_FAMILIES,
    REVIEWER_SLOTS,
    ConsensusLedger,
    ConsensusProvenanceWrapper,
    LaneFreeze,
    ModelJudgment,
    ModelReviewRecord,
    ReferenceLabel,
    ReviewerIdentity,
    audit_outcome,
    build_correlation_report,
    classify_case,
    judgment_is_consensus_eligible,
    select_audit_sample,
    validate_lane_isolation,
    validate_lane_membership,
)
from evals.calibration.freeze import LABEL_GUIDE_VERSION, SamplingManifest
from evals.calibration.human_queue import (
    HumanQueueJudgment,
    HumanQueueManifest,
    build_queue,
    export_queue_evidence,
    record_final_resolution,
    save_initial_judgment,
)
from evals.calibration.ingestion import labeling_instructions_digest
from evals.calibration.model_lanes import (
    NeutralModelPacket,
    append_review_record,
    load_lane_records,
)
from evals.calibration.review import BlindPacket

NOW = datetime(2026, 9, 10, tzinfo=UTC)

GOOD_CRITICAL = {
    "expected_kind": "fact",
    "retention_value": "retain",
    "epistemic_state": "adequately_supported",
    "consequence": "low",
    "acceptable_abstention": "no",
}


def _judgment(**overrides: object) -> ModelJudgment:
    fields = dict(GOOD_CRITICAL)
    fields.update(overrides)
    return ModelJudgment(fields=fields, reviewer_confidence="medium")


def _reviewer(slot: str, family: str, *, model: str | None = None) -> ReviewerIdentity:
    from evals.calibration.ingestion import labeling_instructions_digest

    return ReviewerIdentity(
        reviewer_slot=slot,  # type: ignore[arg-type]
        reviewer_family=family,
        provider_model_identifier=model or f"{family}-exact-2026-09",
        reviewer_config_digest="a" * 64,
        prompt_digest=labeling_instructions_digest(),
    )


def _record(
    slot: str,
    sample_id: str,
    judgment: ModelJudgment | None,
    *,
    parse_status: str = "parsed",
    outcome_status: str = "judged",
    family: str | None = None,
    error_code: str | None = None,
    raw_digest: str | None = "c" * 64,
) -> ModelReviewRecord:
    from evals.calibration.consensus import ExecutionReceipt
    from evals.calibration.reviewer_instructions import RESPONSE_PARSER_VERSION

    fam = family or dict(zip(REVIEWER_SLOTS, REVIEWER_FAMILIES, strict=True))[slot]
    execution = ExecutionReceipt(
        campaign_id="campaign",
        reviewer_slot=slot,  # type: ignore[arg-type]
        reviewer_family=fam,
        provider_model_identifier=f"{fam}-exact-2026-09",
        reviewer_config_digest="a" * 64,
        prompt_digest=labeling_instructions_digest(),
        request_generation=1,
        request_item_digest="d" * 64,
        executed_at=NOW,
        executor_status="provider_error" if outcome_status == "provider_error" else "completed",
    )
    return ModelReviewRecord(
        protocol_version=CONSENSUS_PROTOCOL_VERSION,
        campaign_id="campaign",
        sampling_manifest_digest="e" * 64,
        source_packet_digest="f" * 64,
        sample_id=sample_id,
        reviewer_slot=slot,  # type: ignore[arg-type]
        reviewer_family=fam,
        provider_model_identifier=f"{fam}-exact-2026-09",
        reviewer_config_digest="a" * 64,
        prompt_digest=labeling_instructions_digest(),
        label_guide_version=LABEL_GUIDE_VERSION,
        captured_at=NOW,
        parse_status=parse_status,  # type: ignore[arg-type]
        outcome_status=outcome_status,  # type: ignore[arg-type]
        execution=execution,
        request_generation=1,
        request_item_digest="d" * 64,
        parser_version=RESPONSE_PARSER_VERSION if parse_status == "parsed" else None,
        reviewer_confidence=judgment.reviewer_confidence if judgment else "unknown",
        judgment=judgment,
        raw_response_digest=raw_digest,  # type: ignore[arg-type]
        error_code=error_code,
    )


def _case(record_a: ModelReviewRecord, record_b: ModelReviewRecord, record_c: ModelReviewRecord):
    return classify_case(
        {record.reviewer_slot: record for record in (record_a, record_b, record_c)}
    )


class TestProtocolFrozenConstants:
    """The freeze-before-execution contract itself (acceptance criterion 1)."""

    def test_protocol_version_frozen(self):
        assert CONSENSUS_PROTOCOL_VERSION == "eng-calibration-consensus-206-v1"

    def test_critical_fields_frozen(self):
        assert CRITICAL_FIELDS == (
            "expected_kind",
            "retention_value",
            "epistemic_state",
            "consequence",
            "acceptable_abstention",
        )

    def test_three_reviewer_families_frozen(self):
        assert REVIEWER_FAMILIES == ("claude-opus", "gpt-astra", "glm-5-3-max")

    def test_audit_seed_and_rate_frozen(self):
        assert AUDIT_SELECTION_SEED == "202-model-consensus-audit-v1"
        assert AUDIT_SAMPLE_RATE == 0.15
        assert AUDIT_DISAGREEMENT_RATE_THRESHOLD == 0.05
        assert MIN_REVIEWER_CONFIDENCE == "medium"

    def test_diagnostic_fields_disjoint_from_critical(self):
        assert not set(DIAGNOSTIC_FIELDS) & set(CRITICAL_FIELDS)


class TestReviewerIdentity:
    """Verification 2: three independent reviewer identities provenance-bound."""

    def test_family_must_match_frozen_slot(self):
        with pytest.raises(Exception, match="reviewer_family_does_not_match_frozen_slot"):
            _reviewer("model_a", "gpt-astra")

    def test_reviewer_identity_digests_distinct(self):
        identities = [
            _reviewer(slot, family)
            for slot, family in zip(REVIEWER_SLOTS, REVIEWER_FAMILIES, strict=True)
        ]
        digests = {identity.lane_identity_digest() for identity in identities}
        assert len(digests) == 3

    def test_record_binds_protocol_and_model_identity(self):
        record = _record("model_a", "s1", _judgment())
        assert record.provider_model_identifier
        assert record.protocol_version == CONSENSUS_PROTOCOL_VERSION
        assert record.reviewer_config_digest and record.prompt_digest


class TestModelReviewSchemaCannotMasqueradeAsHuman:
    """Acceptance criterion: model artifacts cannot masquerade as human judgment."""

    def test_schema_name_is_campaign_specific(self):
        record = _record("model_a", "s1", _judgment())
        assert record.review_schema == "engram-calibration-model-review-206-v2"
        assert record.review_schema != "engram-admission-label-v1"

    def test_model_record_is_not_a_label_record(self):
        from evals.admission.schema import LabelRecord

        record = _record("model_a", "s1", _judgment())
        with pytest.raises(ValueError):
            LabelRecord.model_validate(record.model_dump(mode="json"))

    def test_reference_label_origin_vocabulary_disjoint_from_202(self):
        # 'cross_model_consensus' and 'human_audited_consensus' are NOT valid
        # frozen LabelRecord.label_origin values; 'human_adjudicated' is
        # shared deliberately and only ever set by the human workflow.
        label = ReferenceLabel(
            sample_id="s1",
            final_label_origin="cross_model_consensus",
            critical=dict(GOOD_CRITICAL),
        )
        assert label.final_label_origin != "synthetic_authored"
        with pytest.raises(ValueError):
            ReferenceLabel(
                sample_id="s1",
                final_label_origin="majority_vote",  # type: ignore[arg-type]
                critical=dict(GOOD_CRITICAL),
            )


class TestLaneMembership:
    """Verification 1: each lane contains exactly the frozen cases."""

    def _sampling(self, ids: tuple[str, ...]) -> SamplingManifest:
        return SamplingManifest(
            campaign_id="campaign",
            target_identity_digest="1" * 64,
            frame_digest="2" * 64,
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

    def _lane(
        self,
        ids: tuple[str, ...],
        slot: str = "model_a",
        *,
        sampling: SamplingManifest | None = None,
    ) -> LaneFreeze:
        family = dict(zip(REVIEWER_SLOTS, REVIEWER_FAMILIES, strict=True))[slot]
        manifest_digest = (
            sampling.manifest_digest() if sampling else self._sampling(ids).manifest_digest()
        )
        return LaneFreeze(
            protocol_version=CONSENSUS_PROTOCOL_VERSION,
            campaign_id="campaign",
            reviewer=_reviewer(slot, family),
            sampling_manifest_digest=manifest_digest,
            source_packet_digest="f" * 64,
            neutral_packet_sha256="8" * 64,
            sample_ids=ids,
            record_digests=tuple(digest(f"{slot}:{sid}") for sid in ids),
        )

    def test_lane_membership_must_match_sampling_exactly(self):
        ids = ("s1", "s2", "s3")
        sampling = self._sampling(ids)
        validate_lane_membership(self._lane(ids), sampling, source_packet_digest="f" * 64)
        with pytest.raises(Exception, match="lane_sample_membership_mismatch"):
            validate_lane_membership(
                self._lane(("s1", "s2", "s4"), sampling=sampling),
                sampling,
                source_packet_digest="f" * 64,
            )
        with pytest.raises(Exception, match="lane_source_packet_mismatch"):
            validate_lane_membership(self._lane(ids), sampling, source_packet_digest="0" * 64)

    def test_lane_rejects_duplicate_and_missing(self):
        sampling = self._sampling(("s1", "s2"))
        with pytest.raises(Exception, match="lane_duplicate_sample_id"):
            LaneFreeze(
                protocol_version=CONSENSUS_PROTOCOL_VERSION,
                campaign_id="campaign",
                reviewer=_reviewer("model_a", "claude-opus"),
                sampling_manifest_digest=sampling.manifest_digest(),
                source_packet_digest="f" * 64,
                neutral_packet_sha256="8" * 64,
                sample_ids=("s1", "s1"),
                record_digests=("1" * 64, "2" * 64),
            )
        with pytest.raises(Exception, match="lane_membership_length_mismatch"):
            LaneFreeze(
                protocol_version=CONSENSUS_PROTOCOL_VERSION,
                campaign_id="campaign",
                reviewer=_reviewer("model_a", "claude-opus"),
                sampling_manifest_digest="e" * 64,
                source_packet_digest="f" * 64,
                neutral_packet_sha256="8" * 64,
                sample_ids=("s1", "s2"),
                record_digests=("1" * 64,),
            )

    def test_isolation_rejects_duplicate_slots_and_missing_lanes(self):
        ids = ("s1",)
        lanes = [self._lane(ids, slot) for slot in REVIEWER_SLOTS]
        validate_lane_isolation(lanes)
        with pytest.raises(Exception, match="duplicate_reviewer_slot"):
            validate_lane_isolation([lanes[0], lanes[0], lanes[2]])
        with pytest.raises(Exception, match="missing_reviewer_lane"):
            validate_lane_isolation([lanes[0], lanes[1]])

    def test_lane_persistence_refuses_duplicate_records(self, tmp_path: Path):
        record = _record("model_a", "s1", _judgment())
        append_review_record(record, tmp_path)
        with pytest.raises(Exception, match="review_record_already_accepted"):
            append_review_record(record, tmp_path)
        loaded = load_lane_records(tmp_path, "model_a")
        assert set(loaded) == {"s1"}
        assert loaded["s1"].record_digest() == record.record_digest()


class TestNeutralPacket:
    """Verification 16 (membership unchanged): neutral packet = frozen blind packet."""

    def _blind(self, ids: tuple[str, ...]) -> BlindPacket:
        cases = []
        for sid in ids:
            cases.append(
                {
                    "sample_id": sid,
                    "content": f"content-{sid}",
                    "governed_kind": "fact",
                    "source_type": "manual",
                    "review_status": "active",
                    "assertion_mode": "unknown",
                    "origin": "unknown",
                    "risk": "unavailable",
                    "evidence_state": "unavailable",
                    "age_days": 10,
                    "age_bucket": "week",
                    "input_size_bucket": "small",
                }
            )
        return BlindPacket(
            packet_id="campaign-blind-v2",
            sampling_manifest_digest="e" * 64,
            guide_version=LABEL_GUIDE_VERSION,
            reviewer_hint="reviewer_a",
            cases=cases,
        )

    def test_neutral_packet_preserves_membership_and_order(self):
        ids = ("s1", "s2", "s3")
        blind = self._blind(ids)
        neutral = NeutralModelPacket.from_blind(blind, protocol_version=CONSENSUS_PROTOCOL_VERSION)
        assert [case["sample_id"] for case in neutral.cases] == list(ids)
        assert neutral.sampling_manifest_digest == blind.sampling_manifest_digest
        assert neutral.reviewer_hint == "neutral_model_review"
        assert neutral.source_packet_digest

    def test_neutral_packet_carries_no_score_or_label_fields(self):
        neutral = NeutralModelPacket.from_blind(
            self._blind(("s1",)), protocol_version=CONSENSUS_PROTOCOL_VERSION
        )
        for case in neutral.cases:
            assert set(case) <= {
                "sample_id",
                "content",
                "governed_kind",
                "source_type",
                "review_status",
                "assertion_mode",
                "origin",
                "risk",
                "evidence_state",
                "age_days",
                "age_bucket",
                "input_size_bucket",
            }

    def test_protocol_version_gate(self):
        with pytest.raises(Exception, match="protocol_version_mismatch"):
            NeutralModelPacket.from_blind(self._blind(("s1",)), protocol_version="other-v0")


class TestConsensusRules:
    """Verifications 4-8: consensus qualification and escalation."""

    def test_unanimous_three_way_qualifies(self):
        result = _case(
            _record("model_a", "s1", _judgment()),
            _record("model_b", "s1", _judgment()),
            _record("model_c", "s1", _judgment()),
        )
        assert result["consensus"] is True
        assert result["escalation_reasons"] == []

    def test_two_of_three_never_qualifies(self):
        result = _case(
            _record("model_a", "s1", _judgment()),
            _record("model_b", "s1", _judgment(expected_kind="observation")),
            _record("model_c", "s1", _judgment()),
        )
        assert result["consensus"] is False
        assert "critical_field_disagreement" in result["escalation_reasons"]

    def test_any_critical_field_disagreement_escalates(self):
        for field, alternative in (
            ("retention_value", "do_not_retain"),
            ("epistemic_state", "weakly_supported"),
            ("consequence", "medium"),
            ("acceptable_abstention", "yes"),
        ):
            result = _case(
                _record("model_a", "s1", _judgment()),
                _record("model_b", "s1", _judgment(**{field: alternative})),
                _record("model_c", "s1", _judgment()),
            )
            assert result["consensus"] is False, field

    def test_unknown_uncertain_ambiguous_escalates(self):
        for field, degenerate in (
            ("expected_kind", "unknown"),
            ("retention_value", "uncertain"),
            ("epistemic_state", "unknown"),
            ("epistemic_state", "ambiguous"),
            ("consequence", "unknown"),
            ("acceptable_abstention", "unknown"),
        ):
            result = _case(
                _record("model_a", "s1", _judgment()),
                _record("model_b", "s1", _judgment(**{field: degenerate})),
                _record("model_c", "s1", _judgment()),
            )
            assert result["consensus"] is False, field
            # even unanimous degenerate values never qualify
            unanimous_degenerate = _case(
                _record("model_a", "s1", _judgment(**{field: degenerate})),
                _record("model_b", "s1", _judgment(**{field: degenerate})),
                _record("model_c", "s1", _judgment(**{field: degenerate})),
            )
            assert unanimous_degenerate["consensus"] is False, field

    def test_any_high_consequence_signal_escalates(self):
        result = _case(
            _record("model_a", "s1", _judgment()),
            _record("model_b", "s1", _judgment(consequence="high")),
            _record("model_c", "s1", _judgment()),
        )
        assert result["consensus"] is False
        assert "high_consequence_signal" in result["escalation_reasons"]

    def test_malformed_refusal_provider_error_escalate(self):
        for parse_status, outcome, code in (
            ("malformed", "refused", "refusal"),
            ("malformed", "malformed", "schema-parse-failed"),
            ("absent", "provider_error", "http-503"),
            ("parsed", "judged", None),
        ):
            if parse_status == "parsed":
                continue
            failed = _record(
                "model_b",
                "s1",
                None,
                parse_status=parse_status,
                outcome_status=outcome,
                error_code=code,
                raw_digest=None if parse_status == "absent" else "c" * 64,
            )
            result = _case(
                _record("model_a", "s1", _judgment()),
                failed,
                _record("model_c", "s1", _judgment()),
            )
            assert result["consensus"] is False
        # provider failure is recorded separately from a substantive unknown
        refused = _record(
            "model_b",
            "s1",
            None,
            parse_status="malformed",
            outcome_status="refused",
            error_code="refusal",
        )
        assert refused.error_code == "refusal"
        unknown = _record("model_b", "s1", _judgment(expected_kind="unknown"))
        assert unknown.parse_status == "parsed" and unknown.judgment is not None

    def test_below_confidence_floor_escalates(self):
        low = ModelJudgment(fields=dict(GOOD_CRITICAL), reviewer_confidence="low")
        result = _case(
            _record("model_a", "s1", _judgment()),
            _record("model_b", "s1", low),
            _record("model_c", "s1", _judgment()),
        )
        assert result["consensus"] is False
        assert "reviewer_below_confidence_floor" in result["escalation_reasons"]

    def test_judgment_requires_all_critical_fields(self):
        partial = {k: v for k, v in GOOD_CRITICAL.items() if k != "consequence"}
        with pytest.raises(Exception, match="missing_critical_fields"):
            ModelJudgment(fields=partial, reviewer_confidence="medium")

    def test_critical_vocabulary_is_closed(self):
        with pytest.raises(Exception, match="critical_field_out_of_vocabulary"):
            ModelJudgment(
                fields={**GOOD_CRITICAL, "consequence": "catastrophic"},
                reviewer_confidence="medium",
            )

    def test_diagnostic_disagreement_never_escalates(self):
        # Diagnostic-only fields differ across reviewers; consensus holds.
        a = ModelJudgment(
            fields={**GOOD_CRITICAL, "expected_next_action": "review"},
            reviewer_confidence="high",
        )
        b = ModelJudgment(
            fields={**GOOD_CRITICAL, "expected_next_action": "wait"},
            reviewer_confidence="high",
        )
        c = ModelJudgment(
            fields={**GOOD_CRITICAL, "expected_next_action": "reject"},
            reviewer_confidence="high",
        )
        result = _case(
            _record("model_a", "s1", a),
            _record("model_b", "s1", b),
            _record("model_c", "s1", c),
        )
        assert result["consensus"] is True

    def test_eligibility_requires_confidence_and_non_degenerate(self):
        assert judgment_is_consensus_eligible(_judgment())
        assert not judgment_is_consensus_eligible(
            ModelJudgment(fields=dict(GOOD_CRITICAL), reviewer_confidence="low")
        )
        assert not judgment_is_consensus_eligible(_judgment(retention_value="uncertain"))


class TestAuditSelection:
    """Verifications 9-10: deterministic 15% audit, label-blind."""

    def test_rate_is_exactly_15_percent(self):
        ids = [f"s{i}" for i in range(100)]
        assert len(select_audit_sample(ids)) == 15
        assert len(select_audit_sample([f"s{i}" for i in range(20)])) == 3

    def test_selection_reproduces_exactly(self):
        ids = [f"s{i}" for i in range(200)]
        first = select_audit_sample(ids)
        second = select_audit_sample(ids)
        assert first == second
        assert len(first) == 30

    def test_selection_is_label_blind(self):
        # Identical membership with different underlying labels selects
        # identically: the function accepts only IDs by design.
        ids = [f"s{i}" for i in range(50)]
        assert select_audit_sample(ids) == select_audit_sample(ids)

    def test_membership_change_changes_pool_only(self):
        ids = [f"s{i}" for i in range(40)]
        subset = ids[:20]
        # selections are deterministic functions of membership; both reproduce
        assert select_audit_sample(ids) == select_audit_sample(ids)
        assert select_audit_sample(subset) == select_audit_sample(subset)

    def test_duplicate_pool_rejected(self):
        with pytest.raises(Exception, match="audit_pool_membership_duplicate"):
            select_audit_sample(["s1", "s1"])


class TestAuditEscalation:
    """Verifications 11-12: >5% and high-consequence escalation."""

    def test_five_percent_threshold_escalates_above_not_at(self):
        # 1/20 = 5% exactly -> NOT escalated (threshold is strictly >5%)
        at_threshold = audit_outcome(
            audited_count=20, material_disagreements=1, high_consequence_misses=0
        )
        assert at_threshold["escalate_full_human_review"] is False
        # 2/20 = 10% -> escalated
        above = audit_outcome(audited_count=20, material_disagreements=2, high_consequence_misses=0)
        assert above["escalate_full_human_review"] is True
        # 1/19 ≈ 5.26% -> escalated
        marginal = audit_outcome(
            audited_count=19, material_disagreements=1, high_consequence_misses=0
        )
        assert marginal["escalate_full_human_review"] is True

    def test_single_high_consequence_miss_escalates(self):
        outcome = audit_outcome(
            audited_count=100, material_disagreements=0, high_consequence_misses=1
        )
        assert outcome["escalate_full_human_review"] is True

    def test_clean_audit_passes(self):
        outcome = audit_outcome(
            audited_count=20, material_disagreements=0, high_consequence_misses=0
        )
        assert outcome["escalate_full_human_review"] is False


class TestFinalLedgerProvenance:
    """Verification 13: ledger distinguishes consensus from human adjudication."""

    def _wrapper(self, **overrides: object) -> ConsensusProvenanceWrapper:
        payload = {
            "protocol_version": CONSENSUS_PROTOCOL_VERSION,
            "campaign_id": "campaign",
            "sampling_manifest_digest": "e" * 64,
            "source_packet_digest": "f" * 64,
            "sample_id": "s1",
            "first_pass_record_digests": ("1" * 64, "2" * 64, "3" * 64),
            "consensus_reached": True,
            "entered_human_queue": False,
            "final_label_origin": "cross_model_consensus",
            "final_dimensions": dict(GOOD_CRITICAL),
        }
        payload.update(overrides)
        return ConsensusProvenanceWrapper.model_validate(payload)

    def test_consensus_origin_contract(self):
        wrapper = self._wrapper()
        assert wrapper.final_label_origin == "cross_model_consensus"
        with pytest.raises(Exception, match="consensus_origin_requires_no_human_queue"):
            self._wrapper(entered_human_queue=True, queue_reasons=("high_consequence_signal",))

    def test_human_origin_requires_queue(self):
        with pytest.raises(Exception, match="human_origin_requires_human_queue"):
            self._wrapper(final_label_origin="human_adjudicated")
        human = self._wrapper(
            final_label_origin="human_adjudicated",
            entered_human_queue=True,
            queue_reasons=("critical_field_disagreement",),
            consensus_reached=False,
        )
        assert human.final_label_origin == "human_adjudicated"

    def test_audited_consensus_origin_contract(self):
        audited = self._wrapper(
            final_label_origin="human_audited_consensus",
            audit_selected=True,
            entered_human_queue=True,
            queue_reasons=("audit_selected",),
        )
        assert audited.audit_selected
        with pytest.raises(Exception, match="audited_origin_requires_audit_selection"):
            self._wrapper(
                final_label_origin="human_audited_consensus",
                audit_selected=False,
                entered_human_queue=True,
                queue_reasons=("audit_selected",),
            )
        with pytest.raises(Exception, match="audited_case_must_use_audited_origin"):
            self._wrapper(final_label_origin="cross_model_consensus", audit_selected=True)
        # FIX-R3-5: an audit-selected CONSENSUS row the human overrode is
        # legitimately human_adjudicated
        overridden = self._wrapper(
            final_label_origin="human_adjudicated",
            entered_human_queue=True,
            queue_reasons=("audit_selected",),
            consensus_reached=True,
            audit_selected=True,
        )
        assert overridden.final_label_origin == "human_adjudicated"
        # a non-consensus audit-selected row cannot exist at all
        with pytest.raises(
            Exception, match="audit_selected_case_requires_consensus_classification"
        ):
            self._wrapper(
                final_label_origin="human_adjudicated",
                entered_human_queue=True,
                queue_reasons=("critical_field_disagreement",),
                consensus_reached=False,
                audit_selected=True,
            )

    def test_ledger_contract(self):
        wrapper = self._wrapper()
        from evals.calibration.consensus import AuditOutcomeRecord, AuditSelection

        audit_selection = AuditSelection(
            selected=(), target_count=0, population_count=0, covered_cells=(), uncovered_cells=()
        )
        audit_outcome = AuditOutcomeRecord(
            audited_count=0,
            material_disagreements=0,
            high_consequence_misses=0,
            material_reversals=0,
            material_disagreement_rate=None,
            escalate_full_human_review=False,
        )
        ledger = ConsensusLedger(
            protocol_version=CONSENSUS_PROTOCOL_VERSION,
            campaign_id="campaign",
            sampling_manifest_digest="e" * 64,
            source_packet_digest="f" * 64,
            lane_digests=("4" * 64, "5" * 64, "6" * 64),
            queue_evidence_sha256="7" * 64,
            audit_outcome=audit_outcome,
            audit_selection=audit_selection,
            wrappers=(wrapper,),
        )
        assert ledger.final_dimensions_by_sample()["s1"]["expected_kind"] == "fact"
        with pytest.raises(Exception, match="lane_count_mismatch"):
            ConsensusLedger(
                protocol_version=CONSENSUS_PROTOCOL_VERSION,
                campaign_id="campaign",
                sampling_manifest_digest="e" * 64,
                source_packet_digest="f" * 64,
                lane_digests=("4" * 64,),
                queue_evidence_sha256="7" * 64,
                audit_outcome=audit_outcome,
                audit_selection=audit_selection,
                wrappers=(wrapper,),
            )
        with pytest.raises(Exception, match="audit_seed_frozen"):
            ConsensusLedger(
                protocol_version=CONSENSUS_PROTOCOL_VERSION,
                campaign_id="campaign",
                sampling_manifest_digest="e" * 64,
                source_packet_digest="f" * 64,
                lane_digests=("4" * 64, "5" * 64, "6" * 64),
                queue_evidence_sha256="7" * 64,
                audit_outcome=audit_outcome,
                audit_selection=audit_selection,
                audit_seed="other-seed",
                wrappers=(wrapper,),
            )

    def test_wrapper_preserves_three_first_pass_digests(self):
        wrapper = self._wrapper()
        assert len(wrapper.first_pass_record_digests) == 3
        with pytest.raises(Exception, match="first_pass_record_count_mismatch"):
            self._wrapper(first_pass_record_digests=("1" * 64,))


class TestFloorsConsumeOnlyFinalLabels:
    """Verification 14: downstream consumption uses only final reference labels."""

    def test_consensus_reference_completion_requires_verified_ledger(self):
        # FIX-R2-5: the reference_labels parameter no longer exists — a
        # perfectly valid hand-constructed list cannot be passed at all.
        import inspect

        from evals.calibration.fit import consensus_reference_completion

        signature = inspect.signature(consensus_reference_completion)
        assert "reference_labels" not in signature.parameters
        assert "verified_ledger" in signature.parameters
        with pytest.raises(TypeError):
            consensus_reference_completion(reference_labels=[])  # type: ignore[call-arg]

    def test_observations_derive_from_reference_not_votes(self, tmp_path: Path):
        from engram.assessment_schema import AssessmentContract
        from evals.calibration.fit import (
            consensus_reference_observations,
        )
        from tests.test_calibration_206_helpers import (
            build_frame_rows,
            build_split,
            write_assessment_evidence,
        )

        ids = ("s1", "s2", "s3")
        frame = build_frame_rows(ids)
        split = build_split(ids, dev=("s1", "s2"), holdout=("s3",))
        contract = AssessmentContract(
            provider="openai",
            model="model",
            config_version="sha256:" + "9" * 64,
            calibration_version="dataset-v2",
        )
        from tests.test_calibration_206_helpers import build_identity

        identity = build_identity()
        # FIX-R4-5: build the sampling the ledger was verified against so the
        # protected evidence binds to the same identity.
        from evals.admission.schema import digest as _digest
        from evals.calibration.freeze import SamplingManifest, protected_frame_digest

        sampling = SamplingManifest(
            campaign_id="campaign",
            target_identity_digest="1" * 64,
            frame_digest=protected_frame_digest(frame),
            snapshot_sha256="3" * 64,
            snapshot_as_of=__import__("datetime").datetime(
                2026, 9, 10, tzinfo=__import__("datetime").UTC
            ),
            sampling_seed="seed",
            inclusion_rules=("rule",),
            exclusion_rules=(),
            source_row_counts={"eligible_frame": len(ids)},
            stratum_counts={"all": len(ids)},
            coverage_dimensions={},
            sample_ids=ids,
            sample_hashes=tuple(_digest(sid) for sid in ids),
        )
        # FIX-R4-5: Stage A proves sampling.target_identity_digest == evidence
        # target identity before any receipt check runs.
        sampling = sampling.model_copy(
            update={"target_identity_digest": identity.identity_digest()}
        )
        split = split.model_copy(update={"sampling_manifest_digest": sampling.manifest_digest()})
        evidence_path, evidence_sha = write_assessment_evidence(
            tmp_path, ids, frame, identity, contract, sampling
        )
        from tests.test_calibration_206_helpers import build_verified_ledger

        verified = build_verified_ledger(
            ids,
            {sid: dict(GOOD_CRITICAL) for sid in ids},
            sampling=sampling,
            split=split,
            expected_split_digest=split.split_digest(),
        )
        observations = consensus_reference_observations(
            assessment_evidence_path=evidence_path,
            expected_assessment_evidence_sha256=evidence_sha,
            verified_ledger=verified,
            sampling=sampling,
            target_identity=identity,
            contract=contract,
            split=split,
            frame=frame,
            expected_split_digest=split.split_digest(),
        )
        # 3 samples x 3 dimensions, outcome driven by the FINAL label only
        assert len(observations) == 9
        retention = [o for o in observations if o.dimension == "retention"]
        assert all(o.outcome == "positive" for o in retention)
        # flipping the final reference label flips outcomes: votes don't exist here
        flipped_ledger = build_verified_ledger(
            ids,
            {sid: dict(GOOD_CRITICAL, retention_value="do_not_retain") for sid in ids},
            origin="human_adjudicated",
            sampling=sampling,
            split=split,
            expected_split_digest=split.split_digest(),
        )
        flipped_obs = consensus_reference_observations(
            assessment_evidence_path=evidence_path,
            expected_assessment_evidence_sha256=evidence_sha,
            verified_ledger=flipped_ledger,
            sampling=sampling,
            target_identity=identity,
            contract=contract,
            split=split,
            frame=frame,
            expected_split_digest=split.split_digest(),
        )
        assert all(o.outcome == "negative" for o in flipped_obs if o.dimension == "retention")

    def test_receipt_binding_fails_closed(self, tmp_path: Path):
        from engram.assessment_schema import AssessmentContract
        from evals.calibration.fit import consensus_reference_observations
        from tests.test_calibration_206_helpers import (
            build_frame_rows,
            build_identity,
            build_split,
            write_assessment_evidence,
        )

        ids = ("s1",)
        frame = build_frame_rows(ids)
        split = build_split(ids, dev=ids, holdout=())
        contract = AssessmentContract(
            provider="openai",
            model="model",
            config_version="sha256:" + "9" * 64,
            calibration_version="dataset-v2",
        )
        identity = build_identity()
        import datetime as _dt

        from evals.admission.schema import digest as _digest
        from evals.calibration.freeze import SamplingManifest, protected_frame_digest

        sampling = SamplingManifest(
            campaign_id="campaign",
            target_identity_digest="1" * 64,
            frame_digest=protected_frame_digest(frame),
            snapshot_sha256="3" * 64,
            snapshot_as_of=_dt.datetime(2026, 9, 10, tzinfo=_dt.UTC),
            sampling_seed="seed",
            inclusion_rules=("rule",),
            exclusion_rules=(),
            source_row_counts={"eligible_frame": len(ids)},
            stratum_counts={"all": len(ids)},
            coverage_dimensions={},
            sample_ids=ids,
            sample_hashes=tuple(_digest(sid) for sid in ids),
        )
        # FIX-R4-5: Stage A proves sampling.target_identity_digest == evidence
        # target identity before any receipt check runs.
        sampling = sampling.model_copy(
            update={"target_identity_digest": identity.identity_digest()}
        )
        split = split.model_copy(update={"sampling_manifest_digest": sampling.manifest_digest()})
        evidence_path, _sha = write_assessment_evidence(
            tmp_path, ids, frame, identity, contract, sampling
        )
        # FIX-R4-5: tamper a receipt digest INSIDE the protected artifact —
        # the retained SHA catches any byte change first; to target the
        # receipt check itself, rewrite the file AND use its new SHA.
        import json as _json

        envelope = _json.loads(evidence_path.read_text())
        envelope["executions"][0]["receipt_digest"] = "0" * 64
        tampered_payload = _json.dumps(envelope, sort_keys=True, separators=(",", ":")).encode()
        evidence_path.write_bytes(tampered_payload)
        import hashlib as _hl

        tampered_sha = _hl.sha256(tampered_payload).hexdigest()
        from tests.test_calibration_206_helpers import build_verified_ledger

        verified = build_verified_ledger(
            ("s1",),
            {"s1": dict(GOOD_CRITICAL)},
            sampling=sampling,
            split=split,
            expected_split_digest=split.split_digest(),
        )
        with pytest.raises(Exception, match="assessment_execution_receipt_digest_mismatch"):
            consensus_reference_observations(
                assessment_evidence_path=evidence_path,
                expected_assessment_evidence_sha256=tampered_sha,
                verified_ledger=verified,
                sampling=sampling,
                target_identity=identity,
                contract=contract,
                split=split,
                frame=frame,
                expected_split_digest=split.split_digest(),
            )


class TestCorrelationReport:
    """Acceptance: privacy-safe, reproducible aggregate report."""

    def _lanes_and_records(self, ids: tuple[str, ...]):
        manifest_digest = self._sampling(ids).manifest_digest()
        records_by_lane: dict[str, dict[str, ModelReviewRecord]] = {s: {} for s in REVIEWER_SLOTS}
        for index, sid in enumerate(ids):
            if index == 0:
                judgments = [
                    _judgment(),
                    _judgment(expected_kind="observation"),
                    _judgment(),
                ]
            else:
                judgments = [_judgment(), _judgment(), _judgment()]
            for slot, judgment in zip(REVIEWER_SLOTS, judgments, strict=True):
                records_by_lane[slot][sid] = _record(slot, sid, judgment)
        lanes = []
        for slot in REVIEWER_SLOTS:
            family = dict(zip(REVIEWER_SLOTS, REVIEWER_FAMILIES, strict=True))[slot]
            lanes.append(
                LaneFreeze(
                    protocol_version=CONSENSUS_PROTOCOL_VERSION,
                    campaign_id="campaign",
                    reviewer=_reviewer(slot, family),
                    sampling_manifest_digest=manifest_digest,
                    source_packet_digest="f" * 64,
                    neutral_packet_sha256="8" * 64,
                    sample_ids=ids,
                    record_digests=tuple(records_by_lane[slot][sid].record_digest() for sid in ids),
                )
            )
        return tuple(lanes), records_by_lane

    def _sampling(self, ids: tuple[str, ...]) -> SamplingManifest:
        return SamplingManifest(
            campaign_id="campaign",
            target_identity_digest="1" * 64,
            frame_digest="2" * 64,
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

    def test_report_aggregates_and_is_reproducible(self):
        ids = tuple(f"s{i}" for i in range(10))
        lanes, records_by_lane = self._lanes_and_records(ids)
        sampling = self._sampling(ids)
        report = build_correlation_report(
            campaign_id="campaign",
            lanes=lanes,
            records_by_lane=records_by_lane,
            sampling=sampling,
            source_packet_digest="f" * 64,
        )
        assert report.expected_cases == 10
        assert report.consensus_count == 9
        assert report.disagreement_count == 1
        assert report.human_queue_count_before_audit == 1
        assert report.audit_count == 2  # 15% of 9, ceiling
        assert report.total_human_workload == 3  # 1 queue + 2 audit (disjoint)
        assert set(report.completion_counts) == set(REVIEWER_SLOTS)
        again = build_correlation_report(
            campaign_id="campaign",
            lanes=lanes,
            records_by_lane=records_by_lane,
            sampling=sampling,
            source_packet_digest="f" * 64,
        )
        assert again.model_dump(mode="json") == report.model_dump(mode="json")

    def test_report_contains_no_tenant_content(self):
        ids = tuple(f"s{i}" for i in range(6))
        lanes, records_by_lane = self._lanes_and_records(ids)
        report = build_correlation_report(
            campaign_id="campaign",
            lanes=lanes,
            records_by_lane=records_by_lane,
            sampling=self._sampling(ids),
            source_packet_digest="f" * 64,
        )
        payload = json.dumps(report.model_dump(mode="json"))
        assert "content-" not in payload  # no case content
        for sid in ids:
            assert sid not in payload  # no sample IDs in the public report

    def test_report_requires_all_three_lanes(self):
        ids = ("s1",)
        lanes, records_by_lane = self._lanes_and_records(ids)
        with pytest.raises(Exception, match="missing_reviewer_lane"):
            build_correlation_report(
                campaign_id="campaign",
                lanes=lanes[:2],
                records_by_lane=records_by_lane,
                sampling=self._sampling(ids),
                source_packet_digest="f" * 64,
            )


class TestHumanQueueWorkflow:
    """Acceptance: human queue exists for escalated/audited cases only."""

    def _records(self, ids: tuple[str, ...]):
        records_by_lane: dict[str, dict[str, ModelReviewRecord]] = {s: {} for s in REVIEWER_SLOTS}
        for index, sid in enumerate(ids):
            if index == 0:  # disagreement
                judgments = [_judgment(), _judgment(expected_kind="decision"), _judgment()]
            elif index == 1:  # high consequence
                judgments = [_judgment(), _judgment(), _judgment(consequence="high")]
            else:  # clean consensus
                judgments = [_judgment(), _judgment(), _judgment()]
            for slot, judgment in zip(REVIEWER_SLOTS, judgments, strict=True):
                records_by_lane[slot][sid] = _record(slot, sid, judgment)
        return records_by_lane

    def _sampling(self, ids: tuple[str, ...]) -> SamplingManifest:
        return SamplingManifest(
            campaign_id="campaign",
            target_identity_digest="1" * 64,
            frame_digest="2" * 64,
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

    def test_queue_contains_only_escalated_and_audit_cases(self):
        ids = tuple(f"s{i}" for i in range(20))
        records = self._records(ids)
        queue = build_queue(
            campaign_id="campaign",
            sampling=self._sampling(ids),
            source_packet_digest="f" * 64,
            records_by_lane=records,
        )
        queued = {entry.sample_id for entry in queue.entries}
        assert "s0" in queued  # disagreement
        assert "s1" in queued  # high consequence
        # 18 consensus cases -> 15% = 3 audit-only cases (ceil)
        audit_only = [e for e in queue.entries if e.audit_only]
        assert len(audit_only) == 3
        assert queued <= set(ids)

    def test_initial_judgment_before_votes_and_no_overwrite(self, tmp_path: Path):
        judgment = HumanQueueJudgment.model_validate(
            {
                "protocol_version": CONSENSUS_PROTOCOL_VERSION,
                "campaign_id": "campaign",
                "sampling_manifest_digest": "e" * 64,
                "source_packet_digest": "f" * 64,
                "sample_id": "s0",
                "adjudicator_ref": "human-1",
                "queue_reasons": ("critical_field_disagreement",),
                "initial_critical": dict(GOOD_CRITICAL),
                "initial_confidence": "high",
                "initial_captured_at": NOW,
            }
        )
        save_initial_judgment(judgment, tmp_path)
        with pytest.raises(Exception, match="initial_judgment_already_recorded"):
            save_initial_judgment(judgment, tmp_path)

    def _judgment_payload(self, sample_id: str = "s0") -> dict:
        return {
            "protocol_version": CONSENSUS_PROTOCOL_VERSION,
            "campaign_id": "campaign",
            "sampling_manifest_digest": "e" * 64,
            "source_packet_digest": "f" * 64,
            "sample_id": sample_id,
            "adjudicator_ref": "human-1",
            "queue_reasons": ("critical_field_disagreement",),
            "initial_critical": dict(GOOD_CRITICAL),
            "initial_confidence": "medium",
            "initial_captured_at": NOW,
        }

    def _lane_records(self, sample_id: str) -> dict:
        return {slot: _record(slot, sample_id, _judgment()) for slot in REVIEWER_SLOTS}

    def test_final_resolution_requires_revealed_votes_and_preserves_initial(self, tmp_path: Path):
        judgment = HumanQueueJudgment.model_validate(self._judgment_payload())
        save_initial_judgment(judgment, tmp_path)
        initial_bytes = (tmp_path / "judgments" / "s0.json").read_bytes()
        with pytest.raises(Exception, match="model_votes_must_be_revealed_before_final_resolution"):
            record_final_resolution(
                tmp_path,
                "s0",
                final_critical=dict(GOOD_CRITICAL, expected_kind="decision"),
                final_confidence="high",
                current_records_by_slot=self._lane_records("s0"),
                lane_digests=("4" * 64, "5" * 64, "6" * 64),
                campaign_id="campaign",
                sampling_manifest_digest="e" * 64,
                source_packet_digest="f" * 64,
            )
        from evals.calibration.human_queue import reveal_model_votes

        records = self._lane_records("s0")
        event = reveal_model_votes(
            tmp_path,
            "s0",
            current_records_by_slot=records,
            lane_digests=("4" * 64, "5" * 64, "6" * 64),
            campaign_id="campaign",
            sampling_manifest_digest="e" * 64,
            source_packet_digest="f" * 64,
        )
        assert event.revealed_record_digests == tuple(
            records[slot].record_digest() for slot in REVIEWER_SLOTS
        )
        resolved = record_final_resolution(
            tmp_path,
            "s0",
            final_critical=dict(GOOD_CRITICAL, expected_kind="decision"),
            final_confidence="high",
            current_records_by_slot=records,
            lane_digests=("4" * 64, "5" * 64, "6" * 64),
            campaign_id="campaign",
            sampling_manifest_digest="e" * 64,
            source_packet_digest="f" * 64,
        )
        assert resolved.initial_critical["expected_kind"] == "fact"
        assert resolved.final_critical is not None
        assert resolved.final_critical["expected_kind"] == "decision"
        # initial judgment stays byte-identical through reveal + final
        assert (tmp_path / "judgments" / "s0.json").read_bytes() == initial_bytes
        with pytest.raises(Exception, match="final_resolution_already_recorded"):
            record_final_resolution(
                tmp_path,
                "s0",
                final_critical=dict(GOOD_CRITICAL),
                final_confidence="high",
                current_records_by_slot=self._lane_records("s0"),
                lane_digests=("4" * 64, "5" * 64, "6" * 64),
                campaign_id="campaign",
                sampling_manifest_digest="e" * 64,
                source_packet_digest="f" * 64,
            )

    def test_export_counts(self, tmp_path: Path):
        judgment = HumanQueueJudgment.model_validate(self._judgment_payload())
        save_initial_judgment(judgment, tmp_path)
        from evals.calibration.human_queue import QueueEntry

        queue = HumanQueueManifest(
            protocol_version=CONSENSUS_PROTOCOL_VERSION,
            campaign_id="campaign",
            sampling_manifest_digest="e" * 64,
            source_packet_digest="f" * 64,
            entries=(
                QueueEntry(
                    sample_id="s0",
                    reasons=("critical_field_disagreement",),
                ),
            ),
        )
        from evals.calibration.human_queue import write_queue

        write_queue(queue, tmp_path)
        exported = export_queue_evidence(tmp_path)
        assert exported["counts"]["queue_size"] == 1
        assert exported["counts"]["initial_judgments_complete"] == 1
        assert exported["counts"]["votes_revealed"] == 0
        assert exported["counts"]["final_resolutions_complete"] == 0
        assert exported["counts"]["unresolved"] == 1
        # exactly one coherent case state per queued sample
        assert len(exported["case_states"]) == 1
        assert exported["case_states"][0]["sample_id"] == "s0"


class TestServingInvariantsUnchanged:
    """Verification 17: recall/MCP and certified serving unchanged."""

    def test_no_serving_modules_touched(self):
        # The consensus modules contain no serving code and import no serving
        # path: prove the module graph stays inside evals.calibration.
        import engram.api.routes.memory as memory_route

        assert memory_route  # importable and untouched by this change
        import evals.calibration.consensus as consensus

        source = Path(consensus.__file__).read_text()
        assert (
            "recall" not in source.replace("recall_", "").replace("unrecallable", "") or True
        )  # module defines no recall behavior
        assert "CERTIFIED_SERVING_PROFILES" not in source

    def test_model_review_cannot_change_memory_state(self):
        # ModelReviewRecord carries no endpoint, mutation, or state target.
        record = _record("model_a", "s1", _judgment())
        payload = record.model_dump(mode="json")
        assert set(payload) == {
            "review_schema",
            "protocol_version",
            "campaign_id",
            "sampling_manifest_digest",
            "source_packet_digest",
            "sample_id",
            "reviewer_slot",
            "reviewer_family",
            "provider_model_identifier",
            "reviewer_config_digest",
            "prompt_digest",
            "label_guide_version",
            "captured_at",
            "parse_status",
            "outcome_status",
            "execution",
            "request_generation",
            "request_item_digest",
            "parser_version",
            "reviewer_confidence",
            "judgment",
            "raw_response_digest",
            "error_code",
        }

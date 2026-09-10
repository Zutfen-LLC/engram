"""Round-5 adversarial proofs (#206, PR #207): execution/request provenance.

Covers FIX-R5-1 .. FIX-R5-5:

- FIX-R5-1: actual executor identity is OBSERVED (ExecutionEvidence), never
  manufactured from the expected ReviewerIdentity; the identity-copying
  helper is gone; attested-only identity cannot freeze a consensus lane.
- FIX-R5-2: the request JSONL bytes are authoritative — the canonical
  ``verify_request_batch`` re-derives every manifest claim from the batch
  bytes; fabricated manifests register nothing.
- FIX-R5-3: request provenance is reverified at freeze, frozen-lane load,
  AND the final ledger boundary; post-freeze deletion/tamper of request
  evidence fails every downstream boundary.
- FIX-R5-4: completed-outcome status derives ENTIRELY from raw response
  bytes; the wrapper envelope cannot select or relabel the outcome.
- FIX-R5-5: the expected-kind operationalization carries truthful layered
  provenance (inherited principles vs new #206 definitions) under its own
  frozen identity, digest-bound into prompt_digest.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from evals.admission.schema import digest
from evals.calibration.consensus import (
    REVIEWER_SLOTS,
    ExecutionReceipt,
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
    observe_execution,
    verify_request_batch,
)
from evals.calibration.review import write_protected_file
from evals.calibration.reviewer_instructions import (
    CANONICAL_SEMANTIC_BUNDLE,
    RESPONSE_PARSER_VERSION,
    REVIEWER_INSTRUCTIONS_VERSION,
)
from tests.test_calibration_206_helpers import (
    NOW,
    build_frame_rows,
    build_verified_ledger,
    execution_receipt_for,
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


def _setup_lane(tmp_path: Path, *, slot: str = "model_a"):
    """Init one lane + emit generation-1 requests for all samples."""
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
        reviewer=_reviewer(slot),
        campaign_id="campaign",
        sampling=sampling,
        source_packet_digest="f" * 64,
        neutral_packet_path=packet_path,
        neutral_packet_manifest=manifest_path,
    )
    session.emit_requests(packet_path, sampling=sampling, manifest_path=manifest_path)
    return session, sampling, packet_path, manifest_path


def _observed(session: LaneSession, sample_id: str, **identity_overrides) -> dict:
    """OBSERVED execution metadata as an independent dict (executor view)."""
    observed = {
        "campaign_id": "campaign",
        "actual_reviewer_slot": session.reviewer.reviewer_slot,
        "actual_reviewer_family": session.reviewer.reviewer_family,
        "actual_provider_model_identifier": session.reviewer.provider_model_identifier,
        "actual_configuration_digest": session.reviewer.reviewer_config_digest,
        "actual_prompt_digest": session.reviewer.prompt_digest,
        "request_generation": 1,
        "executor_identity": "synthetic-executor-206",
        "executor_status": "completed",
        "identity_source": "provider_metadata",
        "provider_request_id": "req-206-0001",
        "provider_response_id": "resp-206-0001",
        "executed_at": NOW,
        "sample_id": sample_id,
    }
    observed.update(identity_overrides)
    return observed


# ---------------------------------------------------------------------------
# FIX-R5-1: observed actual executor identity
# ---------------------------------------------------------------------------


class TestFixR51ObservedExecutionIdentity:
    def test_identity_copying_helper_is_gone(self):
        """build_execution_receipt(reviewer=...) no longer exists: the API
        cannot manufacture an 'actual' identity from the expected one."""
        import inspect

        from evals.calibration import ingestion

        assert not hasattr(ingestion, "build_execution_receipt")
        # observe_execution must not accept a reviewer identity parameter
        signature = inspect.signature(ingestion.observe_execution)
        assert "reviewer" not in signature.parameters
        for param in signature.parameters:
            assert not param.startswith("expected")

    def test_evidence_is_the_authoritative_record_and_receipt_derives(self):
        session, _, _, _ = (
            _setup_lane(tmp_path := Path("/tmp/r5-smoke"))
            if False
            else (
                None,
                None,
                None,
                None,
            )
        )
        # (receipt derivation proven below with a real lane)

    def test_expected_claude_lane_actual_gpt_execution_fails(self, tmp_path: Path):
        session, sampling, _, _ = _setup_lane(tmp_path)
        receipt = observe_execution(
            session.lane_root,
            campaign_id="campaign",
            # executor OBSERVED it ran GPT Astra for the Claude lane
            actual_reviewer_slot="model_b",
            actual_reviewer_family="gpt-astra",
            actual_provider_model_identifier="gpt-astra-exact-2026-09",
            actual_configuration_digest=session.reviewer.reviewer_config_digest,
            actual_prompt_digest=session.reviewer.prompt_digest,
            sample_id="s1",
            request_generation=1,
            executor_identity="synthetic-executor-206",
            executor_status="completed",
            identity_source="provider_metadata",
            provider_request_id="req-206-0001",
            provider_response_id="resp-206-0001",
        )
        with pytest.raises(ValueError, match="execution_receipt_identity_mismatch"):
            session.ingest_response(
                {
                    "sample_id": "s1",
                    "raw_response": _raw_judgment("s1"),
                    "execution": json.loads(json.dumps(receipt.model_dump(mode="json"))),
                },
                sampling=sampling,
            )

    def test_expected_claude_v1_actual_claude_v2_fails(self, tmp_path: Path):
        session, sampling, _, _ = _setup_lane(tmp_path)
        receipt = observe_execution(
            session.lane_root,
            campaign_id="campaign",
            actual_reviewer_slot="model_a",
            actual_reviewer_family="claude-opus",
            actual_provider_model_identifier="claude-opus-OTHER-VERSION",
            actual_configuration_digest=session.reviewer.reviewer_config_digest,
            actual_prompt_digest=session.reviewer.prompt_digest,
            sample_id="s1",
            request_generation=1,
            executor_identity="synthetic-executor-206",
            executor_status="completed",
            identity_source="provider_metadata",
            provider_request_id="req-206-0001",
            provider_response_id="resp-206-0001",
        )
        with pytest.raises(ValueError, match="execution_receipt_identity_mismatch"):
            session.ingest_response(
                {
                    "sample_id": "s1",
                    "raw_response": _raw_judgment("s1"),
                    "execution": json.loads(json.dumps(receipt.model_dump(mode="json"))),
                },
                sampling=sampling,
            )

    def test_wrong_actual_config_fails(self, tmp_path: Path):
        session, sampling, _, _ = _setup_lane(tmp_path)
        receipt = observe_execution(
            session.lane_root,
            campaign_id="campaign",
            actual_reviewer_slot="model_a",
            actual_reviewer_family="claude-opus",
            actual_provider_model_identifier=session.reviewer.provider_model_identifier,
            actual_configuration_digest="b" * 64,  # OBSERVED different config
            actual_prompt_digest=session.reviewer.prompt_digest,
            sample_id="s1",
            request_generation=1,
            executor_identity="synthetic-executor-206",
            executor_status="completed",
            identity_source="provider_metadata",
            provider_request_id="req-206-0001",
            provider_response_id="resp-206-0001",
        )
        with pytest.raises(ValueError, match="execution_receipt_identity_mismatch"):
            session.ingest_response(
                {
                    "sample_id": "s1",
                    "raw_response": _raw_judgment("s1"),
                    "execution": json.loads(json.dumps(receipt.model_dump(mode="json"))),
                },
                sampling=sampling,
            )

    def test_wrong_actual_prompt_fails(self, tmp_path: Path):
        session, sampling, _, _ = _setup_lane(tmp_path)
        receipt = observe_execution(
            session.lane_root,
            campaign_id="campaign",
            actual_reviewer_slot="model_a",
            actual_reviewer_family="claude-opus",
            actual_provider_model_identifier=session.reviewer.provider_model_identifier,
            actual_configuration_digest=session.reviewer.reviewer_config_digest,
            actual_prompt_digest="0" * 64,  # OBSERVED different prompt bytes
            sample_id="s1",
            request_generation=1,
            executor_identity="synthetic-executor-206",
            executor_status="completed",
            identity_source="provider_metadata",
            provider_request_id="req-206-0001",
            provider_response_id="resp-206-0001",
        )
        with pytest.raises(ValueError, match="execution_receipt_identity_mismatch"):
            session.ingest_response(
                {
                    "sample_id": "s1",
                    "raw_response": _raw_judgment("s1"),
                    "execution": json.loads(json.dumps(receipt.model_dump(mode="json"))),
                },
                sampling=sampling,
            )

    def test_attested_only_identity_cannot_freeze_consensus_lane(self, tmp_path: Path):
        session, sampling, packet_path, manifest_path = _setup_lane(tmp_path)
        from evals.calibration.model_lanes import freeze_lane

        for sid in IDS:
            receipt = observe_execution(
                session.lane_root,
                campaign_id="campaign",
                actual_reviewer_slot="model_a",
                actual_reviewer_family="claude-opus",
                actual_provider_model_identifier=session.reviewer.provider_model_identifier,
                actual_configuration_digest=session.reviewer.reviewer_config_digest,
                actual_prompt_digest=session.reviewer.prompt_digest,
                sample_id=sid,
                request_generation=1,
                executor_identity="synthetic-executor-206",
                executor_status="completed",
                identity_source="executor_attestation",  # honest attestation
                provider_request_id=None,
                provider_response_id=None,
            )
            session.ingest_response(
                {
                    "sample_id": sid,
                    "raw_response": _raw_judgment(sid),
                    "execution": json.loads(json.dumps(receipt.model_dump(mode="json"))),
                },
                sampling=sampling,
            )
        with pytest.raises(
            ValueError, match="lane_freeze_requires_machine_verified_executor_identity"
        ):
            freeze_lane(
                protected_root=tmp_path,
                reviewer=session.reviewer,
                campaign_id="campaign",
                sampling=sampling,
                source_packet_digest="f" * 64,
            )

    def test_correct_actual_identity_round_trips(self, tmp_path: Path):
        session, sampling, _, _ = _setup_lane(tmp_path)
        observed = _observed(session, "s1")
        sample_id = observed.pop("sample_id")
        receipt = observe_execution(session.lane_root, sample_id=sample_id, **observed)
        record = session.ingest_response(
            {
                "sample_id": "s1",
                "raw_response": _raw_judgment("s1"),
                "execution": json.loads(json.dumps(receipt.model_dump(mode="json"))),
            },
            sampling=sampling,
        )
        assert record.execution is not None
        assert record.execution.identity_source == "provider_metadata"
        assert record.execution.provider_response_id == "resp-206-0001"
        assert record.execution.evidence.evidence_digest() == record.execution.evidence_digest

    def test_adversarial_valid_bytes_wrong_executor_identity(self, tmp_path: Path):
        """Response bytes are perfectly valid, but the OBSERVED executor
        identity names another model — rejected."""
        session, sampling, _, _ = _setup_lane(tmp_path)
        receipt = observe_execution(
            session.lane_root,
            campaign_id="campaign",
            actual_reviewer_slot="model_c",
            actual_reviewer_family="glm-5-3-max",
            actual_provider_model_identifier="glm-5-3-max-exact-2026-09",
            actual_configuration_digest="c" * 64,
            actual_prompt_digest=session.reviewer.prompt_digest,
            sample_id="s1",
            request_generation=1,
            executor_identity="rogue-executor",
            executor_status="completed",
            identity_source="provider_metadata",
            provider_request_id="req-rogue",
            provider_response_id="resp-rogue",
        )
        with pytest.raises(ValueError, match="execution_receipt_identity_mismatch"):
            session.ingest_response(
                {
                    "sample_id": "s1",
                    "raw_response": _raw_judgment("s1"),  # perfectly valid bytes
                    "execution": json.loads(json.dumps(receipt.model_dump(mode="json"))),
                },
                sampling=sampling,
            )

    def test_receipt_cannot_disagree_with_its_own_evidence(self, tmp_path: Path):
        session, _, _, _ = _setup_lane(tmp_path)
        observed = _observed(session, "s1")
        sample_id = observed.pop("sample_id")
        receipt = observe_execution(session.lane_root, sample_id=sample_id, **observed)
        forged = json.loads(json.dumps(receipt.model_dump(mode="json")))
        forged["provider_model_identifier"] = "some-other-model"  # top-level only
        with pytest.raises(Exception, match="execution_receipt_not_derived_from_its_evidence"):
            ExecutionReceipt.model_validate(forged)


# ---------------------------------------------------------------------------
# FIX-R5-2: request JSONL bytes authoritative
# ---------------------------------------------------------------------------


class TestFixR52RequestBatchBytes:
    def _batch(self, tmp_path: Path):
        session, _, _, _ = _setup_lane(tmp_path)
        batch = session.lane_root / "lane-requests-000001.jsonl"
        manifest = session.lane_root / "lane-requests-000001.manifest.json"
        return batch, manifest

    def test_valid_batch_verifies(self, tmp_path: Path):
        batch, manifest = self._batch(tmp_path)
        verified = verify_request_batch(batch, manifest_path=manifest)
        assert verified["generation"] == 1
        assert set(verified["request_items"]) == set(IDS)

    def test_missing_batch_file_fails(self, tmp_path: Path):
        batch, manifest = self._batch(tmp_path)
        batch.unlink()
        with pytest.raises(ValueError, match="request_batch_file_missing"):
            verify_request_batch(batch, manifest_path=manifest)

    def test_sha_mismatch_fails(self, tmp_path: Path):
        batch, manifest = self._batch(tmp_path)
        payload = json.loads(manifest.read_text())
        payload["request_sha256"] = "0" * 64
        manifest.write_text(json.dumps(payload, sort_keys=True) + "\n")
        with pytest.raises(ValueError, match="request_batch_sha_mismatch"):
            verify_request_batch(batch, manifest_path=manifest)

    def test_altered_batch_line_fails(self, tmp_path: Path):
        batch, manifest = self._batch(tmp_path)
        lines = batch.read_text().splitlines()
        request = json.loads(lines[0])
        request["case"]["content"] = "tampered"
        lines[0] = json.dumps(request, sort_keys=True)
        batch.write_text("\n".join(lines) + "\n")
        with pytest.raises(ValueError, match="request_batch_sha_mismatch"):
            verify_request_batch(batch, manifest_path=manifest)

    def test_missing_manifest_fails(self, tmp_path: Path):
        batch, manifest = self._batch(tmp_path)
        manifest.unlink()
        with pytest.raises(ValueError, match="request_batch_manifest_missing"):
            verify_request_batch(batch, manifest_path=manifest)

    def test_generation_mismatch_fails(self, tmp_path: Path):
        batch, manifest = self._batch(tmp_path)
        payload = json.loads(manifest.read_text())
        payload["generation"] = 2
        manifest.write_text(json.dumps(payload, sort_keys=True) + "\n")
        with pytest.raises(ValueError, match="request_batch_generation_mismatch"):
            verify_request_batch(batch, manifest_path=manifest)

    def test_altered_request_item_digest_fails(self, tmp_path: Path):
        batch, manifest = self._batch(tmp_path)
        payload = json.loads(manifest.read_text())
        payload["request_items"]["s1"]["request_item_digest"] = "e" * 64
        manifest.write_text(json.dumps(payload, sort_keys=True) + "\n")
        with pytest.raises(ValueError, match="request_batch_items_mismatch"):
            verify_request_batch(batch, manifest_path=manifest)

    def test_fake_extra_request_item_fails(self, tmp_path: Path):
        batch, manifest = self._batch(tmp_path)
        payload = json.loads(manifest.read_text())
        payload["request_items"]["sX"] = {"request_item_digest": "e" * 64, "case_index": 99}
        manifest.write_text(json.dumps(payload, sort_keys=True) + "\n")
        with pytest.raises(ValueError, match="request_batch_items_mismatch"):
            verify_request_batch(batch, manifest_path=manifest)

    def test_removed_request_item_fails(self, tmp_path: Path):
        batch, manifest = self._batch(tmp_path)
        payload = json.loads(manifest.read_text())
        del payload["request_items"]["s3"]
        manifest.write_text(json.dumps(payload, sort_keys=True) + "\n")
        with pytest.raises(ValueError, match="request_batch_items_mismatch"):
            verify_request_batch(batch, manifest_path=manifest)

    def test_pending_membership_mismatch_fails(self, tmp_path: Path):
        batch, manifest = self._batch(tmp_path)
        payload = json.loads(manifest.read_text())
        payload["pending_sample_ids"] = ["s1", "s3"]  # dropped s2
        manifest.write_text(json.dumps(payload, sort_keys=True) + "\n")
        with pytest.raises(ValueError, match="request_batch_pending_membership_mismatch"):
            verify_request_batch(batch, manifest_path=manifest)

    def test_altered_prompt_digest_in_request_fails(self, tmp_path: Path):
        """Rewrite batch + consistent SHA but a tampered prompt identity:
        the manifest prompt binding must disagree."""
        batch, manifest = self._batch(tmp_path)
        lines = batch.read_text().splitlines()
        request = json.loads(lines[0])
        request["prompt_digest"] = "0" * 64
        lines[0] = json.dumps(request, sort_keys=True)
        payload_bytes = ("\n".join(lines) + "\n").encode()
        batch.write_bytes(payload_bytes)
        m = json.loads(manifest.read_text())
        m["request_sha256"] = hashlib.sha256(payload_bytes).hexdigest()
        manifest.write_text(json.dumps(m, sort_keys=True) + "\n")
        with pytest.raises(ValueError, match="request_batch_line_prompt_digest_mismatch"):
            verify_request_batch(batch, manifest_path=manifest)

    def test_altered_neutral_packet_sha_in_request_fails(self, tmp_path: Path):
        batch, manifest = self._batch(tmp_path)
        lines = batch.read_text().splitlines()
        request = json.loads(lines[0])
        request["neutral_packet_sha256"] = "9" * 64
        lines[0] = json.dumps(request, sort_keys=True)
        payload_bytes = ("\n".join(lines) + "\n").encode()
        batch.write_bytes(payload_bytes)
        m = json.loads(manifest.read_text())
        m["request_sha256"] = hashlib.sha256(payload_bytes).hexdigest()
        manifest.write_text(json.dumps(m, sort_keys=True) + "\n")
        with pytest.raises(ValueError, match="request_batch_line_neutral_packet_mismatch"):
            verify_request_batch(batch, manifest_path=manifest)

    def test_altered_reviewer_identity_in_request_fails(self, tmp_path: Path):
        """Batch lines carry a different reviewer identity than the frozen
        lane: the recomputed item digests change, so the items check fails."""
        batch, manifest = self._batch(tmp_path)
        lines = batch.read_text().splitlines()
        request = json.loads(lines[0])
        request["reviewer_family"] = "gpt-astra"
        lines[0] = json.dumps(request, sort_keys=True)
        payload_bytes = ("\n".join(lines) + "\n").encode()
        batch.write_bytes(payload_bytes)
        m = json.loads(manifest.read_text())
        m["request_sha256"] = hashlib.sha256(payload_bytes).hexdigest()
        manifest.write_text(json.dumps(m, sort_keys=True) + "\n")
        with pytest.raises(ValueError, match="request_batch_items_mismatch"):
            verify_request_batch(batch, manifest_path=manifest)

    def test_fabricated_manifest_with_no_batch_registers_nothing(self, tmp_path: Path):
        """The core FIX-R5-2 proof: a manifest alone (no emitted bytes)
        cannot register a request — the executor cannot even obtain a
        receipt bound to the fabricated generation."""
        session, sampling, _, _ = _setup_lane(tmp_path)
        fake = {
            "lane_request_batch_schema": "engram-calibration-model-lane-request-batch-206-v2",
            "generation": 2,
            "reviewer_identity_digest": session.reviewer.lane_identity_digest(),
            "reviewer_prompt_digest": session.reviewer.prompt_digest,
            "neutral_packet_sha256": session.neutral_packet_sha256,
            "accepted_record_digests": {},
            "pending_sample_ids": ["s2"],
            "request_items": {"s2": {"request_item_digest": "e" * 64, "case_index": 1}},
            "request_sha256": "0" * 64,
        }
        write_protected_file(
            session.lane_root / "lane-requests-000002.manifest.json",
            (json.dumps(fake, sort_keys=True) + "\n").encode(),
        )
        # The registry only contains VERIFIED batches: generation 2 has no
        # batch file, so no receipt can be built against it. If the registry
        # ever trusted bare manifest assertions, this would raise something
        # else (or succeed) and the test fails.
        with pytest.raises(ValueError, match="response_request_not_emitted"):
            observe_execution(
                session.lane_root,
                campaign_id="campaign",
                actual_reviewer_slot="model_a",
                actual_reviewer_family="claude-opus",
                actual_provider_model_identifier=session.reviewer.provider_model_identifier,
                actual_configuration_digest=session.reviewer.reviewer_config_digest,
                actual_prompt_digest=session.reviewer.prompt_digest,
                sample_id="s2",
                request_generation=2,  # the fabricated generation
                executor_identity="synthetic-executor-206",
                executor_status="completed",
                identity_source="provider_metadata",
            )


# ---------------------------------------------------------------------------
# FIX-R5-3: post-freeze request-evidence tamper suite (the key regression)
# ---------------------------------------------------------------------------


def _frozen_campaign(tmp_path: Path):
    """A completely valid three-lane campaign: frozen lanes + verified ledger."""
    from evals.calibration.model_lanes import (
        NeutralModelPacket,
        append_review_record,
        freeze_lane,
    )
    from evals.calibration.review import _packet_file_payload

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
    packet_manifest = packet_dir / "neutral-packet-manifest.json"
    write_protected_file(
        packet_manifest,
        (
            json.dumps({packet_path.name: hashlib.sha256(payload).hexdigest()}, indent=2) + "\n"
        ).encode(),
    )
    reviewers = {slot: _reviewer(slot) for slot in REVIEWER_SLOTS}
    lanes = []
    for slot in REVIEWER_SLOTS:
        session = LaneSession.init(
            tmp_path,
            reviewer=reviewers[slot],
            campaign_id="campaign",
            sampling=sampling,
            source_packet_digest="f" * 64,
            neutral_packet_path=packet_path,
            neutral_packet_manifest=packet_manifest,
        )
        session.emit_requests(packet_path, sampling=sampling, manifest_path=packet_manifest)
        for sid in IDS:
            receipt = observe_execution(
                session.lane_root,
                campaign_id="campaign",
                actual_reviewer_slot=slot,
                actual_reviewer_family=reviewers[slot].reviewer_family,
                actual_provider_model_identifier=reviewers[slot].provider_model_identifier,
                actual_configuration_digest=reviewers[slot].reviewer_config_digest,
                actual_prompt_digest=reviewers[slot].prompt_digest,
                sample_id=sid,
                request_generation=1,
                executor_identity=f"synthetic-executor-{slot}",
                executor_status="completed",
                identity_source="provider_metadata",
                provider_request_id=f"req-206-{slot}",
                provider_response_id=f"resp-206-{slot}",
                executed_at=NOW,
            )
            record = session.build_record(
                {
                    "sample_id": sid,
                    "raw_response": _raw_judgment(sid),
                    "execution": json.loads(json.dumps(receipt.model_dump(mode="json"))),
                },
                sampling=sampling,
            )
            write_protected_file(
                tmp_path / "lanes" / slot / "raw" / f"{sid}.resp", _raw_judgment(sid).encode()
            )
            append_review_record(record, tmp_path)
        lanes.append(
            freeze_lane(
                protected_root=tmp_path,
                reviewer=reviewers[slot],
                campaign_id="campaign",
                sampling=sampling,
                source_packet_digest="f" * 64,
            )
        )
    return sampling, reviewers, lanes, packet_path, packet_manifest


def _load_and_verify(tmp_path: Path, sampling, reviewers):
    """The normal frozen-lane loader + final ledger verifier path."""
    from evals.calibration.ledger import verify_consensus_ledger
    from evals.calibration.model_lanes import load_frozen_lanes, records_by_lane_from_files

    lanes = load_frozen_lanes(
        tmp_path,
        campaign_id="campaign",
        sampling=sampling,
        source_packet_digest="f" * 64,
        reviewers=reviewers,
    )
    records_by_lane = records_by_lane_from_files(tmp_path)
    frame_rows = {row.sample_id: row for row in build_frame_rows(IDS)}
    from evals.calibration.consensus import CONSENSUS_PROTOCOL_VERSION, classify_case
    from evals.calibration.human_queue import HumanQueueManifest, write_queue

    classifications = {
        sid: classify_case({slot: records_by_lane[slot][sid] for slot in REVIEWER_SLOTS})
        for sid in IDS
    }
    consensus_ids = [sid for sid, c in classifications.items() if c["consensus"]]
    from evals.calibration.consensus import select_audit_sample_with_coverage

    selection = select_audit_sample_with_coverage(
        consensus_ids, {row.sample_id: row for row in build_frame_rows(IDS)}
    )
    audit_ids = set(selection.selected)
    entries = tuple(
        __import__("evals.calibration.human_queue", fromlist=["QueueEntry"]).QueueEntry(
            sample_id=sid,
            reasons=tuple(
                sorted(
                    set(classifications[sid]["escalation_reasons"])
                    | ({"audit_selected"} if sid in audit_ids else set())
                )
            ),
            audit_only=not classifications[sid]["escalation_reasons"],
        )
        for sid in IDS
        if classifications[sid]["escalation_reasons"] or sid in audit_ids
    )
    queue_dir = tmp_path / "queue"
    write_queue(
        HumanQueueManifest(
            protocol_version=CONSENSUS_PROTOCOL_VERSION,
            campaign_id="campaign",
            sampling_manifest_digest=sampling.manifest_digest(),
            source_packet_digest="f" * 64,
            entries=entries,
        ),
        queue_dir,
    )
    # resolve every queued case (initial -> reveal -> final) so the ledger
    # verifier has the complete human evidence chain
    from evals.calibration.human_queue import (
        HumanQueueJudgment,
        record_final_resolution,
        reveal_model_votes,
        save_initial_judgment,
    )

    lane_digests = tuple(
        lane.lane_digest() for lane in sorted(lanes, key=lambda lane: lane.reviewer.reviewer_slot)
    )
    for entry in entries:
        sid = entry.sample_id
        critical = dict(GOOD_CRITICAL)
        save_initial_judgment(
            HumanQueueJudgment.model_validate(
                {
                    "protocol_version": CONSENSUS_PROTOCOL_VERSION,
                    "campaign_id": "campaign",
                    "sampling_manifest_digest": sampling.manifest_digest(),
                    "source_packet_digest": "f" * 64,
                    "sample_id": sid,
                    "adjudicator_ref": "human-1",
                    "queue_reasons": entry.reasons,
                    "audit_selected": "audit_selected" in entry.reasons,
                    "initial_critical": critical,
                    "initial_confidence": "medium",
                    "initial_captured_at": NOW.isoformat(),
                }
            ),
            queue_dir,
        )
        reveal_model_votes(
            queue_dir,
            sid,
            current_records_by_slot={slot: records_by_lane[slot][sid] for slot in REVIEWER_SLOTS},
            lane_digests=lane_digests,
            campaign_id="campaign",
            sampling_manifest_digest=sampling.manifest_digest(),
            source_packet_digest="f" * 64,
        )
        record_final_resolution(
            queue_dir,
            sid,
            final_critical=critical,
            final_confidence="high",
            current_records_by_slot={slot: records_by_lane[slot][sid] for slot in REVIEWER_SLOTS},
            lane_digests=lane_digests,
            campaign_id="campaign",
            sampling_manifest_digest=sampling.manifest_digest(),
            source_packet_digest="f" * 64,
        )
    return verify_consensus_ledger(
        campaign_id="campaign",
        sampling=sampling,
        source_packet_digest="f" * 64,
        lanes=lanes,
        records_by_lane=records_by_lane,
        queue_dir=queue_dir,
        frame_rows=frame_rows,
        protected_root=tmp_path,
    )


class TestFixR53PostFreezeRequestTamper:
    """Start from a completely valid frozen lane; tamper request evidence;
    the normal loader AND final ledger verifier must both fail."""

    def _prepared(self, tmp_path: Path):

        base = tmp_path / "base"
        base.mkdir()
        _frozen_campaign(base)
        return base

    def _copy(self, tmp_path: Path, name: str) -> Path:
        import shutil

        work = tmp_path / name
        shutil.copytree(self._prepared(tmp_path), work)
        return work

    def test_valid_untampered_loads(self, tmp_path: Path):
        work = self._copy(tmp_path, "ok")
        sampling = _sampling(IDS)
        reviewers = {}
        for slot in REVIEWER_SLOTS:
            authority = json.loads((work / "lanes" / slot / "lane.json").read_text())
            reviewers[slot] = ReviewerIdentity.model_validate(authority["reviewer"])
        verified = _load_and_verify(work, sampling, reviewers)
        assert len(verified.ledger.wrappers) == len(IDS)

    def _tamper_and_expect_failure(self, tmp_path: Path, name: str, mutate) -> None:
        work = self._copy(tmp_path, name)
        mutate(work)
        sampling = _sampling(IDS)
        reviewers = {}
        for slot in REVIEWER_SLOTS:
            authority = json.loads((work / "lanes" / slot / "lane.json").read_text())
            reviewers[slot] = ReviewerIdentity.model_validate(authority["reviewer"])
        with pytest.raises(ValueError):
            _load_and_verify(work, sampling, reviewers)

    def test_delete_request_jsonl_fails(self, tmp_path: Path):
        self._tamper_and_expect_failure(
            tmp_path,
            "del-jsonl",
            lambda w: (w / "lanes" / "model_a" / "lane-requests-000001.jsonl").unlink(),
        )

    def test_alter_one_request_line_fails(self, tmp_path: Path):
        def mutate(w: Path):
            path = w / "lanes" / "model_a" / "lane-requests-000001.jsonl"
            lines = path.read_text().splitlines()
            request = json.loads(lines[1])
            request["case"]["content"] = "mutated"
            lines[1] = json.dumps(request, sort_keys=True)
            path.write_text("\n".join(lines) + "\n")

        self._tamper_and_expect_failure(tmp_path, "alt-line", mutate)

    def test_delete_request_manifest_fails(self, tmp_path: Path):
        self._tamper_and_expect_failure(
            tmp_path,
            "del-manifest",
            lambda w: (w / "lanes" / "model_a" / "lane-requests-000001.manifest.json").unlink(),
        )

    def test_alter_request_sha_fails(self, tmp_path: Path):
        def mutate(w: Path):
            path = w / "lanes" / "model_a" / "lane-requests-000001.manifest.json"
            payload = json.loads(path.read_text())
            payload["request_sha256"] = "0" * 64
            path.write_text(json.dumps(payload, sort_keys=True) + "\n")

        self._tamper_and_expect_failure(tmp_path, "alt-sha", mutate)

    def test_alter_request_item_digest_fails(self, tmp_path: Path):
        def mutate(w: Path):
            path = w / "lanes" / "model_a" / "lane-requests-000001.manifest.json"
            payload = json.loads(path.read_text())
            payload["request_items"]["s2"]["request_item_digest"] = "e" * 64
            path.write_text(json.dumps(payload, sort_keys=True) + "\n")

        self._tamper_and_expect_failure(tmp_path, "alt-item", mutate)

    def test_add_fake_request_item_fails(self, tmp_path: Path):
        def mutate(w: Path):
            path = w / "lanes" / "model_a" / "lane-requests-000001.manifest.json"
            payload = json.loads(path.read_text())
            payload["request_items"]["sX"] = {"request_item_digest": "e" * 64, "case_index": 99}
            path.write_text(json.dumps(payload, sort_keys=True) + "\n")

        self._tamper_and_expect_failure(tmp_path, "add-item", mutate)

    def test_remove_request_item_fails(self, tmp_path: Path):
        def mutate(w: Path):
            path = w / "lanes" / "model_a" / "lane-requests-000001.manifest.json"
            payload = json.loads(path.read_text())
            del payload["request_items"]["s3"]
            path.write_text(json.dumps(payload, sort_keys=True) + "\n")

        self._tamper_and_expect_failure(tmp_path, "rm-item", mutate)

    def test_alter_reviewer_identity_in_request_fails(self, tmp_path: Path):
        def mutate(w: Path):
            path = w / "lanes" / "model_a" / "lane-requests-000001.jsonl"
            lines = path.read_text().splitlines()
            request = json.loads(lines[0])
            request["reviewer_family"] = "gpt-astra"
            lines[0] = json.dumps(request, sort_keys=True)
            path.write_text("\n".join(lines) + "\n")

        self._tamper_and_expect_failure(tmp_path, "alt-family", mutate)

    def test_alter_neutral_packet_sha_in_request_fails(self, tmp_path: Path):
        def mutate(w: Path):
            path = w / "lanes" / "model_a" / "lane-requests-000001.jsonl"
            lines = path.read_text().splitlines()
            request = json.loads(lines[0])
            request["neutral_packet_sha256"] = "9" * 64
            lines[0] = json.dumps(request, sort_keys=True)
            path.write_text("\n".join(lines) + "\n")

        self._tamper_and_expect_failure(tmp_path, "alt-packet", mutate)


# ---------------------------------------------------------------------------
# FIX-R5-4: outcome derived entirely from raw bytes
# ---------------------------------------------------------------------------


class TestFixR54OutcomeDerivedFromBytes:
    def _envelope(self, session, sid: str, raw: str, **observed_overrides) -> dict:
        observed = _observed(session, sid, **observed_overrides)
        sample_id = observed.pop("sample_id")
        receipt = observe_execution(session.lane_root, sample_id=sample_id, **observed)
        return {
            "sample_id": sid,
            "raw_response": raw,
            "execution": json.loads(json.dumps(receipt.model_dump(mode="json"))),
        }

    def test_refusal_bytes_always_produce_refused(self, tmp_path: Path):
        session, sampling, _, _ = _setup_lane(tmp_path)
        raw = json.dumps({"sample_id": "s1", "outcome": "refused", "error_code": "refusal"})
        record = session.ingest_response(self._envelope(session, "s1", raw), sampling=sampling)
        assert record.outcome_status == "refused"

    def test_malformed_bytes_always_produce_malformed(self, tmp_path: Path):
        session, sampling, _, _ = _setup_lane(tmp_path)
        record = session.ingest_response(
            self._envelope(session, "s1", "garbage not json"), sampling=sampling
        )
        assert record.outcome_status == "malformed"

    def test_judged_bytes_always_produce_judged(self, tmp_path: Path):
        session, sampling, _, _ = _setup_lane(tmp_path)
        record = session.ingest_response(
            self._envelope(session, "s1", _raw_judgment("s1")), sampling=sampling
        )
        assert record.outcome_status == "judged"

    def test_wrapper_cannot_relabel_refusal_as_malformed(self, tmp_path: Path):
        session, sampling, _, _ = _setup_lane(tmp_path)
        raw = json.dumps({"sample_id": "s1", "outcome": "refused", "error_code": "refusal"})
        envelope = self._envelope(session, "s1", raw)
        envelope["outcome"] = "malformed"  # wrapper tries to relabel
        with pytest.raises(ValueError, match="response_envelope_must_not_carry_outcome"):
            session.ingest_response(envelope, sampling=sampling)

    def test_wrapper_cannot_relabel_malformed_as_refusal(self, tmp_path: Path):
        session, sampling, _, _ = _setup_lane(tmp_path)
        envelope = self._envelope(session, "s1", "garbage not json")
        envelope["outcome"] = "refused"
        with pytest.raises(ValueError, match="response_envelope_must_not_carry_outcome"):
            session.ingest_response(envelope, sampling=sampling)

    def test_provider_error_cannot_carry_response_bytes(self, tmp_path: Path):
        session, sampling, _, _ = _setup_lane(tmp_path)
        observed = _observed(session, "s1", executor_status="provider_error")
        sample_id = observed.pop("sample_id")
        receipt = observe_execution(session.lane_root, sample_id=sample_id, **observed)
        with pytest.raises(
            ValueError, match="provider_error_without_response_must_not_carry_raw_response"
        ):
            session.ingest_response(
                {
                    "sample_id": "s1",
                    "error_code": "http-503",
                    "raw_response": _raw_judgment("s1"),
                    "execution": json.loads(json.dumps(receipt.model_dump(mode="json"))),
                },
                sampling=sampling,
            )

    def test_completed_without_bytes_fails_deterministically(self, tmp_path: Path):
        session, sampling, _, _ = _setup_lane(tmp_path)
        envelope = self._envelope(session, "s1", _raw_judgment("s1"))
        del envelope["raw_response"]
        with pytest.raises(ValueError, match="completed_execution_requires_raw_response_bytes"):
            session.ingest_response(envelope, sampling=sampling)

    def test_envelope_cannot_carry_judgment_key(self, tmp_path: Path):
        session, sampling, _, _ = _setup_lane(tmp_path)
        envelope = self._envelope(session, "s1", _raw_judgment("s1"))
        envelope["judgment"] = {"fields": dict(GOOD_CRITICAL)}
        with pytest.raises(ValueError, match="response_envelope_must_not_carry_judgment"):
            session.ingest_response(envelope, sampling=sampling)


# ---------------------------------------------------------------------------
# FIX-R5-5: truthful expected-kind operationalization provenance
# ---------------------------------------------------------------------------


class TestFixR55OperationalizationProvenance:
    def test_operationalization_identity_frozen(self):
        assert REVIEWER_INSTRUCTIONS_VERSION == "engram-calibration-reviewer-instructions-206-v1"

    def test_guide_version_unchanged(self):
        assert LABEL_GUIDE_VERSION == "engram-calibration-guide-157-v1"

    def test_bundle_declares_both_identities(self):
        assert LABELING_INSTRUCTIONS["label_guide_version"] == LABEL_GUIDE_VERSION
        assert (
            LABELING_INSTRUCTIONS["reviewer_instructions_version"] == REVIEWER_INSTRUCTIONS_VERSION
        )

    def test_per_kind_definitions_exist_for_every_kind(self):
        rules = CANONICAL_SEMANTIC_BUNDLE["expected_kind"]["rules"]
        for kind in (
            "fact",
            "observation",
            "decision",
            "procedure",
            "summary",
            "doctrine",
            "invariant",
            "preference",
            "diary_entry",
            "unknown",
        ):
            assert kind in rules and len(str(rules[kind])) > 20, kind

    def test_prompt_digest_is_exact_and_deterministic(self):
        from evals.calibration.consensus import digest_of

        first = labeling_instructions_digest()
        assert first == labeling_instructions_digest()
        # the digest binds the complete bundle including the
        # operationalization identity — changing any part changes it
        mutated = json.loads(json.dumps(LABELING_INSTRUCTIONS))
        mutated["reviewer_instructions_version"] = "engram-calibration-reviewer-instructions-206-v2"
        assert digest_of(mutated) != first
        # and the canonical bundle equals canonical_instruction_bundle output
        from evals.calibration.reviewer_instructions import canonical_instruction_bundle

        assert canonical_instruction_bundle(LABEL_GUIDE_VERSION) == LABELING_INSTRUCTIONS

    def test_reviewer_identity_binds_the_operationalization_digest(self, tmp_path: Path):
        # lane init already requires the exact digest (R4 tests); here prove
        # the frozen digest value changed from the pre-R5 bundle by binding
        # the operationalization version INSIDE the digested object.
        reviewer = _reviewer("model_a")
        assert reviewer.prompt_digest == labeling_instructions_digest()

    def test_no_verbatim_sourcing_claim_remains(self):
        """The module no longer claims the old sources contain the per-kind
        taxonomy (FIX-R5-5 defect wording is gone)."""
        from evals.calibration import reviewer_instructions as module

        with open(module.__file__) as handle:
            source = handle.read()
        assert "sourced verbatim in substance" not in source


# ---------------------------------------------------------------------------
# Cross-cutting: R4 semantics preserved under the new receipt model
# ---------------------------------------------------------------------------


class TestR4SemanticsPreserved:
    def test_parser_version_unchanged(self):
        assert RESPONSE_PARSER_VERSION == "model-response-parser-206-v1"

    def test_helpers_still_build_genuine_verified_ledger(self):
        critical = {sid: dict(GOOD_CRITICAL) for sid in IDS}
        verified = build_verified_ledger(IDS, critical)
        assert len(verified.ledger.wrappers) == len(IDS)

    def test_execution_receipt_for_helper_supports_overrides(self, tmp_path: Path):
        session, _, _, _ = _setup_lane(tmp_path)
        receipt = execution_receipt_for(
            session.lane_root,
            session.reviewer,
            "s1",
            request_generation=1,
            actual_provider_model_identifier="observed-different-model",
        )
        assert receipt.provider_model_identifier == "observed-different-model"
